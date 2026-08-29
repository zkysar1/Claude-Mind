"""test_goal_selector_malformed_goal.py — one bad goal record costs one warning, not the
fleet's selection (2026-08-29).

Measured on coach (zc-03): a Body rewrote asp-002's ``goals`` as ``{"goal_id", "status"}``
stubs (no ``id``), and every ``goal-selector.sh select`` on the fleet died on
``KeyError: 'id'`` while building the cross-aspiration dependency sets — five Bodies
unable to pick a goal because of one malformed record. The daemon now refuses such a
write (test_runtime_aspirations_update.py::TestGoalsFieldGuard); this pins the reader
side: the three identical id-set loops are one builder, ``global_goal_id_sets``, which
skips a record without an id and warns once.

Harness mirrors test_goal_selector_completability.py: capture/restore MIND_AGENT
around the module import; call the builder directly. No subprocess.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


def _asp(asp_id, goals, status="active"):
    return {"id": asp_id, "status": status, "goals": goals}


def test_well_formed_goals_build_the_done_and_live_sets():
    done, live = gs.global_goal_id_sets([
        _asp("asp-001", [
            {"id": "g-001-01", "status": "completed"},
            {"id": "g-001-02", "status": "pending"},
            {"id": "g-001-03", "status": "decomposed"},
            {"id": "g-001-04", "status": "skipped"},
        ]),
        _asp("asp-002", [{"id": "g-002-01", "status": "in-progress"}]),
        _asp("asp-009", [{"id": "g-009-01", "status": "pending"}], status="retired"),
    ])
    assert done == {"g-001-01", "g-001-03"}
    assert live == {"g-001-02", "g-002-01"}  # the retired aspiration contributes nothing


def test_a_record_without_an_id_is_skipped_with_one_warning(capsys):
    gs._MALFORMED_GOALS_WARNED.clear()
    stubs = [
        {"id": "g-002-01", "title": "real", "status": "completed"},
        {"goal_id": "g-002-02", "status": "completed"},  # the measured shape
        {"goal_id": "g-002-03", "status": "pending"},
        "g-002-04",  # a bare string ref (legacy shape)
        {"id": "", "status": "pending"},
    ]
    done, live = gs.global_goal_id_sets([_asp("asp-002", stubs)])
    assert done == {"g-002-01"}
    assert live == set()
    err = capsys.readouterr().err
    assert err.count("[goal-selector] WARN") == 4
    assert "asp-002" in err and "goal_id" in err and "string ref" in err
    # Warned once per record: a second build of the same records is silent.
    gs.global_goal_id_sets([_asp("asp-002", stubs)])
    assert capsys.readouterr().err == ""


def test_collect_candidates_skips_a_string_ref_and_an_id_less_stub(capsys):
    """Back-port of a downstream Body's own patch (2026-08-28, 'skip string-ref goals in raw
    JSONL'): a string ref used to raise AttributeError in collect_candidates, and a PENDING
    stub without an id would have become a candidate with goal_id None."""
    gs._MALFORMED_GOALS_WARNED.clear()
    asp = _asp("asp-007", [
        "g-007-01",
        {"goal_id": "g-007-02", "status": "pending"},
        {"id": "g-007-03", "title": "the real one", "status": "pending",
         "participants": ["agent"]},
    ])
    candidates = gs.collect_candidates([asp])
    assert [c["goal"]["id"] for c in candidates] == ["g-007-03"]
    err = capsys.readouterr().err
    assert err.count("[goal-selector] WARN") == 2


def test_census_effective_counts_ignores_a_non_record():
    census = importlib.import_module("_goal_census")
    asp = _asp("asp-008", [
        "g-008-01",
        {"id": "g-008-02", "title": "done", "status": "completed"},
        {"id": "g-008-03", "title": "open", "status": "pending"},
    ])
    total, completed = census.effective_counts(asp)
    assert (total, completed) == (2, 1)


def test_goal_record_id_never_raises():
    assert gs.goal_record_id({"id": "asp-x"}, {"id": "g-001-01"}) == "g-001-01"
    assert gs.goal_record_id({"id": "asp-x"}, {"goal_id": "g-001-01"}) is None
    assert gs.goal_record_id({"id": "asp-x"}, "g-001-01") is None
    assert gs.goal_record_id({"id": "asp-x"}, None) is None
    assert gs.goal_record_id(None, {"id": 7}) is None
