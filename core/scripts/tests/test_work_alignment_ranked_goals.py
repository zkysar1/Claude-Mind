"""work-alignment.py --ranked-goals accepts what a Body actually passes (2026-08-30, coach@zc-03).

The flag is documented as goal-selector's ranked array, but a Body that has lost the
selector output to compaction passes the ids it remembers -- ``'["g-006-21"]'`` -- and
``str.get`` raised an AttributeError the except clause did not name, so the alignment
check died with a traceback instead of the ``recurring_ratio: null`` degradation the code
intended. Pins: ids resolve against the active aspirations; selector entries and whole
selector documents both work; junk degrades to None, never raises.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "work-alignment.py"


@pytest.fixture(scope="module")
def wa():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("work_alignment_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


ACTIVE = [
    {"id": "asp-006", "status": "active", "goals": [
        {"id": "g-006-21", "recurring": False},
        {"id": "g-006-05", "recurring": True},
        {"id": "g-006-09"},
    ]},
    {"id": "asp-005", "status": "active", "goals": [
        {"id": "g-005-01", "recurring": True},
    ]},
]


def test_goal_objects_keep_working(wa):
    ranked = json.dumps([{"goal_id": "x", "recurring": True}, {"goal_id": "y", "recurring": False}])
    assert wa.recurring_ratio_of(ranked, ACTIVE) == 0.5


def test_id_strings_resolve_against_the_active_aspirations(wa):
    # The reducer's verbatim shape: one id, a non-recurring goal.
    assert wa.recurring_ratio_of('["g-006-21"]', ACTIVE) == 0.0
    # Two recurring of three known ids; an unknown id is dropped, not counted.
    assert wa.recurring_ratio_of('["g-006-05", "g-005-01", "g-006-09", "g-999-99"]', ACTIVE) == 0.67


def test_a_whole_selector_document_is_unwrapped(wa):
    doc = json.dumps({"ranked_goals": [{"goal_id": "g-006-05", "recurring": True}], "warnings": []})
    assert wa.recurring_ratio_of(doc, ACTIVE) == 1.0


def test_mixed_objects_and_ids(wa):
    ranked = json.dumps([{"goal_id": "z", "recurring": True}, "g-006-21"])
    assert wa.recurring_ratio_of(ranked, ACTIVE) == 0.5


@pytest.mark.parametrize(
    "bad",
    [None, "", "not json", "[]", '["g-999-99"]', "42", '"g-006-21"', "[1, 2]", '{"warnings": []}'],
)
def test_junk_degrades_to_none_and_never_raises(wa, bad):
    assert wa.recurring_ratio_of(bad, ACTIVE) is None


def test_the_reducers_shape_no_longer_tracebacks_end_to_end(wa, capsys):
    """The AttributeError was the whole incident: `'str' object has no attribute 'get'`
    out of a generator the except clause could not catch."""
    assert wa.recurring_ratio_of('["g-006-21"]', []) is None
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
