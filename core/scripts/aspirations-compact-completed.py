#!/usr/bin/env python3
"""Compact the bulky text bodies of AGED, COMPLETED, non-recurring goals out of
the live aspirations queue — the deep fix for B6/B9 (asp-115 add-goal 30-35s +
hot-lock contention on world/aspirations.jsonl).

THE PROBLEM (measured 2026-06-01)
  world/aspirations.jsonl = 3.78 MB / 27 aspirations / 2095 goals. asp-115 alone
  holds 1329 goals of which 1241 (93%) are terminal — it is `status: active`
  (the perpetual framework-maintenance aspiration that never completes), so its
  completed goals accumulate inline FOREVER (whole-aspiration archive never
  fires). Every write to the queue — append OR modify — is a full-file
  read+rewrite under ONE global lock; on own-cloud that is an S3 GET + S3 PUT of
  3.78 MB per write, serialized across all agents. The byte-dominant field is
  `description` (1.11 MB across asp-115's completed goals; 990 B avg).

WHY COMPACT, NOT SHARD / EVICT
  - Sharding addresses cross-aspiration lock contention but each shard still
    grows unboundedly — it delays the wall, not removes it. The ROOT is unbounded
    terminal-goal body accumulation.
  - Eviction (removing completed goals) would crater completion_ratio /
    completion_pressure / tail_bonus, which goal-selector.py derives from LIVE
    goal counts (done/total). Preserving scoring would require touching the
    selection scorer — wrong bet right before the multi-machine test.
  - COMPACTION keeps every goal RECORD (so counts — and therefore every scoring
    signal and recompute_progress — are byte-identical: NO scorer change) and
    strips only the bulky TEXT fields from goals that are already terminal and
    old. The selection path never reads those fields for completed goals (it
    scores only non-terminal goals; for terminal it reads status + recurring).
    Full bodies are recoverable from the .history snapshot taken automatically
    by locked_modify_jsonl immediately before the write.

  Sharding + full-evict-with-scorer-patch remain the documented 12+-agent
  escalations (see lodestar-bug-master-list B9). This tool is the foundational,
  low-risk, reversible first move.

SAFETY
  - Routes through _fileops.locked_modify_jsonl → DDB lock (cross-machine mutex)
    + force-fresh-from-S3 read + If-Match fenced PUT + .history snapshot +
    post-write JSONL canary. Same code path the daemon uses; under own-cloud the
    scoped MIND_AWS_* creds are used (fail-closed in OwnCloudBackend.from_env).
  - Strips ONLY {description, verification, outcome_note} and ONLY from goals
    that are status==completed AND not recurring AND completed > --age-days ago
    AND not already compacted. Recurring goals, live work (pending/blocked/
    in-progress), and recently-completed goals are never touched.
  - Idempotent: marks `body_compacted: true`; re-runs skip already-compacted.
  - The modifier asserts goal COUNT and per-status COUNTS are unchanged before
    returning; any mismatch raises and the locked write is aborted (no write).
  - DRY-RUN by default. --apply performs the write.

USAGE
  Dry-run (read-only; reports projected shrink):
    set -a; source .env.local; set +a   # load STORAGE_BACKEND + scoped creds
    MIND_AGENT=alpha py -3 core/scripts/aspirations-compact-completed.py --source world
  Apply:
    ... --source world --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, AGENT_DIR  # noqa: E402

# Bulky text fields stripped from aged-completed goals. Chosen empirically:
# `description` is 1.11 MB of 's 2.4 MB; `verification` + `outcome_note`
# are the next-largest free-text fields. All three are unread by the selection
# scorer for terminal goals. Everything else (status, dates, priority, recurring,
# blocked_by, intended_agent, counters, …) is preserved verbatim.
STRIP_FIELDS = ("description", "verification", "outcome_note")
MARKER = "body_compacted"


def _completed_dt(goal: dict):
    """Best-effort completion datetime from a goal record, or None.

    Tries completed_at (ISO datetime), then completed_date (ISO date), then
    last_modified. Returns None when nothing parses — caller treats None as
    'cannot confirm age' and conservatively SKIPS (never strips an undateable
    goal)."""
    for field in ("completed_at", "completed_date", "last_modified"):
        raw = goal.get(field)
        if not raw:
            continue
        s = str(raw)
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:19] if "T" in s else s[:10], fmt)
            except ValueError:
                continue
    return None


def _eligible(goal: dict, cutoff: datetime) -> bool:
    """True iff this goal's body should be compacted."""
    if goal.get("status") != "completed":
        return False
    if goal.get("recurring"):
        return False
    if goal.get(MARKER):
        return False  # idempotent — already compacted
    if not any(f in goal for f in STRIP_FIELDS):
        return False  # nothing to strip
    dt = _completed_dt(goal)
    if dt is None:
        return False  # cannot confirm age → conservative skip
    return dt < cutoff


def _status_counts(items):
    """{(asp_id): {status: n}} + total goal count — the scoring-relevant census
    that MUST be invariant across compaction."""
    per = {}
    total = 0
    for asp in items:
        c = {}
        for g in asp.get("goals", []) or []:
            c[g.get("status", "?")] = c.get(g.get("status", "?"), 0) + 1
            total += 1
        per[asp.get("id", "?")] = c
    return per, total


def _plan(items, cutoff):
    """Return (per_asp_report, total_goals_to_compact, total_bytes_saved)."""
    report = []
    grand_n = 0
    grand_bytes = 0
    for asp in items:
        n = 0
        saved = 0
        for g in asp.get("goals", []) or []:
            if _eligible(g, cutoff):
                n += 1
                for f in STRIP_FIELDS:
                    if f in g:
                        saved += len(json.dumps(g[f], ensure_ascii=True))
        if n:
            report.append((asp.get("id", "?"), n, saved))
            grand_n += n
            grand_bytes += saved
    report.sort(key=lambda x: -x[2])
    return report, grand_n, grand_bytes


def _make_compactor(cutoff):
    """Build the locked_modify_jsonl modifier_fn (pure in-memory). Strips bodies
    and asserts the goal census is invariant before returning."""
    def _compact(items):
        before_per, before_total = _status_counts(items)
        for asp in items:
            for g in asp.get("goals", []) or []:
                if _eligible(g, cutoff):
                    for f in STRIP_FIELDS:
                        g.pop(f, None)
                    g[MARKER] = True
        after_per, after_total = _status_counts(items)
        # Hard invariant: compaction must never change goal counts (scoring
        # depends on them). If it did, abort the write loudly.
        if before_total != after_total or before_per != after_per:
            raise RuntimeError(
                "compaction changed the goal census — ABORTING write. "
                f"total {before_total}->{after_total}; this is a bug, the "
                "modifier must only strip fields, never add/remove goals.")
        return items
    return _compact


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("world", "agent"), default="world")
    ap.add_argument("--age-days", type=int, default=14,
                    help="only compact goals completed MORE than this many days "
                         "ago (default 14 — keeps recent completions full for "
                         "completion reports / reflection lookback)")
    ap.add_argument("--apply", action="store_true",
                    help="perform the write (default: dry-run, read-only)")
    args = ap.parse_args()

    base = WORLD_DIR if args.source == "world" else AGENT_DIR
    if base is None:
        print(f"[compact] {args.source} dir unresolved (WORLD_DIR/AGENT_DIR "
              "None — no MIND_WORLD/MIND_AGENT?)", file=sys.stderr)
        return 2
    path = Path(base) / "aspirations.jsonl"
    if not path.exists():
        print(f"[compact] {path} not found", file=sys.stderr)
        return 2

    cutoff = datetime.now().replace(microsecond=0)
    from datetime import timedelta
    cutoff = cutoff - timedelta(days=args.age_days)

    # Read current state for the plan. Under own-cloud, refresh from S3 first so
    # the dry-run reflects exactly what --apply will operate on.
    try:
        from storage_backend import get_backend
        get_backend().refresh(path)
    except Exception as e:
        print(f"[compact] (refresh skipped: {e})", file=sys.stderr)
    items = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip()]

    before_bytes = path.stat().st_size
    report, total_n, total_saved = _plan(items, cutoff)

    print(f"[compact] source={args.source} path={path}")
    print(f"[compact] current: {before_bytes:,} bytes, "
          f"{sum(len(a.get('goals',[]) or []) for a in items)} goals across "
          f"{len(items)} aspirations")
    print(f"[compact] cutoff: completed before {cutoff.isoformat()} "
          f"(--age-days {args.age_days})")
    print(f"[compact] eligible: {total_n} goals, ~{total_saved:,} bytes of "
          f"body text to strip ({STRIP_FIELDS})")
    for asp_id, n, saved in report[:12]:
        print(f"           {asp_id:12} {n:5} goals  ~{saved:,} bytes")

    if total_n == 0:
        print("[compact] nothing to do.")
        return 0

    if not args.apply:
        print(f"[compact] DRY-RUN — no write. Re-run with --apply to compact "
              f"(projected ~{before_bytes - total_saved:,} bytes after, full "
              f"bodies recoverable from .history).")
        return 0

    # APPLY: locked read-modify-write (DDB lock + If-Match + .history snapshot +
    # post-write canary). The modifier asserts the census is invariant.
    from _fileops import locked_modify_jsonl
    locked_modify_jsonl(path, _make_compactor(cutoff))

    # Post-write validation: re-read fresh, confirm shrink + census invariant.
    try:
        get_backend().refresh(path)
    except Exception as e:
        try:  # report, never raise — see note_swallowed_backend_error ()
            from storage_backend import note_swallowed_backend_error
            note_swallowed_backend_error("refresh", path, e)
        except Exception:
            pass
    after_items = [json.loads(ln) for ln in
                   path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    after_bytes = path.stat().st_size
    bp, bt = _status_counts(items)
    ap2, at = _status_counts(after_items)
    ok = (bt == at and bp == ap2)
    print(f"[compact] APPLIED: {before_bytes:,} -> {after_bytes:,} bytes "
          f"({100 * (before_bytes - after_bytes) // max(before_bytes,1)}% smaller)")
    print(f"[compact] census invariant: {'OK' if ok else 'FAILED'} "
          f"(total {bt}->{at})")
    if not ok:
        print("[compact] ERROR: census changed — restore from .history "
              "(history.py) and investigate.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
