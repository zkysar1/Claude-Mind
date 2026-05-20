"""User-leg-scope advisory — daemon-safe extraction (PR 7c/5).

Emits a WARN when a goal's participants include 'user' but user_leg_scope
is absent. Warn-not-block: legacy goals keep moving; the field is
backfillable via `aspirations-update-goal.sh <id> user_leg_scope <scope>`.

Single source of truth for the check — used by cmd_add_goal (single-goal
add), cmd_add (aspiration with embedded goals), and cmd_update_goal
(when participants is updated).

Public API:
    evaluate(*, goal_id, participants, user_leg_scope,
             valid_scopes=VALID_USER_LEG_SCOPES) -> dict

Return shape:
    {
      "warned": bool,
      "message": str | None,
      "participants_include_user": bool,
    }

Caller emits `message` to stderr / wherever appropriate. No telemetry —
the legacy inline helper had none.

Daemon safety: pure function, no I/O, no env reads.
"""
from __future__ import annotations

from typing import Iterable, Optional


# Canonical set — duplicated from aspirations.py for the standalone module.
# If the set changes, both copies must update; a parity test could enforce.
VALID_USER_LEG_SCOPES = frozenset({
    "commit", "push", "deployment-approval",
    "architecture-decision", "credential-grant",
    "data-provision", "new-resource",
})


def evaluate(*, goal_id: str,
             participants,
             user_leg_scope: Optional[str],
             valid_scopes: Iterable[str] = VALID_USER_LEG_SCOPES) -> dict:
    """Run the advisory. See module docstring.

    Args:
        goal_id: Goal identifier (for the warning text).
        participants: List from the goal record. Non-list inputs are
            treated as "no user participant" (no warning).
        user_leg_scope: The scope value, or None / "".
        valid_scopes: Iterable of scope strings shown in the warning to
            help the user pick a value.
    """
    user_present = isinstance(participants, list) and "user" in participants
    if not user_present:
        return {
            "warned": False,
            "message": None,
            "participants_include_user": False,
        }
    if user_leg_scope:
        return {
            "warned": False,
            "message": None,
            "participants_include_user": True,
        }
    return {
        "warned": True,
        "message": (
            f"[aspirations] WARN: goal {goal_id} has participants including 'user' but no "
            f"user_leg_scope set. Standing-grant matching will fall back to prose recognition. "
            f"Consider adding one of {sorted(valid_scopes)}."
        ),
        "participants_include_user": True,
    }
