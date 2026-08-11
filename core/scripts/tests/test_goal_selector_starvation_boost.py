"""test_goal_selector_starvation_boost.py —  (2026-08-10).

Pins apply_starvation_boost, the anti-starvation post-scoring pass wired into
goal-selector.py cmd_select. The 21-term score_goal formula has an age-based
anti-starvation term for RECURRING goals (recurring_urgency / overdue_ratio) but
NONE for one-shot goals: a one-shot goal's score is fixed at filing time, so a
lone HIGH goal in a sprawling low-completion aspiration (canonical: an
alert-sweep Unblock in asp-115) never rises and can sit unclaimed indefinitely.
Motivating incident: g-115-5426, an env-server LLM-service alert unclaimed 29h
against an active 5-agent fleet.

Contracts pinned:
  * NO-REGRESSION BY CONSTRUCTION: a goal younger than min_age_hours gets ZERO
    boost and no breakdown key (normal selection byte-identical).
  * enabled=false -> apply_starvation_boost is a byte-identical no-op.
  * an aged HIGH one-shot goal gets a positive, ramping, clamped lift; recurring
    goals are skipped (recurring_urgency covers them); non-HIGH priorities get
    the configured multiplier (0.0 by default).
  * boost-only: no candidate's score is ever lowered.
  * fail-open: missing/unparseable created_at -> no boost.
  * EMITTER (guard-1362): score_goal itself emits created_at into the candidate
    dict — the pass reads it from there, so the emitter is tested, not only the
    consumer.
  * the shipped aspirations.yaml starvation_boost block loads (default ON).

Pattern mirrors test_goal_selector_cell_return.py: spec_from_file_location load
of the hyphen-named module, capture/restore MIND_AGENT around import (score_goal
derives AGENT_DIR at import), pure in-memory dicts, no subprocess, no daemon.
"""
from __future__ import annotations

import copy
import importlib.util
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gs = _load("goal_selector_starv", "goal-selector.py")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


# --- config + fixtures ------------------------------------------------------

CFG = {
    "enabled": True,
    "min_age_hours": 12.0,
    "full_boost_age_hours": 36.0,
    "max_boost": 4.0,
    "priority_multipliers": {"HIGH": 1.0, "MEDIUM": 0.0, "LOW": 0.0},
}


def _ts(hours_ago):
    """A naive-local ISO timestamp `hours_ago` hours in the past (TZ=UTC fleet-wide)."""
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _entry(*, score=5.0, priority=3, recurring=False, created_at=None):
    """A synthetic scored-entry dict of the shape score_goal emits."""
    return {
        "goal_id": "g-x-1",
        "aspiration_id": "asp-x",
        "score": score,
        "recurring": recurring,
        "breakdown": {},
        "raw": {"priority": priority},
        "created_at": created_at,
    }


# --- emitter (guard-1362) ---------------------------------------------------

def test_score_goal_emits_created_at():
    """guard-1362: the emitter (score_goal), not only the consumer, carries the field."""
    gs._ACTIVE_DIRECTIVES = []
    goal = {"id": "g-x-1", "title": "t", "priority": "HIGH",
            "created_at": "2026-08-08T00:00:00"}
    cand = {"goal": goal, "aspiration": {"id": "asp-x"}, "source": "world"}
    result = gs.score_goal(cand, {}, [], [])
    assert result["created_at"] == "2026-08-08T00:00:00"
    # routing fields guard-1362 protects must still be present
    assert "intended_agent" in result
    assert "routed_to_me" in result


def test_score_goal_created_at_falls_back_to_created():
    gs._ACTIVE_DIRECTIVES = []
    goal = {"id": "g-x-2", "title": "t", "priority": "HIGH",
            "created": "2026-08-01T00:00:00"}  # legacy field name
    cand = {"goal": goal, "aspiration": {"id": "asp-x"}, "source": "world"}
    result = gs.score_goal(cand, {}, [], [])
    assert result["created_at"] == "2026-08-01T00:00:00"


# --- the pass ---------------------------------------------------------------

def test_aged_high_goal_boosted():
    scored = [_entry(score=5.0, priority=3, created_at=_ts(30))]
    gs.apply_starvation_boost(scored, CFG)
    e = scored[0]
    assert e["breakdown"]["starvation_boost"] > 0
    assert e["score"] > 5.0
    assert e["raw"]["starvation_age_hours"] >= 29.9


def test_young_goal_no_boost():
    """No-regression: a goal younger than min_age_hours is byte-identical."""
    before = _entry(score=5.0, priority=3, created_at=_ts(6))
    scored = [copy.deepcopy(before)]
    gs.apply_starvation_boost(scored, CFG)
    assert scored[0]["score"] == 5.0
    assert "starvation_boost" not in scored[0]["breakdown"]


def test_at_min_age_exactly_no_boost():
    """At exactly min_age the ramp is 0 -> boost 0 -> no key (guard the boundary)."""
    scored = [_entry(score=5.0, priority=3, created_at=_ts(12))]
    gs.apply_starvation_boost(scored, CFG)
    # age is min_age + tiny execution delta; ramp ~0 -> boost rounds to 0 -> skipped
    assert scored[0]["breakdown"].get("starvation_boost", 0) < 0.05


def test_recurring_skipped():
    scored = [_entry(score=5.0, priority=3, recurring=True, created_at=_ts(30))]
    gs.apply_starvation_boost(scored, CFG)
    assert scored[0]["score"] == 5.0
    assert "starvation_boost" not in scored[0]["breakdown"]


def test_medium_priority_no_boost_by_default():
    scored = [_entry(score=5.0, priority=2, created_at=_ts(30))]
    gs.apply_starvation_boost(scored, CFG)
    assert scored[0]["score"] == 5.0
    assert "starvation_boost" not in scored[0]["breakdown"]


def test_medium_priority_boosts_when_configured():
    cfg = copy.deepcopy(CFG)
    cfg["priority_multipliers"]["MEDIUM"] = 0.5
    scored = [_entry(score=5.0, priority=2, created_at=_ts(48))]
    gs.apply_starvation_boost(scored, cfg)
    # full ramp * 0.5 * max_boost(4.0) = 2.0
    assert abs(scored[0]["breakdown"]["starvation_boost"] - 2.0) < 0.05


def test_disabled_is_noop():
    cfg = copy.deepcopy(CFG)
    cfg["enabled"] = False
    before = _entry(score=5.0, priority=3, created_at=_ts(30))
    scored = [copy.deepcopy(before)]
    gs.apply_starvation_boost(scored, cfg)
    assert scored[0] == before  # byte-identical


def test_ramp_midpoint_and_clamp():
    # midpoint (24h; span 12->36) -> ramp 0.5 -> boost ~2.0
    s24 = [_entry(score=5.0, priority=3, created_at=_ts(24))]
    gs.apply_starvation_boost(s24, CFG)
    assert abs(s24[0]["breakdown"]["starvation_boost"] - 2.0) < 0.05
    # at full_age (36h) -> ramp 1.0 -> boost == max_boost
    s36 = [_entry(score=5.0, priority=3, created_at=_ts(36))]
    gs.apply_starvation_boost(s36, CFG)
    assert s36[0]["breakdown"]["starvation_boost"] == 4.0
    # beyond full_age (72h) -> clamped at max_boost
    s72 = [_entry(score=5.0, priority=3, created_at=_ts(72))]
    gs.apply_starvation_boost(s72, CFG)
    assert s72[0]["breakdown"]["starvation_boost"] == 4.0
    # monotonic non-decreasing with age
    assert (s72[0]["breakdown"]["starvation_boost"]
            >= s24[0]["breakdown"]["starvation_boost"])


def test_missing_created_at_failopen():
    scored = [_entry(score=5.0, priority=3, created_at=None)]
    gs.apply_starvation_boost(scored, CFG)
    assert scored[0]["score"] == 5.0
    assert "starvation_boost" not in scored[0]["breakdown"]


def test_unparseable_created_at_failopen():
    scored = [_entry(score=5.0, priority=3, created_at="not-a-timestamp")]
    gs.apply_starvation_boost(scored, CFG)
    assert scored[0]["score"] == 5.0
    assert "starvation_boost" not in scored[0]["breakdown"]


def test_boost_never_lowers_score():
    scored = [_entry(score=5.0, priority=3, created_at=_ts(t)) for t in (6, 24, 48, 200)]
    originals = [e["score"] for e in scored]
    gs.apply_starvation_boost(scored, CFG)
    for e, orig in zip(scored, originals):
        assert e["score"] >= orig


def test_boost_bounded_by_max():
    scored = [_entry(score=5.0, priority=3, created_at=_ts(10000))]
    gs.apply_starvation_boost(scored, CFG)
    assert scored[0]["breakdown"]["starvation_boost"] <= CFG["max_boost"]


def test_missing_raw_priority_failsafe():
    """A scored entry with no raw priority must not raise; it simply gets no boost."""
    e = _entry(score=5.0, priority=3, created_at=_ts(30))
    del e["raw"]["priority"]
    scored = [e]
    gs.apply_starvation_boost(scored, CFG)
    assert scored[0]["score"] == 5.0
    assert "starvation_boost" not in scored[0]["breakdown"]


# --- shipped config ---------------------------------------------------------

def test_shipped_config_loads_default_on():
    cfg = gs.STARVATION_CONFIG
    assert cfg["enabled"] is True
    assert cfg["min_age_hours"] == 12.0
    assert cfg["full_boost_age_hours"] == 36.0
    assert cfg["max_boost"] == 4.0
    assert cfg["priority_multipliers"]["HIGH"] == 1.0
    assert cfg["priority_multipliers"]["MEDIUM"] == 0.0


def test_max_boost_below_directive_ceiling():
    """max_boost must stay below directive_boost's raw ceiling so a fresh user
    directive still outranks a maximally-starved goal (design invariant)."""
    assert gs.STARVATION_CONFIG["max_boost"] < 4.5
