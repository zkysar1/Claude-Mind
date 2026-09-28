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


# --------------------------------------------------------------------------
# Pass memory in cadence_signals.py (): a record the goal's last
# completed pass already saw and HELD does not re-fire the signal until the
# goal's interval elapses;
# a record that became eligible after that pass fires it at once.
# Offsets are whole hours or days with wide margins, so no case straddles a
# date boundary whatever time of day the suite runs (guard-566).
# --------------------------------------------------------------------------

def _eval_with(monkeypatch, signal, goal, records):
    cadence_signals.clear_cache()
    monkeypatch.setattr(cadence_signals, "_iter_pipeline", lambda: iter(records))
    return cadence_signals.evaluate_cadence_signal(signal, goal)


def _due_rec(due_days_ago, formed_at=None, stage="active"):
    today = datetime.now().date()
    return {"id": "h-due", "stage": stage,
            "resolves_by": (today - timedelta(days=due_days_ago)).isoformat(),
            "formed_date": (today - timedelta(days=30)).isoformat(),
            "formed_at": formed_at or _iso(datetime.now() - timedelta(days=30))}


def _resolved_rec(resolved_hours_ago, outcome="CONFIRMED"):
    return {"id": "h-res", "stage": "resolved", "outcome": outcome, "reflected": False,
            "resolved_at": _iso(datetime.now() - timedelta(hours=resolved_hours_ago))}


def _passed(hours_ago, interval, gid):
    return _rec_goal(gid, interval_hours=interval,
                     lastAchievedAt=_iso(datetime.now() - timedelta(hours=hours_ago)))


RESOLVABLE = "resolvable_hypotheses_present"
UNREFLECTED = "unreflected_hypotheses_present"


def test_resolvable_held_record_seen_by_last_pass_does_not_fire(monkeypatch):
    # Due 2 days ago, formed 30 days ago; the pass 1h ago already saw it.
    goal = _passed(1, 60, "g-mem-r1")
    assert _eval_with(monkeypatch, RESOLVABLE, goal, [_due_rec(2)]) is False


def test_resolvable_held_record_refires_once_interval_elapsed(monkeypatch):
    goal = _passed(61, 60, "g-mem-r2")
    assert _eval_with(monkeypatch, RESOLVABLE, goal, [_due_rec(3)]) is True


def test_resolvable_record_due_after_last_pass_fires(monkeypatch):
    # The pass ran 49h ago (two or three dates back); the record fell due
    # yesterday, a date after the pass, so the pass could not have seen it.
    goal = _passed(49, 60, "g-mem-r3")
    assert _eval_with(monkeypatch, RESOLVABLE, goal, [_due_rec(1)]) is True


def test_resolvable_record_formed_after_last_pass_fires(monkeypatch):
    goal = _passed(2, 60, "g-mem-r4")
    fresh = _due_rec(5, formed_at=_iso(datetime.now() - timedelta(minutes=30)))
    assert _eval_with(monkeypatch, RESOLVABLE, goal, [fresh]) is True


def test_resolvable_no_due_record_stays_absent_past_the_floor(monkeypatch):
    goal = _passed(100, 60, "g-mem-r5")
    not_due = _due_rec(-3)  # resolves_by three days from now
    assert _eval_with(monkeypatch, RESOLVABLE, goal, [not_due]) is False


def test_no_usable_pass_stamp_behaves_as_before_memory(monkeypatch):
    # Never passed, or the stamp is the precondition sweep's shelve (not a pass).
    never = _rec_goal("g-mem-r6")
    assert _eval_with(monkeypatch, RESOLVABLE, never, [_due_rec(2)]) is True
    stamp = _iso(datetime.now() - timedelta(hours=1))
    shelved = _rec_goal("g-mem-r7", interval_hours=60, lastAchievedAt=stamp,
                        last_shelved_at=stamp)
    assert _eval_with(monkeypatch, RESOLVABLE, shelved, [_due_rec(2)]) is True


def test_unreflected_ignores_outcomes_that_cannot_be_reflected(monkeypatch):
    # No pass memory and the floor long elapsed: only the outcome filter decides.
    recs = [_resolved_rec(0.5, "UNRESOLVABLE"), _resolved_rec(0.5, "EXPIRED"),
            _resolved_rec(0.5, None)]
    assert _eval_with(monkeypatch, UNREFLECTED, _rec_goal("g-mem-u1"), recs) is False


def test_unreflected_held_record_seen_by_last_pass_does_not_fire(monkeypatch):
    # Resolved 5h ago; the pass 1h ago saw it and abstained (guard-5623).
    goal = _passed(1, 6.75, "g-mem-u2")
    assert _eval_with(monkeypatch, UNREFLECTED, goal, [_resolved_rec(5)]) is False


def test_unreflected_newly_resolved_reflectable_record_fires(monkeypatch):
    goal = _passed(1, 6.75, "g-mem-u3")
    assert _eval_with(monkeypatch, UNREFLECTED, goal,
                      [_resolved_rec(5), _resolved_rec(0.5, "CORRECTED")]) is True


def test_unreflected_held_record_refires_once_interval_elapsed(monkeypatch):
    goal = _passed(7, 6.75, "g-mem-u4")
    assert _eval_with(monkeypatch, UNREFLECTED, goal, [_resolved_rec(9)]) is True


def test_verdict_is_cached_per_goal_not_per_signal(monkeypatch):
    records = [_due_rec(2)]
    cadence_signals.clear_cache()
    monkeypatch.setattr(cadence_signals, "_iter_pipeline", lambda: iter(records))
    held = _passed(1, 60, "g-mem-c1")
    fresh = _rec_goal("g-mem-c2")
    assert cadence_signals.evaluate_cadence_signal(RESOLVABLE, held) is False
    assert cadence_signals.evaluate_cadence_signal(RESOLVABLE, fresh) is True


def test_pipeline_is_read_once_per_process(monkeypatch):
    reads = []
    monkeypatch.setattr(cadence_signals, "_read_pipeline",
                        lambda: reads.append(1) or [{"id": "h-1"}])
    cadence_signals.clear_cache()
    assert [h["id"] for h in cadence_signals._iter_pipeline()] == ["h-1"]
    assert [h["id"] for h in cadence_signals._iter_pipeline()] == ["h-1"]
    assert len(reads) == 1
    cadence_signals.clear_cache()
    list(cadence_signals._iter_pipeline())
    assert len(reads) == 2


def test_interval_hours_mirrors_goal_selector():
    for goal in ({"interval_hours": 60}, {"interval_hours": 1.995},
                 {"remind_days": 2}, {"interval_hours": 6.75, "remind_days": 9}, {}):
        assert cadence_signals._interval_hours(goal) == float(gs.get_interval_hours(goal))
