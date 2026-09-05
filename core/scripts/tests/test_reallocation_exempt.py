"""Reallocation-exemption predicate tests (, reallocation-exempt-gate).

The defect: goal-selector.py DELIBERATELY surfaces a cross-lane goal when the
reallocation condition holds, and the daemon claim endpoint then REFUSED that
very goal with `cross_lane_refused` — a rescue path that surfaced work it could
not deliver. The components never disagreed about POLICY; the daemon could not
EXPRESS the selector's exception. `gates.reallocation_exempt` is that expression,
imported by both, so the two cannot desync.

Each test pins one property whose loss re-opens the defect or opens a WORSE one.
The asymmetry that shapes every assertion below: wrongly WITHHOLDING an exemption
costs nothing new (the claim falls back to today's refusal), while wrongly
GRANTING one yanks work from a live peer. So every unreadable input must close
the door, and the tests assert that direction explicitly rather than trusting it.

Daemon-safe: pure dict arithmetic over the predicate. No daemon, no store reads,
no `daemon_integration` marker. Liveness is never probed here because no test
nominates an agent by AGE — `idle_agents` is passed directly.

Run:
  STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_reallocation_exempt.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

from gates.reallocation_exempt import (  # noqa: E402
    evaluate, idle_agents, is_owner_scoped_goal, recurring_cadence_stranded)


# --------------------------------------------------------------------------
# The two directions build constraint 4 names explicitly.
# --------------------------------------------------------------------------

def test_dormant_target_claims_cleanly():
    """DIRECTION 1. The exempt case. A goal routed to a verifiably-unreachable
    agent, unclaimed and not owner-scoped, IS exempt — so the claim endpoint
    lets it through with no override and no ledger entry. This is the whole
    point: before the gate, the selector ranked this goal #1 and the endpoint
    refused it, every iteration."""
    out = evaluate({"title": "ordinary work"}, intended_agent="foxtrot",
                   idle_agents={"foxtrot"}, cadence_threshold=2.0)

    assert out["exempt"] is True
    assert out["door"] == "idle"
    assert out["failed"] == []


def test_live_target_still_refuses():
    """DIRECTION 2. The non-exempt case, and the one that protects a live peer.
    An agent absent from the idle set is NOT unreachable, so its routed work
    stays its own — the endpoint falls back to the pre-existing refusal."""
    out = evaluate({"title": "ordinary work"}, intended_agent="bravo",
                   idle_agents={"foxtrot"}, cadence_threshold=2.0)

    assert out["exempt"] is False
    assert out["door"] is None
    assert "target-neither-idle-nor-cadence-stranded" in out["failed"]


# --------------------------------------------------------------------------
# Conjuncts. Each is load-bearing on its own.
# --------------------------------------------------------------------------

def test_owner_scoped_goal_never_exempt_even_when_target_is_idle():
    """An owner-scoped goal is UNEXECUTABLE by a reallocatee whatever the
    target's liveness — running /drain-temp as another agent drains the wrong
    agent's temp. Exempting it would strand the reallocatee's top-of-queue on
    work it cannot do (rb-4792)."""
    out = evaluate({"skill": "/drain-temp", "title": "drain"},
                   intended_agent="foxtrot", idle_agents={"foxtrot"},
                   cadence_threshold=2.0)

    assert out["exempt"] is False
    assert "owner-scoped" in out["failed"]


def test_already_claimed_goal_never_exempt():
    """A goal an active peer already owns is never yanked away, however
    unreachable its intended_agent looks."""
    out = evaluate({"title": "ordinary", "claimed_by": "zeta"},
                   intended_agent="foxtrot", idle_agents={"foxtrot"},
                   cadence_threshold=2.0)

    assert out["exempt"] is False
    assert "already-claimed" in out["failed"]


def test_cadence_door_opens_without_any_idleness():
    """The SECOND door (, landed 2026-09-03 — AFTER this goal's design
    note was written, which is why the note's single-door predicate is stale). A
    recurring goal far past its interval is stranded on a LIVE but busy owner
    that never ranks it, and for cadence purposes that is indistinguishable from
    a dormant one. idle_agents is EMPTY here on purpose."""
    goal = {"title": "recurring sweep", "recurring": True,
            "interval_hours": 6, "lastAchievedAt": "2026-01-01T00:00:00"}

    out = evaluate(goal, intended_agent="bravo", idle_agents=set(),
                   cadence_threshold=2.0)

    assert out["exempt"] is True
    assert out["door"] == "cadence"


# --------------------------------------------------------------------------
# Fail-safe direction. Every unreadable input must CLOSE the door.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("threshold", [None, "junk", ""])
def test_unreadable_cadence_threshold_closes_the_door(threshold):
    """guard-3024: a number feeding a protective cutoff must fail toward keeping
    the protection ON. An unreadable config must never become a fleet-wide
    exemption — so the cadence door shuts, it does not default open."""
    goal = {"title": "recurring", "recurring": True, "interval_hours": 6,
            "lastAchievedAt": "2026-01-01T00:00:00"}

    assert recurring_cadence_stranded(goal, cadence_threshold=threshold) is False
    out = evaluate(goal, intended_agent="bravo", idle_agents=set(),
                   cadence_threshold=threshold)
    assert out["exempt"] is False


def test_idle_agents_empty_when_reallocation_disabled():
    """reallocation_hours=None means the mechanism is OFF. No rows may be
    nominated, whatever team-state says."""
    rows = {"foxtrot": {"last_active": "2020-01-01T00:00:00"}}

    assert idle_agents(rows, reallocation_hours=None,
                       confirm=lambda n, la: True) == set()


def test_unparseable_last_active_is_not_idle():
    """A missing or corrupt `last_active` is NOT evidence of death — it is
    absence of evidence. Keep routing (check-team-state-before-silent rule 5)."""
    rows = {"a": {"last_active": "not-a-timestamp"}, "b": {}, "c": "not-a-dict"}

    # confirm would say True for anything -- but no row is even NOMINATED,
    # so it is never consulted. Age nominates; confirm only decides.
    assert idle_agents(rows, reallocation_hours=8,
                       confirm=lambda n, la: True) == set()


# --------------------------------------------------------------------------
# Diagnosis quality — the audit trail's whole value (guard-3644).
# --------------------------------------------------------------------------

def test_every_failed_conjunct_is_reported_not_just_the_first():
    """An AND-predicate that short-circuits reports the CHEAPEST failing
    condition, which is almost never the decisive one. The gate-firings record
    is only worth writing if it says which conjuncts actually failed, so all
    three must appear together."""
    out = evaluate({"skill": "/drain-temp", "title": "drain",
                    "claimed_by": "zeta"},
                   intended_agent="bravo", idle_agents=set(),
                   cadence_threshold=2.0)

    assert out["exempt"] is False
    assert set(out["failed"]) == {"target-neither-idle-nor-cadence-stranded",
                                  "already-claimed", "owner-scoped"}


def test_owner_scoped_matcher_does_not_fire_on_goals_merely_mentioning_drain():
    """The positive-signature matcher (). The old substring fallback
    marked any goal MENTIONING temp-drain as owner-scoped, wrongly stranding
    analysis goals about the drain with a dormant owner."""
    assert is_owner_scoped_goal(
        {"title": "Investigate: why the temp drain goal keeps re-filing"}) is False
    assert is_owner_scoped_goal(
        {"title": "Maintain: drain 12 accumulated temp/ working docs"}) is True


def test_confirm_decides_and_age_only_nominates():
    """A stale `last_active` NOMINATES an agent; only `confirm` may promote it.
    This is the seam that keeps a broken heartbeat writer from being read as a
    dead agent — two live agents once read 59h and 66h stale. A confirm that
    refuses must veto a very old timestamp."""
    rows = {"foxtrot": {"last_active": "2020-01-01T00:00:00"}}

    assert idle_agents(rows, reallocation_hours=8,
                       confirm=lambda n, la: False) == set()
    assert idle_agents(rows, reallocation_hours=8,
                       confirm=lambda n, la: True) == {"foxtrot"}
