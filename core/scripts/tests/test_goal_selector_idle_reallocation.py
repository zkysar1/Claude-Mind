"""test_goal_selector_idle_reallocation.py -- 6 gap #4.

Proves the intended_agent idle-reallocation: a goal routed (intended_agent) to
an agent that has gone idle beyond reallocation_hours AND is unclaimed falls
through the intended_agent filter so a running capable agent can pick up
otherwise-stranded work. Mirrors the reallocatable+reallocation_hours mechanism
but keyed on intended-agent idleness rather than the explicit reallocatable flag.

Root incident (2026-07-08, alpha MIND-only box): 15 framework goals routed to a
5.75-day-idle agent (zeta) vanished from BOTH the selectable AND the blocked
selector outputs (the intended_agent filter is a select-time drop, not a block),
so the running agent falsely concluded all-blocked while executable framework
work sat invisible. The idle-reallocation surfaces them to any running agent.

Fixture mirrors test_goal_selector_capability_filter.py: pin MIND_AGENT=alpha
around import; monkeypatch _load_team_state_cached to control agent liveness and
_get_runner_capabilities to neutralize the (orthogonal) capability filter.
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
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


def _goal(gid, intended=None, claimed_by=None):
    """Minimal pending, agent-eligible goal; optionally intended_agent-routed."""
    g = {
        "id": gid, "title": "goal %s" % gid, "status": "pending",
        "participants": ["agent"], "category": "test", "priority": "MEDIUM",
    }
    if intended is not None:
        g["intended_agent"] = intended
    if claimed_by is not None:
        g["claimed_by"] = claimed_by
    return g


def _asps(goals):
    return [{"id": "asp-test", "status": "active", "goals": goals}]


def _iso(hours_ago):
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _pin_team_state(monkeypatch, statuses):
    """statuses: {agent_name: hours_since_last_active}."""
    doc = {"agent_status": {n: {"last_active": _iso(h)} for n, h in statuses.items()}}
    monkeypatch.setattr(gs, "_load_team_state_cached", lambda: doc)


def _collect(monkeypatch, goals, reallocation_hours=8):
    """Collect world candidates with the capability filter neutralized."""
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: set())
    return {c["goal"]["id"] for c in gs.collect_candidates(
        _asps(goals), source="world", reallocation_hours=reallocation_hours)}


def test_idle_target_unclaimed_reallocates(monkeypatch):
    """Goal routed to an idle (200h) agent, unclaimed -> COLLECTED (the fix)."""
    _pin_team_state(monkeypatch, {"zeta": 200})
    ids = _collect(monkeypatch, [_goal("g-stranded", intended="zeta")])
    assert "g-stranded" in ids, "idle-routed unclaimed goal must reallocate/surface"


def test_active_target_stays_routed(monkeypatch):
    """Goal routed to a FRESH (1h) agent -> NOT collected (routing preserved)."""
    _pin_team_state(monkeypatch, {"zeta": 1})
    ids = _collect(monkeypatch, [_goal("g-routed", intended="zeta")])
    assert "g-routed" not in ids, "goal routed to an active agent must stay hidden"


def test_reallocation_disabled_preserves_status_quo(monkeypatch):
    """reallocation_hours=None (disabled) -> idle-routed goal stays hidden."""
    _pin_team_state(monkeypatch, {"zeta": 200})
    ids = _collect(monkeypatch, [_goal("g-stranded", intended="zeta")],
                   reallocation_hours=None)
    assert "g-stranded" not in ids, "reallocation disabled must preserve status-quo hiding"


def test_missing_last_active_not_idle(monkeypatch):
    """Target with no team-state record -> NOT idle -> goal stays routed
    (conservative: never surface on absent liveness evidence)."""
    monkeypatch.setattr(gs, "_load_team_state_cached", lambda: {"agent_status": {}})
    ids = _collect(monkeypatch, [_goal("g-routed", intended="zeta")])
    assert "g-routed" not in ids, "missing liveness evidence must NOT trigger reallocation"


def test_either_and_self_routed_unaffected(monkeypatch):
    """'either' and self-routed goals surface regardless of team-state (the
    intended_agent filter never dropped these; the fix must not change that)."""
    _pin_team_state(monkeypatch, {"zeta": 200})
    ids = _collect(monkeypatch, [_goal("g-either", intended="either"),
                                 _goal("g-self", intended="alpha")])
    assert "g-either" in ids and "g-self" in ids, ids


def test_mixed_only_idle_routed_surfaces(monkeypatch):
    """Mixed queue: idle-routed surfaces; active-routed stays hidden;
    'either' always surfaces -- the exact shape of the root incident."""
    _pin_team_state(monkeypatch, {"zeta": 200, "bravo": 1})
    ids = _collect(monkeypatch, [
        _goal("g-idle-routed", intended="zeta"),     # idle target -> surface
        _goal("g-active-routed", intended="bravo"),  # active target -> hidden
        _goal("g-open", intended="either"),          # open -> surface
    ])
    assert ids == {"g-idle-routed", "g-open"}, ids
