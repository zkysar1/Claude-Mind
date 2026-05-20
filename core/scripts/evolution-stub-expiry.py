#!/usr/bin/env python3
"""Evolution stub expiry sweep.

Closes the gap that produced finding F2 (2026-05-15): guard-544 promised a
deadline-driven auto-completion of `awaiting_completion` evolution stubs, but
NO such mechanism was ever implemented (verified by exhaustive negative
search). Stale stubs therefore accumulated forever — 436 across the 5 streams
at the time of writing.

Honest design choice: a stale stub means "the WHY was never recorded within
the deadline." We do NOT fabricate `reasoning='[AUTO-FILLED]'` for an edit
some other session made (that pollutes the evolution history with non-
explanations and violates verify-before-assuming — we cannot know the why we
did not record). Instead we transition the stub to `status: "expired"`, which
is a status the schema ALREADY defines (self-program-evolution.md Entry
schema, status enum line). `expired` is the honest terminal state for
"rationale never supplied"; the edit itself already happened on disk and the
`.history` snapshot survives — only the rationale is permanently absent, and
we say so plainly rather than inventing it.

Idempotent: already-`expired` (and any non-`awaiting_completion`) records are
left untouched. Lock-safe: each stream is rewritten via
`_fileops.locked_modify_jsonl`, so a concurrent loop appending a new stub
cannot be clobbered.

Wiring: invoked every iteration from `iteration-close.sh`'s
do_productivity_check maintenance tick (`--threshold-hours 24`), beside
recurring-precondition-sweep — idempotent, cheap, no-LLM, fail-open, and
contention-free (NOT a recurring aspirations-queue goal: that queue is
heavily write-locked by the loops). Also runnable on demand.

Usage:
  py -3 core/scripts/evolution-stub-expiry.py [--threshold-hours N]
       [--stream self|program|skill|rule|script] [--dry-run] [--json]
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

_STREAMS = (
    "self-evolution.jsonl",
    "program-evolution.jsonl",
    "skill-evolution.jsonl",
    "rule-evolution.jsonl",
    "script-evolution.jsonl",
)

DEFAULT_THRESHOLD_HOURS = 24


def resolve_world_dir():
    """Resolve WORLD_DIR via _paths.py (uses MIND_AGENT binding)."""
    try:
        from _paths import WORLD_DIR
        if WORLD_DIR:
            return Path(WORLD_DIR)
    except Exception:
        pass
    return None


def _parse_ts(entry):
    """Best-effort creation timestamp for a stub, ALWAYS naive-local.

    Prefers the explicit `ts` field (ISO-8601). Falls back to the timestamp
    embedded in revision_id (`<kind>-<YYYYMMDDTHHMMSS>-<agent>-<hex>`).
    Returns a naive LOCAL datetime, or None if neither is parseable — in
    which case the caller treats the stub as NOT expirable (cannot prove
    staleness). The naive-local guarantee is load-bearing: callers subtract
    this from `datetime.now()` (also naive-local).
    """
    ts = (entry.get("ts") or "").strip()
    if ts:
        try:
            dt = datetime.fromisoformat(ts)
            # CRITICAL — do not remove this tzinfo strip. ~75% of historical
            # stubs carry an offset (e.g. '2026-05-10T01:52:21-04:00') and
            # parse tz-AWARE; `now` is naive-local, and `aware - naive` is a
            # TypeError that silently kills the whole sweep (the maintenance
            # tick swallows it via `|| true`). Collapse to ONE time domain
            # (naive-local). guard-420 failure class.
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except ValueError:
            pass
    rid = (entry.get("revision_id") or "").strip()
    parts = rid.split("-")
    if len(parts) >= 2:
        try:
            return datetime.strptime(parts[1], "%Y%m%dT%H%M%S")
        except ValueError:
            pass
    return None


def _collect_stale(items, now, cutoff_seconds):
    """THE staleness predicate — single source of truth.

    Returns (stale_entries, awaiting_count). A stub is stale iff its status
    is awaiting_completion AND it has a parseable creation timestamp older
    than the cutoff. Used by the dry-run report, the pre-write gate, AND
    the locked modifier — never reimplement this inline (the 2026-05-15
    review found a duplicated copy in the dry-run path that could drift
    from the writer's predicate).
    """
    stale, awaiting = [], 0
    for entry in items:
        if entry.get("status") != "awaiting_completion":
            continue
        awaiting += 1
        created = _parse_ts(entry)
        if created is None:
            # Cannot prove staleness → never expire (verify-before-assuming:
            # a missing/garbled ts is zero signal, not licence to expire).
            continue
        if (now - created).total_seconds() >= cutoff_seconds:
            stale.append(entry)
    return stale, awaiting


def sweep_stream(path, threshold_hours, now, dry_run):
    """Expire stale awaiting_completion stubs in one stream.

    Returns {"expired": int, "awaiting": int, "rids": [revision_id, ...]}.
    """
    result = {"expired": 0, "awaiting": 0, "rids": []}
    if not path.exists():
        return result
    cutoff_seconds = threshold_hours * 3600
    now_iso = now.isoformat(timespec="seconds")

    # Cheap read-only pre-scan (skip blank/corrupt lines). This is the
    # GATE, not the authority — the locked modifier below re-reads under
    # the lock and is the source of truth for the actual mutation.
    items = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        return result

    stale, awaiting = _collect_stale(items, now, cutoff_seconds)
    result["awaiting"] = awaiting
    if dry_run:
        result["expired"] = len(stale)
        result["rids"] = [e.get("revision_id") for e in stale]
        return result
    if not stale:
        # CRITICAL — keep this early return. locked_modify_jsonl ALWAYS
        # snapshots .history + rewrites the file + appends a changelog row
        # (no unchanged-skip — verified _fileops.py). Calling it when
        # nothing is stale (the common steady state) would churn .history
        # and contend the write-lock with the loops on every tick.
        return result

    def modifier(locked_items):
        # CRITICAL — re-collect from locked_items; do NOT close over the
        # pre-scan `stale` list. Those are different dict objects (read
        # outside the lock): mutating them would not change what is
        # written, and a concurrent evolution-complete may have finalized
        # one since the pre-scan. This lock'd pass is the authority; the
        # counts below reflect what ACTUALLY changed here (honest, not the
        # pre-scan estimate).
        fresh, _ = _collect_stale(locked_items, now, cutoff_seconds)
        for entry in fresh:
            entry["status"] = "expired"
            entry["expired_at"] = now_iso
            entry["expired_by"] = "evolution-stub-expiry"
            entry["expiry_reason"] = (
                f"awaiting_completion exceeded {threshold_hours}h deadline; "
                f"rationale was never supplied (honest expiry — not fabricated)"
            )
        result["expired"] = len(fresh)
        result["rids"] = [e.get("revision_id") for e in fresh]
        return locked_items

    from _fileops import locked_modify_jsonl
    locked_modify_jsonl(path, modifier)
    return result


def main():
    ap = argparse.ArgumentParser(
        description="Expire stale awaiting_completion evolution stubs (F2)."
    )
    ap.add_argument("--threshold-hours", type=int,
                    default=DEFAULT_THRESHOLD_HOURS,
                    help=f"Age past which an awaiting_completion stub is "
                         f"expired (default {DEFAULT_THRESHOLD_HOURS}h).")
    ap.add_argument("--stream", choices=["self", "program", "skill", "rule",
                                         "script"],
                    help="Restrict to one stream (default: all 5).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would expire without writing.")
    ap.add_argument("--json", action="store_true",
                    help="Emit a JSON summary on stdout.")
    args = ap.parse_args()

    world_dir = resolve_world_dir()
    if not world_dir:
        print("ERROR: cannot resolve WORLD_DIR (is MIND_AGENT set with a "
              "valid local-paths.conf?)", file=sys.stderr)
        return 2

    if args.threshold_hours < 1:
        print("ERROR: --threshold-hours must be >= 1", file=sys.stderr)
        return 2

    now = datetime.now()
    streams = _STREAMS
    if args.stream:
        streams = (f"{args.stream}-evolution.jsonl",)

    summary = {"dry_run": args.dry_run, "threshold_hours": args.threshold_hours,
               "swept_at": now.isoformat(timespec="seconds"), "streams": {}}
    total_expired = 0
    total_awaiting = 0
    for name in streams:
        res = sweep_stream(world_dir / name, args.threshold_hours, now,
                           args.dry_run)
        summary["streams"][name] = {
            "awaiting_seen": res["awaiting"],
            "expired": res["expired"],
            "expired_rids": res["rids"],
        }
        total_expired += res["expired"]
        total_awaiting += res["awaiting"]

    summary["total_expired"] = total_expired
    summary["total_awaiting_seen"] = total_awaiting

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        verb = "WOULD expire" if args.dry_run else "expired"
        print(f"evolution-stub-expiry: {verb} {total_expired} stale stub(s) "
              f"of {total_awaiting} awaiting_completion seen "
              f"(threshold {args.threshold_hours}h)")
        for name, s in summary["streams"].items():
            if s["expired"]:
                print(f"  {name}: {s['expired']} {verb}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
