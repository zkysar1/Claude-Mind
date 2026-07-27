"""test_validate_goal_completed_after_precondition.py —  / rb-4371 (FIX 2).

A structured goal_completed_after precondition MISSING after_ref silently
perma-blocks the goal: predicate.py returns False forever, goal-selector
classifies precondition_unmet and EXCLUDES the goal from selection while status
stays `pending` (invisible except via `goal-selector blocked` — the
g-115-2688-b/-c shape). validate_verification (core/scripts/aspirations.py) now
REFUSES such a precondition at filing — fail LOUD, not silent.

Pins: the gate fires ONLY for type==goal_completed_after, requires BOTH goal_id
and after_ref, and leaves string / other-type preconditions untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import aspirations  # noqa: E402


def test_well_formed_goal_completed_after_passes():
    aspirations.validate_verification({
        "outcomes": ["x"],
        "preconditions": [
            {"type": "goal_completed_after", "goal_id": "g-1",
             "after_ref": "iso:2026-07-19T10:00:00"}
        ],
    }, "g-000-00")


def test_missing_after_ref_raises():
    with pytest.raises(ValueError, match="after_ref"):
        aspirations.validate_verification({
            "outcomes": ["x"],
            "preconditions": [{"type": "goal_completed_after", "goal_id": "g-1"}],
        }, "g-000-01")


def test_missing_goal_id_raises():
    with pytest.raises(ValueError, match="after_ref"):
        aspirations.validate_verification({
            "outcomes": ["x"],
            "preconditions": [
                {"type": "goal_completed_after", "after_ref": "iso:2026-07-19T10:00:00"}
            ],
        }, "g-000-02")


def test_empty_after_ref_raises():
    with pytest.raises(ValueError):
        aspirations.validate_verification({
            "outcomes": ["x"],
            "preconditions": [
                {"type": "goal_completed_after", "goal_id": "g-1", "after_ref": ""}
            ],
        }, "g-000-03")


def test_string_preconditions_untouched():
    # Free-form string preconditions are not affected by the gate.
    aspirations.validate_verification({
        "outcomes": ["x"],
        "preconditions": ["PR #62 merged to main"],
    }, "g-000-04")


def test_other_type_precondition_untouched():
    # The gate targets ONLY goal_completed_after; a different-type dict without
    # after_ref is unaffected.
    aspirations.validate_verification({
        "outcomes": ["x"],
        "preconditions": [{"type": "file_check", "path": "foo.txt"}],
    }, "g-000-05")
