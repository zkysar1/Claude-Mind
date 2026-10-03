"""test_goal_selector_candidate_tier_visibility.py --  regression.

Pins spec section 2 of world/conventions/goal-intake-management.md: a
candidate-status goal is INVISIBLE to the selector until grooming promotes it,
and is reported as blocked (`candidate_tier`) instead of falling out of both
lists.

WHY THIS NEEDS A TEST. Spec section 2 called the invisibility FREE, because
collect_candidates keeps only `status == "pending"`. A free property has no
test and no owner (rb-12687), so it lasted until g-353-82 (2069365e21) widened
that loop to `("pending", "candidate")` on the strength of a sweep rubric whose
"intended" column did not exist. From then on a candidate-status goal was a
SCORING candidate: with the tier ON the selector could pick one, Phase 4
refused candidate -> in-progress, and `complete_by` then completed it ungated.
g-353-165 restored the property; this file is what keeps it restored.

WHAT IS PINNED, one test per clause of the filing's verification block:

  1. collect_candidates excludes the candidate-status goal and still returns
     the pending one in the same aspiration (verification outcome 1; the
     in-process probe from the filing, g-901-01 candidate + g-901-02 pending).
  2. collect_blocked reports the candidate-status goal under its OWN reason,
     `candidate_tier` (verification outcome 2).
  3. THE SYMMETRY INVARIANT (guard-1698): every goal is in EXACTLY ONE of the
     two lists. A select-time filter with no complementary classifier drops the
     goal from both.
  4. THE DELIBERATE EXCEPTION to rb-3009: `candidate_tier` carries NO typed
     blocker_ref. rb-3009 says every new collect_blocked reason must synth one,
     because the quiescence gate's C2 fails over a goal without it. Here that
     failure is the point: the agent can lift this block itself (a promote), so
     the queue is not structurally gated, and a synthesized ref would launder an
     internal lever into an external gate. If someone "fixes" this per rb-3009,
     this test fails and points at the reasoning.
  5. The reason SET stays consistent: _blocked_reason_counts tallies the new
     reason, so sum(by_reason) == total_blocked.
  6. The new branch is gated on `status == "candidate"` alone, and a candidate
     neighbour does not change what the pending goals do.

Harness mirrors test_goal_selector_fresh_session_marker.py: pin MIND_AGENT
around import, per-test AGENT_NAME pin, neutralize the orthogonal capability
filter. No subprocess, no tmp world, no daemon.
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


def _goal(gid, status="pending"):
    """Minimal agent-eligible, unclaimed, non-recurring goal."""
    return {
        "id": gid, "title": "goal %s" % gid, "status": status,
        "participants": ["agent"], "category": "test", "priority": "MEDIUM",
    }


def _asps(goals):
    return [{"id": "asp-test", "status": "active", "goals": goals}]


def _pin(monkeypatch):
    """Pin agent identity and neutralize the capability filter."""
    monkeypatch.setattr(gs, "AGENT_NAME", "alpha")
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: set())


def _candidate_ids(goals):
    return {c["goal"]["id"]
            for c in gs.collect_candidates(_asps(goals), source="world")}


def _blocked_by_id(goals):
    return {e["goal_id"]: e for e in gs.collect_blocked(_asps(goals))}


# The probe from the filing: one candidate-status goal beside one pending goal.
MIXED = [_goal("g-901-01", "candidate"), _goal("g-901-02")]


# -- 1. collect_candidates (verification outcome 1) ------------------------

def test_candidates_exclude_a_candidate_status_goal(monkeypatch):
    _pin(monkeypatch)
    # pre-fix (): BOTH ids -- the candidate-status goal was a scoring candidate
    assert _candidate_ids(MIXED) == {"g-901-02"}


def test_an_aspiration_of_only_candidate_status_goals_offers_nothing(monkeypatch):
    _pin(monkeypatch)
    assert _candidate_ids([_goal("g-901-01", "candidate"),
                           _goal("g-901-03", "candidate")]) == set()


# -- 2. collect_blocked (verification outcome 2) ---------------------------

def test_blocked_reports_a_candidate_status_goal_under_its_own_reason(monkeypatch):
    _pin(monkeypatch)
    blocked = _blocked_by_id(MIXED)
    assert blocked["g-901-01"]["block_reason"] == "candidate_tier"
    assert "section 2" in blocked["g-901-01"]["block_detail"].replace("§", "section ")
    assert "g-901-02" not in blocked     # pending and unblocked: nothing to report


# -- 3. THE SYMMETRY INVARIANT (guard-1698) --------------------------------

@pytest.mark.parametrize("gid", ["g-901-01", "g-901-02"])
def test_every_goal_is_in_exactly_one_list(monkeypatch, gid):
    """Never in NEITHER. A select-time filter with no classifier drops it from both."""
    _pin(monkeypatch)
    in_candidates = gid in _candidate_ids(MIXED)
    in_blocked = gid in _blocked_by_id(MIXED)
    assert in_candidates != in_blocked, (
        "%s is in %s -- SYMMETRY broken"
        % (gid, "BOTH lists" if in_candidates else "NEITHER list"))


# -- 4. THE DELIBERATE EXCEPTION to rb-3009 --------------------------------

def test_candidate_tier_carries_no_typed_blocker_ref(monkeypatch):
    """quiescence-gate.py C2 counts a blocked entry whose blocker_ref is not a
    dict as `missing_ref`, and C2 then refuses to approve a sleep. That is the
    wanted outcome for this reason: the agent can lift the block itself."""
    _pin(monkeypatch)
    entry = _blocked_by_id(MIXED)["g-901-01"]
    assert not isinstance(entry.get("blocker_ref"), dict), (
        "candidate_tier now carries a typed blocker_ref, so quiescence C2 would "
        "approve a sleep over a queue the agent can still groom (see this "
        "file's docstring, clause 4)")


# -- 5. the reason SET stays consistent ------------------------------------

def test_reason_counts_tally_the_new_reason(monkeypatch):
    _pin(monkeypatch)
    blocked = gs.collect_blocked(_asps(MIXED))
    counts = gs._blocked_reason_counts(blocked)
    assert counts["candidate_tier"] == 1
    assert sum(counts.values()) == len(blocked)    # the 2026-08-21 drift class


# -- 6. the branch is gated on the candidate status alone ------------------

@pytest.mark.parametrize("status",
                         ["pending", "blocked", "in-progress", "completed", "skipped"])
def test_candidate_tier_is_reserved_for_the_candidate_status(monkeypatch, status):
    _pin(monkeypatch)
    reasons = {gid: e.get("block_reason")
               for gid, e in _blocked_by_id([_goal("g-x", status)]).items()}
    assert reasons.get("g-x") != "candidate_tier"


def test_a_candidate_status_neighbour_does_not_change_the_pending_goals(monkeypatch):
    """The anti-regression half: what fails if the new branch is ever widened."""
    _pin(monkeypatch)
    plain = [_goal("g-a"), _goal("g-b"), _goal("g-c")]
    with_neighbour = plain + [_goal("g-cand", "candidate")]
    assert (_candidate_ids(plain) == _candidate_ids(with_neighbour)
            == {"g-a", "g-b", "g-c"})
