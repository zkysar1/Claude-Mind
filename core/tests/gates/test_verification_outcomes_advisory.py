"""Tests for gates.verification_outcomes ().

Sibling of test_description_length_advisory.py / test_user_leg_scope_advisory.py.

The advisory's whole value is that it is NOISELESS on goals that are fine and
LOUD on goals that cannot be verified, so the negative controls below are not
padding — a gate that warns on everything is worth exactly as little as one that
warns on nothing.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from gates.verification_outcomes import evaluate  # noqa: E402


# --- warns: nothing machine-checkable to verify a close against ---------------

@pytest.mark.parametrize("goal,label", [
    ({"id": "g-1"}, "verification key absent entirely"),
    ({"id": "g-2", "verification": None}, "verification is None"),
    ({"id": "g-3", "verification": {}}, "verification is an empty dict"),
    ({"id": "g-4", "verification": {"outcomes": []}}, "outcomes is an empty list"),
    ({"id": "g-5", "verification": {"outcomes": ["", "   "]}}, "outcomes are blank strings"),
    ({"id": "g-6", "verification": {"checks": ["c"]}}, "checks present but no outcomes"),
    ({"id": "g-7", "verification": {"outcomes": "not-a-list"}}, "outcomes is not a list"),
])
def test_warns_when_no_usable_outcomes(goal, label):
    result = evaluate(goal)
    assert result["warned"] is True, label
    assert result["reason"] == "outcomes-absent"
    assert result["message"]


def test_message_names_the_goal_id():
    """Outcome 1 of : the warning must name the goal id.

    The daemon backfills the real id at response assembly (Phase-A advisories
    run before _allocate_goal_id), so the gate renders whatever it was given.
    """
    assert "g-115-4209" in evaluate({"id": "g-115-4209"})["message"]


def test_unassigned_id_renders_the_backfill_token():
    """An id-less goal must render the exact literal the daemon substitutes.

    aspirations_write.py replaces "<unassigned>" with the allocated id. If this
    token is ever reworded here, that backfill silently stops working and the
    warning goes back to being unactionable — which is the defect it fixed.
    """
    assert "<unassigned>" in evaluate({})["message"]


# --- silent: outcomes present (the negative control) --------------------------

@pytest.mark.parametrize("goal,label", [
    ({"id": "g-8", "verification": {"outcomes": ["x"]}}, "one outcome"),
    ({"id": "g-9", "verification": {"outcomes": ["a", "b"]}}, "several outcomes"),
    ({"id": "g-10", "verification": {"outcomes": ["", "real"]}}, "one blank, one real"),
])
def test_silent_when_outcomes_present(goal, label):
    result = evaluate(goal)
    assert result["warned"] is False, label
    assert result["reason"] == "outcomes-present"
    assert result["message"] is None


# --- silent: owned by gates.prose_verification --------------------------------

def test_prose_advertised_goal_is_left_to_the_prose_gate():
    """A description advertising criteria is prose_verification's to judge.

    Warning here as well would double-report one defect and put an advisory in
    front of that gate's hard block.
    """
    result = evaluate({"id": "g-11", "description": "Acceptance criteria:\n1. it works"})
    assert result["warned"] is False
    assert result["reason"] == "prose-advertised-owned-by-prose-verification"


def test_marker_inside_a_code_fence_does_not_suppress():
    """guard-1668: a description that QUOTES a marker has not advertised criteria.

    Reuses prose_verification's own code-stripping, so a goal documenting this
    gate does not silently exempt itself from it.
    """
    result = evaluate({"id": "g-12", "description": "```\nAcceptance criteria:\n```"})
    assert result["warned"] is True


def test_prose_suppression_loses_to_real_outcomes():
    """Outcomes present + prose marker -> 'outcomes-present', not the prose branch.

    Order matters: a goal that has BOTH is simply fine, and reporting it as
    'owned by the other gate' would misattribute why it was silent.
    """
    result = evaluate({
        "id": "g-13",
        "description": "Acceptance criteria:\n1. x",
        "verification": {"outcomes": ["real"]},
    })
    assert result["warned"] is False
    assert result["reason"] == "outcomes-present"


# --- shape contract -----------------------------------------------------------

def test_return_shape_is_stable():
    """Callers append result['message'] to a warnings list; keys must not drift."""
    for goal in ({}, {"verification": {"outcomes": ["x"]}}):
        result = evaluate(goal)
        assert set(result) == {"warned", "message", "reason"}
        assert isinstance(result["warned"], bool)
        assert isinstance(result["reason"], str)


def test_evaluate_does_not_mutate_the_goal():
    """Advisories run mid-pipeline alongside mutators; this one must not mutate."""
    goal = {"id": "g-14", "verification": {"outcomes": []}}
    before = repr(goal)
    evaluate(goal)
    assert repr(goal) == before
