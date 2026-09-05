#!/usr/bin/env python3
# domain-leak-exempt: harness names (Claude Code, zak-code) and S3 client calls
# are the subject matter of this module, not domain terms.
"""transcript_archive — harness-agnostic archiver for agent session transcripts.

WHY THIS EXISTS (g-328-55). Claude Code deletes every transcript modified more
than ``cleanupPeriodDays`` ago ON STARTUP — no recycle bin, no restore, no
warning. The fleet-wide mitigation (``cleanupPeriodDays=3650``) stops the
bleeding; this is the durable answer.

WHY IT IS BUILT RATHER THAN ADOPTED. Nine open-source tools were evaluated for
this goal (see g-328-55 ``progress_note``). The category is INDEX / SEARCH /
ANALYTICS: exactly 3 of 9 copy raw bytes at all, and the three largest
(agentsview 5.7k stars, cass 1.1k, Wake 729) read agent directories in place and
can never restore a deleted file byte-for-byte. An indexer is worthless against
a threat that deletes the ORIGINAL. Decisively, NOT ONE of the nine has a
configurable destination — every one hardcodes a local path or a git repo — so
the storage-backend routing requirement eliminates the entire field on its own.

TWO DESIGNS ARE REUSED (designs, not code, so no license attaches):
  * deletion detection, from DazzleML/Claude-Session-Backup (GPL-3.0): diff a
    live scan against a PERSISTED index of everything previously seen and mark
    previously-seen-but-now-absent as deleted. This is what turns the archive
    from a copy into a RECORD OF WHAT EXISTED, which is the only thing that
    makes "did we lose anything?" answerable.
  * mtime+size incremental scan, from iAmCorey/Wake (MIT): reported at ~310
    sessions / ~800 MB indexed in ~5 s — the same shape as a real fleet box
    (DESKTOP-O91DLK2 measured 310 files / 763 MB), so a plain mtime diff is
    demonstrably sufficient and nothing cleverer is warranted.

RAW ONLY, NO FORMAT BINDING. Bytes are copied verbatim: no parsing, no schema,
no normalisation. Four trajectory formats exist (ADP, ATIF v1.2, UATD, Letta
trajectory-v1) serving three different JOBS; capture is format-agnostic because
raw is already on disk, and format binds at EXPORT time, never at capture.

DESTINATION IS SELECTED BY THE STORAGE BACKEND — one code path, two
destinations, never two forks:
  * ``own-cloud`` -> ``s3://<bucket>/<env-id>/transcripts/<machine>/...``
    reusing the live backend's client, bucket and env-id so credentials are
    resolved exactly once, in the place that already does it.
  * ``local``     -> a directory OUTSIDE the repo working tree (default
    ``~/.mind-transcript-archive``), asserted not to be under PROJECT_ROOT.

The S3 prefix ``transcripts/`` deliberately sits under NO governed root. The
sync machinery iterates ``backend._roots`` (``agents``/``world``/``meta``) and
``owncloud_sync._EAGER_PULL_ROOTS`` is ``("world","meta")``, so archived objects
are invisible to every sweep and can never be pulled down onto a box. That is
what keeps a 763 MB archive from materialising in 13 local mirrors — and it is
why the governed-path API (``mirror_put``/``_put``) is NOT used here: ``_rel()``
raises for any path under no configured root, and transcripts live outside all
of them by construction.

*** NEVER PUTS TRANSCRIPTS IN GIT. *** One observed session file is 222 MB
against GitHub's hard 100 MB limit; the largest three on one measured box are
96/85/69 MB. They are immutable append-only blobs (git buys nothing) and are
plaintext containing everything echoed to a terminal, in a repo cloned to every
container.

Subcommands:
  scan      read-only: what WOULD be archived, what is new, what disappeared
  archive   copy new/changed transcripts, update the index, write a receipt
  verify    re-read archived objects and compare sha256 against the index
  restore   pull one archived transcript back to a chosen path
  index     print the persisted index (or one entry)
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

INDEX_VERSION = 1
ARCHIVE_PREFIX = "transcripts"
DEFAULT_LOCAL_DIRNAME = ".mind-transcript-archive"


# ---------------------------------------------------------------------------
# Harness adapters — the extensible half. Adding a harness is one entry.
# ---------------------------------------------------------------------------

class Harness:
    """One agent harness's on-disk transcript shape.

    ``roots`` are candidate directories (the first that exists wins).
    ``glob`` is applied recursively beneath it. ``rel`` of a discovered file is
    its path relative to the winning root, POSIX-normalised — which preserves
    Claude Code's per-project nesting and zak-code's flat layout without either
    adapter knowing about the other.
    """

    def __init__(self, name: str, roots: List[Path], glob: str):
        self.name = name
        self.roots = roots
        self.glob = glob

    def root(self) -> Optional[Path]:
        for r in self.roots:
            if r.is_dir():
                return r
        return None

    def discover(self) -> Iterator[Tuple[Path, str]]:
        root = self.root()
        if root is None:
            return
        for p in root.rglob(self.glob):
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            yield p, p.relative_to(root).as_posix()


def _home() -> Path:
    # USERPROFILE first: on Windows/MSYS, HOME can point at the MSYS home while
    # both harnesses write under the Windows profile.
    for var in ("USERPROFILE", "HOME"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return Path(v)
    return Path.home()


def harnesses() -> List[Harness]:
    h = _home()
    return [
        Harness("claude-code", [h / ".claude" / "projects"], "*.jsonl"),
        Harness("zak-code", [h / ".zakcode" / "transcripts"], "*.jsonl"),
        # zak-code keeps a sibling session-document dir; it is small, it is the
        # join key for a transcript, and it is useless to archive one without
        # the other.
        Harness("zak-code-sessions", [h / ".zakcode" / "sessions"], "*.json"),
    ]


# ---------------------------------------------------------------------------
# Destinations — the one selection point
# ---------------------------------------------------------------------------

class Destination:
    name = "abstract"

    def put(self, local: Path, key: str) -> int:  # returns bytes written
        raise NotImplementedError

    def get_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


class LocalDestination(Destination):
    name = "local"

    def __init__(self, root: Path, project_root: Optional[Path]):
        root = root.expanduser().resolve()
        if project_root is not None:
            try:
                root.relative_to(Path(project_root).resolve())
            except ValueError:
                pass  # good: outside the working tree
            else:
                raise SystemExit(
                    "REFUSING: local archive root %s is INSIDE the repo working tree %s. "
                    "Transcripts must never reach git (g-328-55: 222MB observed files vs "
                    "GitHub's 100MB hard limit; plaintext in a repo cloned to every container). "
                    "Set TRANSCRIPT_ARCHIVE_DIR to a path outside the repo." % (root, project_root)
                )
        self.root = root

    def _p(self, key: str) -> Path:
        return self.root / key

    def put(self, local: Path, key: str) -> int:
        dest = self._p(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp-%d" % os.getpid())
        shutil.copyfile(local, tmp)
        os.replace(tmp, dest)
        return dest.stat().st_size

    def get_bytes(self, key: str) -> bytes:
        return self._p(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._p(key).is_file()

    def describe(self) -> str:
        return "local:%s" % self.root


class S3Destination(Destination):
    name = "own-cloud"

    def __init__(self, backend):
        self.s3 = backend.s3
        self.bucket = backend.bucket
        self.env_id = backend.env_id

    def _k(self, key: str) -> str:
        return "%s/%s" % (self.env_id, key)

    def put(self, local: Path, key: str) -> int:
        # upload_file is the managed transfer: it multiparts large objects
        # instead of holding a 96 MB body in memory.
        self.s3.upload_file(str(local), self.bucket, self._k(key))
        return local.stat().st_size

    def get_bytes(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._k(key))
            return True
        except Exception:
            return False

    def describe(self) -> str:
        return "s3://%s/%s/%s" % (self.bucket, self.env_id, ARCHIVE_PREFIX)


def resolve(project_root: Optional[Path] = None) -> Tuple[Destination, str]:
    """Pick the destination from the SAME backend selection everything else uses.

    Returns (destination, machine_id). Never re-derives credentials or the
    bucket — that resolution already exists in storage_backend/owncloud_backend
    and duplicating it is how two sources of truth start.
    """
    machine = (os.environ.get("MACHINE_ID") or "").strip() or socket.gethostname()
    try:
        import storage_backend as sb
        backend = sb.get_backend()
    except Exception as exc:  # no boto3, no config, pure-local box
        backend = None
        if (os.environ.get("STORAGE_BACKEND") or "").strip() == "own-cloud":
            raise SystemExit("own-cloud selected but backend unavailable: %r" % (exc,))
    if backend is not None and getattr(backend, "name", "local") == "own-cloud":
        machine = (getattr(backend, "machine_id", "") or machine)
        return S3Destination(backend), machine
    override = (os.environ.get("TRANSCRIPT_ARCHIVE_DIR") or "").strip()
    root = Path(override) if override else (_home() / DEFAULT_LOCAL_DIRNAME)
    return LocalDestination(root, project_root), machine


# ---------------------------------------------------------------------------
# Index — the persisted record of what existed
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def index_key(machine: str) -> str:
    return "%s/%s/_index.json" % (ARCHIVE_PREFIX, machine)


def load_index(dest: Destination, machine: str) -> dict:
    try:
        raw = dest.get_bytes(index_key(machine))
    except Exception:
        return {"version": INDEX_VERSION, "machine": machine,
                "updated_at": None, "entries": {}}
    try:
        d = json.loads(raw.decode("utf-8"))
    except Exception:
        # A corrupt index must NEVER silently become an empty one: that would
        # re-upload everything AND erase the record of what existed, which is
        # the one thing this file is for.
        raise SystemExit(
            "REFUSING: index at %s is present but unparseable. Investigate before "
            "running archive — an empty index would erase the deletion record." % index_key(machine))
    d.setdefault("entries", {})
    return d


def save_index(dest: Destination, machine: str, idx: dict, tmpdir: Path) -> None:
    idx["version"] = INDEX_VERSION
    idx["machine"] = machine
    idx["updated_at"] = _now()
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp = tmpdir / ("_index.%d.json" % os.getpid())
    tmp.write_text(json.dumps(idx, indent=1, sort_keys=True), encoding="utf-8")
    dest.put(tmp, index_key(machine))
    try:
        tmp.unlink()
    except OSError:
        pass


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with io.open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# The scan / archive core
# ---------------------------------------------------------------------------

def scan() -> Dict[str, dict]:
    """Every transcript currently on disk, keyed <harness>/<relpath>."""
    live: Dict[str, dict] = {}
    for h in harnesses():
        for path, rel in h.discover():
            try:
                st = path.stat()
            except OSError:
                continue
            live["%s/%s" % (h.name, rel)] = {
                "path": str(path), "size": st.st_size, "mtime": st.st_mtime,
            }
    return live


def _unchanged(entry: dict, cur: dict) -> bool:
    # mtime+size is the Wake discriminator. A transcript is append-only, so a
    # changed file always changes size; mtime alone would be enough and size is
    # the cheap second signal.
    return (entry.get("archived_at")
            and entry.get("size") == cur["size"]
            and abs(float(entry.get("mtime") or -1) - cur["mtime"]) < 1e-6)


def do_scan(dest: Destination, machine: str, as_json: bool) -> int:
    live = scan()
    idx = load_index(dest, machine)
    entries = idx["entries"]
    new, changed, unchanged = [], [], []
    for k, cur in live.items():
        e = entries.get(k)
        if e is None:
            new.append(k)
        elif _unchanged(e, cur):
            unchanged.append(k)
        else:
            changed.append(k)
    vanished = [k for k, e in entries.items()
                if k not in live and not e.get("deleted_at")]
    total_new_bytes = sum(live[k]["size"] for k in new + changed)
    out = {
        "destination": dest.describe(), "machine": machine,
        "live_files": len(live),
        "live_bytes": sum(v["size"] for v in live.values()),
        "indexed": len(entries),
        "new": len(new), "changed": len(changed), "unchanged": len(unchanged),
        "would_transfer_bytes": total_new_bytes,
        "newly_vanished": len(vanished),
        "vanished_sample": vanished[:10],
        "by_harness": {},
    }
    for h in harnesses():
        pfx = h.name + "/"
        sel = {k: v for k, v in live.items() if k.startswith(pfx)}
        r = h.root()
        out["by_harness"][h.name] = {
            "root": str(r) if r else None,
            "files": len(sel), "bytes": sum(v["size"] for v in sel.values()),
        }
    print(json.dumps(out, indent=1) if as_json else _fmt_scan(out))
    return 0


def _fmt_scan(o: dict) -> str:
    L = ["destination : %s" % o["destination"],
         "machine     : %s" % o["machine"],
         "live        : %d files, %.1f MB" % (o["live_files"], o["live_bytes"] / 1048576.0),
         "indexed     : %d" % o["indexed"],
         "new         : %d   changed: %d   unchanged: %d" % (o["new"], o["changed"], o["unchanged"]),
         "to transfer : %.1f MB" % (o["would_transfer_bytes"] / 1048576.0),
         "vanished    : %d newly absent since last scan" % o["newly_vanished"]]
    for name, d in o["by_harness"].items():
        L.append("  %-18s %5d files  %8.1f MB  root=%s"
                 % (name, d["files"], d["bytes"] / 1048576.0, d["root"]))
    return "\n".join(L)


def do_archive(dest: Destination, machine: str, tmpdir: Path,
               limit_bytes: Optional[int], dry_run: bool, as_json: bool) -> int:
    started = _now()
    t0 = time.time()
    live = scan()
    idx = load_index(dest, machine)
    entries = idx["entries"]

    todo: List[Tuple[str, dict]] = []
    unchanged = 0
    for k, cur in live.items():
        e = entries.get(k)
        if e is not None and _unchanged(e, cur):
            unchanged += 1
            entries[k]["last_seen"] = started
            continue
        todo.append((k, cur))
    todo.sort(key=lambda kv: kv[1]["size"])  # small files first: partial runs still help

    archived, failed, sent_bytes = [], [], 0
    for k, cur in todo:
        if limit_bytes is not None and sent_bytes >= limit_bytes:
            break
        src = Path(cur["path"])
        key = "%s/%s/%s" % (ARCHIVE_PREFIX, machine, k)
        if dry_run:
            archived.append(k)
            sent_bytes += cur["size"]
            continue
        try:
            digest = sha256_of(src)
            n = dest.put(src, key)
        except Exception as exc:
            failed.append({"key": k, "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        prev = entries.get(k) or {}
        entries[k] = {
            "size": cur["size"], "mtime": cur["mtime"], "sha256": digest,
            "archive_key": key, "archived_at": _now(),
            "first_seen": prev.get("first_seen") or started,
            "last_seen": started, "deleted_at": None,
            "harness": k.split("/", 1)[0],
        }
        archived.append(k)
        sent_bytes += n

    # --- deletion detection (the DazzleML design) --------------------------
    newly_deleted = []
    for k, e in entries.items():
        if k not in live and not e.get("deleted_at"):
            e["deleted_at"] = started
            newly_deleted.append(k)

    receipt = {
        "receipt_version": 1, "event": "transcript-archive",
        "machine": machine, "destination": dest.describe(),
        "started_at": started, "finished_at": _now(),
        "elapsed_seconds": round(time.time() - t0, 2),
        "dry_run": dry_run,
        "live_files": len(live), "live_bytes": sum(v["size"] for v in live.values()),
        "archived_count": len(archived), "archived_bytes": sent_bytes,
        "unchanged_skipped": unchanged,
        "failed_count": len(failed), "failures": failed[:20],
        "newly_deleted_detected": len(newly_deleted),
        "newly_deleted_sample": newly_deleted[:20],
        "index_total_entries": len(entries),
        "by_harness": {h.name: sum(1 for k in archived if k.startswith(h.name + "/"))
                       for h in harnesses()},
        "restore": ("py -3 core/scripts/transcript_archive.py restore --key '<key>' "
                    "--out <path>   # verifies sha256 against the index"),
        "restore_warning": ("Do NOT restore into the live harness directory. Claude Code "
                            "owns that tree and a restored file can be re-deleted at the "
                            "next startup. Restore to a scratch path and copy deliberately."),
    }

    if not dry_run:
        save_index(dest, machine, idx, tmpdir)
        tmpdir.mkdir(parents=True, exist_ok=True)
        rp = tmpdir / ("RECEIPT-%d.json" % os.getpid())
        rp.write_text(json.dumps(receipt, indent=1, sort_keys=True), encoding="utf-8")
        dest.put(rp, "%s/%s/receipts/RECEIPT-%s.json"
                 % (ARCHIVE_PREFIX, machine, started.replace(":", "").replace("-", "")))
        try:
            rp.unlink()
        except OSError:
            pass

    print(json.dumps(receipt, indent=1) if as_json else _fmt_receipt(receipt))
    return 1 if failed else 0


def _fmt_receipt(r: dict) -> str:
    return "\n".join([
        "%s transcript-archive %s" % ("[DRY-RUN]" if r["dry_run"] else "[ARCHIVED]", r["machine"]),
        "  destination : %s" % r["destination"],
        "  archived    : %d files, %.1f MB" % (r["archived_count"], r["archived_bytes"] / 1048576.0),
        "  skipped     : %d unchanged" % r["unchanged_skipped"],
        "  deleted     : %d newly detected as gone from disk" % r["newly_deleted_detected"],
        "  failed      : %d" % r["failed_count"],
        "  index       : %d total entries" % r["index_total_entries"],
        "  elapsed     : %ss" % r["elapsed_seconds"],
    ])


def do_verify(dest: Destination, machine: str, sample: int, as_json: bool) -> int:
    idx = load_index(dest, machine)
    live_entries = [(k, e) for k, e in idx["entries"].items() if e.get("archive_key")]
    live_entries.sort(key=lambda kv: kv[1].get("size", 0))
    if sample > 0:
        live_entries = live_entries[:sample]
    ok, bad, missing = 0, [], []
    for k, e in live_entries:
        try:
            blob = dest.get_bytes(e["archive_key"])
        except Exception as exc:
            missing.append({"key": k, "error": str(exc)[:120]})
            continue
        if hashlib.sha256(blob).hexdigest() == e.get("sha256"):
            ok += 1
        else:
            bad.append({"key": k, "expected": e.get("sha256"),
                        "actual": hashlib.sha256(blob).hexdigest()})
    out = {"checked": len(live_entries), "ok": ok, "mismatched": len(bad),
           "unreadable": len(missing), "mismatches": bad[:10], "missing": missing[:10]}
    print(json.dumps(out, indent=1) if as_json else
          "verified %d/%d  mismatched=%d unreadable=%d"
          % (ok, len(live_entries), len(bad), len(missing)))
    return 0 if (not bad and not missing) else 1


def do_restore(dest: Destination, machine: str, key: str, out: Path) -> int:
    idx = load_index(dest, machine)
    e = idx["entries"].get(key)
    if e is None:
        raise SystemExit("no index entry for %r (use `index` to list)" % key)
    blob = dest.get_bytes(e["archive_key"])
    got = hashlib.sha256(blob).hexdigest()
    if got != e.get("sha256"):
        raise SystemExit("REFUSING to write: sha256 mismatch (index %s, archive %s)"
                         % (e.get("sha256"), got))
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise SystemExit("REFUSING to clobber existing %s — choose another --out" % out)
    out.write_bytes(blob)
    print(json.dumps({"restored": str(out), "bytes": len(blob), "sha256": got,
                      "source_key": e["archive_key"],
                      "original_path_hint": key,
                      "warning": "Do NOT copy this into the live harness tree; "
                                 "the harness owns that directory and may re-delete it."},
                     indent=1))
    return 0


def do_index(dest: Destination, machine: str, key: Optional[str], deleted_only: bool) -> int:
    idx = load_index(dest, machine)
    if key:
        print(json.dumps(idx["entries"].get(key, {}), indent=1))
        return 0
    ents = idx["entries"]
    if deleted_only:
        ents = {k: v for k, v in ents.items() if v.get("deleted_at")}
    keys = sorted(ents)
    # The listing is capped, so say so IN THE OUTPUT. A consumer grepping
    # `keys` for a harness that sorts after the cap gets a clean, wrong
    # absence -- measured while writing this: `grep '"zak-code/'` returned 0
    # against 741 archived zak-code files, because 200 claude-code keys sort
    # first. `count` was right the whole time; nothing said the two disagreed.
    print(json.dumps({"machine": idx.get("machine"), "updated_at": idx.get("updated_at"),
                      "count": len(ents), "keys_listed": min(len(keys), 200),
                      "keys_truncated": len(keys) > 200,
                      "keys": keys[:200]}, indent=1))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command",
                    choices=["scan", "archive", "verify", "restore", "index"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="archive: report what would transfer; write nothing")
    ap.add_argument("--max-bytes", type=int, default=None,
                    help="archive: stop after roughly this many bytes (staged first runs)")
    ap.add_argument("--sample", type=int, default=5,
                    help="verify: check the N smallest archived objects (0 = all)")
    ap.add_argument("--key", default=None, help="restore/index: the <harness>/<relpath> key")
    ap.add_argument("--out", default=None, help="restore: destination path")
    ap.add_argument("--deleted", action="store_true", help="index: only entries seen deleted")
    ap.add_argument("--tmpdir", default=None,
                    help="staging dir for the index/receipt (default: system temp)")
    a = ap.parse_args(argv)

    project_root = Path(__file__).resolve().parent.parent.parent
    dest, machine = resolve(project_root)
    import tempfile
    tmpdir = Path(a.tmpdir) if a.tmpdir else Path(tempfile.gettempdir()) / "transcript-archive"

    if a.command == "scan":
        return do_scan(dest, machine, a.json)
    if a.command == "archive":
        return do_archive(dest, machine, tmpdir, a.max_bytes, a.dry_run, a.json)
    if a.command == "verify":
        return do_verify(dest, machine, a.sample, a.json)
    if a.command == "restore":
        if not a.key or not a.out:
            raise SystemExit("restore requires --key and --out")
        return do_restore(dest, machine, a.key, Path(a.out))
    if a.command == "index":
        return do_index(dest, machine, a.key, a.deleted)
    return 2


if __name__ == "__main__":
    sys.exit(main())
