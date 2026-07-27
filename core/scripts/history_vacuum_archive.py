#!/usr/bin/env python3
# domain-leak-exempt: S3 graveyard archival — the bucket/prefix are
# Lodestar-owned infra identifiers (alpha self.md) and the S3 client mirrors
# owncloud_backend.py; both are functional cloud-backend strings, not examples.
"""Archive-before-delete wrapper around _history_store vacuum (g-115-2792-b).

Implements Decision 3 of the g-115-2792-a design brief
(world/knowledge/tree/system/history-store-gc-cadence.md): before ANY orphan
unlink or retention drop on the SINGLE-COPY .history store, the payload is
COPIED to a lifecycle-exempt REMOTE graveyard, VERIFIED (object count + bytes +
per-object content-hash key), and only then deleted locally. If archive OR
verify fails, the run ABORTS without deleting anything — a fail-safe that
mirrors vacuum()'s Phase-2a abort-on-corrupt posture (archive-before-delete.md,
rb-2859: on a single-copy store a reachability bug would otherwise permanently
destroy live content with no recovery path).

Ordering (archive-before-delete.md):
  ENUMERATE -> ARCHIVE -> VERIFY -> DELETE -> RECEIPT.

The Archiver is an INJECTED seam so the ordering/verification logic is
hermetically testable (LocalDirArchiver, fault-injecting archivers in tests)
while production uses S3Archiver against the lifecycle-exempt graveyard.

SAFE-LANDING NOTE (g-115-2792-b): the production tick lands with
`history_vacuum.apply=false`, so run() takes the dry_run branch — ENUMERATE +
report only, NO archive and NO delete. g-115-2792-c runs the Decision-5
positive control against real S3 (reachable-survives + orphan
archived+deleted+restorable) and only then flips apply=true. This module never
deletes from the live store until that verification passes.
"""

import argparse
import json
import os
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _history_store import (  # noqa: E402
    enumerate_vacuum_targets,
    delete_vacuum_targets,
    enumerate_legacy_dir_targets,
    delete_legacy_targets,
)


# ---------------------------------------------------------------------------
# Item normalization — uniform shape the archivers consume
# ---------------------------------------------------------------------------

def normalize_items(targets):
    """Flatten enumerate_vacuum_targets() orphan payloads into archive items.

    Each item: {kind, hash_id, path, size_bytes, gkey}
      - hash_id: stable identity for reporting/receipt
      - gkey:    graveyard-relative key (content-addressed, so identical bytes
                 map to identical keys — idempotent re-archive)
    drop_manifests are NOT archived here: their metadata survives the rewrite
    and their reconstructable payload blob/patch is already an orphan item.
    """
    items = []
    for ob in targets.get("orphan_blobs", []):
        h = ob["hash"]
        items.append({
            "kind": "blob",
            "hash_id": h,
            "path": ob["path"],
            "size_bytes": ob["size_bytes"],
            "gkey": f"blobs/{h[:2]}/{h[2:]}.gz",
        })
    for op in targets.get("orphan_patches", []):
        th, bh = op["target_hash"], op["base_hash"]
        items.append({
            "kind": "patch",
            "hash_id": f"{th}.from.{bh}",
            "path": op["path"],
            "size_bytes": op["size_bytes"],
            "gkey": f"patches/{th[:2]}/{th[2:]}.from.{bh}.gz",
        })
    return items


def normalize_legacy_items(targets):
    """Flatten enumerate_legacy_dir_targets() into archive items (g-115-2989).

    Legacy files are NOT content-addressed — the gkey PRESERVES the original
    relative path so a restore lands the file at its historical .history path
    (path-based restore, not hash-based). The gkey is prefixed `legacy/` to keep
    it disjoint from the CAS `blobs/`+`patches/` graveyard namespaces.
    """
    items = []
    for lf in targets.get("legacy_files", []):
        rel = lf["rel_path"]
        items.append({
            "kind": "legacy",
            "hash_id": rel,          # rel_path is the stable identity for the receipt
            "path": lf["path"],
            "size_bytes": lf["size_bytes"],
            "gkey": f"legacy/{rel}",
        })
    return items


# ---------------------------------------------------------------------------
# Archiver seam
# ---------------------------------------------------------------------------
#
# Contract (all three methods take the item list / receipt + a run_ctx dict
# carrying box_id + run_id, and must never delete anything):
#   archive(items, run_ctx) -> {"archived": [hash_id], "failed": [(hash_id, reason)]}
#   verify(items,  run_ctx) -> {"verified": [hash_id], "missing": [(hash_id, reason)]}
#   write_receipt(receipt, run_ctx) -> str   # durable location of the receipt


class LocalDirArchiver:
    """Filesystem archiver — TEST / DEV ONLY.

    Decision 3 forbids a LOCAL graveyard in production: the .history disk is
    98% full, so a local copy would double space before the delete frees it.
    This archiver targets an arbitrary directory (a tmp dir in tests) and is
    the hermetic stand-in for S3Archiver. verify() re-reads and byte-compares
    (stronger than the S3 head-only check) so the ordering tests are exact.
    """

    def __init__(self, root):
        self.root = Path(root)

    def _dest(self, item, run_ctx):
        return self.root / run_ctx["box_id"] / run_ctx["run_id"] / item["gkey"]

    def archive(self, items, run_ctx):
        archived, failed = [], []
        for it in items:
            try:
                dest = self._dest(it, run_ctx)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(it["path"], dest)
                archived.append(it["hash_id"])
            except OSError as e:
                failed.append((it["hash_id"], str(e)))
        return {"archived": archived, "failed": failed}

    def verify(self, items, run_ctx):
        verified, missing = [], []
        for it in items:
            dest = self._dest(it, run_ctx)
            try:
                if not dest.exists():
                    missing.append((it["hash_id"], "absent in graveyard"))
                    continue
                src_bytes = Path(it["path"]).read_bytes()
                dst_bytes = dest.read_bytes()
                if src_bytes != dst_bytes:
                    missing.append((it["hash_id"], "byte mismatch"))
                else:
                    verified.append(it["hash_id"])
            except OSError as e:
                missing.append((it["hash_id"], str(e)))
        return {"verified": verified, "missing": missing}

    def write_receipt(self, receipt, run_ctx):
        dest = (self.root / run_ctx["box_id"] / run_ctx["run_id"]
                / "receipt.json")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        return str(dest)


class S3Archiver:
    """Production archiver — copies orphan payloads to the lifecycle-exempt
    REMOTE graveyard (Decision 3). Reuses boto3 exactly the way
    owncloud_backend.py constructs its client (default credential chain: env
    AWS_*, shared config, instance role). Keys embed the content-hash, so the
    key IS the checksum verify() confirms (plus a byte-size head check).

    Not exercised in hermetic tests (no real S3); g-115-2792-c runs the live
    positive control against it and only then flips history_vacuum.apply=true.
    """

    def __init__(self, bucket, prefix, *, s3_client=None, region=None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if s3_client is not None:
            self.s3 = s3_client
        else:
            import boto3
            self.s3 = boto3.client("s3", region_name=region)

    def _key(self, item, run_ctx):
        return (f"{self.prefix}/{run_ctx['box_id']}/{run_ctx['run_id']}"
                f"/{item['gkey']}")

    def archive(self, items, run_ctx):
        archived, failed = [], []
        for it in items:
            try:
                with open(it["path"], "rb") as f:
                    self.s3.put_object(Bucket=self.bucket,
                                       Key=self._key(it, run_ctx),
                                       Body=f.read())
                archived.append(it["hash_id"])
            except Exception as e:  # boto3 ClientError et al.
                failed.append((it["hash_id"], repr(e)))
        return {"archived": archived, "failed": failed}

    def verify(self, items, run_ctx):
        verified, missing = [], []
        for it in items:
            try:
                head = self.s3.head_object(Bucket=self.bucket,
                                           Key=self._key(it, run_ctx))
                if head.get("ContentLength") != it["size_bytes"]:
                    missing.append(
                        (it["hash_id"],
                         f"size {head.get('ContentLength')}!={it['size_bytes']}"))
                else:
                    verified.append(it["hash_id"])
            except Exception as e:
                missing.append((it["hash_id"], repr(e)))
        return {"verified": verified, "missing": missing}

    def write_receipt(self, receipt, run_ctx):
        key = (f"{self.prefix}/{run_ctx['box_id']}/{run_ctx['run_id']}"
               f"/receipt.json")
        self.s3.put_object(Bucket=self.bucket, Key=key,
                           Body=json.dumps(receipt, indent=2).encode("utf-8"))
        return f"s3://{self.bucket}/{key}"


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------

def build_receipt(base_dir, targets, items, run_ctx):
    """The integrity baseline + restore instructions written WITH the archive."""
    return {
        "created_at": run_ctx["created_at"],
        "box_id": run_ctx["box_id"],
        "run_id": run_ctx["run_id"],
        "base_dir": str(base_dir),
        "reason": "history-store vacuum GC (g-115-2792-b)",
        "enumeration": {
            "orphan_blobs": [
                {"hash": ob["hash"], "size_bytes": ob["size_bytes"],
                 "gkey": f"blobs/{ob['hash'][:2]}/{ob['hash'][2:]}.gz"}
                for ob in targets.get("orphan_blobs", [])
            ],
            "orphan_patches": [
                {"target_hash": op["target_hash"], "base_hash": op["base_hash"],
                 "size_bytes": op["size_bytes"],
                 "gkey": (f"patches/{op['target_hash'][:2]}/"
                          f"{op['target_hash'][2:]}.from.{op['base_hash']}.gz")}
                for op in targets.get("orphan_patches", [])
            ],
            "drop_manifests": [
                {"path": dm["path"], "hash": dm["hash"],
                 "prior_encoding": dm["encoding"]}
                for dm in targets.get("drop_manifests", [])
            ],
            "total_objects": len(items),
            "total_bytes": targets.get("bytes_freed", 0),
        },
        "restore_instructions": (
            "Restore a payload by copying its graveyard object back to a "
            "content-addressed path OUTSIDE any live .history/ tree, then "
            "reconstruct the file offline from the manifest chain. Do NOT "
            "restore into live .history/ paths — that can re-arm read-through "
            "resurrection (rb-2859 donor-agent class). For a retention-dropped "
            "manifest, flip its encoding back to the prior_encoding above AND "
            "restore its blob/patch before the next vacuum runs."
        ),
    }


def build_legacy_receipt(base_dir, targets, items, run_ctx):
    """Integrity baseline + restore instructions for the legacy-dir drain
    (g-115-2989). Path-preserving: each graveyard object's key is legacy/<rel_path>."""
    return {
        "created_at": run_ctx["created_at"],
        "box_id": run_ctx["box_id"],
        "run_id": run_ctx["run_id"],
        "base_dir": str(base_dir),
        "reason": "history-store legacy per-file-dir drain (g-115-2989)",
        "enumeration": {
            "legacy_entries": targets.get("entries", []),
            "legacy_files": [
                {"rel_path": lf["rel_path"], "size_bytes": lf["size_bytes"],
                 "gkey": f"legacy/{lf['rel_path']}"}
                for lf in targets.get("legacy_files", [])
            ],
            "total_objects": len(items),
            "total_bytes": targets.get("bytes_freed", 0),
        },
        "restore_instructions": (
            "Each object's graveyard key is legacy/<rel_path>, where rel_path is "
            "relative to <base_dir>/.history/. Restore by copying the object back "
            "to a path OUTSIDE any live .history/ tree; the rel_path IS the "
            "reconstruction target (legacy files are plain copy-on-write snapshots, "
            "not content-addressed). Do NOT restore into live .history/ paths — "
            "that can re-arm read-through resurrection (rb-2859 donor-agent class)."
        ),
    }


# ---------------------------------------------------------------------------
# Orchestration — ENUMERATE -> ARCHIVE -> VERIFY -> DELETE -> RECEIPT
# ---------------------------------------------------------------------------

def run(base_dir, *, archiver, apply, metadata_only_after_days=None,
        run_ctx=None):
    """Archive-before-delete vacuum. Returns a status dict; never raises on the
    normal paths. Any archive/verify failure -> status "aborted", NO delete."""
    base_dir = Path(base_dir)
    if run_ctx is None:
        run_ctx = make_run_ctx()

    targets = enumerate_vacuum_targets(base_dir, metadata_only_after_days)
    summary = {
        "base_dir": str(base_dir),
        "orphan_blobs": len(targets.get("orphan_blobs", [])),
        "orphan_patches": len(targets.get("orphan_patches", [])),
        "drop_manifests": len(targets.get("drop_manifests", [])),
        "bytes_freed_estimate": targets.get("bytes_freed", 0),
    }

    # Phase-2a abort-on-corrupt is load-bearing: refuse everything.
    if targets.get("aborted"):
        return {"status": "aborted", "reason": targets["aborted"],
                "corrupt_manifests": targets.get("corrupt_manifests", []),
                **summary}

    items = normalize_items(targets)

    if not apply:
        # SAFE landing: enumerate + report only. No archive, no delete.
        return {"status": "dry_run", **summary}

    if not items and not targets.get("drop_manifests"):
        return {"status": "clean", **summary}

    # ARCHIVE + VERIFY (only when there is payload to archive).
    if items:
        arch = archiver.archive(items, run_ctx)
        if arch["failed"]:
            return {"status": "aborted", "reason": "archive_failed",
                    "failed": arch["failed"], **summary}
        ver = archiver.verify(items, run_ctx)
        if ver["missing"]:
            return {"status": "aborted", "reason": "verify_failed",
                    "missing": ver["missing"], **summary}

    # RECEIPT (persisted WITH the archive) BEFORE the delete.
    receipt = build_receipt(base_dir, targets, items, run_ctx)
    receipt_loc = archiver.write_receipt(receipt, run_ctx)

    # DELETE — only now, only the exact enumerated set.
    deleted = delete_vacuum_targets(base_dir, targets)
    return {"status": "deleted", "receipt": receipt_loc,
            "deleted": deleted, **summary}


def run_legacy_drain(base_dir, *, archiver, apply, run_ctx=None):
    """Archive-before-delete drain of the frozen legacy per-file .history dirs
    (g-115-2989). SEPARATE from run()'s CAS vacuum — a different store with a
    path-based (not content-addressed) restore model. Same ordering:
    ENUMERATE -> ARCHIVE -> VERIFY -> DELETE -> RECEIPT. Any archive/verify
    failure -> status "aborted", NO delete (fail-safe on the single-copy store).

    Gated by history_vacuum.drain_legacy_dirs (checked by the CALLER, main()):
    this runs only when the drain is enabled. apply=False still enumerates +
    reports (dry_run) so the mandatory pre-flip review can inspect exactly what
    WOULD be drained before the flag is ever flipped."""
    base_dir = Path(base_dir)
    if run_ctx is None:
        run_ctx = make_run_ctx()

    targets = enumerate_legacy_dir_targets(base_dir)
    summary = {
        "pass": "legacy_drain",
        "base_dir": str(base_dir),
        "legacy_entries": targets.get("entries", []),
        "legacy_files": len(targets.get("legacy_files", [])),
        "bytes_freed_estimate": targets.get("bytes_freed", 0),
    }

    if not apply:
        # SAFE: enumerate + report only. No archive, no delete.
        return {"status": "dry_run", **summary}

    items = normalize_legacy_items(targets)
    if not items:
        return {"status": "clean", **summary}

    # ARCHIVE + VERIFY.
    arch = archiver.archive(items, run_ctx)
    if arch["failed"]:
        return {"status": "aborted", "reason": "archive_failed",
                "failed": arch["failed"], **summary}
    ver = archiver.verify(items, run_ctx)
    if ver["missing"]:
        return {"status": "aborted", "reason": "verify_failed",
                "missing": ver["missing"], **summary}

    # RECEIPT (persisted WITH the archive) BEFORE the delete.
    receipt = build_legacy_receipt(base_dir, targets, items, run_ctx)
    receipt_loc = archiver.write_receipt(receipt, run_ctx)

    # DELETE — only now, only the exact enumerated set.
    deleted = delete_legacy_targets(base_dir, targets)
    return {"status": "deleted", "receipt": receipt_loc,
            "deleted": deleted, **summary}


def make_run_ctx():
    now = datetime.now()
    box_id = socket.gethostname() or "unknown-box"
    run_id = now.strftime("%Y-%m-%d_%H%M%S")
    return {"box_id": box_id, "run_id": run_id,
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%S")}


# ---------------------------------------------------------------------------
# Config + CLI
# ---------------------------------------------------------------------------

def load_config():
    """Read the history_vacuum block from core/config/aspirations.yaml.

    Fail-SAFE: any error returns enabled=false (GC never runs on a bad config —
    a silently-skipped GC is safe; an accidental delete is not)."""
    try:
        import yaml
        from _paths import CONFIG_DIR
        with open(CONFIG_DIR / "aspirations.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        hv = cfg.get("history_vacuum") or {}
        return {
            "enabled": bool(hv.get("enabled", False)),
            "interval_hours": int(hv.get("interval_hours", 24)),
            "metadata_only_after_days": hv.get("metadata_only_after_days", 30),
            "apply": bool(hv.get("apply", False)),
            "drain_legacy_dirs": bool(hv.get("drain_legacy_dirs", False)),
            "archiver": str(hv.get("archiver", "s3")),
            "graveyard_bucket": hv.get("graveyard_bucket", ""),
            "graveyard_prefix": hv.get("graveyard_prefix", ""),
            "stale_lock_reclaim_hours": int(hv.get("stale_lock_reclaim_hours", 2)),
        }
    except Exception:
        return {"enabled": False, "interval_hours": 24,
                "metadata_only_after_days": 30, "apply": False,
                "drain_legacy_dirs": False,
                "archiver": "s3", "graveyard_bucket": "",
                "graveyard_prefix": "", "stale_lock_reclaim_hours": 2}


def _make_archiver(cfg, args):
    kind = args.archiver or cfg["archiver"]
    if kind == "local":
        root = args.local_graveyard_dir or os.environ.get(
            "HISTORY_GRAVEYARD_DIR")
        if not root:
            raise SystemExit(
                "archiver=local requires --local-graveyard-dir or "
                "HISTORY_GRAVEYARD_DIR (Decision 3: local archive is test/dev "
                "only — production MUST use archiver=s3)")
        return LocalDirArchiver(root)
    bucket = args.graveyard_bucket or cfg["graveyard_bucket"]
    prefix = args.graveyard_prefix or cfg["graveyard_prefix"]
    if not bucket or not prefix:
        raise SystemExit("archiver=s3 requires graveyard_bucket + "
                         "graveyard_prefix (config or flags)")
    return S3Archiver(bucket, prefix)


def main():
    p = argparse.ArgumentParser(
        description="Archive-before-delete history-store vacuum (g-115-2792-b)")
    p.add_argument("--base-dir", help="dir CONTAINING .history (WORLD/META dir)")
    p.add_argument("--config-probe", action="store_true",
                   help="print {enabled, interval_hours} JSON and exit")
    p.add_argument("--apply", dest="apply", action="store_true", default=None,
                   help="override config: perform the archive+delete")
    p.add_argument("--dry-run", dest="apply", action="store_false",
                   help="override config: enumerate + report only")
    p.add_argument("--drain-legacy", dest="drain_legacy", action="store_true",
                   default=None,
                   help="override config: ALSO drain the frozen legacy per-file "
                        ".history dirs (g-115-2989). With --dry-run: enumerate "
                        "the legacy set for pre-flip review without deleting.")
    p.add_argument("--no-drain-legacy", dest="drain_legacy",
                   action="store_false",
                   help="override config: skip the legacy-dir drain")
    p.add_argument("--metadata-only-after-days", type=int, default=None)
    p.add_argument("--archiver", choices=["s3", "local"], default=None)
    p.add_argument("--local-graveyard-dir", default=None)
    p.add_argument("--graveyard-bucket", default=None)
    p.add_argument("--graveyard-prefix", default=None)
    args = p.parse_args()

    cfg = load_config()

    if args.config_probe:
        # Shell-parseable: "<enabled> <interval_hours> <reclaim_hours>" for the
        # tick's `read`. enabled is lowercased bool ("true"/"false").
        print(f"{str(cfg['enabled']).lower()} {cfg['interval_hours']} "
              f"{cfg['stale_lock_reclaim_hours']}")
        return

    if not args.base_dir:
        p.error("--base-dir is required unless --config-probe")

    apply = cfg["apply"] if args.apply is None else args.apply
    meta_days = (args.metadata_only_after_days
                 if args.metadata_only_after_days is not None
                 else cfg["metadata_only_after_days"])

    # A dry-run needs no archiver; only build one when actually applying.
    archiver = None
    if apply:
        archiver = _make_archiver(cfg, args)

    result = run(args.base_dir, archiver=archiver, apply=apply,
                 metadata_only_after_days=meta_days)
    print(json.dumps(result, default=str))

    # Legacy per-file-dir drain (g-115-2989) — a SEPARATE, independently-gated
    # pass. OFF by default (drain_legacy_dirs=false), so the tick never touches
    # legacy dirs until a deliberate flip. Runs under a distinct "-legacy" run_id
    # so its graveyard subdir + receipt never collide with the CAS pass above.
    drain_legacy = (cfg["drain_legacy_dirs"] if args.drain_legacy is None
                    else args.drain_legacy)
    if drain_legacy:
        legacy_ctx = make_run_ctx()
        legacy_ctx["run_id"] = legacy_ctx["run_id"] + "-legacy"
        legacy_result = run_legacy_drain(args.base_dir, archiver=archiver,
                                         apply=apply, run_ctx=legacy_ctx)
        print(json.dumps(legacy_result, default=str))


if __name__ == "__main__":
    main()
