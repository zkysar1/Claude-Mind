#!/usr/bin/env python3
"""Drain the machine-local utilization-counter spools into the shared sidecars.

The writer half of g-358-05. `_utilization_store.record_increment` appends one
delta line per increment to a machine-local spool (O(1), lockless); this script
batches every pending delta into `<kind>-utilization.jsonl` with ONE locked
read-modify-write per kind, per flush.

WHAT IT REPLACES: today each increment is a whole-object RMW of the 20.46MB /
9.37MB CONTENT store to change one integer — measured ~51 GB/day of S3 PUT
traffic across the two stores, ~93-94% of all writes to them (g-358-02). After
the flip an increment touches a machine-local file, and the shared write is one
batched RMW of a ~2.14MB / ~1.02MB sidecar per flush interval.

DEFAULT-OFF: nothing spools until `UTILIZATION_COUNTERS_SPOOLED` is set on a box,
and no box may set it until `store-cutover-check.sh --store utilization` reports
SAFE fleet-wide. This script is a no-op on a box with no spool file, so it is
safe to wire into the maintenance tick BEFORE the flip — which is the point:
the drain path is exercised and proven long before it carries load.

MODELLED ON gate-firings-flush.py, deliberately and not incidentally — same
rotate-then-drain sequence, same crash-residue-first ordering, same interval
gate, same fail-open posture. Two DELIBERATE DIVERGENCES, both forced by the
payload being counters rather than log lines:

  1. AGGREGATION. gate-firings appends records; this SUMS deltas per
     (id, counter) before touching the store, so N increments of one counter
     cost one integer add rather than N appended lines. A spool holding 4,000
     increments across 300 records writes 300 changed records, not 4,000.
  2. SEEDING (see `_seed_from_content`). A counter is a RUNNING TOTAL, so an
     absent sidecar entry does not mean zero — it means "not migrated yet", and
     the two are indistinguishable in the file. Getting this wrong makes every
     counter appear to RESET on first increment.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _paths import WORLD_DIR                      # noqa: E402
from storage_backend import LocalBackend          # noqa: E402
import _utilization_store as us                   # noqa: E402


def _load_rb_module():
    """Load the hyphenated reasoning-bank.py module (established idiom —
    verbatim from recompute-utilization-scores.py `_load_rb_module`)."""
    spec = importlib.util.spec_from_file_location(
        "rb_cli_flush", _SCRIPTS / "reasoning-bank.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rb_cli_flush"] = mod
    spec.loader.exec_module(mod)
    return mod


def _parse_lossy(path: Path):
    """Torn-line-tolerant JSONL parse. Returns (records, torn_count).

    A torn line is possible only when a process died mid-append; it costs one
    advisory increment and must never abort a flush. Same contract as
    gate-firings-flush._parse_lossy and _utilization_store.load_counters.
    """
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
        if isinstance(rec, dict):
            records.append(rec)
        else:
            torn += 1
    return records, torn


def aggregate(deltas):
    """Sum spool lines into {id: {counter: total_delta}}.

    Pure and separately testable on purpose — this is where N increments become
    one integer add, and it is the only part of the flush whose arithmetic can
    be wrong without anything failing loudly.

    Non-integer / non-positive-shaped lines are dropped rather than coerced: a
    delta is a count, and a malformed one is telemetry noise, not a value to
    guess at.
    """
    out = {}
    for d in deltas:
        rec_id = d.get("id")
        counter = d.get("counter")
        if not rec_id or not counter:
            continue
        try:
            delta = int(d.get("delta", 1))
        except (TypeError, ValueError):
            continue
        if delta == 0:
            continue
        out.setdefault(rec_id, {})
        out[rec_id][counter] = out[rec_id].get(counter, 0) + delta
    return out


def _seed_from_content(kind, missing_ids, world_dir=None):
    """Embedded counters for ids that have no sidecar entry yet.

    WHY THIS EXISTS — the correctness crux of the whole cutover. `utilization_of`
    prefers the SIDECAR over the embedded field ("the sidecar wins on purpose",
    because during cutover the embedded copy is a frozen pre-split snapshot). So
    the first time a record is incremented, writing a sidecar entry containing
    ONLY that counter would make every OTHER counter for that record read as
    absent — i.e. zero. A record with times_helpful=41 would report 0 the moment
    something incremented times_active, and it would look entirely correct: no
    error, no exception, just counters silently reset across the corpus as
    increments trickled in.

    So a first-touch entry is seeded from the record's embedded counters and the
    delta is applied ON TOP. After the first touch the sidecar is authoritative
    for that id and this path is never taken for it again.

    Reads through the BACKEND, not `open()`: `store_paths` sets that contract
    explicitly ("a returned path may name an object that is not materialised
    locally yet"), and under own-cloud a bare open() would raise for a segment
    this box has never fetched.

    Returns {} on any failure — the caller then seeds from defaults, which is
    the correct fallback for a genuinely new record and a lossy-but-safe one for
    an old record whose store could not be read (the merge handler's per-counter
    MAX repairs it from any peer that did read it).
    """
    if not missing_ids:
        return {}
    wanted = set(missing_ids)
    found = {}
    try:
        from storage_backend import get_backend
        backend = get_backend()
    except Exception:
        backend = None
    for path in us.store_paths(kind, world_dir):
        if not wanted:
            break
        text = None
        try:
            if backend is not None:
                text = backend.read_bytes(path).decode("utf-8", errors="replace")
        except Exception:
            text = None
        if text is None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            rec_id = rec.get("id")
            if rec_id in wanted:
                counters = rec.get("utilization")
                if isinstance(counters, dict):
                    found[rec_id] = dict(counters)
                wanted.discard(rec_id)
                if not wanted:
                    break
    return found


def apply_deltas(existing, agg, seeds, counter_names, recompute=None):
    """Merge aggregated deltas into the sidecar map. Pure; returns a new map.

    `existing` and the result are both {id: counters_dict}. Every counter named
    in `counter_names` is materialised on a touched record, because
    `recompute_utilization_score` documents that its callers MUST have
    normalized first and that a missing key signals schema drift and "should
    fail loudly, not silently coerce to 0".
    """
    out = {rec_id: dict(counters) for rec_id, counters in existing.items()}
    for rec_id, counters in agg.items():
        base = out.get(rec_id)
        if base is None:
            base = dict(seeds.get(rec_id) or {})
        for name in counter_names:
            if not isinstance(base.get(name), int):
                base[name] = 0
        for counter, delta in counters.items():
            base[counter] = base.get(counter, 0) + delta
        if recompute is not None:
            try:
                recompute({"utilization": base})
            except Exception:
                pass          # a scoring nuance must never fail a flush
        out[rec_id] = base
    return out


def _drain(kind, flushing: Path, world_dir, counter_names, recompute,
           dry_run: bool) -> int:
    """Apply `flushing`'s deltas to the sidecar in ONE locked RMW.

    Returns the number of RECORDS changed (not the number of spool lines).
    """
    deltas, torn = _parse_lossy(flushing)
    if torn:
        print(f"[utilization-flush] WARN: skipped {torn} torn line(s) in "
              f"{flushing.name} (interrupted appends; one advisory increment "
              f"each)", file=sys.stderr)
    agg = aggregate(deltas)
    if not agg:
        if not dry_run:
            flushing.unlink(missing_ok=True)
        return 0

    if dry_run:
        total = sum(sum(c.values()) for c in agg.values())
        print(f"[utilization-flush] dry-run {kind}: {len(deltas)} spool line(s) "
              f"-> {total} increment(s) across {len(agg)} record(s)")
        return len(agg)

    sidecar = us.counters_path(kind, world_dir)
    if sidecar is None:
        return 0

    changed = {"n": 0}

    def _modifier(items):
        existing = {}
        for it in items:
            if isinstance(it, dict) and it.get("id"):
                counters = it.get("utilization")
                existing[it["id"]] = dict(counters) if isinstance(counters, dict) else {}
        seeds = _seed_from_content(
            kind, [i for i in agg if i not in existing], world_dir)
        merged = apply_deltas(existing, agg, seeds, counter_names, recompute)
        changed["n"] = len(agg)
        return [{"id": rec_id, "utilization": merged[rec_id]}
                for rec_id in sorted(merged)]

    from _fileops import locked_modify_jsonl
    locked_modify_jsonl(sidecar, _modifier, initial=[])
    flushing.unlink(missing_ok=True)
    return changed["n"]


def flush_kind(kind, world_dir, counter_names, recompute, args) -> int:
    """Rotate + drain one kind's spool. Returns records changed."""
    base = Path(world_dir)
    spool = base / us.spool_name(kind)
    flushing = base / us.flushing_name(kind)
    stamp = base / us.flush_stamp_name(kind)
    lock_path = base / us.flush_lock_name(kind)

    residue = flushing.exists()
    spool_lines = 0
    if spool.exists():
        try:
            with open(spool, "rb") as f:
                spool_lines = sum(1 for _ in f)
        except OSError:
            spool_lines = 0
    if not residue and spool_lines == 0:
        return 0                      # quiet common case: nothing spooled

    if not args.force and not residue and spool_lines < args.burst_records:
        try:
            last = float(stamp.read_text().strip())
        except (OSError, ValueError):
            last = 0.0
        if time.time() - last < args.min_interval_seconds:
            return 0

    lb = LocalBackend()
    try:
        lb.acquire_lock(lock_path, timeout=10, stale_seconds=120)
    except Exception as e:
        print(f"[utilization-flush] {kind}: lock busy/failed ({e}) — deferring "
              f"to next tick", file=sys.stderr)
        return 0
    total = 0
    try:
        t0 = time.time()
        # Crash residue drains BEFORE the fresh spool rotates onto it, or the
        # rename would destroy it (gate-firings-flush ordering, same reason).
        if flushing.exists():
            total += _drain(kind, flushing, world_dir, counter_names,
                            recompute, args.dry_run)
        if spool.exists() and os.path.getsize(spool) > 0:
            if args.dry_run:
                deltas, _ = _parse_lossy(spool)
                agg = aggregate(deltas)
                print(f"[utilization-flush] dry-run {kind}: spool holds "
                      f"{len(deltas)} line(s) across {len(agg)} record(s)")
            else:
                os.replace(spool, flushing)
                total += _drain(kind, flushing, world_dir, counter_names,
                                recompute, args.dry_run)
        if not args.dry_run:
            try:
                stamp.write_text(str(time.time()))
            except OSError:
                pass
            if total:
                print(f"[utilization-flush] {kind}: {total} record(s) updated "
                      f"in {time.time() - t0:.2f}s (one batched RMW)")
    except Exception as e:  # noqa: BLE001 — fail-open maintenance sweep
        print(f"[utilization-flush] WARN: {kind} flush failed "
              f"({type(e).__name__}: {e}) — spool retained, next tick retries",
              file=sys.stderr)
    finally:
        try:
            lb.release_lock(lock_path)
        except Exception:
            pass
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--world-dir", default=None,
                    help="Override WORLD_DIR (tests / multi-tenant).")
    ap.add_argument("--kind", default=None, choices=list(us.KINDS),
                    help="Flush only this kind (default: all).")
    ap.add_argument("--min-interval-seconds", type=int, default=300,
                    help="Skip when the last flush is more recent than this, "
                         "unless the spool holds >= --burst-records lines.")
    ap.add_argument("--burst-records", type=int, default=500,
                    help="Spool line count that overrides the interval gate.")
    ap.add_argument("--force", action="store_true",
                    help="Ignore the interval gate.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    world = args.world_dir or WORLD_DIR
    if world is None:
        print("[utilization-flush] WORLD_DIR unresolved — nothing to flush",
              file=sys.stderr)
        return 0

    try:
        rb_mod = _load_rb_module()
        recompute = rb_mod.recompute_utilization_score
        counter_names = sorted(rb_mod.UTILIZATION_COUNTERS)
    except Exception as e:   # noqa: BLE001
        # Degrade rather than skip: the counters are still summed correctly, only
        # the derived score is left for recompute-utilization-scores.py. Losing
        # the increments entirely would be far worse than a stale score.
        print(f"[utilization-flush] WARN: reasoning-bank module unavailable "
              f"({type(e).__name__}: {e}) — flushing without score recompute",
              file=sys.stderr)
        recompute = None
        counter_names = []

    kinds = [args.kind] if args.kind else list(us.KINDS)
    for kind in kinds:
        flush_kind(kind, world, counter_names, recompute, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
