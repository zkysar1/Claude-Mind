""" item (1), ELIGIBILITY half: a live pull_signal must lift a
not-yet-due recurring goal INTO the candidate set, not merely re-rank it once
it is already there.

WHY THIS FILE EXISTS. The boost shipped 2026-08-17 and was measured INERT on
2026-08-28: `apply_pull_boost` runs over the ALREADY-SCORED list, while the
recurring hour gate drops a not-yet-due goal with a bare `continue` before
scoring. The bypass that existed at that gate keys on `cadence_signal`, a
DIFFERENT field. Live evidence: g-306-284 held a pull_signal set 22 minutes
earlier while ZERO of 1375 returned candidates carried the field at all -- the
boost was operating on an empty set. Sizing or tuning it would have been wasted
work, and it would have read as a mis-sized constant rather than dead code.

The tests below therefore pin BOTH directions at the ELIGIBILITY layer (the
scoring layer is already pinned by test_goal_selector_pull_boost.py), plus the
structural property that makes the two halves un-driftable: they consume ONE
liveness predicate.

Harness mirrors test_cadence_signal_gate.py deliberately -- that file exercises
the sibling branch of the very same if/else, so the two gates stay tested the
same way.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timedelta
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


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _sig(hours_ago=0.5):
    return {
        "set_at": _iso(datetime.now() - timedelta(hours=hours_ago)),
        "by": "test/producer",
        "reason": "synthetic carrier",
    }


def _rec_goal(gid="g-test-pull", **overrides):
    """A recurring agent goal that is NOT yet due (fired one minute ago)."""
    g = {
        "id": gid,
        "title": "Recurring: synthetic pull-signal test goal",
        "status": "pending",
        "priority": "MEDIUM",
        "participants": ["agent"],
        "recurring": True,
        "interval_hours": 24,
        "lastAchievedAt": _iso(datetime.now() - timedelta(minutes=1)),
    }
    g.update(overrides)
    return g


def _candidate_ids(goal):
    asp = {"id": "asp-test", "status": "active", "priority": "MEDIUM", "goals": [goal]}
    return {r["goal"]["id"] for r in gs.collect_candidates([asp], source="agent")}


# --------------------------------------------------------------------------
# The fix, and its negative control. These two together are the whole claim:
# the signal is what changes the verdict, not the goal being synthetic.
# --------------------------------------------------------------------------

def test_not_due_recurring_with_live_pull_signal_becomes_eligible():
    assert "g-test-pull" in _candidate_ids(_rec_goal(pull_signal=_sig()))


def test_not_due_recurring_without_pull_signal_stays_ineligible():
    """NO-REGRESSION CONTROL. Without a signal the hour gate must still hold;
    if this ever passes, the fix has stopped being conditional and every
    not-yet-due recurring goal is flooding the candidate set."""
    assert "g-test-pull" not in _candidate_ids(_rec_goal())


# --------------------------------------------------------------------------
# The safety valve: a lost CLEAR must not pin a goal eligible forever.
# --------------------------------------------------------------------------

def test_aged_out_pull_signal_does_not_lift_eligibility():
    max_age = float(gs.PULL_CONFIG.get("max_age_hours", 24.0))
    stale = _sig(hours_ago=max_age + 1.0)
    assert "g-test-pull" not in _candidate_ids(_rec_goal(pull_signal=stale))


def test_signal_just_inside_the_window_still_lifts_eligibility():
    max_age = float(gs.PULL_CONFIG.get("max_age_hours", 24.0))
    fresh_enough = _sig(hours_ago=max_age - 0.5)
    assert "g-test-pull" in _candidate_ids(_rec_goal(pull_signal=fresh_enough))


# --------------------------------------------------------------------------
# Cross-box clock skew (guard-3221). The producer writes on ITS box and the
# consumer reads on ANOTHER, so a set_at slightly in the reader's future is
# normal and must stay live -- but an unbounded future stamp must not.
# --------------------------------------------------------------------------

def test_small_clock_skew_is_tolerated_at_the_eligibility_gate():
    assert "g-test-pull" in _candidate_ids(_rec_goal(pull_signal=_sig(hours_ago=-0.5)))


def test_far_future_signal_is_bogus_not_live():
    assert "g-test-pull" not in _candidate_ids(_rec_goal(pull_signal=_sig(hours_ago=-72.0)))


# --------------------------------------------------------------------------
# Malformed input must not raise inside candidate collection.
# --------------------------------------------------------------------------

def test_malformed_pull_signal_does_not_raise_and_does_not_lift():
    for bad in ("not-a-dict", 42, [], {}, {"set_at": None}, {"set_at": "nonsense"}):
        assert "g-test-pull" not in _candidate_ids(_rec_goal(pull_signal=bad))


# --------------------------------------------------------------------------
# One flag turns the WHOLE mechanism off, on both axes.
# --------------------------------------------------------------------------

def test_disabled_config_lifts_neither_axis(monkeypatch):
    monkeypatch.setattr(gs, "PULL_CONFIG", {**gs.PULL_CONFIG, "enabled": False})
    assert "g-test-pull" not in _candidate_ids(_rec_goal(pull_signal=_sig()))
    assert gs.pull_signal_live_age_hours(_sig(), gs.PULL_CONFIG) is None


# --------------------------------------------------------------------------
# Normally-due goals are untouched -- positive control that the gate still
# admits on the ordinary timer path and this change is purely additive.
# --------------------------------------------------------------------------

def test_past_due_recurring_still_eligible_without_any_signal():
    due = _rec_goal(lastAchievedAt=_iso(datetime.now() - timedelta(hours=48)))
    assert "g-test-pull" in _candidate_ids(due)


# --------------------------------------------------------------------------
# STRUCTURAL: rank and eligibility consume ONE predicate, so they cannot drift
# into disagreeing about which signals are live. Monkeypatching the shared
# predicate must move BOTH -- if a future edit re-inlines the liveness test at
# either call site, this fails.
# --------------------------------------------------------------------------

def test_gate_and_boost_share_one_liveness_predicate(monkeypatch):
    sig = _sig()
    # Sanity: with the real predicate, both axes act on this signal.
    assert "g-test-pull" in _candidate_ids(_rec_goal(pull_signal=sig))
    scored = [{"goal_id": "g-test-pull", "score": 5.0, "pull_signal": sig}]
    gs.apply_pull_boost(scored, gs.PULL_CONFIG)
    assert scored[0]["score"] > 5.0

    # Now force the shared predicate to report "not live" and assert BOTH stop.
    monkeypatch.setattr(gs, "pull_signal_live_age_hours", lambda *a, **k: None)
    assert "g-test-pull" not in _candidate_ids(_rec_goal(pull_signal=sig))
    scored2 = [{"goal_id": "g-test-pull", "score": 5.0, "pull_signal": sig}]
    gs.apply_pull_boost(scored2, gs.PULL_CONFIG)
    assert scored2[0]["score"] == 5.0
