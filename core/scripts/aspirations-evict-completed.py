#!/usr/bin/env python3
"""Evict AGED, TERMINAL, non-recurring goals out of the live aspirations queue —
the deep, growth-bounding fix for B9 (asp-115 add-goal latency + hot-lock
contention on world/aspirations.jsonl under own-cloud).

WHY EVICT, NOT JUST COMPACT (the deeper redesign)
  aspirations-compact-completed.py strips the bulky TEXT of aged-completed goals
  but KEEPS the record in the live list, so the live file still grows without
  bound (just ~9x slower). Eviction REMOVES the aged terminal record from the
  live list entirely, so the live file is bounded by {non-terminal goals +
  recently-terminal goals} — terminal work ages out instead of accumulating
  forever. That is the actual fix for unbounded growth, not a deferral of it.

  The classic objection to eviction is that goal-selector.py and every other
  completion metric derive their ratios from LIVE goal counts (done/total), so
  dropping done goals would crater completion_ratio / completion_pressure /
  tail_bonus / the zombie scan / progress. B9-deep removes that objection: a
  PER-STATUS census (`archived_census.by_status`) is cached on the aspiration and
  every completion consumer reads its counts through _goal_census.effective_counts,
  which folds the census back in honoring that consumer's exact denominator. The
  result is metric-NEUTRAL eviction — every derived value is byte-identical
  before and after. See _goal_census.py and tests/test_goal_eviction_invariance.py.

WHAT IS EVICTED
  A goal is eligible iff ALL hold:
    - status in TERMINAL_STATUSES (completed | skipped | expired | decomposed |
      superseded)
    - NOT recurring (recurring goals never terminate; they stay live forever)
    - its terminal date (completed_at | completed_date | last_modified) is MORE
      than --age-days ago (default 45 — well beyond completion-report lookback
      (~7d), reflection lookback, and trajectory's velocity window (last 5), so
      no record-consumer loses signal it actually uses)
  Recurring goals, live work (pending/blocked/in-progress), and recently-terminal
  goals are NEVER evicted. Undateable goals are conservatively SKIPPED (never
  evicted on a guess).

WHERE THE RECORDS GO
  The full records remain recoverable from the .history snapshot that
  locked_modify_jsonl takes automatically immediately before the write. We do NOT
  append individual goal records to aspirations-archive.jsonl: that file holds
  whole-ASPIRATION records (each line a nested {id, goals:[...]}), and the cadence
  checks count it by iterating each line's `goals`. Writing goal-shaped lines
  there would be invisible to that count and would require a non-atomic two-file
  write. Census + .history is simpler and crash-atomic (one locked write).

SAFETY
  - Routes through _fileops.locked_modify_jsonl -> DDB lock (cross-machine mutex)
    + force-fresh-from-S3 read + If-Match fenced PUT + .history snapshot +
    post-write JSONL canary. Same path the daemon uses; under own-cloud the
    scoped MIND_AWS_* creds are used (fail-closed in OwnCloudBackend.from_env).
  - HARD INVARIANT baked into the modifier: for every aspiration, all four
    completion denominators AND the cadence completed-count are recomputed before
    and after the move; ANY mismatch raises and the locked write is aborted (no
    write lands). This is the runtime twin of the unit invariance test.
  - Idempotent: a re-run finds the already-evicted goals gone from the live list
    and the census already reflecting them, so it evicts only newly-aged goals.
  - DRY-RUN by default. --apply performs the write.

USAGE
  Dry-run (read-only; reports projected eviction):
    set -a; source .env.local; set +a   # STORAGE_BACKEND + scoped creds
    MIND_AGENT=alpha py -3 core/scripts/aspirations-evict-completed.py --source world
  Apply (own-cloud backend — the --apply WRITE needs the governed-root map too):
    source core/scripts/_paths.sh                                     # sets WORLD_PATH/META_PATH (shell-local only)
    export MIND_WORLD="$WORLD_PATH"; export MIND_META="$META_PATH"  # guard-879: _paths.sh does NOT export these to subprocesses; OwnCloudBackend.from_env needs one of MIND_WORLD/WORLD_PATH or MIND_META/META_PATH in the SUBPROCESS env or the --apply write aborts BEFORE the lock ("neither ... is set — cannot map a governed path to a root"). Dry-run does not hit this (read-only path resolution uses the Python _paths import). Local backend needs none of this.
    set -a; source .env.local; set +a                                # STORAGE_BACKEND + scoped MIND_AWS_* creds
    MIND_AGENT=alpha py -3 core/scripts/aspirations-evict-completed.py --source world --apply
    ... then repeat with --source agent --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, AGENT_DIR  # noqa: E402
from _goal_census import (  # noqa: E402
    ABANDONED_STATUSES, TERMINAL_STATUSES, CENSUS_KEY, effective_counts,
    census_completed,
)

# The exact denominators the live consumers use — recomputed before/after each
# eviction to prove metric-neutrality. (exclude_statuses, include_recurring, label)
_INVARIANT_VARIANTS = (
    (ABANDONED_STATUSES, True, "scorer_active"),                 # goal-selector
    (frozenset(), False, "non_recurring"),                       # recompute/zombie
    (ABANDONED_STATUSES, False, "pulse"),                        # strategic-pulse
    (frozenset({"skipped", "expired", "decomposed"}), True, "precheck_tracked"),
)


def _terminal_dt(goal: dict):
    """Best-effort terminal datetime, or None (None -> conservative skip)."""
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
    """True iff this goal should be evicted (aged, terminal, non-recurring)."""
    if goal.get("recurring"):
        return False
    if goal.get("status") not in TERMINAL_STATUSES:
        return False
    dt = _terminal_dt(goal)
    if dt is None:
        return False  # cannot confirm age -> conservative skip
    return dt < cutoff


def _metric_fingerprint(asp: dict) -> dict:
    """All completion metrics this aspiration influences — must be invariant."""
    fp = {label: effective_counts(asp, exclude_statuses=excl, include_recurring=rec)
          for (excl, rec, label) in _INVARIANT_VARIANTS}
    fp["cadence_completed"] = census_completed(asp) + sum(
        1 for g in (asp.get("goals") or []) if g.get("status") == "completed")
    return fp


def _bump_census(asp: dict, status: str) -> None:
    census = asp.setdefault(CENSUS_KEY, {})
    if not isinstance(census, dict):
        census = {}
        asp[CENSUS_KEY] = census
    by_status = census.setdefault("by_status", {})
    if not isinstance(by_status, dict):
        by_status = {}
        census["by_status"] = by_status
    by_status[status] = int(by_status.get(status, 0) or 0) + 1


def _plan(items, cutoff):
    """Return (per_asp_report, total_goals, bytes_freed)."""
    report = []
    grand_n = 0
    grand_bytes = 0
    for asp in items:
        n = 0
        freed = 0
        for g in asp.get("goals", []) or []:
            if _eligible(g, cutoff):
                n += 1
                freed += len(json.dumps(g, ensure_ascii=True))
        if n:
            report.append((asp.get("id", "?"), n, freed))
            grand_n += n
            grand_bytes += freed
    report.sort(key=lambda x: -x[1])
    return report, grand_n, grand_bytes


def _make_evictor(cutoff):
    """Build the locked_modify_jsonl modifier_fn (pure in-memory). Moves eligible
    goals out of `goals` into the per-status census and asserts every completion
    metric is invariant before returning; mismatch raises and aborts the write."""
    def _evict(items):
        before = {asp.get("id", f"#{i}"): _metric_fingerprint(asp)
                  for i, asp in enumerate(items)}
        for asp in items:
            goals = asp.get("goals", []) or []
            kept = []
            for g in goals:
                if _eligible(g, cutoff):
                    _bump_census(asp, g.get("status"))
                else:
                    kept.append(g)
            asp["goals"] = kept
        after = {asp.get("id", f"#{i}"): _metric_fingerprint(asp)
                 for i, asp in enumerate(items)}
        if before != after:
            # Find the first differing aspiration for a precise diagnostic.
            diffs = [k for k in before if before.get(k) != after.get(k)]
            raise RuntimeError(
                "eviction changed a completion metric — ABORTING write. "
                f"differing aspirations: {diffs[:5]} (of {len(diffs)}). "
                "This is a census-bump bug: effective_counts must read back the "
                "evicted goals from archived_census byte-identically.")
        return items
    return _evict


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("world", "agent"), default="world")
    ap.add_argument("--age-days", type=int, default=45,
                    help="evict goals terminal MORE than this many days ago "
                         "(default 45 — beyond every record-consumer's window)")
    ap.add_argument("--apply", action="store_true",
                    help="perform the write (default: dry-run, read-only)")
    args = ap.parse_args()

    base = WORLD_DIR if args.source == "world" else AGENT_DIR
    if base is None:
        print(f"[evict] {args.source} dir unresolved (WORLD_DIR/AGENT_DIR None "
              "— no MIND_WORLD/MIND_AGENT?)", file=sys.stderr)
        return 2
    path = Path(base) / "aspirations.jsonl"
    if not path.exists():
        print(f"[evict] {path} not found", file=sys.stderr)
        return 2

    cutoff = datetime.now().replace(microsecond=0) - timedelta(days=args.age_days)

    # Read current state for the plan. Under own-cloud, refresh from S3 first so
    # the dry-run reflects exactly what --apply will operate on.
    try:
        from storage_backend import get_backend
        get_backend().refresh(path)
    except Exception as e:
        print(f"[evict] (refresh skipped: {e})", file=sys.stderr)
    items = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip()]

    before_bytes = path.stat().st_size
    total_goals_now = sum(len(a.get("goals", []) or []) for a in items)
    report, total_n, freed = _plan(items, cutoff)

    print(f"[evict] source={args.source} path={path}")
    print(f"[evict] current: {before_bytes:,} bytes, {total_goals_now} goals "
          f"across {len(items)} aspirations")
    print(f"[evict] cutoff: terminal before {cutoff.isoformat()} "
          f"(--age-days {args.age_days})")
    print(f"[evict] eligible: {total_n} goals to evict, ~{freed:,} bytes freed")
    for asp_id, n, fb in report[:12]:
        print(f"         {asp_id:12} {n:5} goals  ~{fb:,} bytes")

    if total_n == 0:
        print("[evict] nothing to do.")
        return 0

    if not args.apply:
        print(f"[evict] DRY-RUN — no write. Re-run with --apply to evict "
              f"(projected ~{before_bytes - freed:,} bytes after; full records "
              f"recoverable from .history; metrics held invariant by census).")
        return 0

    # APPLY: locked read-modify-write (DDB lock + If-Match + .history + canary).
    # The modifier asserts every completion metric is invariant or aborts.
    from _fileops import locked_modify_jsonl
    locked_modify_jsonl(path, _make_evictor(cutoff))

    # Post-write validation: re-read fresh, confirm shrink + goal-count drop.
    try:
        get_backend().refresh(path)
    except Exception:
        pass
    after_items = [json.loads(ln) for ln in
                   path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    after_bytes = path.stat().st_size
    after_goals = sum(len(a.get("goals", []) or []) for a in after_items)
    evicted = total_goals_now - after_goals
    print(f"[evict] APPLIED: {before_bytes:,} -> {after_bytes:,} bytes "
          f"({100 * (before_bytes - after_bytes) // max(before_bytes, 1)}% smaller); "
          f"{evicted} goals evicted ({total_goals_now} -> {after_goals})")
    if evicted != total_n:
        print(f"[evict] WARN: evicted {evicted} but planned {total_n} — "
              "concurrent write between plan and apply? Re-run dry-run to confirm "
              "state.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
