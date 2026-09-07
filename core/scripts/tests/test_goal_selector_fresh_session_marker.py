"""test_goal_selector_fresh_session_marker.py --  regression.

Pins the `requires_fresh_session` structural suppressor added to
goal-selector.py so a goal whose terminal step is a mandatory destructive
cleanup is not ranked into the middle of a session.

WHY THE MARKER EXISTS. guard-5683 encoded the property BEHAVIOURALLY, and a
guardrail only tells an agent what to conclude AFTER it has paid the cost of
reading the goal. Measured 2026-08-31: four agents claimed and declined
g-368-28 for the same stewardship reason in one day, and RELEASING refreshes
the scorer's recency terms -- the goal came back at TOP eight minutes later
with a HIGHER score (18.90 -> 19.18). Each principled decline made the next
re-offer stronger. Nothing in goal-selector.py knew the property existed.

WHAT IS PINNED, one test per clause of the filing's verification block:

  1. `_requires_fresh_session` unit shapes, including the short-circuit: an
     UNMARKED goal must not even probe session state (asserted by making the
     probe raise -- if the order ever inverts, this test fails loudly rather
     than silently paying a WM read per goal).
  2. `_session_has_closed_goals` reads the IN-SESSION list, caches, and
     FAIL-OPENS to "fresh" on an unreadable working memory.
  3. collect_candidates suppresses a marked goal mid-session and offers it in
     a fresh session (verification outcome 1).
  4. collect_blocked classifies it `fresh_session_only` -- routed to blocked[]
     rather than dropped, so all_blocked stays assertable and quiescence can
     still fire.
  5. THE SYMMETRY INVARIANT (g-115-3150): the marked goal must be in EXACTLY
     ONE of the two lists. A suppressor wired into only one site makes it fall
     out of BOTH -- not a candidate there, not blocked here -- which is the
     defect `_has_future_deferred_until`'s docstring records.
  6. Absent the marker, the candidate list is IDENTICAL whether or not the
     session has closed goals (verification outcome 2: "ranking is
     byte-identical to today"). This is the anti-regression half -- it is what
     would fail if the new branch were ever widened past its marker test.

Harness mirrors test_goal_selector_rne_time_gate.py: pin MIND_AGENT around
import, per-test AGENT_NAME pin, neutralize the orthogonal capability filter.
No subprocess, no tmp world, no daemon.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


def _goal(gid, fresh_only=False):
    """Minimal pending, agent-eligible, unclaimed, non-recurring goal."""
    g = {
        "id": gid, "title": "goal %s" % gid, "status": "pending",
        "participants": ["agent"], "category": "test", "priority": "MEDIUM",
    }
    if fresh_only:
        g["requires_fresh_session"] = True
    return g


def _asps(goals):
    return [{"id": "asp-test", "status": "active", "goals": goals}]


def _pin(monkeypatch, session_closed):
    """Pin agent identity, neutralize the capability filter, and force the
    session-state answer WITHOUT touching the real working memory."""
    monkeypatch.setattr(gs, "AGENT_NAME", "alpha")
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: set())
    monkeypatch.setattr(gs, "_SESSION_HAS_CLOSED_GOALS", None)
    monkeypatch.setattr(gs, "_session_has_closed_goals", lambda: session_closed)


TWO_GOALS = [_goal("g-fresh-only", fresh_only=True), _goal("g-plain")]


# -- 1. _requires_fresh_session unit shapes ---------------------------------

def test_unmarked_goal_is_never_suppressed(monkeypatch):
    monkeypatch.setattr(gs, "_SESSION_HAS_CLOSED_GOALS", None)
    monkeypatch.setattr(gs, "_session_has_closed_goals", lambda: True)
    assert gs._requires_fresh_session(_goal("g-plain")) is False


def test_unmarked_goal_short_circuits_before_probing_session(monkeypatch):
    """The marker test MUST come first. If the order ever inverts, every goal
    in the queue pays a working-memory read -- and this raises instead."""
    def _boom():
        raise AssertionError("session probe ran for an UNMARKED goal")
    monkeypatch.setattr(gs, "_SESSION_HAS_CLOSED_GOALS", None)
    monkeypatch.setattr(gs, "_session_has_closed_goals", _boom)
    assert gs._requires_fresh_session(_goal("g-plain")) is False


def test_marked_goal_suppressed_only_mid_session(monkeypatch):
    monkeypatch.setattr(gs, "_SESSION_HAS_CLOSED_GOALS", None)
    monkeypatch.setattr(gs, "_session_has_closed_goals", lambda: True)
    assert gs._requires_fresh_session(_goal("g-x", fresh_only=True)) is True
    monkeypatch.setattr(gs, "_session_has_closed_goals", lambda: False)
    assert gs._requires_fresh_session(_goal("g-x", fresh_only=True)) is False


# -- 2. _session_has_closed_goals: source, cache, fail-open ----------------

def test_session_probe_reads_in_session_list(monkeypatch):
    monkeypatch.setattr(gs, "_SESSION_HAS_CLOSED_GOALS", None)
    monkeypatch.setattr(gs, "read_wm",
                        lambda: {"goals_completed_this_session": [{"goal_id": "g-1"}]})
    assert gs._session_has_closed_goals() is True


def test_session_probe_empty_list_is_fresh(monkeypatch):
    monkeypatch.setattr(gs, "_SESSION_HAS_CLOSED_GOALS", None)
    monkeypatch.setattr(gs, "read_wm", lambda: {"goals_completed_this_session": []})
    assert gs._session_has_closed_goals() is False


def test_session_probe_fail_opens_on_unreadable_wm(monkeypatch):
    """A filter that cannot read its input must not silently hide work."""
    def _raise():
        raise OSError("working memory unreadable")
    monkeypatch.setattr(gs, "_SESSION_HAS_CLOSED_GOALS", None)
    monkeypatch.setattr(gs, "read_wm", _raise)
    assert gs._session_has_closed_goals() is False


def test_session_probe_is_cached(monkeypatch):
    """Cached module-wide so collect_candidates and collect_blocked cannot
    disagree within one run -- the desync SYMMETRY exists to prevent."""
    calls = []

    def _counting():
        calls.append(1)
        return {"goals_completed_this_session": [{"goal_id": "g-1"}]}

    monkeypatch.setattr(gs, "_SESSION_HAS_CLOSED_GOALS", None)
    monkeypatch.setattr(gs, "read_wm", _counting)
    assert gs._session_has_closed_goals() is True
    assert gs._session_has_closed_goals() is True
    assert len(calls) == 1


# -- 3. collect_candidates (verification outcome 1) ------------------------

def test_candidates_suppress_marked_goal_mid_session(monkeypatch):
    _pin(monkeypatch, session_closed=True)
    ids = {c["goal"]["id"]
           for c in gs.collect_candidates(_asps(TWO_GOALS), source="world")}
    assert "g-fresh-only" not in ids     # pre-fix: PRESENT, and top-ranked
    assert "g-plain" in ids


def test_candidates_offer_marked_goal_in_a_fresh_session(monkeypatch):
    _pin(monkeypatch, session_closed=False)
    ids = {c["goal"]["id"]
           for c in gs.collect_candidates(_asps(TWO_GOALS), source="world")}
    assert "g-fresh-only" in ids
    assert "g-plain" in ids


# -- 4. collect_blocked classification -------------------------------------

def test_blocked_classifies_marked_goal_mid_session(monkeypatch):
    _pin(monkeypatch, session_closed=True)
    blocked = {e["goal_id"]: e for e in gs.collect_blocked(_asps(TWO_GOALS))}
    assert "g-fresh-only" in blocked
    assert blocked["g-fresh-only"]["block_reason"] == "fresh_session_only"
    assert "requires_fresh_session" in blocked["g-fresh-only"]["block_detail"]


def test_blocked_omits_marked_goal_in_a_fresh_session(monkeypatch):
    _pin(monkeypatch, session_closed=False)
    reasons = {e["goal_id"]: e.get("block_reason")
               for e in gs.collect_blocked(_asps(TWO_GOALS))}
    assert reasons.get("g-fresh-only") != "fresh_session_only"


# -- 5. THE SYMMETRY INVARIANT () --------------------------------

@pytest.mark.parametrize("session_closed", [True, False])
def test_marked_goal_is_in_exactly_one_list(monkeypatch, session_closed):
    """Never in NEITHER. A one-site suppressor drops it from both."""
    _pin(monkeypatch, session_closed=session_closed)
    cand = {c["goal"]["id"]
            for c in gs.collect_candidates(_asps(TWO_GOALS), source="world")}
    blocked = {e["goal_id"] for e in gs.collect_blocked(_asps(TWO_GOALS))}
    in_candidates = "g-fresh-only" in cand
    in_blocked = "g-fresh-only" in blocked
    assert in_candidates != in_blocked, (
        "marked goal is in %s -- SYMMETRY broken"
        % ("BOTH lists" if in_candidates else "NEITHER list"))


# -- 6. Absent the marker, ranking is unchanged (verification outcome 2) ---

def test_unmarked_ranking_identical_across_session_states(monkeypatch):
    """The anti-regression half: this is what fails if the new branch is ever
    widened past its marker test."""
    plain = [_goal("g-a"), _goal("g-b"), _goal("g-c")]

    _pin(monkeypatch, session_closed=False)
    fresh = [c["goal"]["id"]
             for c in gs.collect_candidates(_asps(plain), source="world")]
    fresh_blocked = [(e["goal_id"], e.get("block_reason"))
                     for e in gs.collect_blocked(_asps(plain))]

    _pin(monkeypatch, session_closed=True)
    midsession = [c["goal"]["id"]
                  for c in gs.collect_candidates(_asps(plain), source="world")]
    mid_blocked = [(e["goal_id"], e.get("block_reason"))
                   for e in gs.collect_blocked(_asps(plain))]

    assert fresh == midsession, (fresh, midsession)
    assert fresh_blocked == mid_blocked, (fresh_blocked, mid_blocked)
    assert not any(r == "fresh_session_only" for _, r in mid_blocked)
