#!/usr/bin/env python3
"""Cold-snapshot the precious world/ + meta/ prefixes to a retention-immune key.

WHY THIS EXISTS (g-328-44, measured 2026-07-31)
------------------------------------------------
The own-cloud bucket's lifecycle tightened `expire-noncurrent-versions`
NoncurrentDays 90 -> 14. That shrank the accidental-overwrite/delete recovery
window 6.4x for every object in the store.

The natural assumption is that `.history` copy-on-write snapshots cover this.
They do not, and the reason is structural rather than a bug:

  `.history` is in owncloud_sync._EXCLUDE_DIRS, so snapshot files are
  MACHINE-LOCAL and never reach the object store at all.

Measured three independent ways on 2026-07-31 (cc-03): `.history` is a literal
member of `_EXCLUDE_DIRS`; `OwnCloudBackend._machine_local()` returns True for a
snapshot path; and a list of the corresponding S3 prefix returns 0 objects. So
`.history` protects against a local mistake on ONE box and dies with that box —
it does not extend the object store's recovery window by one second.

Census the same day (excluded dirs pruned), across all three governed roots:

    world/ + meta/ ................. 1959 files
      covered by .history (local) ..... 74
      machine-local telemetry ......... 18   (acceptable loss)
      in the store, NO .history ..... 1867   <-- 14-day net and nothing else
        of which knowledge-tree nodes 1329
                  world/scripts ....... 180  (gitignored external path)
                  world/conventions .... 89  (gitignored external path)

    agents/ ........................ 6095 files
      machine-local ..................... 6
      in the store .................. 6089   <-- LARGER than world+meta
        of which experience records .. 3914
                  temp/ (scratch) .... 1278  (acceptable loss, own drain cycle)
                  journal ............. 424
                  health .............. 257
      agent dirs carrying any .history ... 1 of 5

THE TWO ROOTS ARE NOT EQUALLY EXPOSED — and the asymmetry is the reverse of
what file counts suggest. Measured, not assumed (`git ls-files`, `git remote`):

    world/ + meta/  -- NOT git-tracked. They are external gitignored paths
                       (CLAUDE.md "External paths"). The 14-day noncurrent
                       window is the ONLY layer that survives losing a box.
    agents/**       -- git-tracked AND pushed to a GitHub remote, so experience
                       (502/503), journal (58/58), health (46/46), self.md and
                       curriculum.yaml already HAVE an off-box second net.
                       agents/<agent>/session/ is the exception: 0 of 96
                       tracked.

So agent dirs are the LARGER surface but the BETTER-protected one, and world +
meta -- a third the file count -- carry nearly all the real exposure. They are
still snapshot together, for two reasons git does not cover: `session/` is
untracked, and a snapshot is a point-in-time COHERENT set across all three
roots, which a stream of independent commits is not.

This paragraph is the corrected version. The first draft asserted the opposite
split from memory -- that self.md was tracked and experience/journal/health were
not -- and one `git ls-files` falsified it. The extension is still right; the
reason written down for it was wrong until it was measured.

WHY A UNIQUE KEY IS THE FIX
---------------------------
The lifecycle was READ, not assumed (archive-before-delete.md step 2 — "when
the identity cannot READ the recovery config, treat the layer as ABSENT"):

    abort-incomplete-multipart      prefix=''  AbortIncompleteMPU 7d
    expire-noncurrent-versions      prefix=''  NoncurrentDays 14
    (no current-version Expiration rule exists)

Only NONCURRENT versions expire. An object written at a key that is never
overwritten stays CURRENT forever, so no retention clock touches it. That is
exactly the archive-before-delete.md step-3 "current-version copies are the
retention-immune form" shape, and it is why each run writes a fresh
timestamped key instead of updating one.

PREFIX CONSTRAINT (measured, not inferred)
------------------------------------------
Snapshots go under the env's OWN prefix (`<customer_prefix><env_id>/`), NOT the
shared top-level `graveyard/`. The backend principal was probed directly:
PutObject under the env prefix SUCCEEDS; PutObject under top-level `graveyard/`
returns AccessDenied. That matches the archive-aws-graveyard forged skill, whose
delete leg routes through efs-ssh.sh for exactly this reason.

Note the principal is the BACKEND CLIENT's, which is not necessarily the one the
SDK's default credential chain reports — those named two different identities on
the measuring box, so a capability probe run through the default chain describes
a client this script never uses. Verification here reads a single object's
metadata rather than listing a prefix, because the list permission is denied to
this principal.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import META_DIR, WORLD_DIR, agents_root  # noqa: E402


# ---------------------------------------------------------------------------
# WHERE THE ARCHIVE GOES — the per-writer endpoint policy ()
#
# STORAGE_S3_ENDPOINT_URL is read by the client factory, so every writer that
# resolves through get_backend() follows an object-store cutover at once. This
# writer must NOT: its whole purpose is a copy held OUTSIDE the live store's
# blast radius, and after a flip to a self-hosted store the "outside" copy would
# land on the same disk as the thing it protects — silently, reporting success
# every run (guard-6373). So the target is resolved HERE, as a pure decision
# over the environment, and the resolved values are printed before anything
# else happens (guard-5551):
#
#   live store is AWS (no override)        -> share the live bucket, as before
#   live store flipped, no COLD_SNAPSHOT_*  -> REFUSE (refused-colocated, rc 2)
#   COLD_SNAPSHOT_* fully set              -> a dedicated backend on that target
#   COLD_SNAPSHOT_* partially set          -> REFUSE (refused-partial-config)
#   COLD_SNAPSHOT endpoint == live endpoint -> REFUSE (same hardware)
#
# A refusal is a FAILED run on purpose: cold-snapshot-tick.py files an
# Investigate on any verdict that is not ok, which is exactly the loudness the
# silent-relocation failure lacked.
# ---------------------------------------------------------------------------
COLD_TARGET_VARS = ("COLD_SNAPSHOT_S3_BUCKET",
                    "COLD_SNAPSHOT_AWS_ACCESS_KEY_ID",
                    "COLD_SNAPSHOT_AWS_SECRET_ACCESS_KEY")
COLD_ENDPOINT_VAR = "COLD_SNAPSHOT_S3_ENDPOINT_URL"
AWS_REGIONAL = "aws-regional"


def resolve_cold_target(env) -> dict:
    """Pure: decide where the archive goes from an env MAPPING (never IO).

    Returns a dict with `mode` in {local, live, pinned, refused}; a refusal
    carries `verdict` (the run's verdict string) and `reason`.
    """
    def _get(k):
        return (env.get(k) or "").strip()

    live_endpoint = _get("STORAGE_S3_ENDPOINT_URL")
    live_bucket = _get("STORAGE_S3_BUCKET")
    kind = (_get("STORAGE_BACKEND") or "local").lower()
    base = {"backend": kind, "live_bucket": live_bucket,
            "live_endpoint": live_endpoint or AWS_REGIONAL}
    if kind != "own-cloud":
        return {**base, "mode": "local",
                "reason": "STORAGE_BACKEND is not own-cloud: no remote "
                          "retention clock to protect against"}
    present = {v: bool(_get(v)) for v in COLD_TARGET_VARS}
    n_set = sum(present.values())
    if n_set == 0:
        if live_endpoint:
            return {**base, "mode": "refused", "verdict": "refused-colocated",
                    "reason": "STORAGE_S3_ENDPOINT_URL is set (the live store is "
                              "not AWS) and no COLD_SNAPSHOT_* target is "
                              "configured, so the DR archive would land on the "
                              "store it exists to protect. Set "
                              + ", ".join(COLD_TARGET_VARS)
                              + f" (+ optional {COLD_ENDPOINT_VAR})."}
        return {**base, "mode": "live", "endpoint": AWS_REGIONAL,
                "bucket": live_bucket,
                "reason": "live store is AWS: the archive shares its bucket at "
                          "a never-overwritten key (pre-cutover posture)"}
    if n_set != len(COLD_TARGET_VARS):
        missing = sorted(v for v, ok in present.items() if not ok)
        return {**base, "mode": "refused", "verdict": "refused-partial-config",
                "reason": "COLD_SNAPSHOT_* is partially configured; missing "
                          + ", ".join(missing)}
    cold_endpoint = _get(COLD_ENDPOINT_VAR)
    if live_endpoint and cold_endpoint == live_endpoint:
        return {**base, "mode": "refused", "verdict": "refused-colocated",
                "reason": f"{COLD_ENDPOINT_VAR} names the live store's own "
                          "endpoint: same hardware is not outside the blast "
                          "radius, whatever the bucket"}
    return {**base, "mode": "pinned", "endpoint": cold_endpoint or AWS_REGIONAL,
            "bucket": _get("COLD_SNAPSHOT_S3_BUCKET"),
            "reason": "dedicated DR target, independent of the live store"}


def target_line(target: dict) -> str:
    """The guard-5551 first line: every value that selects the destination."""
    return (f"[target] mode={target['mode']} "
            f"cold_endpoint={target.get('endpoint', '-')} "
            f"cold_bucket={target.get('bucket', '-')} "
            f"live_endpoint={target['live_endpoint']} "
            f"live_bucket={target['live_bucket'] or '-'} "
            f"backend={target['backend']}")


def build_cold_backend(target: dict):
    """The backend the archive is written through, or None for `local`.

    `live` reuses the process-wide backend. `pinned` builds a SEPARATE
    OwnCloudBackend from an overlaid copy of the env — never by mutating
    os.environ, which would also repoint get_backend()'s cached instance.
    """
    if target["mode"] == "local":
        return None
    from storage_backend import get_backend
    live = get_backend()
    if not hasattr(live, "s3"):
        return None
    if target["mode"] == "live":
        return live
    from owncloud_backend import OwnCloudBackend
    env = dict(os.environ)
    env["STORAGE_S3_BUCKET"] = target["bucket"]
    env["STORAGE_S3_ENDPOINT_URL"] = (
        "" if target["endpoint"] == AWS_REGIONAL else target["endpoint"])
    env["MIND_AWS_ACCESS_KEY_ID"] = os.environ["COLD_SNAPSHOT_AWS_ACCESS_KEY_ID"]
    env["MIND_AWS_SECRET_ACCESS_KEY"] = os.environ["COLD_SNAPSHOT_AWS_SECRET_ACCESS_KEY"]
    return OwnCloudBackend.from_env(env=env)

try:
    AGENTS_DIR = agents_root()
except Exception:  # unresolvable agents root -- world/meta still snapshot
    AGENTS_DIR = None

# Directory segments never worth cold-snapshotting. `.history` is excluded for a
# different reason than the rest: it is itself the local snapshot store, so
# including it would square the archive size for no recovery value (the live
# files it shadows are already in the tarball).
#
# `temp` is the agent scratch store, which has its own drain lifecycle
# (temp-store.md) -- 1278 files on the measuring box, none of them state worth
# restoring.
_SKIP_DIRS = {
    ".history", ".locks", "__pycache__", ".git", "node_modules",
    ".pytest_cache", "presence", "sessions", "temp",
}


def _roots():
    """(root, archive_prefix) pairs to snapshot, in archive order.

    AGENT DIRS ARE NOT OPTIONAL, and leaving them out is the easy mistake: the
    originating goal named `agents/<agent>/health/*.jsonl` as its one known
    example, so it is tempting to treat agent state as a footnote to world/meta.
    Measured on cc-03 2026-07-31, agent dirs are the LARGEST synced surface of
    the three -- 6089 files reach the object store, against 1959 for
    world+meta -- and they carry 3914 experience records, 424 journal entries
    and every agent's `self.md` identity file. Only one agent dir on that box
    had any `.history` store at all, so for the rest the 14-day noncurrent
    window was the sole recovery layer for the entire learning archive.
    """
    pairs = [(WORLD_DIR, "world"), (META_DIR, "meta")]
    if AGENTS_DIR and AGENTS_DIR.exists():
        for adir in sorted(p for p in AGENTS_DIR.iterdir() if p.is_dir()):
            pairs.append((adir, f"agents/{adir.name}"))
    return pairs

# Suffixes whose content is regenerable telemetry / already-consumed reports.
# Excluded to keep the archive to irreplaceable state. Deliberately NOT a
# blanket *.jsonl skip: aspirations/reasoning-bank/guardrails are .jsonl and are
# the most precious files in the store.
_SKIP_SUFFIXES = ("-telemetry.jsonl", "-metrics.jsonl", ".tmp", ".lock")

# Rolled-off audit archives, excluded by default and restorable with
# --include-archives. This is the goal's step-2 classification, and it is a
# measurement rather than a taste call (cc-03, 2026-07-31):
#
#     archives ................. 289.0MB across    13 files   (66% of bytes)
#     knowledge tree nodes ......  10.6MB across  1331 files
#     world/scripts .............   1.9MB across   184 files
#     world/conventions .........   1.0MB across    89 files
#
# The whole irreplaceable core is ~13.5MB; two changelog archives alone are
# 269MB. Archives are append-only rolled-off history whose live head is already
# in the snapshot, so paying 20x the bytes every run to re-ship immutable tails
# would make a frequent cadence unaffordable — and an unaffordable cadence is
# how a backup silently stops running. Excluded by DEFAULT, never
# unconditionally: --include-archives produces the complete set when a
# migration or audit needs it.
_ARCHIVE_MARKERS = ("-archive.jsonl", "gate-firings.jsonl")


def _is_archive(rel: str) -> bool:
    """True for rolled-off audit archives (see _ARCHIVE_MARKERS)."""
    base = rel.rsplit("/", 1)[-1]
    # Catches both `x-archive.jsonl` and sidecar copies like
    # `changelog-archive.jsonl.8cA3dEA5`, which carry the same bytes twice.
    return any(m in base for m in _ARCHIVE_MARKERS) or ".bak-" in base


def _iter_precious(root: Path, include_archives: bool = False):
    """Yield (abs_path, rel_posix) for files worth archiving under `root`."""
    if not root or not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(seg in _SKIP_DIRS for seg in rel.parts[:-1]):
            continue
        if path.name.endswith(_SKIP_SUFFIXES):
            continue
        rel_posix = str(rel).replace("\\", "/")
        if not include_archives and _is_archive(rel_posix):
            continue
        yield path, rel_posix


def build_manifest(include_archives: bool = False):
    """Enumerate what WOULD be archived, with per-file sha256.

    The enumeration is the integrity baseline (archive-before-delete.md step 1),
    so it is produced before the tarball and stored inside the receipt.
    """
    entries = []
    total = 0
    for root, prefix in _roots():
        for path, rel in _iter_precious(root, include_archives):
            try:
                data = path.read_bytes()
            except OSError as exc:  # unreadable file is a reported gap, not a crash
                entries.append({"path": f"{prefix}/{rel}", "error": str(exc)})
                continue
            entries.append({
                "path": f"{prefix}/{rel}",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
            total += len(data)
    return entries, total


def build_snapshot(include_archives: bool = False):
    """ONE walk: read each file ONCE, hash those bytes, tar those same bytes.

    Returns (entries, total_bytes, tar_gz_blob).

    THE SINGLE WALK IS THE CORRECTNESS PROPERTY, not an optimization. The first
    version of this module hashed in `build_manifest` and then re-read every file
    in a separate `build_tarball` walk. On a live fleet the two reads see
    DIFFERENT BYTES: world/aspirations.jsonl, the board channels and the
    changelog are written continuously, and 6740 files take ~10-25s to walk, so
    anything written in that window landed in the archive with a receipt hash
    describing its PREVIOUS content. Measured directly (fresh-eyes probe,
    2026-07-31): manifest recorded 2 bytes / sha 44136fa3..., the archive held
    22 bytes / sha b8ae104c... for the same path, in the same run.

    That silently voided the receipt's whole purpose. `archive-before-delete.md`
    step 4 requires object count, total bytes AND per-object checksums to all
    match, and step 1 calls the enumeration "the integrity baseline" -- a
    manifest that disagrees with its own archive fails both, and fails them in
    the direction that reads as corruption during a restore, exactly when a
    reader most needs to trust it.

    Reading once and tarring the in-memory bytes makes manifest and archive
    consistent BY CONSTRUCTION, so no window exists to get wrong. Peak memory is
    one file plus the compressed stream, not the whole tree.
    """
    buf = io.BytesIO()
    entries = []
    total = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, prefix in _roots():
            for path, rel in _iter_precious(root, include_archives):
                arc = f"{prefix}/{rel}"
                try:
                    data = path.read_bytes()
                    mtime = int(path.stat().st_mtime)
                except OSError as exc:  # reported gap, not a crash or a silent drop
                    entries.append({"path": arc, "error": str(exc)})
                    continue
                entries.append({
                    "path": arc,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                })
                total += len(data)
                info = tarfile.TarInfo(name=arc)
                info.size = len(data)
                info.mtime = mtime
                tar.addfile(info, io.BytesIO(data))
    return entries, total, buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="enumerate + size the archive, upload nothing")
    ap.add_argument("--prefix", default="cold-snapshots",
                    help="key prefix under the env root (default: cold-snapshots)")
    ap.add_argument("--include-archives", action="store_true",
                    help="also archive rolled-off audit tails (~20x the bytes; "
                         "see _ARCHIVE_MARKERS for why they are off by default)")
    ap.add_argument("--output", choices=("text", "json"), default="text")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")

    # Resolve and PRINT the destination before the first read or request
    # (guard-5551). The bootstrap fills STORAGE_* from .env.local for a bare
    # subprocess exactly as get_backend() would have (), so the
    # decision sees the same env the upload will use. Under pytest it is a
    # no-op by design.
    from storage_backend import _bootstrap_env_defaults
    _bootstrap_env_defaults()
    target = resolve_cold_target(os.environ)
    print(target_line(target), file=sys.stderr)
    if target["mode"] == "refused":
        result = {"stamp": stamp, "target": target, "verdict": target["verdict"],
                  "error": target["reason"], "dry_run": bool(args.dry_run),
                  "files": 0, "unreadable": 0, "source_bytes": 0}
        _emit(result, args.output, [])
        return 2

    if args.dry_run:
        # Enumerate only -- no archive is produced, so there is nothing for the
        # manifest to be consistent WITH and the cheaper hash-only walk is right.
        entries, total = build_manifest(args.include_archives)
        blob = None
    else:
        entries, total, blob = build_snapshot(args.include_archives)
    ok = [e for e in entries if "error" not in e]
    failed = [e for e in entries if "error" in e]

    result = {
        "stamp": stamp,
        "files": len(ok),
        "unreadable": len(failed),
        "source_bytes": total,
        "dry_run": bool(args.dry_run),
        "target": target,
    }

    if args.dry_run:
        result["verdict"] = "dry-run"
        _emit(result, args.output, entries)
        return 0

    result["archive_bytes"] = len(blob)
    result["archive_sha256"] = hashlib.sha256(blob).hexdigest()

    backend = build_cold_backend(target)
    if backend is None:
        # LocalBackend (tests, or a local-only deployment): nothing to protect
        # against a remote retention clock. Say so rather than silently passing.
        result["verdict"] = "skipped-local-backend"
        _emit(result, args.output, entries)
        return 0

    base = f"{backend._customer_prefix()}{backend.env_id}/{args.prefix}/{stamp}"
    archive_key, receipt_key = f"{base}/world-meta.tar.gz", f"{base}/receipt.json"

    receipt = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "goal": "g-328-44",
        "why": (
            "expire-noncurrent-versions NoncurrentDays=14 is the only recovery "
            "layer for ~1867 store objects that have no .history twin; .history "
            "is in owncloud_sync._EXCLUDE_DIRS and is machine-local, so it does "
            "not survive loss of the box."
        ),
        "retention": (
            "Unique key => the object stays CURRENT and is never noncurrent, so "
            "the 14-day noncurrent expiry never applies. Verified by reading the "
            "bucket lifecycle: no current-version Expiration rule exists."
        ),
        "archive_key": archive_key,
        "target": target,
        "archive_bytes": len(blob),
        "archive_sha256": result["archive_sha256"],
        "file_count": len(ok),
        "source_bytes": total,
        "unreadable": failed,
        "restore": [
            "aws s3 cp s3://<bucket>/<archive_key> ./world-meta.tar.gz",
            "tar -tzf world-meta.tar.gz   # inspect BEFORE extracting",
            "Extract to a SCRATCH dir and diff against live before copying back.",
        ],
        "do_not_restore_into": (
            "Never extract straight over live WORLD_DIR/META_DIR. Under own-cloud "
            "the local tree is a read-through cache; a bulk overwrite republishes "
            "stale bytes to the store for every file it touches. Restore only the "
            "specific files you intend, through the normal fenced write path."
        ),
        "manifest": entries,
    }

    backend.s3.put_object(Bucket=backend.bucket, Key=archive_key, Body=blob)
    backend.s3.put_object(
        Bucket=backend.bucket, Key=receipt_key,
        Body=json.dumps(receipt, indent=2).encode("utf-8"),
    )

    # Verify against the enumeration (archive-before-delete.md step 4). head,
    # not list: ListBucket is denied to this principal, and a verification that
    # cannot run is worse than one that reports honestly.
    head = backend.s3.head_object(Bucket=backend.bucket, Key=archive_key)
    remote_bytes = head["ContentLength"]
    result["archive_key"] = archive_key
    result["receipt_key"] = receipt_key
    result["remote_bytes"] = remote_bytes
    result["verified"] = remote_bytes == len(blob)
    result["verdict"] = "ok" if result["verified"] else "size-mismatch"

    _emit(result, args.output, entries)
    return 0 if result["verified"] else 1


def _emit(result, fmt, entries):
    if fmt == "json":
        print(json.dumps(result, indent=2))
        return
    if result.get("target"):
        print(target_line(result["target"]))
    print(f"[cold-snapshot] {result['verdict']}: "
          f"{result['files']} files, {result['source_bytes'] / 1024 / 1024:.1f}MB source")
    if result.get("error"):
        print(f"[cold-snapshot] REFUSED: {result['error']}")
    if result.get("archive_bytes"):
        print(f"[cold-snapshot] archive {result['archive_bytes'] / 1024 / 1024:.1f}MB "
              f"sha256={result.get('archive_sha256', '')[:16]}...")
    if result.get("archive_key"):
        print(f"[cold-snapshot] key={result['archive_key']}")
        print(f"[cold-snapshot] verified={result['verified']} "
              f"(remote {result['remote_bytes']}B == local {result['archive_bytes']}B)")
    if result["unreadable"]:
        print(f"[cold-snapshot] WARNING {result['unreadable']} unreadable file(s) "
              f"EXCLUDED from the archive:")
        for e in entries:
            if "error" in e:
                print(f"    {e['path']}: {e['error']}")


if __name__ == "__main__":
    sys.exit(main())
