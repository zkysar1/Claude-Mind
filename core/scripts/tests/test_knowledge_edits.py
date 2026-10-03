"""Tests for the member knowledge-edit decision core ()."""

from __future__ import annotations

import knowledge_edits as ke
from knowledge_edits import EDIT_FIELDS, EDIT_TEXT_CAP, plan_knowledge_edit

_NODE = {"key": "reefs", "category": "marine-biology/reefs", "body": "old"}
_HYP = {"id": "2026-09-30_reef-heat", "category": "marine-biology/reefs", "claim": "old"}
_GUARD = {"id": "guard-9001", "category": "marine-biology/method", "rule": "old"}


def test_edit_writes_only_the_member_visible_text_field_of_each_kind() -> None:
    for kind, record, field in (("node", _NODE, "body"), ("hypothesis", _HYP, "claim"),
                                ("guardrail", _GUARD, "rule")):
        plan = plan_knowledge_edit(kind, record, "edit", "  Corrected text.  ")
        assert plan.ok and not plan.remove, (kind, plan)
        assert plan.writes == {field: "Corrected text."}, (kind, plan.writes)


def test_forget_is_a_removal_with_no_field_writes() -> None:
    for kind, record in (("node", _NODE), ("hypothesis", _HYP), ("guardrail", _GUARD)):
        plan = plan_knowledge_edit(kind, record, "forget")
        assert plan.ok and plan.remove and plan.writes == {}, (kind, plan)


def test_undo_is_a_restore_with_no_field_writes() -> None:
    """The record an undo plans against is the RETAINED record: the item is no longer exposed."""
    for kind in EDIT_FIELDS:
        plan = plan_knowledge_edit(kind, {"item_id": "reefs"}, "undo")
        assert plan.ok and plan.restore and not plan.remove and plan.writes == {}, (kind, plan)
    assert not plan_knowledge_edit("node", _NODE, "forget").restore
    assert not plan_knowledge_edit("node", _NODE, "edit", "x").restore


def test_unresolved_record_is_refused_the_same_for_every_op_and_kind() -> None:
    """A miss never says which handles exist: one refusal, whatever was asked."""
    for kind in EDIT_FIELDS:
        for op in ("edit", "forget", "undo"):
            assert plan_knowledge_edit(kind, None, op, "x").refusal == "not_addressable"
            assert plan_knowledge_edit(kind, {}, op, "x").refusal == "not_addressable"


def test_unknown_op_and_unknown_kind_are_refused() -> None:
    assert plan_knowledge_edit("node", _NODE, "delete", "x").refusal == "unknown_op"
    assert plan_knowledge_edit("node", _NODE, "", "x").refusal == "unknown_op"
    assert plan_knowledge_edit("lesson", _NODE, "edit", "x").refusal == "unknown_kind"
    assert plan_knowledge_edit("", _NODE, "forget").refusal == "unknown_kind"
    # The op is matched case-insensitively, as it crosses a JSON boundary to get here.
    assert plan_knowledge_edit("node", _NODE, " EDIT ", "x").ok


def test_an_empty_edit_is_refused_rather_than_read_as_a_forget() -> None:
    for text in ("", "   ", "\x00\x07", None):
        assert plan_knowledge_edit("node", _NODE, "edit", text).refusal == "invalid_value"


def test_member_text_is_capped_and_stripped_of_control_characters() -> None:
    plan = plan_knowledge_edit("guardrail", _GUARD, "edit", "a\x00b\x1bc\r\nd\te")
    assert plan.writes == {"rule": "abc\nd\te"}
    long_plan = plan_knowledge_edit("node", _NODE, "edit", "x" * (EDIT_TEXT_CAP + 50))
    assert len(long_plan.writes["body"]) == EDIT_TEXT_CAP


def test_an_edit_that_would_write_an_exposure_field_is_refused(monkeypatch) -> None:
    """The runtime check holds even when the field table is edited to point at one.

    rb-10157: writing category, file or key could move the item out of the exposed set
    and strand the member's handle, so the check reads the plan's OUTPUT, not the table.
    """
    monkeypatch.setitem(ke.EDIT_FIELDS, "node", "category")
    assert plan_knowledge_edit("node", _NODE, "edit", "x").refusal == "forbidden_field"
    # Positive control: an untouched kind still plans normally under the same patch.
    assert plan_knowledge_edit("guardrail", _GUARD, "edit", "x").ok


def test_the_forget_marker_is_a_field_no_edit_may_write(monkeypatch) -> None:
    """The exposure cut reads ``forgotten_at``, and the table names it so the two cannot drift.

    An edit that wrote it would hide an item the member never forgot, or bring back one they
    did, so it is held to the same runtime check as every other exposure-read field.
    """
    from knowledge_projection import FORGOTTEN_FIELD

    assert FORGOTTEN_FIELD in ke.EXPOSURE_READ_FIELDS
    monkeypatch.setitem(ke.EDIT_FIELDS, "hypothesis", FORGOTTEN_FIELD)
    assert plan_knowledge_edit("hypothesis", _HYP, "edit", "x").refusal == "forbidden_field"
    # Positive control: an untouched kind still plans normally under the same patch.
    assert plan_knowledge_edit("guardrail", _GUARD, "edit", "x").ok
