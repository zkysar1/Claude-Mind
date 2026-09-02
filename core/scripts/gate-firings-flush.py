"""Drain the per-box gate-firings spool into the shared store in ONE batched RMW.

Companion to `_gate_log.py`'s own-cloud spool lane (g-115-2405). Under
STORAGE_BACKEND=own-cloud, `_gate_log.log()` appends each firing record to a
machine-local spool (`{META_DIR}/gate-firings.spool.jsonl`, lockless O_APPEND
— same idiom as `_fileops._record_fallback_hit`) instead of paying a
whole-object S3 read-modify-write per record (measured 3.8-10.1s per append
at 38-40MB / ~118k records). This script moves the spooled records into the
shared `gate-firings.jsonl` with ONE locked RMW per flush, so the per-firing
hot-path cost is O(1) local and the O(N) store cost is paid once per flush
batch instead of once per record.

Invocation: wired into `iteration-close.sh` do_productivity_check among the
idempotent once-per-iteration maintenance sweeps (fail-open, stderr sink).
Safe to run manually any time. No daemon endpoint — pure local + _fileops.

Flush protocol (crash-safe, duplicate-safe):
  1. Take the LOCAL flush lock (`gate-firings.spool.flush.lock` via
     LocalBackend.acquire_lock — the spool is per-box, so cross-machine DDB
     locking is meaningless; stale-break frees a crashed flusher).
  2. Gate: skip when the spool is empty AND no crash residue exists; skip
     when the last flush was < --min-interval-seconds ago UNLESS the spool
     has >= --burst-records lines (bounds S3 churn to ~1 RMW / interval).
  3. If crash residue (`gate-firings.spool.flushing.jsonl`) exists, flush
     THAT first (the prior flusher died between rename and unlink). The
     store-append dedups by serialized line, so a re-flush after a
     landed-PUT-then-crash is a no-op — duplicate-safe.
  4. Else atomically rename spool -> .flushing (os.replace). Appenders
     opening the spool path after the rename create a fresh spool; the
     flusher owns .flushing exclusively.
  5. Parse .flushing torn-line-tolerant (a torn tail line = an interrupted
     append; skip + warn, matching coordination_merge._parse_jsonl_lossy's
     posture for append-only logs).
  6. ONE `locked_modify_jsonl(store, ...)` appending all parsed records that
     are not already present (dedup key = the record's serialized line,
     `json.dumps(rec, ensure_ascii=True)` — the same identity
     merge_append_only_jsonl dedups by, so flush/merge/union all agree).
     gate-firings.jsonl is in _SNAPSHOT_BLACKLIST (file IS the history), so
     this RMW takes no .history snapshot.
  7. Re-stat .flushing before unlink: a raced appender holding the pre-rename
     fd can land a line after step 5's read; if the file grew, loop once to
     pick up the delta instead of unlinking it away.
  8. Unlink .flushing, stamp `gate-firings.spool.last-flush` (epoch seconds).

Contract: fail-open — every error path prints to stderr and exits 0 (a
missed flush is fully recovered by the next tick; the spool just grows).
Exit 0 always except argparse errors.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import META_DIR  # noqa: E402
from _gate_log import (  # noqa: E402
    LEGACY_STORE_NAME, SEGMENTED_ENV, segmented_enabled, store_name,
)
from storage_backend import LocalBackend  # noqa: E402

SPOOL_NAME = "gate-firings.spool.jsonl"
FLUSHING_NAME = "gate-firings.spool.flushing.jsonl"
STAMP_NAME = "gate-firings.spool.last-flush"
FLUSH_LOCK_NAME = "gate-firings.spool.flush.lock"
STORE_NAME = LEGACY_STORE_NAME

#  Stage 2: date-segmented flush target, DEFAULT OFF.
#
# With the flag off this resolves to STORE_NAME and behaviour is byte-identical
# to before the flag existed. With it on, each flush re-PUTs only today's
# segment (~1MB at ~3.2k records/day) instead of the full 40-day retention
# window (~42MB) -- measured amplification near 42,000:1, producing 26,610 S3
# versions / 968 GB, 65% of the whole bucket.
#
# ORDERING CONSTRAINT -- do not enable this on any box until _gate_log
# .firings_paths() is deployed FLEET-WIDE. A box still running the pre-seam
# consumers reads only the legacy filename, so it would see a segment holding a
# few hours of data and report it as the full 30-day window: a gate then looks
# unfired and therefore RETIRABLE. That is a false all-clear, which is the worst
# direction this system can fail in. The flag is per-box precisely so the seam
# can be rolled out first.
#
# The flag name, its truthiness rule and the resolved basename live in _gate_log
# (SEGMENTED_ENV / segmented_enabled / store_name) so this flush lane and the
# direct locked-append lane inside _gate_log.log() can never disagree about
# where a firing goes — they did, for the 12h after the fleet flip (2026-08-18).
_segmented_enabled = segmented_enabled   # name kept for the tests that pin it


def _store_path(meta: Path) -> Path:
    """Flush target: the legacy store, or today's segment when segmentation is on.

    The basename comes from _gate_log.store_name so the writer's filename and
    the reader's matcher share one definition (see segment_name there).
    """
    return meta / store_name()


def _serialize(rec: dict) -> str:
    # MUST match _fileops/_jsonl_text + merge_append_only_jsonl's dedup
    # identity: json.dumps(rec, ensure_ascii=True), no key sorting.
    return json.dumps(rec, ensure_ascii=True)


def _parse_lossy(path: Path):
    """Torn-line-tolerant JSONL parse. Returns (records, torn_count)."""
    records, torn = [], 0
    try:
        data = path.read_bytes()
    except OSError:
        return records, torn
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            torn += 1
            continue
        # A torn append can land on a valid JSON SCALAR — an interleaved write
        # that happens to break after a digit leaves a line like `7`, which
        # parses fine and so was never counted as torn. This flush then appended
        # it into the shared store, where every reader doing rec.get() died on
        # it: meta/gate-firings-2026-08-19.jsonl line 1 is exactly that, and it
        # took BOTH gate-telemetry tools down from 08-19 to 08-30. A non-dict is
        # torn by the same argument as an unparseable line — it is not a record.
        # This is the write-side stop; the read-side guards are the defence in
        # depth (guard-1512), not a substitute.
        if not isinstance(rec, dict):
            torn += 1
            continue
        records.append(rec)
    return records, torn


def _flush_file(flushing: Path, store: Path, dry_run: bool) -> int:
    """Append `flushing`'s records to `store` in one locked RMW. Returns the
    number of records appended (post-dedup). Raises on store-write failure
    (caller converts to fail-open)."""
    records, torn = _parse_lossy(flushing)
    if torn:
        print(f"[gate-firings-flush] WARN: skipped {torn} torn line(s) in "
              f"{flushing.name} (interrupted appends; harmless for telemetry)",
              file=sys.stderr)
    if not records:
        if not dry_run:
            flushing.unlink(missing_ok=True)
        return 0

    if dry_run:
        print(f"[gate-firings-flush] dry-run: would append {len(records)} "
              f"record(s) to {store}")
        return len(records)

    appended = {"n": 0}

    def _modifier(items):
        existing = {_serialize(it) for it in items}
        fresh = [r for r in records if _serialize(r) not in existing]
        appended["n"] = len(fresh)
        return items + fresh

    from _fileops import locked_modify_jsonl
    locked_modify_jsonl(store, _modifier, initial=[])

    # Raced-append delta: an appender holding the pre-rename fd may have
    # landed a line after our read. One re-read pass picks it up; the
    # recursion terminates because the fd race window is microseconds.
    try:
        post_records, _ = _parse_lossy(flushing)
    except Exception:
        post_records = records
    if len(post_records) > len(records):
        extra = post_records[len(records):]
        def _delta_modifier(items):
            existing = {_serialize(it) for it in items}
            fresh = [r for r in extra if _serialize(r) not in existing]
            appended["n"] += len(fresh)
            return items + fresh
        locked_modify_jsonl(store, _delta_modifier, initial=[])

    flushing.unlink(missing_ok=True)
    return appended["n"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--meta-dir", default=None,
                    help="Override META_DIR (multi-tenant daemon / tests).")
    ap.add_argument("--min-interval-seconds", type=int, default=300,
                    help="Skip when last flush is more recent than this, "
                         "unless the spool holds >= --burst-records lines.")
    ap.add_argument("--burst-records", type=int, default=200,
                    help="Spool line count that overrides the interval gate.")
    ap.add_argument("--force", action="store_true",
                    help="Ignore the interval gate.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    meta = Path(args.meta_dir) if args.meta_dir else META_DIR
    if meta is None:
        print("[gate-firings-flush] META_DIR unresolved — nothing to flush",
              file=sys.stderr)
        return 0

    spool = meta / SPOOL_NAME
    flushing = meta / FLUSHING_NAME
    stamp = meta / STAMP_NAME
    store = _store_path(meta)
    lock_path = meta / FLUSH_LOCK_NAME

    residue = flushing.exists()
    spool_lines = 0
    if spool.exists():
        try:
            with open(spool, "rb") as f:
                spool_lines = sum(1 for _ in f)
        except OSError:
            spool_lines = 0
    if not residue and spool_lines == 0:
        return 0  # quiet common case: nothing spooled

    # Interval gate (bounds whole-object S3 churn).
    if not args.force and not residue and spool_lines < args.burst_records:
        try:
            last = float(stamp.read_text().strip())
        except (OSError, ValueError):
            last = 0.0
        if time.time() - last < args.min_interval_seconds:
            return 0

    lb = LocalBackend()
    try:
        # stale_seconds > worst observed store RMW (~10s) with margin.
        lb.acquire_lock(lock_path, timeout=10, stale_seconds=120)
    except Exception as e:
        print(f"[gate-firings-flush] lock busy/failed ({e}) — deferring to "
              f"next tick", file=sys.stderr)
        return 0
    try:
        total = 0
        t0 = time.time()
        # Crash residue first — it must drain before the fresh spool rotates.
        if flushing.exists():
            total += _flush_file(flushing, store, args.dry_run)
        if spool.exists() and os.path.getsize(spool) > 0:
            if args.dry_run:
                recs, _ = _parse_lossy(spool)
                print(f"[gate-firings-flush] dry-run: spool holds "
                      f"{len(recs)} record(s)")
            else:
                os.replace(spool, flushing)
                total += _flush_file(flushing, store, args.dry_run)
        if not args.dry_run:
            try:
                stamp.write_text(str(time.time()))
            except OSError:
                pass
            if total:
                print(f"[gate-firings-flush] flushed {total} record(s) to "
                      f"{store.name} in {time.time() - t0:.2f}s (one batched RMW)")
    except Exception as e:  # noqa: BLE001 — fail-open maintenance sweep
        print(f"[gate-firings-flush] WARN: flush failed ({type(e).__name__}: "
              f"{e}) — spool retained, next tick retries", file=sys.stderr)
    finally:
        try:
            lb.release_lock(lock_path)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
