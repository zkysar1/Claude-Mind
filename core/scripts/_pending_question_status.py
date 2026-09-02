"""Single source of truth for the PENDING-QUESTION status vocabulary.

g-115-3746, the designated survivor of a 9-goal / 6-agent duplicate cluster.

THE TWO SETS BELOW ARE DELIBERATELY DIFFERENT, AND THAT IS THE WHOLE POINT.
The original defect was NOT that two scripts disagreed -- it is that they
disagreed BY ACCIDENT, with nothing naming the difference or pinning it, so
every reader who found it reasonably concluded one side was a bug. Six agents
did, over eleven days. This module makes the difference explicit and testable
instead of removing it.

    CLOSED_STATUSES     -- "this question is finished; stop asking."
    SWEEP_SETTLED       -- "this question needs no further TRANSITION."

The gap between them is exactly TRANSITION_PENDING = {answered, agent_answered}:
states that are finished from the asker's point of view but still owe a
canonicalisation pass.

WHY THAT IS CORRECT, AND WHY THE OBVIOUS "FIX" IS A REGRESSION. The obvious
reading -- close.py:121 writes `answered`, so `answered` must be terminal
everywhere -- was this goal's own recommendation in 2026-07. It has since been
OVERTAKEN by sibling goals g-115-3753 / g-115-5025, which built the executor the
cluster asked for: `pending-questions-sweep.py` classifies an answered entry as
`needs_transition` and `--apply-cleanup` discharges it to `resolved`. So the
transition the goal called "a state transition nothing currently consumes" now
HAS a consumer. Adding `answered` to the sweep's settled set makes
`_h_answered_not_cleaned` unreachable (heuristic 1 fires first) and makes the
writer skip those entries -- silently deleting a working, tested mechanism.
Measured 2026-08-31 (alpha, cc-08): doing exactly that turned
test_pending_questions_sweep.py red in three places, which is the suite working.

`retired` WAS THE REAL SHARED GAP (g-115-4276, confirmed 2026-08-03 on
foxtrot's pq-fox-wsl-relay-restart, which carries status=retired WITH a
resolved_at). It was in NEITHER set: not closed, so the closer would re-close
it; not settled, so the sweep kept it eligible for staleness flagging forever.
It belongs in BOTH, and it is not part of the transition backlog -- a retired
question is not awaiting canonicalisation, it is abandoned.

THIS IS *NOT* THE GOAL-STATUS VOCABULARY, AND THE TWO MUST NOT BE MERGED.
Goal statuses are {completed, archived, skipped, expired, ...}. A pending
question is never "completed" and a goal is never "answered". guard-1127 is
explicit: a constant serving two subsystems is DECOUPLED at the consumer, never
widened into one shared value. A reader handling both kinds of referent must
select by REFERENT KIND -- see blocked-signal-resolution-check.py, whose pq
branch used the GOAL set until this goal, so an `answered` pending question read
as unresolved forever and a blocked signal citing one could never discharge.

TOTAL FUNCTIONS ON PURPOSE (rb-1915): the predicates normalise case and
whitespace and accept None, so a caller never gets a spurious False from a
stray " Resolved".
"""

from __future__ import annotations

# States that still owe a canonicalisation pass: finished for the asker, not yet
# settled for the sweep. The sweep classifies these `needs_transition` and
# `--apply-cleanup` discharges them to `resolved`.
TRANSITION_PENDING = frozenset({
    "answered",        # what pending-questions-close.sh WRITES (close.py:121)
    "agent_answered",  # closer variant: answered by the agent, not the user
})

# States needing no further transition. This is the sweep's set: an entry here
# is skipped by the staleness heuristics and refused by the auto-resolve writer.
SWEEP_SETTLED = frozenset({
    "resolved",    # the canonical settled state
    "superseded",
    "closed",
    "done",
    "retired",     # the  gap -- was in neither set
})

# "This question is finished; stop asking." The closer's set: it refuses to
# re-close anything here. Strict superset of SWEEP_SETTLED.
CLOSED_STATUSES = SWEEP_SETTLED | TRANSITION_PENDING

# "This question was actually ANSWERED, so a blocker citing it is discharged."
# CLOSED_STATUSES minus `retired`, and the exclusion is load-bearing rather than
# fastidious: `retired` means the question was WITHDRAWN, which KILLS a defer's
# clearing path instead of satisfying it. Treating it as a discharge silently
# unblocks a goal whose blocking question was never answered.
#
# NOT invented here -- this repo already carries the distinction, measured and
# documented, in human-blocked-defer-join.py:83-87, whose ANSWERED_STATUSES and
# RETIRED_STATUSES are deliberately separate because "both are deterministic and
# both are actionable, but they call for opposite actions". That file is a FIFTH
# definition of this vocabulary and is CORRECTLY divergent; do not fold it in.
DISCHARGES_A_BLOCKER = CLOSED_STATUSES - {"retired"}

# What the canonical closer emits, and what the sweep canonicalises toward.
CLOSER_WRITES = "answered"
CANONICAL_SETTLED = "resolved"


def normalize(status) -> str:
    """Lower-case and strip a status; None becomes the empty string."""
    if status is None:
        return ""
    return str(status).strip().lower()


def is_closed(status) -> bool:
    """True iff the question is finished — the CLOSER's notion of terminal.

    Use this to decide "is this question still open?" — for referent
    resolution, blocked-signal discharge, and anything asking whether someone
    is still owed an answer. An absent status is NOT closed, which is the
    fail-safe direction: an unknown entry stays visible.
    """
    return normalize(status) in CLOSED_STATUSES


def discharges_a_blocker(status) -> bool:
    """True iff a blocker citing this question is discharged.

    is_closed minus `retired`. Use this for blocked-signal / defer resolution —
    NOT is_closed, which would count a withdrawn question as an answer.
    """
    return normalize(status) in DISCHARGES_A_BLOCKER


def is_settled(status) -> bool:
    """True iff the question needs no further TRANSITION — the SWEEP's notion.

    Narrower than is_closed by exactly TRANSITION_PENDING. Use this only inside
    the sweep's transition pipeline; using it for "is this question open?" is
    the bug that made a blocked signal citing an answered question undischargeable.
    """
    return normalize(status) in SWEEP_SETTLED
