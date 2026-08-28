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
    is_decision_like(*, user_leg_scope=None, title="") -> bool

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
    # An act the principal must perform AS A LEGAL OR CREDENTIALED PERSON:
    # signature, certification, attestation, or a filing made under their
    # identity. Added 2026-08-28 (zeta,  / ) because two
    # genuinely-human goals were unclassifiable: an unsigned counterparty
    # agreement and an OASIS+ SDVOSB submission asserting the principal's
    # certification. `data-provision` was the nearest fit and is FALSE for
    # both — the user is not supplying data to the agent, they are being the
    # legal person. grant-010 retains exactly this class.
    "principal-identity",
})

# Scopes where the user's JUDGMENT is the deliverable (approval, architectural
# choice, credential grant). Goals with these scopes MUST surface individually
# in /open-questions "Decisions Needed" — never compressed or collapsed
# (2026-08-04 incident: , architecture-decision, was folded into a
# collapsed reviewer bucket and the user found out later).
# SYNC OBLIGATION: any value added to VALID_USER_LEG_SCOPES above needs a
# classification decision here — decision-like or not. Keep the two sets in
# the same file precisely so that decision cannot be skipped silently.
DECISION_LIKE_SCOPES = frozenset({
    "architecture-decision",
    "deployment-approval",
    "credential-grant",
    # SYNC OBLIGATION discharged for "principal-identity": decision-like YES.
    # The principal's own person/judgment IS the deliverable, so it must
    # surface individually in /open-questions. Note it does NOT match
    # _DECISION_SCOPE_SUBSTRINGS ("decision"/"approval"/"grant"), so this
    # explicit membership is load-bearing, not belt-and-braces.
    "principal-identity",
})

# Title prefixes that imply a decision even when user_leg_scope is unset —
# fallback classification for the unscoped majority (21 of 39 open
# user-participant goals measured 2026-08-04).
DECISION_TITLE_PREFIXES = (
    "Decide:",
    "Decide ",
    "USER DIRECTIVE:",
    "USER:",
    "PARKED tracker:",
)

# Free-text substrings that mark a NON-vocabulary scope as decision-like.
# Historical records carry free-text scopes ("values-tradeoff-decision",
# "restart-timing-approval-for-live-agent", "resource-allocation-decision-
# on-user-hardware") that predate vocabulary validation. Over-surfacing is
# the safe direction: a false positive costs the user one glance; a false
# negative hides a decision they owe.
_DECISION_SCOPE_SUBSTRINGS = ("decision", "approval", "grant")


def is_decision_like(*, user_leg_scope=None, title: str = "") -> bool:
    """Whether a goal belongs in /open-questions "Decisions Needed" (Bucket A).

    True when the scope is a decision-like vocabulary value, when a
    free-text scope reads as a decision, or when the title's framing
    implies one. Pure function, no I/O.
    """
    if user_leg_scope:
        if user_leg_scope in DECISION_LIKE_SCOPES:
            return True
        low = str(user_leg_scope).lower()
        if any(s in low for s in _DECISION_SCOPE_SUBSTRINGS):
            return True
    return any(title.startswith(p) for p in DECISION_TITLE_PREFIXES)


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
    # The decision-like hint must respect the caller's valid_scopes override —
    # test_custom_valid_scopes pins that default scopes never leak into a
    # customized warning. Intersect; omit the hint when nothing survives.
    decision_hint = sorted(DECISION_LIKE_SCOPES & set(valid_scopes))
    hint = f" (decision-like: {decision_hint})" if decision_hint else ""
    return {
        "warned": True,
        "message": (
            f"[aspirations] WARN: goal {goal_id} has participants including 'user' but no "
            f"user_leg_scope set. Standing-grant matching will fall back to prose recognition, "
            f"and /open-questions will render this goal in the collapsed 'Reviewer / Improvement "
            f"Work' bucket — if the user must DECIDE something here, it will not be surfaced "
            f"individually. Set user_leg_scope to one of {sorted(valid_scopes)}{hint}."
        ),
        "participants_include_user": True,
    }
