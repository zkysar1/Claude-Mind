"""Drain the per-box trigger-firings spool into the shared store in ONE batched RMW.

Companion to `trigger_firings.py`'s own-cloud spool lane (g-358-79). This is a
PORT of `gate-firings-flush.py` (g-115-2405 / g-306-432), which has run the same
protocol in production since 2026-08; read that file alongside this one, and fix
both when the protocol changes.

WHY: under STORAGE_BACKEND=own-cloud, `record_firing` used to call
`locked_append_jsonl` directly on `meta/trigger-firings.jsonl`. That is a
whole-object S3 read-modify-write per firing AND — unlike gate-firings, which is
in `_fileops._SNAPSHOT_BLACKLIST` — also a whole-file `.history` snapshot per
firing, since `locked_append_jsonl` is "lock -> history -> append -> changelog".
Measured 810 MB of PUT bytes / 672 object versions per 24h on a store whose
churn nobody reads.

TWO AXES, AND THIS FIXES ONE. The spool collapses WRITE FREQUENCY (many small
appends -> a few flushes). The OBJECT-SIZE axis is untouched: each flush still
re-PUTs the whole object. That is decisive here anyway because the FIFO cap
bounds this store at 5000 records (~1-2 MB), so collapsing hundreds of daily
whole-object PUTs into a few tens is the win. Do not read this script as having
solved amplification in general (g-328-38 store-composition seam).

Flush protocol (crash-safe, duplicate-safe) — identical to the gate-firings lane:
  1. Take the LOCAL flush lock (the spool is per-box, so cross-machine locking
     is meaningless; stale-break frees a crashed flusher).
  2. Gate: skip when the spool is empty AND no crash residue exists; skip when
     the last flush was < --min-interval-seconds ago UNLESS the spool holds
     >= --burst-records lines.
  3. Crash residue (`.flushing`) drains FIRST — a prior flusher died between
     rename and unlink. The append dedups by serialized line, so re-flushing a
     landed batch is a no-op.
  4. Else atomically rename spool -> .flushing. Appenders opening the spool path
     after the rename create a fresh spool; the flusher owns .flushing.
  5. Parse torn-line-tolerant, and reject non-dict records: a torn append can
     land on a valid JSON scalar (`7`), which parses fine and then kills every
     reader doing rec.get(). That exact line took both gate-telemetry tools down
     for 11 days.
  6. ONE `locked_modify_jsonl` appending records not already present (dedup key
     = `json.dumps(rec, ensure_ascii=True)`, the identity
     merge_append_only_jsonl unions by, so flush/merge/union all agree).
  7. Re-stat .flushing before unlink to pick up a raced appender's delta.
  8. Unlink .flushing, stamp last-flush, then enforce the FIFO cap.

THE CAP IS ENFORCED HERE, and that is a real difference from the sibling.
`record_firing`'s lazy 1-in-50 `_enforce_cap()` does a locked read of the whole
shared store — the exact whole-object RMW the spool exists to avoid — so the
spool lane skips it and the flusher, which already holds the store open, does it
instead. Without this the cap would silently stop being enforced under own-cloud.

Contract: fail-open — every error path prints to stderr and exits 0 (a missed
flush is fully recovered by the next tick; the spool just grows).
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import META_DIR  # noqa: E402
from storage_backend import LocalBackend  # noqa: E402
from trigger_firings import (  # noqa: E402
    SPOOL_NAME, FLUSHING_NAME, _enforce_cap,
)

STAMP_NAME = "trigger-firings.spool.last-flush"
FLUSH_LOCK_NAME = "trigger-firings.spool.flush.lock"
STORE_NAME = "trigger-firings.jsonl"


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
        if not isinstance(rec, dict):
            # See protocol step 5 — a scalar is torn by the same argument as an
            # unparseable line, and this is the write-side stop.
            torn += 1
            continue
        records.append(rec)
    return records, torn


def _flush_file(flushing: Path, store: Path, dry_run: bool) -> int:
    """Append `flushing`'s records to `store` in one locked RMW. Returns the
    number appended (post-dedup). Raises on store-write failure (caller
    converts to fail-open)."""
    records, torn = _parse_lossy(flushing)
    if torn:
        print(f"[trigger-firings-flush] WARN: skipped {torn} torn line(s) in "
              f"{flushing.name} (interrupted appends; harmless for telemetry)",
              file=sys.stderr)
    if not records:
        if not dry_run:
            flushing.unlink(missing_ok=True)
        return 0

    if dry_run:
        print(f"[trigger-firings-flush] dry-run: would append {len(records)} "
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

    # Raced-append delta: an appender holding the pre-rename fd may have landed
    # a line after our read. One re-read pass picks it up.
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
        print("[trigger-firings-flush] META_DIR unresolved — nothing to flush",
              file=sys.stderr)
        return 0

    spool = meta / SPOOL_NAME
    flushing = meta / FLUSHING_NAME
    stamp = meta / STAMP_NAME
    store = meta / STORE_NAME
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
        print(f"[trigger-firings-flush] lock busy/failed ({e}) — deferring to "
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
                print(f"[trigger-firings-flush] dry-run: spool holds "
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
                print(f"[trigger-firings-flush] flushed {total} record(s) to "
                      f"{store.name} in {time.time() - t0:.2f}s "
                      f"(one batched RMW)")
                # The spool lane skips record_firing's lazy cap check (it would
                # cost the very RMW the spool avoids), so the cap is enforced
                # HERE, after the batch has landed. Only when something was
                # appended — an empty flush must stay a true no-op.
                _enforce_cap(store)
    except Exception as e:  # noqa: BLE001 — fail-open maintenance sweep
        print(f"[trigger-firings-flush] WARN: flush failed ({type(e).__name__}: "
              f"{e}) — spool retained, next tick retries", file=sys.stderr)
    finally:
        try:
            lb.release_lock(lock_path)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
