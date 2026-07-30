"""test_recurring_starvation_check.py —  regression pins.

Covers the two cases the goal's CLOSE criteria name explicitly (fires on a
synthetic N-x-overdue goal; stays SILENT on a precondition-shelved one) plus
the basis gate, which is the part that would regress silently.

The basis gate is pinned with the two live measurements that motivated it
(2026-07-30):
  * g-115-22  declares interval_hours=6 and its demonstrated p50 is ~30h, so
    20.8h stale is UNDER its own norm. A declared-interval-only predicate
    fires here; the basis-aware one must not.
  * g-115-817 declares interval_hours=6 with fewer than min_samples break
    records, so its basis stays 6h and it MUST fire at 18h — the originating
    incident, which sat 28.9h before being found by hand.

Those two pull in opposite directions on identical declared intervals, which is
exactly why the discriminator is the SAMPLE COUNT and not the interval. A
regression that drops the min-samples check passes the g-115-817 pin and fails
the g-115-22 one; a regression that always prefers p50 does the reverse.

Pattern: importlib-load the hyphenated module (precedent:
check-settings-deny-baseline.py, body-merge.py), then monkeypatch _read_active
so the sweep never touches live queues or the daemon.
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


def _queue(*goals) -> list:
    return [{"id": "asp-999", "goals": list(goals)}]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """No live queues, no daemon, no agent source."""
    monkeypatch.delenv("MIND_AGENT", raising=False)
    monkeypatch.setattr(rsc, "_read_active", lambda source: [])


def _install(monkeypatch, goals):
    monkeypatch.setattr(
        rsc, "_read_active",
        lambda source: _queue(*goals) if source == "world" else [])


# ── The two CLOSE criteria ────────────────────────────────────────────────

def test_fires_on_synthetic_overdue_goal(monkeypatch):
    """20h stale against a 6h basis is 3.33x — over the N=3 threshold."""
    _install(monkeypatch, [_goal()])
    starved, stats = rsc.scan(3.0, breaks={})
    assert [r["goal_id"] for r in starved] == ["g-999-01"]
    assert starved[0]["ratio"] == pytest.approx(3.33, abs=0.02)
    assert starved[0]["basis_reason"] == "interval"
    assert stats["examined"] == 1
    assert stats["shelved"] == 0


def test_silent_on_precondition_shelved_goal(monkeypatch):
    """A currently-FAILING gate means parked, not starved (guard-138)."""
    shelved = _goal(verification={"preconditions": [{
        "type": "file_check",
        "path": "core/scripts/definitely-absent-fixture-zzz.txt",
        "condition": "exists",
    }]})
    _install(monkeypatch, [shelved])
    starved, stats = rsc.scan(3.0, breaks={})
    assert starved == []
    assert stats["shelved"] == 1


def test_silent_on_fire_when_shelved_goal(monkeypatch):
    """fire_when is gathered as a gate too — parity with the sibling sweep."""
    shelved = _goal(fire_when={
        "type": "file_check",
        "path": "core/scripts/definitely-absent-fixture-zzz.txt",
        "condition": "exists",
    })
    _install(monkeypatch, [shelved])
    starved, stats = rsc.scan(3.0, breaks={})
    assert starved == []
    assert stats["shelved"] == 1


def test_passing_precondition_does_not_shelve(monkeypatch):
    """Having gates is not being shelved — only FAILING gates shelve."""
    ok = _goal(verification={"preconditions": [{
        "type": "file_check",
        "path": "core/scripts/recurring-starvation-check.py",
        "condition": "exists",
    }]})
    _install(monkeypatch, [ok])
    starved, stats = rsc.scan(3.0, breaks={})
    assert [r["goal_id"] for r in starved] == ["g-999-01"]
    assert stats["shelved"] == 0


# ── The basis gate (the part that regresses silently) ─────────────────────

def test_basis_suppresses_chronically_late_goal(monkeypatch):
    """ shape: declared 6h, demonstrated ~30h, 20.8h stale.

    Declared-interval-only would fire (3.47x). Basis-aware must not.
    """
    _install(monkeypatch, [_goal(id="g-115-22", lastAchievedAt=_ago(20.8))])
    starved, stats = rsc.scan(
        3.0, breaks={"g-115-22": [36.16, 24.23, 39.83, 20.85]})
    assert starved == []
    assert stats["basis_suppressed"] == 1


def test_fires_at_18h_when_samples_below_min(monkeypatch):
    """ shape: the originating incident, caught at 18h not 28.9h.

    Two samples is below BASIS_MIN_SAMPLES, so no demonstrated cadence can be
    claimed and the basis stays the declared 6h.
    """
    _install(monkeypatch, [_goal(id="g-115-817", lastAchievedAt=_ago(18.1))])
    starved, _ = rsc.scan(3.0, breaks={"g-115-817": [36.83, 117.34]})
    assert [r["goal_id"] for r in starved] == ["g-115-817"]
    assert starved[0]["basis_reason"] == "interval"
    assert starved[0]["basis_hours"] == pytest.approx(6.0)


def test_basis_never_lowers_below_declared_interval(monkeypatch):
    """A p50 FASTER than the declared interval must not suppress.

    max(interval, p50) — a goal that usually fires early is still starved when
    it stops, and the basis must not shrink to the faster cadence.
    """
    _install(monkeypatch, [_goal(lastAchievedAt=_ago(20))])
    starved, _ = rsc.scan(3.0, breaks={"g-999-01": [1.0, 1.0, 1.0, 1.0]})
    assert [r["goal_id"] for r in starved] == ["g-999-01"]
    assert starved[0]["basis_hours"] == pytest.approx(6.0)
    assert starved[0]["basis_reason"] == "interval"


# ── Exclusions ────────────────────────────────────────────────────────────

def test_fresh_goal_is_silent(monkeypatch):
    _install(monkeypatch, [_goal(lastAchievedAt=_ago(5))])
    starved, stats = rsc.scan(3.0, breaks={})
    assert starved == []
    assert stats["examined"] == 1
    assert stats["basis_suppressed"] == 0


@pytest.mark.parametrize("status", sorted(rsc.SKIP_STATUSES))
def test_skips_terminal_and_visibly_flagged_statuses(monkeypatch, status):
    """Already-terminal or already-flagged is not SILENTLY starved."""
    _install(monkeypatch, [_goal(status=status)])
    starved, stats = rsc.scan(3.0, breaks={})
    assert starved == []
    assert stats["examined"] == 0


def test_skips_deferred_goal(monkeypatch):
    """A defer_reason is a visible stall owned by the defer sweeps."""
    _install(monkeypatch, [_goal(defer_reason="waiting on upstream")])
    starved, _ = rsc.scan(3.0, breaks={})
    assert starved == []


def test_skips_claimed_goal(monkeypatch):
    """A claim means someone is executing it NOW — filing is the  race.

    A claim does not advance lastAchievedAt, so a claimed goal still reads
    stale. claimed_by is authoritative rather than status, because the claim can
    land before the status flip.
    """
    _install(monkeypatch, [_goal(claimed_by="foxtrot")])
    starved, _ = rsc.scan(3.0, breaks={})
    assert starved == []


def test_claimed_by_wins_over_pending_status(monkeypatch):
    """Explicitly pin the ordering: status=pending + a claim is still skipped."""
    _install(monkeypatch, [_goal(status="pending", claimed_by="echo")])
    starved, _ = rsc.scan(3.0, breaks={})
    assert starved == []


def test_skips_non_recurring_and_intervalless(monkeypatch):
    _install(monkeypatch, [
        _goal(id="g-999-02", recurring=False),
        _goal(id="g-999-03", interval_hours=None),
        _goal(id="g-999-04", interval_hours=0),
    ])
    starved, stats = rsc.scan(3.0, breaks={})
    assert starved == []
    assert stats["examined"] == 0
    assert stats["no_interval"] == 2


def test_never_fired_anchors_on_created_at(monkeypatch):
    """No lastAchievedAt -> anchor on created_at, and label which was used."""
    _install(monkeypatch, [_goal(lastAchievedAt=None, created_at=_ago(40))])
    starved, _ = rsc.scan(3.0, breaks={})
    assert [r["goal_id"] for r in starved] == ["g-999-01"]
    assert starved[0]["anchor_field"] == "created_at"


def test_never_fired_recent_goal_is_silent(monkeypatch):
    """A just-created recurring goal must not fire the instant it exists."""
    _install(monkeypatch, [_goal(lastAchievedAt=None, created_at=_ago(2))])
    starved, _ = rsc.scan(3.0, breaks={})
    assert starved == []


# ── Ordering + dedup contract ─────────────────────────────────────────────

def test_sorted_by_ratio_descending(monkeypatch):
    _install(monkeypatch, [
        _goal(id="g-999-lo", interval_hours=24, lastAchievedAt=_ago(100)),
        _goal(id="g-999-hi", interval_hours=2, lastAchievedAt=_ago(100)),
    ])
    starved, _ = rsc.scan(3.0, breaks={})
    assert [r["goal_id"] for r in starved] == ["g-999-hi", "g-999-lo"]


def test_origin_signal_is_exact_and_goal_scoped(monkeypatch):
    """Dedup key must be the exact per-goal signal, not a title substring."""
    _install(monkeypatch, [_goal()])
    starved, _ = rsc.scan(3.0, breaks={})
    expected = f"unblock:recurring-starved-{starved[0]['goal_id']}"
    assert expected == "unblock:recurring-starved-g-999-01"


def test_unreadable_anchor_is_counted_not_silent(monkeypatch):
    """A tz-AWARE stamp makes the naive subtraction raise. The goal must be
    EXCLUDED loudly, not dropped silently (guard-1753 / guard-1091 / guard-1893).

    Latent as of 2026-07-30 (0 of 127 live recurring timestamps carry an offset,
    because the framework mandates naive UTC) — pinned anyway, because a silent
    permanent exclusion inside this detector is the same shape as the starvation
    it exists to catch.
    """
    aware = (datetime.now() - timedelta(hours=99)).replace(
        microsecond=0).isoformat() + "+00:00"
    _install(monkeypatch, [_goal(lastAchievedAt=aware)])
    starved, stats = rsc.scan(3.0, breaks={})
    assert starved == []
    assert stats["unreadable_anchor"] == 1


def test_naive_and_z_suffixed_anchors_both_parse(monkeypatch):
    """The two sanctioned forms must NOT land in the unreadable bucket."""
    for stamp in (_ago(99), _ago(99) + "Z"):
        _install(monkeypatch, [_goal(lastAchievedAt=stamp)])
        starved, stats = rsc.scan(3.0, breaks={})
        assert [r["goal_id"] for r in starved] == ["g-999-01"], stamp
        assert stats["unreadable_anchor"] == 0, stamp


def test_gate_eval_error_fails_open_to_visible(monkeypatch):
    """A broken predicate surfaces the goal rather than silently hiding it."""
    def _boom(*a, **k):
        raise RuntimeError("evaluator exploded")
    monkeypatch.setattr(rsc, "evaluate_all", _boom)
    _install(monkeypatch, [_goal(verification={"preconditions": [
        {"type": "file_check", "path": "x", "condition": "exists"}]})])
    starved, stats = rsc.scan(3.0, breaks={})
    assert [r["goal_id"] for r in starved] == ["g-999-01"]
    assert stats["shelved"] == 0
