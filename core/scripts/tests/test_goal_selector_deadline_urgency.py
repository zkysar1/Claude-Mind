"""test_goal_selector_deadline_urgency.py —  regression (2026-06-15).

Exercises score_goal's deadline_urgency criterion after g-318-04 added:
  (b) aspiration-level `deadline` inheritance — a goal with no own
      resolves_by/deadline inherits asp["deadline"];
  (c) a long-horizon ramp — 0.5 at <=30d, 0.25 at <=90d, so a fixed external
      deadline (e.g. the ARC clock 2026-11-02) creates prioritization pull
      months out without overriding near-term urgency (3/2/1) or priority.

Backward-compat anchor: with NO deadline anywhere, deadline_urgency is 0 and
the near-term steps (<=1/<=3/<=7 -> 3/2/1) are unchanged.

Pattern mirrors test_goal_selector_substantive_demotion.py: capture/restore
MIND_AGENT around the module-level import (goal-selector derives AGENT_DIR at
import), then call score_goal directly with minimal fixtures. _ACTIVE_DIRECTIVES
is pre-set to [] so the directive_boost path does no board I/O. No subprocess.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import date, timedelta
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


def _du(goal, asp):
    """Return raw deadline_urgency for a (goal, aspiration) pair via score_goal."""
    # Bypass the board-reading directive memo so the call does no file I/O.
    gs._ACTIVE_DIRECTIVES = []
    asp = dict(asp)
    asp.setdefault("id", "asp-x")
    cand = {"goal": goal, "aspiration": asp, "source": "world"}
    result = gs.score_goal(cand, {}, [], [])
    return result["raw"]["deadline_urgency"]


def _iso(days_from_today):
    return (date.today() + timedelta(days=days_from_today)).isoformat()


# ── Inheritance ( part b) ─────────────────────────────────────────

def test_inherits_aspiration_deadline_when_goal_has_none():
    """Goal with no own deadline inherits asp.deadline (20d out -> 0.5 ramp step)."""
    assert _du({}, {"deadline": _iso(20)}) == 0.5


def test_goal_resolves_by_takes_precedence_over_aspiration_deadline():
    """Goal's own resolves_by (2d) wins over a far aspiration deadline (60d)."""
    assert _du({"resolves_by": _iso(2)}, {"deadline": _iso(60)}) == 2


def test_goal_deadline_takes_precedence_over_aspiration_deadline():
    """Goal's own `deadline` (5d) wins over a far aspiration deadline (60d)."""
    assert _du({"deadline": _iso(5)}, {"deadline": _iso(60)}) == 1


# ── Long-horizon ramp ( part c) ───────────────────────────────────

def test_ramp_30d_step():
    """A deadline 25d out yields the 0.5 long-horizon step."""
    assert _du({}, {"deadline": _iso(25)}) == 0.5


def test_ramp_30d_boundary_inclusive():
    """Exactly 30d out is still the 0.5 step (boundary is <=30)."""
    assert _du({}, {"deadline": _iso(30)}) == 0.5


def test_ramp_90d_step():
    """A deadline 60d out yields the 0.25 long-horizon step."""
    assert _du({}, {"deadline": _iso(60)}) == 0.25


def test_ramp_90d_boundary_inclusive():
    """Exactly 90d out is still the 0.25 step (boundary is <=90)."""
    assert _du({}, {"deadline": _iso(90)}) == 0.25


def test_beyond_90d_is_zero():
    """A deadline 120d out is beyond the ramp -> 0 (gentle: no pull yet)."""
    assert _du({}, {"deadline": _iso(120)}) == 0


# ── Near-term steps unchanged (backward compat) ───────────────────────────

def test_near_term_1d():
    assert _du({"resolves_by": _iso(1)}, {}) == 3


def test_near_term_3d():
    assert _du({"resolves_by": _iso(3)}, {}) == 2


def test_near_term_7d():
    assert _du({"resolves_by": _iso(7)}, {}) == 1


def test_no_deadline_anywhere_is_zero():
    """No goal deadline and no aspiration deadline -> 0 (pre- behavior)."""
    assert _du({}, {}) == 0
