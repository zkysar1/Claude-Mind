"""The four Planned-board member verbs, as a pure decision core (, PEARL B2b).

The member-facing Planned board (``knowledge_projection.project_goals``) is read-only
by construction: it publishes three fields plus an opaque ``handle``. This module is the
WRITE half — it turns an addressed verb into the exact set of goal-field writes that
should land, or into a refusal. It performs no I/O: resolution lives in
``knowledge_projection.resolve_goal_handle`` and the write lands through the canonical
``aspirations`` update path, so this file stays unit-testable and side-effect free.

WHY NO VERB MAY WRITE ``status`` OR ``work_class`` — the load-bearing invariant
================================================================================
``knowledge_projection._exposed_goal`` is the SINGLE predicate deciding both what a
member can SEE and what a member can ADDRESS; ``project_goals`` and
``resolve_goal_handle`` deliberately route through it so the two sets are identical by
construction. That tie is correct for security and, left alone, catastrophic for a write
path: a verb that moves a goal out of the published set makes the goal stop being
published AND stop resolving, so the member who paused a goal can never address it again
to un-pause it. The handle they hold is now permanently inert.

``_exposed_goal`` reads exactly three inputs: ``work_class``, ``status`` and ``title``.
So the invariant that buys reversibility is narrow and checkable: **no verb writes any
field ``_exposed_goal`` reads.** Every verb below writes advisory fields the LOOP honors,
never the fields the PROJECTION reads. The goal stays published, stays addressable, and
every verb can be undone by its own inverse. :func:`plan_verb` enforces this at runtime
rather than trusting the table, because the table is the thing a future editor changes.

This is the deliberate verb-layer choice g-369-119's outcome_note said was the only way
to have both properties; it is recorded here rather than in prose because the next editor
will meet the table before they meet the note.

PAUSE RIDES THE EXISTING DEFER MECHANISM, NOT A NEW SUPPRESSION PATH
====================================================================
This goal's own description says ``pause (->defer/pause)`` and it is right: the selector
already suppresses deferred goals, so a parallel "member_paused" filter would be a second
policy on one decision. ``pause`` and ``not-this`` therefore write ``defer_reason`` under
the structured prefix ``member_paused:``, registered in
``gates.defer_classifier.STRUCTURED_DEFER_PREFIXES``.

That prefix is deliberately given the NEVER-AUTO-CLEARS property of ``human_blocked:``
rather than the 120h fail-open of ``precondition_unmet:``. The fail-open window exists to
re-probe defers whose premise is a WORLD condition ("is the service up yet?"); a member's
pause is not a world condition and no probe can answer it. A pause that silently expired
after five days would resume work the member explicitly stopped, which is the failure the
member would least expect and least easily notice.

MEMBER TEXT IS UNTRUSTED INPUT
==============================
``comment`` carries free text authored by a member and lands in a store the agent reads.
It is DATA, never instructions: it is length-capped, stripped of control characters, and
must never be interpreted as a directive by any consumer. The cap is enforced here so a
single verb cannot inflate a goal record without bound.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "PLANNED_VERBS",
    "MEMBER_WRITE_OPT_IN_FIELD",
    "MEMBER_PAUSE_DEFER_PREFIX",
    "PROJECTION_READ_FIELDS",
    "VerbPlan",
    "plan_verb",
]

# The positive opt-in gate. A goal is member-writable only when this field is truthy on
# its own record. Recorded decision ( outcome_note, carried forward): the gate
# belongs on the VERB, not on the projection — "read stays wide, write narrows" — and the
# curation is a POSITIVE opt-in rather than a second suppression heuristic.
#
# WHY A GATE AT ALL, measured twice by two independent methods: the published board is
# roughly a third to a half the agent's own maintenance backlog mis-tagged as product
# work (129/377 = 34% by category, zeta cc-02 2026-08-30; 178/344 = 51.7% by title
# prefix, alpha cc-04 2026-09-04). Read-only that is cosmetic. With write verbs attached
# a member can pause the fleet's own upkeep believing it is roadmap.
#
# The field name is GENERIC on purpose. A hardcoded lane or aspiration id here would be a
# domain leak in a core framework file (.claude/rules/domain-free-examples.md) and would
# rot at the first re-org; the DOMAIN decides what sets this field.
MEMBER_WRITE_OPT_IN_FIELD = "member_writable"

# Structured defer prefix for member-initiated suppression. Registered in
# gates.defer_classifier.STRUCTURED_DEFER_PREFIXES and exempted from the selector's 120h
# fall-through alongside human_blocked: — see the module docstring for why it must never
# auto-clear.
MEMBER_PAUSE_DEFER_PREFIX = "member_paused:"

# The exact fields knowledge_projection._exposed_goal reads. No verb may write any of
# them; see the module docstring. Kept as an explicit constant so the runtime assertion in
# plan_verb() has something to check against and so a future editor who widens
# _exposed_goal has one obvious place to mirror the change.
PROJECTION_READ_FIELDS = frozenset({"work_class", "status", "title"})

_PRIORITIES = ("HIGH", "MEDIUM", "LOW")
_COMMENT_CAP = 1000
_ON = frozenset({"on", "true", "1", "yes"})
_OFF = frozenset({"off", "false", "0", "no", ""})


class VerbPlan:
    """The outcome of planning one verb: either ``writes`` or a ``refusal``.

    Exactly one of the two is meaningful. ``refusal`` is a short machine-stable reason
    string; it is deliberately NOT distinguished per-cause at the caller's boundary for
    unknown-vs-not-yours (see :func:`plan_verb`).
    """

    __slots__ = ("writes", "refusal")

    def __init__(
        self, writes: dict[str, Any] | None = None, refusal: str | None = None
    ) -> None:
        self.writes: dict[str, Any] = writes or {}
        self.refusal = refusal

    @property
    def ok(self) -> bool:
        return self.refusal is None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"VerbPlan(writes={self.writes!r}, refusal={self.refusal!r})"


def _sanitize_comment(value: str) -> str:
    """Cap length and strip control characters. Member text is data, never instructions."""
    text = "".join(ch for ch in str(value or "") if ch >= " " or ch == "\n")
    text = text.replace("\r", "").strip()
    return text[:_COMMENT_CAP]


def _as_toggle(value: Any) -> bool | None:
    """Parse an on/off value. ``None`` when the token is not a recognised toggle."""
    tok = str(value if value is not None else "").strip().lower()
    if tok in _ON:
        return True
    if tok in _OFF:
        return False
    return None


def _plan_prioritize(value: Any, _goal: Mapping[str, Any]) -> VerbPlan:
    want = str(value or "").strip().upper()
    if want not in _PRIORITIES:
        return VerbPlan(refusal="invalid_value")
    return VerbPlan({"priority": want})


def _plan_suppress(value: Any, _goal: Mapping[str, Any], *, verb: str) -> VerbPlan:
    """``pause`` and ``not-this`` share one suppression mechanism, one prefix, one
    selector exemption — the two differ in recorded INTENT, which the agent reads from
    ``member_directive``, not in how the loop suppresses them. Two prefixes would mean two
    exemptions to keep in sync for no behavioural difference."""
    on = _as_toggle(value)
    if on is None:
        return VerbPlan(refusal="invalid_value")
    if not on:
        # The inverse. Clearing to None is what makes every verb reversible; see the
        # module docstring on why reversibility is not optional here.
        return VerbPlan(
            {"defer_reason": None, "defer_reason_set_at": None, "member_directive": None}
        )
    return VerbPlan(
        {
            "defer_reason": f"{MEMBER_PAUSE_DEFER_PREFIX} {verb} via the Planned board",
            "member_directive": verb,
        }
    )


def _plan_comment(value: Any, _goal: Mapping[str, Any]) -> VerbPlan:
    text = _sanitize_comment(value)
    if not text:
        return VerbPlan(refusal="invalid_value")
    return VerbPlan({"member_comment": text})


PLANNED_VERBS: dict[str, Any] = {
    "prioritize": _plan_prioritize,
    "pause": lambda v, g: _plan_suppress(v, g, verb="pause"),
    "not-this": lambda v, g: _plan_suppress(v, g, verb="not-this"),
    "comment": _plan_comment,
}


def plan_verb(goal: Mapping[str, Any] | None, verb: str, value: Any) -> VerbPlan:
    """Plan the field writes for one addressed verb, or refuse.

    ``goal`` is the RAW store record (already resolved from a handle by the caller).
    ``None`` means the handle resolved to nothing and is refused identically to an
    un-opted-in goal: telling an unauthenticated caller apart unknown-from-not-yours
    leaks which handles exist, which is the same reason
    ``knowledge-export --resolve-handle`` prints nothing on a miss.

    Refusal reasons are machine-stable: ``unknown_verb``, ``not_addressable``,
    ``invalid_value``, ``forbidden_field``.
    """
    handler = PLANNED_VERBS.get(str(verb or "").strip().lower())
    if handler is None:
        return VerbPlan(refusal="unknown_verb")
    if not goal or not goal.get(MEMBER_WRITE_OPT_IN_FIELD):
        return VerbPlan(refusal="not_addressable")

    plan = handler(value, goal)
    if not plan.ok:
        return plan

    # Runtime enforcement of the module's load-bearing invariant. The table above is what
    # a future editor changes, so checking the TABLE'S OUTPUT is what actually holds the
    # line: a verb that starts writing `status` would silently make its own goal
    # unaddressable and its own effect irreversible, and nothing else in the system would
    # report it — the goal would simply vanish from the board.
    forbidden = PROJECTION_READ_FIELDS & set(plan.writes)
    if forbidden:
        return VerbPlan(refusal="forbidden_field")
    return plan
