"""test_goal_selector_pull_signal_eligibility.py -- , CONSUMER half.

Pins the EVENT-keyed bypass in goal-selector.collect_candidates' recurring
interval gate: a not-yet-due recurring goal carrying a LIVE `pull_signal` is
ELIGIBLE, so `apply_pull_boost` gets a candidate to lift.

WHY A SECOND CONSUMER TEST EXISTS. `apply_pull_boost` shipped 2026-08-17 with 20
green pins and `test_pull_signal_producer.py` later pinned the producer->consumer
JOIN -- and the mechanism was STILL inert for its first real signal, because both
of those exercise SCORING. `apply_pull_boost` runs on `scored`, and a not-yet-due
recurring goal is dropped by the interval gate inside `collect_candidates`
BEFORE anything is scored. `overdue_exemption_level(ratio, interval_hours,
config)` takes no goal and so cannot see `pull_signal` at all, which is why the
design's "fully overdue-exempt" half never shipped. MEASURED 2026-08-28:
g-306-284 carried a live pull_signal for 1h50m (producer healthy -- set by
alpha/cc-08 with a carrier ref) and was ABSENT from BOTH bravo's 1137-goal and
alpha's 1372-goal candidate sets. Absence, not a low rank: no score-side pin
could have caught it, and none did.

So the family is: producer (test_pull_signal_producer) -> ELIGIBILITY (here) ->
LIFT (test_goal_selector_pull_boost*). Three links, and the wire is only live
when all three hold.

Shape mirrors test_cadence_signal_gate.py -- the sibling bypass on the same gate,
same function, same `_rec_goal`/`_candidate_ids` harness. Timestamps are computed
DYNAMICALLY (now - delta) per guard-566/guard-4364: an absolute stamp here would
expire against max_age_hours exactly as it did in the producer file, which is the
defect that took the join tests red for four days.
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


MAX_AGE = 24.0
ENABLED = {"enabled": True, "boost": 4.0, "max_age_hours": MAX_AGE}
DISABLED = {"enabled": False, "boost": 4.0, "max_age_hours": MAX_AGE}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _sig(hours_ago):
    """A producer-shaped signal aged `hours_ago` against the REAL clock."""
    return {
        "set_at": _iso(datetime.now() - timedelta(hours=hours_ago)),
        "by": "alpha/cc-08",
        "reason": "carrier ref 281160376, 1 framework file(s)",
    }


def _rec_goal(gid="g-test-pull", *, hours_since_achieved=2.1, **overrides):
    """A recurring goal WITHIN its interval gate -- i.e. not yet due.

    The defaults reproduce the measured g-306-284 shape: interval 4.45h with
    2.1h elapsed, so the legacy gate drops it and only the bypass can admit it.
    """
    g = {
        "id": gid,
        "title": "Recurring: synthetic pull-signal test goal",
        "status": "pending",
        "priority": "MEDIUM",
        "participants": ["agent"],
        "recurring": True,
        "interval_hours": 4.45,
        "lastAchievedAt": _iso(datetime.now() - timedelta(hours=hours_since_achieved)),
    }
    g.update(overrides)
    return g


def _candidate_ids(goal):
    asp = {"id": "asp-test", "status": "active", "priority": "MEDIUM", "goals": [goal]}
    return {r["goal"]["id"] for r in gs.collect_candidates([asp], source="agent")}


# ------------------------------------------------------- the bypass itself

def test_live_pull_signal_admits_a_not_yet_due_recurring_goal(monkeypatch):
    """THE REGRESSION TEST. Live signal + inside the interval gate -> candidate.

    This is the exact g-306-284 condition that was absent from two agents'
    candidate sets for 1h50m.
    """
    monkeypatch.setattr(gs, "PULL_CONFIG", ENABLED)
    assert "g-test-pull" in _candidate_ids(_rec_goal(pull_signal=_sig(1.87)))


def test_NEGATIVE_CONTROL_no_pull_signal_is_still_filtered(monkeypatch):
    """guard-3221's mandated control, and the no-regression proof.

    Same goal, same gate, signal absent -> still dropped. Without this the test
    above would pass against any change that widened the gate for everything,
    which is the failure mode that would silently flood candidacy with every
    not-yet-due recurring goal in the store.
    """
    monkeypatch.setattr(gs, "PULL_CONFIG", ENABLED)
    assert "g-test-pull" not in _candidate_ids(_rec_goal())


def test_stale_pull_signal_does_not_admit(monkeypatch):
    """Eligibility and LIFT must share one boundary.

    A signal past max_age_hours is one `apply_pull_boost` would refuse to
    honour, so admitting it would put an unliftable goal into candidacy --
    producer and consumer drifting apart, the exact defect the shared
    `pull_signal_producer.is_live` predicate exists to prevent.
    """
    monkeypatch.setattr(gs, "PULL_CONFIG", ENABLED)
    assert "g-test-pull" not in _candidate_ids(_rec_goal(pull_signal=_sig(MAX_AGE + 1)))


def test_cleared_pull_signal_does_not_admit(monkeypatch):
    """The producer's CLEAR round-trips to None; a null signal must not admit."""
    monkeypatch.setattr(gs, "PULL_CONFIG", ENABLED)
    assert "g-test-pull" not in _candidate_ids(_rec_goal(pull_signal=None))


def test_disabled_config_disables_the_bypass_too(monkeypatch):
    """Matches apply_pull_boost's own early return: turning the mechanism off
    must turn BOTH halves off, or disabling it would leave eligibility widened
    with no boost to justify it."""
    monkeypatch.setattr(gs, "PULL_CONFIG", DISABLED)
    assert "g-test-pull" not in _candidate_ids(_rec_goal(pull_signal=_sig(1.87)))


# --------------------------------------------------- the legacy path is intact

def test_legacy_past_the_interval_still_fires_without_any_signal(monkeypatch):
    """Backwards-compat: the ordinary due-by-time path is untouched."""
    monkeypatch.setattr(gs, "PULL_CONFIG", ENABLED)
    assert "g-test-pull" in _candidate_ids(_rec_goal(hours_since_achieved=48))
