"""test_cadence_signal_gate.py --  signal-gated recurring cadence.

Exercises the additive `cadence_signal` filter in goal-selector.collect_candidates
(design g-303-16) plus the cadence_signals.evaluate_cadence_signal dispatch.

Core contract (the goal's sandbox outcome): for a recurring goal carrying a
`cadence_signal`, signal ABSENT -> filtered out of candidacy ("skip"); signal
PRESENT -> a candidate ("fire"), bypassing the hour-interval gate. Goals WITHOUT
`cadence_signal` keep the legacy time gate (backwards-compat). Hybrid goals (with
`cadence_fallback_days`) fire on signal OR after the day-floor.

Integration cases monkeypatch gs.evaluate_cadence_signal so the selector path is
exercised WITHOUT touching wm.py / pipeline I/O (guard-862). Module cases inject
a probe into SIGNAL_REGISTRY to exercise the real dispatch + fail-open.

Import pattern mirrors test_goal_selector_never_fired_recurring.py: capture and
restore MIND_AGENT around the module-level import. Timestamps are computed
DYNAMICALLY (now - delta) per guard-566.
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
cadence_signals = importlib.import_module("cadence_signals")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _rec_goal(gid="g-test-sig", **overrides):
    """A recurring agent goal minimal enough to reach the recurring gate."""
    g = {
        "id": gid,
        "title": "Recurring: synthetic signal-gated test goal",
        "status": "pending",
        "priority": "MEDIUM",
        "participants": ["agent"],
        "recurring": True,
        "interval_hours": 24,
    }
    g.update(overrides)
    return g


def _candidate_ids(goal):
    asp = {"id": "asp-test", "status": "active", "priority": "MEDIUM", "goals": [goal]}
    results = gs.collect_candidates([asp], source="agent")
    return {r["goal"]["id"] for r in results}


# --------------------------------------------------------------------------
# Integration: the selector filter (gs.evaluate_cadence_signal monkeypatched)
# --------------------------------------------------------------------------

def test_pure_gate_signal_absent_is_skipped(monkeypatch):
    """Pure signal-gate, signal ABSENT, PAST time gate -> skipped (no fire)."""
    monkeypatch.setattr(gs, "evaluate_cadence_signal", lambda *a, **k: False)
    g = _rec_goal(
        cadence_signal="encoding_queue_nonempty",
        lastAchievedAt=_iso(datetime.now() - timedelta(hours=48)),  # well past 24h gate
    )
    assert "g-test-sig" not in _candidate_ids(g)


def test_pure_gate_signal_present_fires_within_time_gate(monkeypatch):
    """Pure signal-gate, signal PRESENT, WITHIN time gate -> fires (bypasses gate)."""
    monkeypatch.setattr(gs, "evaluate_cadence_signal", lambda *a, **k: True)
    g = _rec_goal(
        cadence_signal="encoding_queue_nonempty",
        lastAchievedAt=_iso(datetime.now() - timedelta(minutes=1)),  # within 24h gate
    )
    assert "g-test-sig" in _candidate_ids(g)


def test_hybrid_signal_absent_within_fallback_is_skipped(monkeypatch):
    """Hybrid, signal ABSENT, within the N-day fallback floor -> skipped."""
    monkeypatch.setattr(gs, "evaluate_cadence_signal", lambda *a, **k: False)
    g = _rec_goal(
        cadence_signal="unreflected_hypotheses_present",
        cadence_fallback_days=7,
        lastAchievedAt=_iso(datetime.now() - timedelta(days=2)),  # within 7d fallback
    )
    assert "g-test-sig" not in _candidate_ids(g)


def test_hybrid_signal_absent_past_fallback_fires(monkeypatch):
    """Hybrid, signal ABSENT, PAST the N-day fallback floor -> fires (safety floor)."""
    monkeypatch.setattr(gs, "evaluate_cadence_signal", lambda *a, **k: False)
    g = _rec_goal(
        cadence_signal="unreflected_hypotheses_present",
        cadence_fallback_days=7,
        lastAchievedAt=_iso(datetime.now() - timedelta(days=10)),  # past 7d fallback
    )
    assert "g-test-sig" in _candidate_ids(g)


def test_legacy_no_signal_within_gate_skipped(monkeypatch):
    """Backwards-compat: no cadence_signal, within time gate -> legacy skip."""
    # Make the signal evaluator explode if called -- it must NOT be reached for
    # a goal without cadence_signal.
    monkeypatch.setattr(gs, "evaluate_cadence_signal",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    g = _rec_goal(lastAchievedAt=_iso(datetime.now() - timedelta(hours=1)))  # within 24h
    assert "g-test-sig" not in _candidate_ids(g)


def test_legacy_no_signal_past_gate_fires(monkeypatch):
    """Backwards-compat: no cadence_signal, past time gate -> legacy fire."""
    monkeypatch.setattr(gs, "evaluate_cadence_signal",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    g = _rec_goal(lastAchievedAt=_iso(datetime.now() - timedelta(hours=48)))  # past 24h
    assert "g-test-sig" in _candidate_ids(g)


# --------------------------------------------------------------------------
# Module: evaluate_cadence_signal dispatch + fail-open
# --------------------------------------------------------------------------

def test_empty_signal_name_fails_open():
    cadence_signals.clear_cache()
    assert cadence_signals.evaluate_cadence_signal("", {}) is True
    assert cadence_signals.evaluate_cadence_signal(None, {}) is True


def test_unknown_signal_fails_open():
    cadence_signals.clear_cache()
    assert cadence_signals.evaluate_cadence_signal("no_such_signal_xyz", {}) is True


def test_registered_probe_present_and_absent(monkeypatch):
    cadence_signals.clear_cache()
    monkeypatch.setitem(cadence_signals.SIGNAL_REGISTRY, "t_present", lambda g: True)
    monkeypatch.setitem(cadence_signals.SIGNAL_REGISTRY, "t_absent", lambda g: False)
    assert cadence_signals.evaluate_cadence_signal("t_present", {}) is True
    assert cadence_signals.evaluate_cadence_signal("t_absent", {}) is False


def test_probe_exception_fails_open(monkeypatch):
    cadence_signals.clear_cache()

    def _boom(_g):
        raise RuntimeError("probe blew up")

    monkeypatch.setitem(cadence_signals.SIGNAL_REGISTRY, "t_boom", _boom)
    assert cadence_signals.evaluate_cadence_signal("t_boom", {}) is True
