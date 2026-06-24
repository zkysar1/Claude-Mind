"""test_goal_selector_never_fired_recurring.py --  regression (2026-06-22).

Exercises the NEVER-FIRED FALLBACK in score_goal's recurring_urgency block.

Root cause (g-303-32 investigation): a recurring goal with no lastAchievedAt
has never fired. Before the fix, overdue_ratio was derived ONLY from
lastAchievedAt (null -> 0.0), so such a goal was pinned at urgency_base forever
regardless of age. The class-level recurring_saturation penalty then buried it
below pickable rank indefinitely and its function silently never ran -- the
canonical case being g-303-04 (tree-decompose drain, 0 fires in 41 days). The
fix treats "never fired" as "overdue since created_at + interval" so the
most-neglected recurring goal accrues the urgency the escalator intends, letting
the existing "overdue overcomes saturation" design finally apply to it.

Pattern mirrors test_goal_selector_substantive_demotion.py: capture/restore
MIND_AGENT around the module-level import (goal-selector derives AGENT_DIR at
import), then call the pure score_goal directly. No subprocess, no file I/O.

Timestamps are computed DYNAMICALLY (now - delta) per guard-566 -- a hard-coded
overdue timestamp would silently stop being overdue as the clock advances.
Thresholds are read from RECURRING_CONFIG (single source of truth) so the test
does not rot if the config knobs are retuned.
"""

from __future__ import annotations

import importlib
import math
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
score_goal = gs.score_goal
RC = gs.RECURRING_CONFIG

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _score(goal):
    """Score a goal with minimal viable inputs; return the scored dict."""
    cand = {
        "goal": goal,
        "aspiration": {"id": "asp-303", "priority": "MEDIUM", "goals": []},
        "source": "world",
    }
    return score_goal(cand, wm={}, resolved=[], session_completions=[])


def _base_recurring(**overrides):
    g = {
        "id": "g-test-rec",
        "title": "Recurring: synthetic test goal",
        "priority": "MEDIUM",
        "participants": ["agent"],
        "recurring": True,
        "interval_hours": 24,
    }
    g.update(overrides)
    return g


def test_never_fired_old_recurring_accrues_max_urgency():
    """41-day-old never-fired 24h goal -> overdue_ratio huge, urgency hits the cap.

    This is the g-303-04 shape. Pre-fix it scored urgency_base (1.5); post-fix it
    saturates urgency_max because (984h - 24h)/24h ~= 40x overdue.
    """
    created = _iso(datetime.now() - timedelta(days=41))
    g = _base_recurring(created_at=created)  # no lastAchievedAt -> never fired
    r = _score(g)
    assert r["recurring_overdue_ratio"] > 35.0, r["recurring_overdue_ratio"]
    assert r["raw"]["recurring_urgency"] == RC["urgency_max"], r["raw"]["recurring_urgency"]


def test_never_fired_recent_recurring_stays_base():
    """A never-fired goal younger than its interval is NOT yet overdue -> base only.

    Guards backward-compat: pre-fix a recent never-fired goal also scored
    urgency_base, so this case must be unchanged (no false urgency inflation).
    """
    created = _iso(datetime.now() - timedelta(hours=2))
    g = _base_recurring(created_at=created)
    r = _score(g)
    assert r["recurring_overdue_ratio"] == 0.0, r["recurring_overdue_ratio"]
    assert r["raw"]["recurring_urgency"] == RC["urgency_base"], r["raw"]["recurring_urgency"]


def test_fired_overdue_recurring_unchanged():
    """A goal WITH lastAchievedAt keeps the original lastAchievedAt-derived path.

    Regression guard that the fix did not alter the normal (already-fired) branch:
    lastAchievedAt 72h ago, interval 24h -> overdue_ratio == 2.0 exactly.
    """
    last = _iso(datetime.now() - timedelta(hours=72))
    g = _base_recurring(lastAchievedAt=last, created_at=_iso(datetime.now() - timedelta(days=99)))
    r = _score(g)
    assert abs(r["recurring_overdue_ratio"] - 2.0) < 0.05, r["recurring_overdue_ratio"]
    expected = RC["urgency_base"] + math.log2(1 + 2.0) * RC["urgency_log_scale"]
    assert abs(r["raw"]["recurring_urgency"] - round(expected, 2)) < 0.05, r["raw"]["recurring_urgency"]


def test_never_fired_no_created_at_failsafe():
    """Never-fired AND no created_at -> fall back to base, never raise.

    The created_at probe yields None (hours_since(None)); the branch must still
    land at urgency_base without an exception or a spurious overdue_ratio.
    """
    g = _base_recurring()  # neither lastAchievedAt nor created_at
    r = _score(g)
    assert r["recurring_overdue_ratio"] == 0.0, r["recurring_overdue_ratio"]
    assert r["raw"]["recurring_urgency"] == RC["urgency_base"], r["raw"]["recurring_urgency"]


def test_non_recurring_has_zero_urgency():
    """A non-recurring goal gets no recurring_urgency regardless of age."""
    g = _base_recurring(recurring=False, created_at=_iso(datetime.now() - timedelta(days=41)))
    r = _score(g)
    assert r["raw"]["recurring_urgency"] == 0, r["raw"]["recurring_urgency"]
    assert r["recurring_overdue_ratio"] == 0.0, r["recurring_overdue_ratio"]
