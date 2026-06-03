"""test_goal_selector_substantive_demotion.py — FW-1 regression (2026-05-25).

Exercises apply_substantive_demotion, extracted from goal-selector.py. Six of
seven agents reported recurring sweeps perpetually out-ranking rare substantive
work; FW-1 caps a recurring goal's score to `substantive_demotion_margin` below
the best non-recurring, agent-executable candidate — unless the recurring goal
is overdue beyond `substantive_demotion_overdue_exempt_ratio`.

Pattern mirrors test_goal_selector_role_affinity.py: capture/restore MIND_AGENT
around the module-level import (goal-selector derives AGENT_DIR at import), then
call the pure function directly. No subprocess, no file I/O.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")
apply_substantive_demotion = gs.apply_substantive_demotion

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


# Canonical config — defaults the selector ships with.
CFG = {
    "substantive_demotion_enabled": True,
    "substantive_demotion_margin": 0.5,
    "substantive_demotion_floor": 5.0,
    "substantive_demotion_overdue_exempt_ratio": 5.0,
}


def _goal(goal_id, score, *, recurring=False, agent_exec=2, overdue=0.0):
    """Build a minimal scored-goal dict shaped like score_goal's return value."""
    return {
        "goal_id": goal_id,
        "aspiration_id": "asp-001",
        "recurring": recurring,
        "recurring_overdue_ratio": overdue,
        "score": score,
        "breakdown": {},
        "raw": {"agent_executable": agent_exec},
    }


def _by_id(scored):
    return {s["goal_id"]: s for s in scored}


def test_fires_caps_recurring_below_top_substantive():
    """Recurring #1 (13.87) demoted below substantive (8.0): cap = 8.0 - 0.5 = 7.5."""
    scored = [
        _goal("g-rec", 13.87, recurring=True),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    m = _by_id(scored)
    assert m["g-rec"]["score"] == 7.5, m["g-rec"]["score"]
    assert m["g-sub"]["score"] == 8.0, "substantive untouched"
    # And after a sort the substantive goal ranks first.
    scored.sort(key=lambda x: -x["score"])
    assert scored[0]["goal_id"] == "g-sub"


def test_exempt_when_overdue_beyond_ratio():
    """Recurring overdue 6.0x (>= 5.0 exempt ratio) keeps full score — monitoring must not rot."""
    scored = [
        _goal("g-rec", 13.87, recurring=True, overdue=6.0),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    m = _by_id(scored)
    assert m["g-rec"]["score"] == 13.87, "overdue-exempt recurring is NOT demoted"
    assert "substantive_demotion" not in m["g-rec"]["breakdown"]


def test_overdue_just_below_ratio_still_demoted():
    """Boundary: overdue 4.99x (< 5.0) is still demoted; the exemption is strict-less-than."""
    scored = [
        _goal("g-rec", 13.87, recurring=True, overdue=4.99),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    assert _by_id(scored)["g-rec"]["score"] == 7.5


def test_no_substantive_candidate_no_change():
    """All-recurring slate: nothing to protect, no demotion."""
    scored = [
        _goal("g-rec1", 13.87, recurring=True),
        _goal("g-rec2", 9.0, recurring=True),
    ]
    apply_substantive_demotion(scored, CFG)
    m = _by_id(scored)
    assert m["g-rec1"]["score"] == 13.87
    assert m["g-rec2"]["score"] == 9.0


def test_substantive_below_floor_no_change():
    """Substantive top score 3.0 < floor 5.0 — don't suppress maintenance for stragglers."""
    scored = [
        _goal("g-rec", 13.87, recurring=True),
        _goal("g-sub", 3.0),
    ]
    apply_substantive_demotion(scored, CFG)
    assert _by_id(scored)["g-rec"]["score"] == 13.87


def test_non_agent_executable_substantive_ignored():
    """A non-recurring goal this agent can't execute (agent_executable=0) is not 'substantive'."""
    scored = [
        _goal("g-rec", 13.87, recurring=True),
        _goal("g-sub", 8.0, agent_exec=0),
    ]
    apply_substantive_demotion(scored, CFG)
    assert _by_id(scored)["g-rec"]["score"] == 13.87, "no eligible substantive work → no demotion"


def test_disabled_no_change():
    cfg = dict(CFG, substantive_demotion_enabled=False)
    scored = [
        _goal("g-rec", 13.87, recurring=True),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, cfg)
    assert _by_id(scored)["g-rec"]["score"] == 13.87


def test_recurring_already_below_cap_untouched():
    """Recurring (4.0) already below cap (7.5) is left alone — the guard is score > cap."""
    scored = [
        _goal("g-rec", 4.0, recurring=True),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    m = _by_id(scored)
    assert m["g-rec"]["score"] == 4.0
    assert "substantive_demotion" not in m["g-rec"]["breakdown"]


def test_multiple_recurring_all_capped():
    """Every recurring goal above the cap is demoted to it (not just #1)."""
    scored = [
        _goal("g-rec1", 13.87, recurring=True),
        _goal("g-rec2", 9.0, recurring=True),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    m = _by_id(scored)
    assert m["g-rec1"]["score"] == 7.5
    assert m["g-rec2"]["score"] == 7.5
    assert m["g-sub"]["score"] == 8.0


def test_telemetry_recorded_on_demotion():
    """Demoted goals carry breakdown + raw telemetry; pre-score preserved."""
    scored = [
        _goal("g-rec", 13.87, recurring=True),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    rec = _by_id(scored)["g-rec"]
    assert rec["breakdown"]["substantive_demotion"] == round(7.5 - 13.87, 2)
    assert rec["raw"]["substantive_demotion_applied"] is True
    assert rec["raw"]["substantive_demotion_pre_score"] == 13.87


def test_single_candidate_no_change():
    """< 2 candidates: nothing to compare against."""
    scored = [_goal("g-rec", 13.87, recurring=True)]
    apply_substantive_demotion(scored, CFG)
    assert scored[0]["score"] == 13.87
