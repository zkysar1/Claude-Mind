"""Tests for planned_verbs — the Planned-board member verbs (, PEARL B2b).

The centrepiece is :class:`TestPauseDoesNotLockTheMemberOut`, which is not a unit test of
this module at all: it drives the REAL projection predicate to prove that a paused goal is
still publishable and still resolvable. That property is the whole reason the verb table
writes advisory fields instead of ``status``, and it is only checkable against the
projection, so a test that mocked it would pass while the product broke.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from planned_verbs import (  # noqa: E402
    MEMBER_PAUSE_DEFER_PREFIX,
    MEMBER_WRITE_OPT_IN_FIELD,
    PLANNED_VERBS,
    PROJECTION_READ_FIELDS,
    plan_verb,
)


def _goal(**over):
    g = {
        "id": "g-369-30",
        "title": "Ship the planned board verbs",
        "status": "pending",
        "work_class": "product",
        MEMBER_WRITE_OPT_IN_FIELD: True,
    }
    g.update(over)
    return g


class TestOptInGate:
    def test_goal_without_opt_in_is_refused(self):
        g = _goal()
        del g[MEMBER_WRITE_OPT_IN_FIELD]
        assert plan_verb(g, "pause", "on").refusal == "not_addressable"

    def test_falsy_opt_in_is_refused(self):
        assert plan_verb(_goal(**{MEMBER_WRITE_OPT_IN_FIELD: False}), "pause", "on").refusal == (
            "not_addressable"
        )

    def test_unresolved_goal_refused_identically_to_not_opted_in(self):
        """A None goal and an un-opted-in goal MUST give the same reason: splitting them
        tells an unauthenticated caller which handles exist."""
        a = plan_verb(None, "pause", "on").refusal
        b = plan_verb(_goal(**{MEMBER_WRITE_OPT_IN_FIELD: False}), "pause", "on").refusal
        assert a == b == "not_addressable"

    def test_opt_in_gate_precedes_value_validation(self):
        """An un-opted-in goal must not leak validity information about the value."""
        g = _goal(**{MEMBER_WRITE_OPT_IN_FIELD: False})
        assert plan_verb(g, "prioritize", "NONSENSE").refusal == "not_addressable"


class TestVerbs:
    def test_unknown_verb(self):
        assert plan_verb(_goal(), "delete-everything", "on").refusal == "unknown_verb"

    @pytest.mark.parametrize("want", ["HIGH", "MEDIUM", "LOW", "high", "  low  "])
    def test_prioritize(self, want):
        plan = plan_verb(_goal(), "prioritize", want)
        assert plan.ok and plan.writes == {"priority": want.strip().upper()}

    def test_prioritize_rejects_unknown_priority(self):
        assert plan_verb(_goal(), "prioritize", "URGENT").refusal == "invalid_value"

    @pytest.mark.parametrize("verb", ["pause", "not-this"])
    def test_suppress_on_writes_structured_defer(self, verb):
        plan = plan_verb(_goal(), verb, "on")
        assert plan.ok
        assert plan.writes["defer_reason"].startswith(MEMBER_PAUSE_DEFER_PREFIX)
        assert plan.writes["member_directive"] == verb

    @pytest.mark.parametrize("verb", ["pause", "not-this"])
    def test_suppress_off_is_the_inverse(self, verb):
        """Reversibility is the point; clearing must null every field the on-branch set."""
        on = plan_verb(_goal(), verb, "on").writes
        off = plan_verb(_goal(), verb, "off").writes
        assert set(on) <= set(off), "off must clear everything on sets"
        assert all(off[k] is None for k in off)

    def test_suppress_rejects_non_toggle(self):
        assert plan_verb(_goal(), "pause", "maybe").refusal == "invalid_value"

    def test_comment_round_trips(self):
        plan = plan_verb(_goal(), "comment", "please do this after the demo")
        assert plan.writes == {"member_comment": "please do this after the demo"}

    def test_comment_rejects_empty(self):
        assert plan_verb(_goal(), "comment", "   ").refusal == "invalid_value"

    def test_comment_strips_control_characters(self):
        plan = plan_verb(_goal(), "comment", "ok\x00\x07 then\r")
        assert "\x00" not in plan.writes["member_comment"]
        assert "\x07" not in plan.writes["member_comment"]
        assert "\r" not in plan.writes["member_comment"]

    def test_comment_is_capped(self):
        plan = plan_verb(_goal(), "comment", "x" * 5000)
        assert len(plan.writes["member_comment"]) == 1000


class TestProjectionFieldsAreNeverWritten:
    """The load-bearing invariant, checked over every verb rather than by inspection."""

    @pytest.mark.parametrize("verb,value", [
        ("prioritize", "HIGH"), ("pause", "on"), ("pause", "off"),
        ("not-this", "on"), ("not-this", "off"), ("comment", "hello"),
    ])
    def test_no_verb_writes_a_projection_field(self, verb, value):
        plan = plan_verb(_goal(), verb, value)
        assert plan.ok
        assert not (PROJECTION_READ_FIELDS & set(plan.writes)), (
            f"{verb} writes a field the projection reads — this makes the goal "
            f"unaddressable and the verb irreversible"
        )

    def test_every_registered_verb_is_covered_by_the_table_above(self):
        """A verb added without a case above would silently escape the invariant test."""
        assert set(PLANNED_VERBS) == {"prioritize", "pause", "not-this", "comment"}

    def test_forbidden_field_is_refused_at_runtime(self, monkeypatch):
        """The table is what a future editor changes, so the guard must catch a BAD table,
        not merely agree with the good one."""
        import planned_verbs as pv
        monkeypatch.setitem(pv.PLANNED_VERBS, "rogue", lambda v, g: pv.VerbPlan({"status": "skipped"}))
        assert plan_verb(_goal(), "rogue", "x").refusal == "forbidden_field"


class TestPauseDoesNotLockTheMemberOut:
    """Drive the REAL projection: a paused goal must stay published AND stay resolvable.

    If this fails, a member who pauses a goal can never un-pause it, because the handle
    they hold stops resolving the moment the goal leaves the published set.
    """

    def _projection(self):
        import knowledge_projection as kp
        return kp

    def test_paused_goal_still_publishes_and_still_resolves(self):
        kp = self._projection()
        secret, env_id = "test-secret", "test-env"
        redactor = kp.Redactor([], [])

        goal = _goal()
        # Apply the pause exactly as the applier would.
        for field, value in plan_verb(goal, "pause", "on").writes.items():
            goal[field] = value

        rows = kp.project_goals([goal], redactor, handle_secret=secret, environment_id=env_id)
        assert len(rows) == 1, "a paused goal must still be PUBLISHED"
        handle = rows[0]["handle"]

        resolved = kp.resolve_goal_handle(handle, [goal], secret, redactor, env_id)
        assert resolved == "g-369-30", "a paused goal must still be ADDRESSABLE"

        # ...and the member can therefore reverse it.
        for field, value in plan_verb(goal, "pause", "off").writes.items():
            goal[field] = value
        assert goal["defer_reason"] is None

    def test_a_status_write_would_have_broken_it(self):
        """Positive control: proves the test above can FAIL, by doing the thing the verb
        table forbids. Without this, the assertion pair could be vacuously true."""
        kp = self._projection()
        redactor = kp.Redactor([], [])
        goal = _goal(status="skipped")
        rows = kp.project_goals([goal], redactor, handle_secret="s", environment_id="e")
        assert rows == [], "control failed: a status write should unpublish the goal"


class TestDeferPrefixIsRegistered:
    def test_prefix_is_in_the_structured_ssot(self):
        from gates.defer_classifier import STRUCTURED_DEFER_PREFIXES
        assert MEMBER_PAUSE_DEFER_PREFIX in STRUCTURED_DEFER_PREFIXES

    def test_member_pause_is_not_a_narrative_defer(self):
        """A narrative defer routes through capability-gate, which would refuse a member
        pause as agent-provisionable work. It must be structured."""
        from gates.defer_classifier import is_narrative_defer
        plan = plan_verb(_goal(), "pause", "on")
        assert not is_narrative_defer("defer_reason", plan.writes["defer_reason"])
