# domain-leak-exempt: tests for the IAUS scorer (BRD Gap 8, g-306-32). IAUS is
# the framework feature name; companion to the exempt _iaus_scorer.py + design.
"""Unit tests for the flag-gated IAUS goal scorer (g-306-32, BRD Gap 8).

Covers the design's required behaviors (core/config/iaus-selector-design.md):
veto-by-zero (the primary win), the PRIMARY-floor that prevents a legit-zero
axis from acting as an unintended veto, the geometric-mean + Dave-Mark makeup
compensation, the bounded tier-3 multiplier, watermark pruning, and the tier
partition invariant.

The additive default path is unchanged by this work — its regression coverage
lives in the goal-selector suite; here we test only the new module.
"""
import os
import sys

import pytest

# core/scripts is on sys.path via conftest (it imports _paths from there); add
# defensively so this file is import-order independent.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import _iaus_scorer as I  # noqa: E402


# A neutral weights dict (every axis weight 1.0) and config for tests that do
# not care about specific weighting.
ALL_AXES = I.VETO_AXES + I.PRIMARY_AXES + I.MAKEUP_AXES
WEIGHTS = {k: 1.0 for k in ALL_AXES}
CFG = {"primary_floor": 0.1, "watermark": 0.0, "bonus_scale": 4.0, "urgency_max": 4.0}


def _raw(**overrides):
    """Build a raw-criteria dict with all axes 0.0, applying overrides."""
    raw = {k: 0.0 for k in ALL_AXES}
    raw.update(overrides)
    return raw


# --- response_curve -------------------------------------------------------

def test_response_curve_identity_linear():
    # m=1,k=1,c=0,b=0 -> clamp(x,0,1)
    assert I.response_curve(0.5) == pytest.approx(0.5)
    assert I.response_curve(0.0) == 0.0
    assert I.response_curve(1.0) == 1.0


def test_response_curve_clamps():
    assert I.response_curve(2.0) == 1.0
    assert I.response_curve(-1.0) == 0.0


def test_response_curve_floor():
    # b raises the floor: x=0 -> b
    assert I.response_curve(0.0, b=0.1) == pytest.approx(0.1)
    # and still clamps at the top
    assert I.response_curve(1.0, b=0.1) == 1.0


def test_response_curve_fractional_k_negative_base_degrades_to_floor():
    # (x-c)**k with negative base and fractional k would raise -> floor b
    assert I.response_curve(-0.5, c=0.0, k=0.5, b=0.2) == pytest.approx(0.2)


# --- scale_axis -----------------------------------------------------------

def test_scale_axis_priority_mapping():
    assert I.scale_axis("priority", 3) == 1.0   # HIGH
    assert I.scale_axis("priority", 2) == 0.6   # MED
    assert I.scale_axis("priority", 1) == 0.3   # LOW


def test_scale_axis_domain_max_and_clamp():
    assert I.scale_axis("completion_pressure", 2.5) == 1.0   # at domain max
    assert I.scale_axis("completion_pressure", 1.25) == pytest.approx(0.5)
    assert I.scale_axis("agent_executable", 2.0) == 1.0
    assert I.scale_axis("agent_executable", 0.0) == 0.0
    # over-max clamps to 1
    assert I.scale_axis("deadline_urgency", 99.0) == 1.0


def test_scale_axis_recurring_uses_urgency_max():
    assert I.scale_axis("recurring_urgency", 4.0, urgency_max=4.0) == 1.0
    assert I.scale_axis("recurring_urgency", 2.0, urgency_max=4.0) == pytest.approx(0.5)


# --- iaus_score: the core behaviors --------------------------------------

def test_veto_by_zero():
    """agent_executable=0 zeros the score regardless of any other axis."""
    raw = _raw(agent_executable=0.0, priority=3, completion_pressure=2.5,
               recurring_urgency=4.0, deadline_urgency=3.0)
    r = I.iaus_score(raw, WEIGHTS, CFG)
    assert r["score"] == 0.0
    assert r["veto"] == 0.0


def test_feasible_goal_scores_positive():
    raw = _raw(agent_executable=2.0, priority=3, completion_pressure=2.5)
    r = I.iaus_score(raw, WEIGHTS, CFG)
    assert r["score"] > 0.0
    assert r["veto"] == 1.0


def test_primary_floor_prevents_unintended_veto():
    """A non-recurring goal (recurring_urgency=0, deadline_urgency=0,
    critical_blocker_surface=0) must NOT be vetoed just because those PRIMARY
    axes are legitimately 0 for its class. This is the key fidelity fix vs the
    naive b=0 linear proposal, which would veto nearly every goal."""
    raw = _raw(agent_executable=2.0, priority=2)  # all other primaries 0
    r = I.iaus_score(raw, WEIGHTS, CFG)
    assert r["score"] > 0.0
    assert r["base"] > 0.0


def test_zero_floor_would_veto_proving_the_floor_matters():
    """With primary_floor=0, the same all-primaries-but-priority-zero goal
    drops to ~0 (the defect the floor fixes)."""
    raw = _raw(agent_executable=2.0, priority=2)
    cfg0 = dict(CFG, primary_floor=0.0)
    r = I.iaus_score(raw, WEIGHTS, cfg0)
    assert r["base"] == pytest.approx(0.0)


def test_makeup_lifts_base():
    raw = _raw(agent_executable=2.0, priority=3, completion_pressure=2.5)
    r = I.iaus_score(raw, WEIGHTS, CFG)
    assert r["makeup"] >= r["base"]


def test_bonus_mult_bounded():
    # Large positive bonus sum -> mult approaches but stays under 1.5
    raw = _raw(agent_executable=2.0, priority=2, role_affinity=5.0,
               reward_history=5.0, novelty_bonus=5.0)
    r = I.iaus_score(raw, WEIGHTS, CFG)
    assert 0.5 < r["bonus_mult"] < 1.5
    # Large negative bonus sum -> mult above 0.5, below 1.0
    raw_neg = _raw(agent_executable=2.0, priority=2, recurring_saturation=-5.0,
                   per_goal_saturation=-5.0)
    r_neg = I.iaus_score(raw_neg, WEIGHTS, CFG)
    assert 0.5 < r_neg["bonus_mult"] < 1.0


def test_bonus_cannot_veto_or_dominate():
    """Tier-3 must not zero or explode the score (design 2b)."""
    base_raw = _raw(agent_executable=2.0, priority=3, completion_pressure=2.5)
    base = I.iaus_score(base_raw, WEIGHTS, CFG)["score"]
    # extreme negative bonus still leaves score > 0
    neg = dict(base_raw, recurring_saturation=-100.0)
    assert I.iaus_score(neg, WEIGHTS, CFG)["score"] > 0.0
    # extreme positive bonus does not more than ~1.5x the makeup
    pos = dict(base_raw, role_affinity=100.0)
    assert I.iaus_score(pos, WEIGHTS, CFG)["score"] <= base * 3.0


def test_higher_priority_scores_higher():
    high = I.iaus_score(_raw(agent_executable=2.0, priority=3), WEIGHTS, CFG)["score"]
    low = I.iaus_score(_raw(agent_executable=2.0, priority=1), WEIGHTS, CFG)["score"]
    assert high > low


def test_watermark_prunes_below_floor():
    raw = _raw(agent_executable=2.0, priority=1)  # modest base
    cfg_hi = dict(CFG, watermark=0.99)
    r = I.iaus_score(raw, WEIGHTS, cfg_hi)
    assert r["pruned"] is True
    assert r["score"] == 0.0


def test_watermark_zero_never_prunes():
    raw = _raw(agent_executable=2.0, priority=1)
    r = I.iaus_score(raw, WEIGHTS, CFG)
    assert r["pruned"] is False


# --- tier partition invariant --------------------------------------------

def test_tiers_are_disjoint():
    veto = set(I.VETO_AXES)
    primary = set(I.PRIMARY_AXES)
    makeup = set(I.MAKEUP_AXES)
    assert veto & primary == set()
    assert veto & makeup == set()
    assert primary & makeup == set()


def test_tiers_cover_all_additive_criteria_except_noise():
    """Every score_goal additive criterion except exploration_noise must be in
    exactly one tier — a missing axis would silently vanish from the IAUS path,
    a double-assigned one would be counted twice."""
    union = set(I.VETO_AXES) | set(I.PRIMARY_AXES) | set(I.MAKEUP_AXES)
    expected = {
        "priority", "deadline_urgency", "agent_executable", "variety_bonus",
        "streak_momentum", "novelty_bonus", "recurring_urgency",
        "recurring_saturation", "per_goal_saturation", "user_signal_boost",
        "class_balance_bonus", "role_affinity", "reward_history",
        "completion_pressure", "tail_bonus", "depth_bonus",
        "cross_aspiration_support", "evidence_backing", "deferred_readiness",
        "context_coherence", "skill_affinity", "directive_boost",
        "handoff_bonus", "co_invest_alignment", "critical_blocker_surface",
    }
    assert union == expected
    assert "exploration_noise" not in union
