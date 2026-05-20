"""Shared default shape for the `loop_state` working-memory slot.

Source of truth for the initial structure of `loop_state` when callers
discover it as `None`/non-dict and must self-initialize before applying
mutations. Replaces ad-hoc dict literals scattered across writer scripts
(g-115-622 — zeta investigation g-115-621 traced 87 silent skips in a
session where `loop_state=null` caused both `recurring-loop-state-mutate.py`
and `loop-state-bump-counters.py` to WARN-and-exit instead of mutating).

Shape mirrors the orchestrator's first-iteration init block in
`/aspirations` SKILL.md Phase -0.5 (the `ELSE` branch when
`wm.loop_state` is null on entry):

    goals_completed_this_session = 0
    productive_goals_this_session = 0
    evolutions_this_session = 0
    last_evolution_goal_count = 0
    goals_since_last_alignment_check = 0
    aspirations_touched_this_session = set()
    session_signals = { ... }

Single-writer invariant: when adding a new key to loop_state, add it HERE
first so all self-init paths converge on the same shape. The g-283-08
loop-state-merge-gate refuses type-transitions on this slot, so a wrong
shape locked in early will reject later writes.

Returned object is freshly-deep-copied per call (defaults() helper) so
mutations by the caller cannot leak into the module-level constant. The
DEFAULT_LOOP_STATE constant is exposed for read-only inspection (tests,
schema docs) but writers should call defaults().
"""

from __future__ import annotations

import copy

# The canonical default shape. Do NOT mutate this module-level dict from any
# caller — use defaults() to obtain a writable copy.
DEFAULT_LOOP_STATE = {
    "goals_completed": 0,
    "productive_goals": 0,
    "evolutions": 0,
    "last_evolution_at": 0,
    "alignment_check_at": 0,
    "touched": [],
    "signals": {
        "routine_streak_global": 0,
        "productive_streak": 0,
        "routine_count_total": 0,
        "goals_since_last_tree_update": 0,
        "consecutive_goal_failures": 0,
        "last_failed_goal_id": None,
        "consecutive_blocked_sleeps": 0,
        "productivity_cooldown_streak": 0,
    },
    "routine_streaks": {},
    "goals_completed_this_session": 0,
    "productive_goals_this_session": 0,
}


def defaults() -> dict:
    """Return a writable deep-copy of DEFAULT_LOOP_STATE.

    Use this when self-initializing a missing `loop_state` slot so the
    caller's mutations don't leak into the module-level constant or
    cross-contaminate other callers."""
    return copy.deepcopy(DEFAULT_LOOP_STATE)
