#!/usr/bin/env python3
"""Recurring-goal close tally: the arithmetic, in ONE place, READ-ONLY.

WHY THIS FILE EXISTS (g-115-6768). The five counters a recurring close must
advance -- substantive_runs, substantive_hits, last_substantive_at,
consecutive_routine, consecutive_deep -- had exactly one writer:
recurring-close.sh's post-phase heredoc (the tally block). That block runs
STRICTLY AFTER all four iteration-close phases, so it is unreachable from any
path that does not go through recurring-close.sh itself.

A worker Body's close does not. worker-loop Phase 4a calls
`iteration-close.sh --phase verify`, whose IS_RECURRING branch routes to
aspirations-complete-by.sh -- and the daemon's complete_by mutates 14 goal
fields, none of them a counter above. So achievedCount, currentStreak,
lastAchievedAt and the streaks all advance while the five counters freeze. The
close LOOKS complete; nothing errors.

DIRECTION OF THE BIAS IS FAIL-UNSAFE. cargo-cult-detector.py keys on
lifetime_hit_rate = substantive_hits / substantive_runs. A denominator that
misses worker closes INFLATES the rate -- same hits, fewer counted runs -- so a
low-value recurring goal executed mostly by workers looks MORE productive than
it is, which is the exact inverse of what the detector exists to catch.

MEASURED 2026-08-29 (alpha, cc-07): of 80 recurring goals that track
substantive_runs, 36 (45%) had their most recent close performed by a worker
Body -- each one definitively lost that close's increment. Read
`completed_by_role` as POSITIVE identification of worker closes only; absent
means reducer-OR-unknown by design (iteration-close.sh g-306-204), so 45% is a
floor, never a partition.

THIS MODULE DOES NOT WRITE. It reads the goal's current counters and returns
what the next values should be; the caller performs the writes through
whatever writer its surrounding code already uses. That split is deliberate:
iteration-close.sh writes goal fields via aspirations-update-goal.sh (daemon-
routed) while recurring-close.sh's heredoc shells aspirations.py directly, and
a shared module that picked one would have to impose it on the other. What
must not diverge is the ARITHMETIC, and that is what lives here -- pinned
against recurring-close.sh's literal source by
tests/test_recurring_tally.py::test_arithmetic_matches_recurring_close_source,
which fails if either side changes alone (guard-1475: a tell needs a test that
reddens when it is removed).
"""
from __future__ import annotations

import json
import os
import sys

# The five fields. guard-3232 defines exactly this set as the hand-repair list
# for an interrupted recurring close; this module mechanizes that same set for
# the worker path, so the two stay describable by one sentence.
TALLY_FIELDS = (
    "substantive_runs",
    "substantive_hits",
    "last_substantive_at",
    "consecutive_routine",
    "consecutive_deep",
)


def compute(current: dict, outcome: str, outcome_origin: str, now: str) -> dict:
    """Return {field: new_value} for the fields that CHANGE. Pure.

    Mirrors recurring-close.sh's tally block exactly:

      * substantive_runs  -- the DENOMINATOR; advances on EVERY close.
      * substantive_hits  -- the NUMERATOR; GENUINE deep only. A forced-flip is
        the anti-drift defense, not real substantive output, so it must not
        inflate the lifetime rate the chronic-low detector reads.
      * last_substantive_at -- stamped only when hits advanced (the "last catch").
      * consecutive_deep  -- +1 on genuine deep, UNCHANGED on a forced flip
        (so the auto-contract trigger reflects real signal), 0 on routine.
      * consecutive_routine -- +1 on routine, 0 on any deep.

    Fields whose value would not change are omitted, so a caller performing one
    write per returned key never issues a no-op write. last_substantive_at is
    therefore absent on a routine close -- which is correct, and is why a stale
    last_substantive_at on a routine-only goal is NOT evidence of this defect.
    """
    def _int(key: str) -> int:
        try:
            return int(current.get(key) or 0)
        except (TypeError, ValueError):
            # A corrupt counter must not abort a close. Treat as 0 and advance;
            # the alternative is refusing to count forever.
            return 0

    cur_runs = _int("substantive_runs")
    cur_hits = _int("substantive_hits")
    cur_routine = _int("consecutive_routine")
    cur_deep = _int("consecutive_deep")

    genuine_deep = (outcome == "deep" and outcome_origin == "genuine")

    out: dict = {"substantive_runs": cur_runs + 1}

    if genuine_deep:
        out["substantive_hits"] = cur_hits + 1
        out["last_substantive_at"] = now

    if outcome == "routine":
        new_routine, new_deep = cur_routine + 1, 0
    elif genuine_deep:
        new_routine, new_deep = 0, cur_deep + 1
    else:  # forced-flip deep: routine resets, deep is PINNED
        new_routine, new_deep = 0, cur_deep

    if new_routine != cur_routine:
        out["consecutive_routine"] = new_routine
    if new_deep != cur_deep:
        out["consecutive_deep"] = new_deep
    return out


def source_file(src_flag: str) -> str:
    """Resolve world|agent -> the aspirations store path, via _paths.

    Callers pass the --source FLAG, not a path. Every bash call site that hand-
    built this path duplicated the world/agent branch, and a duplicated store
    path is the shape that drifts when the layout moves (CLAUDE.md "Agent-dir
    Resolution"): resolve it in ONE place instead.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _paths  # noqa: E402 -- deliberate late import; _paths reads conf at import
    base = _paths.WORLD_DIR if src_flag == "world" else _paths.AGENT_DIR
    if not base:
        raise RuntimeError(f"no directory configured for --source {src_flag!r}")
    return os.path.join(str(base), "aspirations" + ".jsonl")


def read_goal(src_file: str, goal_id: str) -> dict | None:
    """Return the goal record, or None. Never raises on a malformed line."""
    try:
        with open(src_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    asp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for goal in asp.get("goals", []):
                    if goal.get("id") == goal_id:
                        return goal
    except OSError:
        return None
    return None


def main() -> int:
    """Emit `field<TAB>value` lines for the caller to write. rc=1 = emit nothing.

    Fail-open by contract: every failure path prints a reason to stderr and
    returns 1 with NO field lines, so a caller that writes whatever it receives
    performs zero writes and the close proceeds. The tally is detection
    value-add; it is never load-bearing for a close.
    """
    try:
        goal_id = os.environ["GID"]
        outcome = os.environ["OUTCOME"]
        now = os.environ["NOW"]
    except KeyError as exc:
        print(f"[recurring-tally] missing env {exc}", file=sys.stderr)
        return 1

    # SF is an explicit-path override for tests/fixtures; production passes
    # SRC_FLAG and lets this module resolve it, so no caller hand-builds a
    # store path.
    src_file = os.environ.get("SF") or ""
    if not src_file:
        try:
            src_file = source_file(os.environ.get("SRC_FLAG") or "world")
        except Exception as exc:  # noqa: BLE001 -- fail-open by contract
            print(f"[recurring-tally] source resolution failed: {exc}",
                  file=sys.stderr)
            return 1

    # A worker close has no forced-flip machinery -- Block A/C anti-drift lives
    # in recurring-close.sh alone -- so "genuine" is correct there by
    # construction, not a convenient default. recurring-close.sh, if it ever
    # calls this, passes its computed value explicitly.
    outcome_origin = os.environ.get("OUTCOME_ORIGIN") or "genuine"

    goal = read_goal(src_file, goal_id)
    if goal is None:
        print(f"[recurring-tally] {goal_id} not found in {src_file}", file=sys.stderr)
        return 1
    if not goal.get("recurring"):
        print(f"[recurring-tally] {goal_id} is not recurring — nothing to advance",
              file=sys.stderr)
        return 1

    for field, value in compute(goal, outcome, outcome_origin, now).items():
        print(f"{field}\t{value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
