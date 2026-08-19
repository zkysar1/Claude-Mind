"""test_recurring_starvation_cadence_parked.py —  regression pins.

Suppression reason 3: a recurring goal carrying `cadence_signal` is skipped by
`goal-selector.collect_candidates` while the signal is ABSENT — outright for a
pure gate, or until `cadence_fallback_days` elapses for a HYBRID. Such a goal is
deliberately parked, not starved, and before this branch the detector filed a
HIGH Unblock against it on every sweep forever.

Reason 2 (SHELVED) cannot cover this: `_structured_gates` reads
verification.preconditions/fire_when, and the live goals in this class express
their parking in `cadence_signal` plus a STRING precondition — both invisible to
`predicate.evaluate_all`. Measured on the live corpus 2026-08-12: `shelved` was
0 across 80 examined goals while two goals sat in exactly this state.

The pins pull in OPPOSITE directions on an identical `cadence_signal`, which is
why the discriminator is the FALLBACK FLOOR and not the presence of the field:
  * zeta's g-001-07 — 40.8h stale against a declared 1.33h (30.7x) while inside
    a 3d floor. MUST stay silent. A predicate that ignores cadence_signal fires
    here; that firing is the originating incident (g-001-81).
  * the same goal past its floor — MUST fire. A regression that suppresses on
    the mere PRESENCE of cadence_signal passes the first pin and fails this one,
    silently hiding a genuinely overdue goal forever.

Pattern follows test_recurring_starvation_check.py: importlib-load the
hyphenated module, monkeypatch `_read_active` so the sweep never touches live
queues or the daemon, and monkeypatch `evaluate_cadence_signal` in the module
namespace so no probe reads real working memory.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_spec = importlib.util.spec_from_file_location(
    "recurring_starvation_check",
    str(SCRIPT_DIR / "recurring-starvation-check.py"),
)
rsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rsc)


def _ago(hours: float) -> str:
    return (datetime.now() - timedelta(hours=hours)).replace(
        microsecond=0).isoformat()


def _goal(**over) -> dict:
    g = {
        "id": "g-999-01",
        "title": "Recurring: synthetic sweep",
        "recurring": True,
        "status": "pending",
        "interval_hours": 6,
        "lastAchievedAt": _ago(20),
    }
    g.update(over)
    return g


def _live_shape(**over) -> dict:
    """zeta's  as measured 2026-08-12 (the originating incident)."""
    g = _goal(
        id="g-001-07",
        title="Flush encoding queue to knowledge tree",
        interval_hours=1.33,
        lastAchievedAt=_ago(40.8),
        cadence_signal="encoding_queue_nonempty",
        cadence_fallback_days=3,
        # String precondition — invisible to predicate.evaluate_all, which is
        # why reason 2 could never suppress this goal.
        verification={"preconditions": [
            "Working memory encoding_queue exists and has items"]},
    )
    g.update(over)
    return g


def _queue(*goals) -> list:
    return [{"id": "asp-001", "goals": list(goals)}]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """No live queues, no daemon, no agent source, no real WM probe."""
    monkeypatch.delenv("MIND_AGENT", raising=False)
    monkeypatch.setattr(rsc, "_read_active", lambda source: [])


def _install(monkeypatch, goals, signal_present):
    monkeypatch.setattr(
        rsc, "_read_active",
        lambda source: _queue(*goals) if source == "world" else [])
    monkeypatch.setattr(
        rsc, "evaluate_cadence_signal",
        lambda name, goal=None: signal_present)


# ── The two pins that pull in opposite directions ────────────────────────

def test_silent_on_hybrid_inside_fallback_floor(monkeypatch):
    """The originating incident: 30.7x declared, but inside its own 3d floor."""
    _install(monkeypatch, [_live_shape()], signal_present=False)
    starved, stats = rsc.scan(3.0, breaks={})
    assert starved == []
    assert stats["cadence_parked"] == 1
    # Reason 2 must NOT be what suppressed it — the parking is invisible there.
    assert stats["shelved"] == 0


def test_fires_when_hybrid_fallback_floor_HAS_elapsed(monkeypatch):
    """Past the 3d floor the selector fires it, so a stale goal is real."""
    _install(monkeypatch, [_live_shape(lastAchievedAt=_ago(100))],
             signal_present=False)
    starved, stats = rsc.scan(3.0, breaks={})
    assert [r["goal_id"] for r in starved] == ["g-001-07"]
    assert stats["cadence_parked"] == 0


# ── Pure signal-gate (no fallback) ───────────────────────────────────────

def test_silent_on_pure_signal_gate_while_signal_absent(monkeypatch):
    """No fallback_days -> the selector skips outright while the signal is off."""
    _install(monkeypatch, [_goal(cadence_signal="encoding_queue_nonempty")],
             signal_present=False)
    starved, stats = rsc.scan(3.0, breaks={})
    assert starved == []
    assert stats["cadence_parked"] == 1


def test_fires_when_signal_is_PRESENT(monkeypatch):
    """Signal present -> the selector fires it, so staleness is genuine."""
    _install(monkeypatch, [_goal(cadence_signal="encoding_queue_nonempty")],
             signal_present=True)
    starved, stats = rsc.scan(3.0, breaks={})
    assert [r["goal_id"] for r in starved] == ["g-999-01"]
    assert stats["cadence_parked"] == 0


# ── Fail-open and backwards-compat ───────────────────────────────────────

def test_probe_exception_fails_open_to_reporting(monkeypatch):
    """A broken probe surfaces the goal rather than silently hiding it."""
    def _boom(name, goal=None):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(
        rsc, "_read_active",
        lambda source: _queue(_goal(cadence_signal="x")) if source == "world" else [])
    monkeypatch.setattr(rsc, "evaluate_cadence_signal", _boom)
    starved, stats = rsc.scan(3.0, breaks={})
    assert [r["goal_id"] for r in starved] == ["g-999-01"]
    assert stats["cadence_parked"] == 0


def test_unreadable_fallback_days_fails_open_to_reporting(monkeypatch):
    """A junk cadence_fallback_days must not silently suppress the goal."""
    _install(monkeypatch, [_goal(cadence_signal="x", cadence_fallback_days="soon")],
             signal_present=False)
    starved, stats = rsc.scan(3.0, breaks={})
    assert [r["goal_id"] for r in starved] == ["g-999-01"]
    assert stats["cadence_parked"] == 0


def test_goal_without_cadence_signal_is_untouched(monkeypatch):
    """Backwards-compat: 117 of 121 live recurring goals carry no signal."""
    _install(monkeypatch, [_goal()], signal_present=False)
    starved, stats = rsc.scan(3.0, breaks={})
    assert [r["goal_id"] for r in starved] == ["g-999-01"]
    assert stats["cadence_parked"] == 0
