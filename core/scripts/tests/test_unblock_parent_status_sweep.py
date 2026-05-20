"""test_unblock_parent_status_sweep.py — regression test for  / rb-908.

Asserts that unblock-parent-status-sweep.py's helper functions correctly
identify the g-250-73 canonical incident shape AND reject false-positive
cases that would have leaked through a less-conservative heuristic.

Cases covered:
  1. parent_id parser: origin_signal "unblock:g-250-69" → g-250-69
  2. parent_id parser: title "Unblock: behavior for g-250-69" → g-250-69
  3. parent_id parser: discovered_by "g-250-69" → g-250-69
  4. parent_id parser: nothing parseable → None
  5. Unblock title matcher: "Unblock: foo" yes, "Investigate: foo" no,
     " Unblock : foo" yes (whitespace tolerant), "" no
  6. Idempotency check: outcome_note already starts with parent-resolved
     phrase → considered already swept
  7. Terminal-state set: skipped/completed/superseded/archived are sweep
     targets; pending/in-progress are NOT.

Pattern: same importlib + sys.path shape as test_parent_supersession_sweep.py.
unblock-parent-status-sweep.py uses a hyphenated filename so we load it via
spec_from_file_location with a hyphen-free attribute name.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_sweep():
    """Load unblock-parent-status-sweep.py via importlib."""
    spec = importlib.util.spec_from_file_location(
        "unblock_parent_status_sweep_mod",
        CORE_SCRIPTS / "unblock-parent-status-sweep.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            "could not load spec for unblock-parent-status-sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parent_id_from_origin_signal_canonical_shape():
    """rb-908 canonical: capability-gate._build_suggestion emits
    origin_signal='unblock:<parent-id>' verbatim."""
    mod = _import_sweep()
    g = {
        "id": "g-250-73",
        "title": "Unblock: behavior for g-250-69",
        "origin_signal": "unblock:g-250-69",
    }
    assert mod._parse_parent_id(g) == "g-250-69"


def test_parent_id_from_title_for_pattern():
    """Layer D canonical title 'Unblock: <verb> for <parent-id>'."""
    mod = _import_sweep()
    g = {
        "id": "g-001",
        "title": "Unblock: deploy for g-115-87",
        # No origin_signal — fallback to title regex
    }
    assert mod._parse_parent_id(g) == "g-115-87"


def test_parent_id_origin_signal_beats_title():
    """When both origin_signal and title contain goal-ids, origin_signal
    takes precedence (priority order — origin_signal is most authoritative
    since it's what capability-gate emits)."""
    mod = _import_sweep()
    g = {
        "id": "g-001",
        "title": "Unblock: thing for ",  # different from origin_signal
        "origin_signal": "unblock:g-250-69",
    }
    # origin_signal wins
    assert mod._parse_parent_id(g) == "g-250-69"


def test_parent_id_from_discovered_by_fallback():
    """If origin_signal and title both lack a goal-id, fall back to
    discovered_by field."""
    mod = _import_sweep()
    g = {
        "id": "g-001",
        "title": "Unblock: do the thing",  # no 'for g-NNN-NN'
        "origin_signal": "unblock:something-narrative",  # not a goal-id form
        "discovered_by": "g-250-69",
    }
    assert mod._parse_parent_id(g) == "g-250-69"


def test_parent_id_unparseable_returns_none():
    """No goal-id anywhere → None (the goal won't be considered for
    sweeping). Conservative — we never guess a parent."""
    mod = _import_sweep()
    g = {
        "id": "g-001",
        "title": "Unblock: do the thing",
        "origin_signal": "unblock:narrative-only",
        "discovered_by": "user_directive",  # not a goal-id pattern
    }
    assert mod._parse_parent_id(g) is None


def test_origin_signal_must_match_exact_form():
    """origin_signal regex requires 'unblock:<g-NNN-NN>' exactly — does
    not consume narrative suffixes like 'unblock:g-115-129-precondition'."""
    mod = _import_sweep()
    # narrative suffix after the goal-id — NOT exact form
    g = {
        "id": "g-001",
        "title": "Unblock: do the thing",  # title path also no match
        "origin_signal": "unblock:g-115-129-precondition",
    }
    # origin_signal regex anchored at $ rejects suffix — falls through
    assert mod._parse_parent_id(g) is None


def test_unblock_title_matcher_accepts_canonical_and_whitespace():
    """Title must start with literal 'Unblock:' (case-insens, ws-tolerant)."""
    mod = _import_sweep()
    assert mod._is_unblock_goal({"title": "Unblock: foo"}) is True
    assert mod._is_unblock_goal({"title": " Unblock : foo"}) is True
    assert mod._is_unblock_goal({"title": "unblock: foo"}) is True


def test_unblock_title_matcher_rejects_other_prefixes():
    """Investigate/Idea/Apply/Maintain/Recurring titles are NOT Layer D
    auto-Unblocks — even if they happen to use 'unblock:' in origin_signal
    for unrelated reasons (g-249-06 / g-250-77 false-positive shape)."""
    mod = _import_sweep()
    assert mod._is_unblock_goal({"title": "Investigate: foo"}) is False
    assert mod._is_unblock_goal({"title": "Idea: foo"}) is False
    assert mod._is_unblock_goal({"title": "Apply: foo"}) is False
    assert mod._is_unblock_goal({"title": "Maintain: foo"}) is False
    assert mod._is_unblock_goal({"title": "Recurring: foo"}) is False
    assert mod._is_unblock_goal({"title": ""}) is False
    assert mod._is_unblock_goal({}) is False


def test_idempotency_already_swept_detection():
    """Once outcome_note carries the parent-resolved phrase, subsequent
    sweeps must not re-mark the goal — prevents double-write loops."""
    mod = _import_sweep()
    already = {
        "id": "g-001",
        "title": "Unblock: x for g-002",
        "origin_signal": "unblock:g-002",
        "outcome_note": ("parent resolved without action needed "
                         "(parent_id=g-002, parent.status=skipped)"),
    }
    assert mod._is_already_swept(already) is True
    fresh = {
        "id": "g-001",
        "title": "Unblock: x for g-002",
        "origin_signal": "unblock:g-002",
        "outcome_note": "",
    }
    assert mod._is_already_swept(fresh) is False
    # No outcome_note at all → not swept
    bare = {
        "id": "g-001",
        "title": "Unblock: x for g-002",
    }
    assert mod._is_already_swept(bare) is False


def test_terminal_states_set():
    """The four states that indicate 'no Unblock action is needed' are
    locked: skipped, completed, superseded, archived. Pending/in-progress
    must NOT be in this set or the sweep would clear actively-working
    parents."""
    mod = _import_sweep()
    assert "skipped" in mod.TERMINAL_STATES
    assert "completed" in mod.TERMINAL_STATES
    assert "superseded" in mod.TERMINAL_STATES
    assert "archived" in mod.TERMINAL_STATES
    assert "pending" not in mod.TERMINAL_STATES
    assert "in-progress" not in mod.TERMINAL_STATES
    assert "blocked" not in mod.TERMINAL_STATES


def test_canonical_incident_g_250_73_shape_recognized():
    """End-to-end shape recognition: an Unblock with exactly the 
    field set should parse to parent g-250-69 AND match the Unblock title
    AND not be considered already-swept."""
    mod = _import_sweep()
    g = {
        "id": "g-250-73",
        "title": "Unblock: behavior for g-250-69",
        "status": "pending",
        "origin_signal": "unblock:g-250-69",
        "tags": ["unblock", "defer-gate-routed", "framework-maintenance"],
        "outcome_note": "",
        "created_at": "2026-05-13T09:45:14",
    }
    assert mod._is_unblock_goal(g) is True
    assert mod._parse_parent_id(g) == "g-250-69"
    assert mod._is_already_swept(g) is False


def test_status_index_builder():
    """_build_status_index returns {goal_id: status} across all
    aspirations regardless of source/aspiration boundary."""
    mod = _import_sweep()
    all_asps = [
        ({"id": "asp-1", "goals": [
            {"id": "g-1", "status": "pending"},
            {"id": "g-2", "status": "completed"},
        ]}, "world"),
        ({"id": "asp-2", "goals": [
            {"id": "g-3", "status": "skipped"},
            {"id": "g-4"},  # status missing
        ]}, "agent"),
    ]
    idx = mod._build_status_index(all_asps)
    assert idx == {"g-1": "pending", "g-2": "completed",
                   "g-3": "skipped", "g-4": None}


if __name__ == "__main__":
    test_parent_id_from_origin_signal_canonical_shape()
    test_parent_id_from_title_for_pattern()
    test_parent_id_origin_signal_beats_title()
    test_parent_id_from_discovered_by_fallback()
    test_parent_id_unparseable_returns_none()
    test_origin_signal_must_match_exact_form()
    test_unblock_title_matcher_accepts_canonical_and_whitespace()
    test_unblock_title_matcher_rejects_other_prefixes()
    test_idempotency_already_swept_detection()
    test_terminal_states_set()
    test_canonical_incident_g_250_73_shape_recognized()
    test_status_index_builder()
    print("All 12 tests passed.")
