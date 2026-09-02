"""Pins the three measured false-positive sources of counted-close-revert-census.

Each assertion below corresponds to a must-keep in the script's docstring, and
each must-keep exists because the condition was MEASURED producing a wrong
answer — not because it was imagined:

  (1) recurring goals are EXCLUDED   — 84 of 90 members in the original hand-run
  (2) not-in-store is SEGREGATED     — 23 members, expected for archived asps
  (3) a healthy terminal close is a  — the positive control; without it a clean
      MATCH, not a miss                verdict is indistinguishable from a
                                       predicate that never matches

A fourth test pins the defect that the store read guard caught in this script's
own first draft: the classifier must key strictly on the counted-id list, so an
id that appears elsewhere in a working memory can never be attributed to the
agent whose counted list does not contain it. That draft hand-parsed the YAML,
picked an id out of the wrong nesting, and reported it as a revert counted by an
agent whose WM never contained the id at all.
"""

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "counted_close_revert_census", _SCRIPTS / "counted-close-revert-census.py")
census = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(census)


def _fixture():
    """One counted id per bucket, so every branch is exercised by one call."""
    counted = {
        "agentA": {"agent-wide": ["g-1-reverted", "g-2-recurring",
                                  "g-3-terminal", "g-4-absent"]},
    }
    suspects = {
        # non-terminal + NOT recurring -> the finding
        "g-1-reverted": {"status": "pending", "recurring": False, "title": "t1"},
        # non-terminal + recurring -> excluded by design (must-keep 1)
        "g-2-recurring": {"status": "pending", "recurring": True, "title": "t2"},
    }
    terminal_ids = {"g-3-terminal"}
    return counted, suspects, terminal_ids


def test_recurring_is_excluded_not_reported_as_revert():
    """Must-keep (1). A recurring goal returns to pending on close BY DESIGN."""
    counted, suspects, terminal_ids = _fixture()
    reverted, recurring_excluded, _, _, _ = census._classify(
        counted, suspects, terminal_ids)
    assert [r["goal_id"] for r in recurring_excluded] == ["g-2-recurring"]
    assert "g-2-recurring" not in [r["goal_id"] for r in reverted]


def test_absent_id_is_segregated_from_reverted():
    """Must-keep (2). Not-in-store is a DIFFERENT finding, never folded in."""
    counted, suspects, terminal_ids = _fixture()
    reverted, _, not_in_store, _, _ = census._classify(
        counted, suspects, terminal_ids)
    assert [n["goal_id"] for n in not_in_store] == ["g-4-absent"]
    assert "g-4-absent" not in [r["goal_id"] for r in reverted]


def test_terminal_close_is_matched_and_revert_is_found():
    """Must-keep (3) positive control, plus the finding itself."""
    counted, suspects, terminal_ids = _fixture()
    reverted, _, _, terminal_ok, _ = census._classify(
        counted, suspects, terminal_ids)
    assert terminal_ok == 1, "a healthy close must MATCH, or a clean verdict is vacuous"
    assert [r["goal_id"] for r in reverted] == ["g-1-reverted"]
    assert reverted[0]["live_status"] == "pending"
    assert reverted[0]["agent"] == "agentA"


def test_id_outside_the_counted_list_is_never_attributed():
    """The first draft's fabricated finding: an id present in a WM but NOT in
    that WM's counted list must not appear in ANY bucket for that agent."""
    counted = {"agentB": {"agent-wide": []}}
    suspects = {"g-elsewhere": {"status": "blocked", "recurring": False, "title": "x"}}
    reverted, recurring_excluded, not_in_store, terminal_ok, _ = census._classify(
        counted, suspects, terminal_ids=set())
    assert reverted == [] and recurring_excluded == [] and not_in_store == []
    assert terminal_ok == 0


def test_unreadable_working_memory_is_reported_not_counted_as_empty():
    """An unreadable WM is not an empty one — it must surface, not vanish."""
    counted = {"agentC": {"agent-wide": {"__error__": "daemon refused"}}}
    reverted, _, not_in_store, terminal_ok, unreadable = census._classify(
        counted, {}, set())
    assert len(unreadable) == 1 and unreadable[0]["agent"] == "agentC"
    assert reverted == [] and not_in_store == [] and terminal_ok == 0


def test_terminal_statuses_are_imported_not_restated():
    """A status added upstream must reclassify here without an edit."""
    from aspirations import TERMINAL_GOAL_STATUSES, VALID_GOAL_STATUSES
    assert set(census._TERMINAL) == set(TERMINAL_GOAL_STATUSES)
    assert set(census._NON_TERMINAL) == set(VALID_GOAL_STATUSES) - set(TERMINAL_GOAL_STATUSES)
    assert census._NON_TERMINAL, "non-terminal set must be non-empty"
