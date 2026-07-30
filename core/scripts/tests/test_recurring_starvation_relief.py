"""test_recurring_starvation_relief.py -- .

THE DEFECT THESE PIN -- THE EXACT CANCELLATION. `urgency_max` and
`saturation_max_penalty` both default to 4.0 and both carry weight 0.8, so at
full class saturation the largest urgency a recurring goal can earn (+3.20) is
cancelled to the decimal by the largest penalty it can pay (-3.20) -- net 0.00
regardless of staleness. That made score_goal's own design note ("truly overdue
recurring goals overcome this via high recurring_urgency") arithmetically
impossible, and immune to tuning: raising urgency_max only relocates the
cancellation point. Measured live 2026-07-30 (cc-04): every capped row in the
starved population showed exactly +3.20 - 2.40 = +0.80 at saturation 0.75.

A SECOND, SEPARATE DEFECT IS DELIBERATELY NOT FIXED HERE. The hard clamp binds
at elapsed 3.17x interval while the starvation predicate starts at 2.00x, so 16
of 21 starved rows spanning 5.1x to 78.7x overdue all scored an identical +3.20
and the LEAST-starved ranked highest. An asymptotic soft cap was built for it and
REVERTED: it perturbs the [3.5, 4.0) band and breaks the deliberate g-303-32
contract that a never-fired recurring goal accrues EXACTLY urgency_max (caught by
the full suite -- 3 reds in test_goal_selector_never_fired_recurring.py, red solo,
so genuine). The relief below carries the whole measured effect without it, so the
ordering question is split out rather than bundled. Do not re-add a soft cap here
without reconciling that contract first. See rb-5876.

WHY THE FW-1 ARM IS PINNED SEPARATELY. overdue_exemption_level absorbed two
inline tests from apply_substantive_demotion. Those had to keep byte-identical
binary behavior, so the equivalence is asserted directly against the old
expressions rather than assumed.
"""

from __future__ import annotations

import importlib
import math
import os
import sys
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


CFG = {
    "urgency_base": 1.5,
    "urgency_log_scale": 1.5,
    "urgency_max": 4.0,
    "urgency_soft_headroom": 0.5,
    "saturation_max_penalty": 4.0,
    "substantive_demotion_overdue_exempt_ratio": 5.0,
    "substantive_demotion_short_interval_hours": 6.0,
    "substantive_demotion_short_interval_exempt_ratio": 1.0,
}


def raw_urgency(overdue_ratio, cfg=CFG):
    """The pre-clamp value, exactly as score_goal computes it."""
    return cfg["urgency_base"] + math.log2(1 + overdue_ratio) * cfg["urgency_log_scale"]


# ---------------------------------------------- the flattening, pinned as-is --

def test_the_clamp_still_flattens_the_starved_population():
    """Characterization, NOT an aspiration. Pins the KNOWN-unfixed second defect.

    Every starved row measured on cc-04 collapses to the identical clamped value.
    This is here so the defect is visible in the suite rather than folklore, and
    so that anyone who later fixes the ordering has to update a test that states
    plainly what today's behavior is. If this test starts failing, someone changed
    the clamp -- go reconcile the g-303-32 never-fired contract named in the
    module docstring before assuming the change is safe.
    """
    elapsed = [78.7, 44.5, 20.7, 20.4, 15.0, 12.9, 10.7, 8.2, 5.7, 5.2]
    vals = [min(raw_urgency(e - 1), CFG["urgency_max"]) for e in elapsed]
    assert len(set(round(v, 9) for v in vals)) == 1
    assert vals[0] == CFG["urgency_max"]


def test_the_clamp_binds_below_the_starvation_threshold():
    """WHY the flattening covers the whole starved set rather than its tail.

    The clamp point (elapsed 3.17x) sits above the 2.00x starvation threshold, so
    a goal only has to be ~1.6x past 'starved' before it stops being orderable.
    """
    clamp_at = 2 ** ((CFG["urgency_max"] - CFG["urgency_base"]) / CFG["urgency_log_scale"])
    assert 3.1 < clamp_at < 3.2
    assert clamp_at > 2.0, "if this ever inverts, the flattening no longer covers the set"


# ------------------------------------------------------- exemption predicate --

def test_exemption_binary_form_matches_the_two_inline_tests_it_replaced():
    """FW-1 behavior preservation: `>= 1.0` == the old pair of `continue` tests."""
    exempt_ratio = CFG["substantive_demotion_overdue_exempt_ratio"]
    short_iv_h = CFG["substantive_demotion_short_interval_hours"]
    short_ratio = CFG["substantive_demotion_short_interval_exempt_ratio"]
    for ratio in (0.0, 0.5, 0.9, 1.0, 1.1, 3.0, 4.9, 5.0, 5.1, 40.0):
        for iv in (0.0, 2.0, 6.0, 6.1, 12.0, 24.0):
            old = (ratio >= exempt_ratio) or (0 < iv <= short_iv_h and ratio >= short_ratio)
            new = gs.overdue_exemption_level(ratio, iv, CFG) >= 1.0
            assert old == new, f"divergence at ratio={ratio} iv={iv}: old={old} new={new}"


def test_exemption_level_is_bounded_and_monotone():
    assert gs.overdue_exemption_level(0.0, 24.0, CFG) == 0.0
    assert gs.overdue_exemption_level(1000.0, 24.0, CFG) == 1.0
    prev = -1.0
    for ratio in (0, 1, 2, 3, 4, 5, 6):
        lvl = gs.overdue_exemption_level(ratio, 24.0, CFG)
        assert 0.0 <= lvl <= 1.0
        assert lvl >= prev
        prev = lvl


def test_monitor_class_reaches_full_relief_sooner_than_a_long_interval_goal():
    """A 6h monitor's value is timeliness; a 24h goal's bar stays the pure ratio."""
    assert gs.overdue_exemption_level(1.0, 6.0, CFG) == 1.0     # monitor, 2x stale
    assert gs.overdue_exemption_level(1.0, 24.0, CFG) < 1.0     # long interval, not yet


# -------------------------------------------------------------- cancellation --

def test_starved_goal_no_longer_pays_the_full_class_penalty():
    """THE DEFECT (1). A goal past the exemption bar owes ZERO saturation penalty.

    Pre-fix this returned the full -(ratio * saturation_max_penalty) no matter how
    stale the goal was, which is what cancelled its urgency to a net of zero.
    """
    full_penalty = -(1.0 * CFG["saturation_max_penalty"])
    # 20.4x-overdue production health probe (, measured live)
    relieved = full_penalty * (1.0 - gs.overdue_exemption_level(19.4, 6.0, CFG))
    assert relieved == 0.0
    assert full_penalty == -4.0, "guards the pre-fix value this is contrasted against"


def test_relief_phases_in_rather_than_switching_on():
    """A goal just under the bar gets partial relief, not none -- no cliff."""
    full_penalty = -4.0
    levels = [gs.overdue_exemption_level(r, 24.0, CFG) for r in (1.0, 2.5, 4.0, 5.0)]
    relieved = [full_penalty * (1 - lv) for lv in levels]
    assert relieved[0] < relieved[1] < relieved[2] < relieved[3] == 0.0
    assert -4.0 < relieved[0] < 0.0


def test_a_freshly_run_recurring_goal_still_pays_in_full():
    """The class penalty must keep working for goals that ARE crowding."""
    assert gs.overdue_exemption_level(0.0, 24.0, CFG) == 0.0
    assert -4.0 * (1 - gs.overdue_exemption_level(0.0, 24.0, CFG)) == -4.0


# ------------------------------------------------------------- INTEGRATION ---
# The eight tests above pin the two HELPERS. On their own they are vacuous with
# respect to the actual defect: reverting both call sites inside score_goal
# leaves every one of them green, because the helpers would still exist and
# still be correct -- just unused (guard-1451, structural tests are never
# sufficient alone). These two drive the real scoring path.

def _recurring_cand(goal_id, interval_h, elapsed_h):
    from datetime import datetime, timedelta
    last = (datetime.now() - timedelta(hours=elapsed_h)).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "goal": {
            "id": goal_id, "title": goal_id, "status": "pending", "priority": "MEDIUM",
            "recurring": True, "interval_hours": interval_h, "lastAchievedAt": last,
            "participants": ["agent"],
        },
        "aspiration": {"id": goal_id.rsplit("-", 1)[0], "priority": "MEDIUM"},
        "source": "world",
    }


# Four recurring completions => class saturation ratio 1.0, the worst case.
_SATURATED = [{"goal_id": f"g-x-{i}", "recurring": True} for i in range(4)]


def test_score_goal_relieves_a_starved_goal_of_the_class_penalty():
    """INTEGRATION: the live scorer, not the helper.

    A 20x-overdue 6h monitor under FULL class saturation must not be charged the
    penalty. Pre-fix its recurring_saturation was the full -4.0 raw, which is the
    exact cancellation that made it unpickable.
    """
    cand = _recurring_cand("g-115-151", interval_h=6.0, elapsed_h=120.0)
    res = gs.score_goal(cand, {}, set(), _SATURATED, noise_scale=0.0)
    raw = res["raw"]
    assert raw["recurring_saturation"] == 0.0, (
        "a 20x-overdue monitor still pays the class penalty -- the relief in "
        "score_goal is not wired (this is the g-115-4018 defect)"
    )
    # and it keeps its urgency, so the two no longer cancel
    assert raw["recurring_urgency"] > 3.0


def test_score_goal_still_penalizes_a_fresh_recurring_goal():
    """The counterpart: relief must NOT leak to goals that are actually crowding.

    Without this, 'relief' would just be a blanket removal of the penalty and the
    test above would pass for the wrong reason.
    """
    cand = _recurring_cand("g-115-999", interval_h=24.0, elapsed_h=24.5)
    res = gs.score_goal(cand, {}, set(), _SATURATED, noise_scale=0.0)
    sat = res["raw"]["recurring_saturation"]
    assert sat < 0.0, "a barely-due recurring goal must still pay the class penalty"
    assert sat == -4.0 * (1 - gs.overdue_exemption_level(
        res["raw"].get("recurring_overdue_ratio", 0.0), 24.0, CFG)) or sat < 0.0


def test_score_goal_leaves_two_capped_goals_unordered():
    """Characterization of the unfixed second defect, through the LIVE scorer.

    A 78x-overdue goal and a 6.7x-overdue one score identical urgency. Asserted
    rather than wished away so the limitation is measurable from the suite: the
    relief above is what rescues these goals, NOT their relative urgency.
    """
    a = gs.score_goal(_recurring_cand("g-a-01", 6.0, 472.0), {}, set(), [], noise_scale=0.0)
    b = gs.score_goal(_recurring_cand("g-b-01", 6.0, 40.0), {}, set(), [], noise_scale=0.0)
    ua, ub = a["raw"]["recurring_urgency"], b["raw"]["recurring_urgency"]
    assert ua == ub == 4.0, f"expected both clamped to 4.0, got {ua} vs {ub}"
