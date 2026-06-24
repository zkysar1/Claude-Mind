"""Tests for goal_budget.py (Phase 4 — boxed scored goal execution)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import goal_budget as gb  # noqa: E402


class FakeClock:
    """Deterministic injectable clock."""
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_construction_validates():
    with pytest.raises(ValueError):
        gb.GoalBox("g", max_attempts=0)
    with pytest.raises(ValueError):
        gb.GoalBox("g", max_attempts=3, max_seconds=0)
    with pytest.raises(ValueError):
        gb.GoalBox("g", max_attempts=3, target_score=1.5)


def test_record_validates_score():
    box = gb.GoalBox("g", max_attempts=3)
    for bad in (-0.1, 1.1, float("nan"), True, "x", None):
        with pytest.raises(ValueError):
            box.record(bad)


def test_default_action_is_continue_not_premature_stop():
    # An imperfect first attempt must NOT stop the box — this is the anti-premature
    # termination guard (AutoLab's #1 failure mode).
    box = gb.GoalBox("g", max_attempts=5, target_score=1.0)
    box.record(0.3)
    d = box.decide()
    assert d.action == "continue" and d.best_score == pytest.approx(0.3)


def test_stop_met_when_target_reached():
    box = gb.GoalBox("g", max_attempts=5, target_score=0.9)
    box.record(0.2)
    box.record(0.95)
    assert box.decide().action == "stop_met"


def test_stop_met_uses_best_not_last():
    # A regression on a later attempt doesn't un-meet a goal already met.
    box = gb.GoalBox("g", max_attempts=5, target_score=0.9)
    box.record(0.95)
    box.record(0.40)
    d = box.decide()
    assert d.action == "stop_met" and d.best_score == pytest.approx(0.95)


def test_epsilon_dead_band_counts_near_target_as_met():
    box = gb.GoalBox("g", max_attempts=3, target_score=1.0, epsilon=1e-6)
    box.record(1.0 - 1e-9)
    assert box.decide().action == "stop_met"


def test_stop_budget_on_attempts():
    box = gb.GoalBox("g", max_attempts=2, target_score=1.0)
    box.record(0.4)
    assert box.decide().action == "continue"
    box.record(0.5)
    d = box.decide()
    assert d.action == "stop_budget" and "attempt budget" in d.reason


def test_stop_budget_on_time():
    clk = FakeClock()
    box = gb.GoalBox("g", max_attempts=100, max_seconds=10, target_score=1.0, clock=clk)
    box.record(0.3)            # starts clock at t=0
    clk.t = 5
    assert box.decide().action == "continue"
    clk.t = 11
    d = box.decide()
    assert d.action == "stop_budget" and "time budget" in d.reason


def test_met_takes_priority_over_budget_and_block():
    box = gb.GoalBox("g", max_attempts=1, target_score=0.5)
    box.mark_blocked("external dependency down")
    box.record(0.9)  # also hits attempt cap (1) — but met wins
    assert box.decide().action == "stop_met"


def test_stop_blocked_only_on_explicit_definitive_block():
    box = gb.GoalBox("g", max_attempts=5, target_score=1.0)
    box.record(0.2)
    assert box.decide().action == "continue"   # a failed attempt is NOT a block
    box.mark_blocked("credential the agent cannot provision")
    d = box.decide()
    assert d.action == "stop_blocked" and "definitively blocked" in d.reason


def test_plateau_is_advisory_not_a_stop():
    box = gb.GoalBox("g", max_attempts=10, target_score=1.0)
    for s in (0.50, 0.505, 0.508):
        box.record(s)
    assert box.plateaued(window=3, min_delta=0.01) is True
    # plateau detected, but the box still says continue (don't terminate early)
    assert box.decide().action == "continue"


def test_plateau_false_when_improving_or_too_few():
    box = gb.GoalBox("g", max_attempts=10)
    box.record(0.2)
    assert box.plateaued() is False  # too few attempts
    box.record(0.5)
    box.record(0.9)
    assert box.plateaued(window=3, min_delta=0.01) is False  # clearly improving


def test_plateau_dip_and_recover_is_not_flat():
    # fresh-eyes MED: a volatile dip-and-recover must NOT read as a plateau.
    box = gb.GoalBox("g", max_attempts=10)
    for s in (0.9, 0.1, 0.9):
        box.record(s)
    assert box.plateaued(window=3, min_delta=0.01) is False  # range 0.8, not flat


def test_plateau_true_only_when_genuinely_flat():
    box = gb.GoalBox("g", max_attempts=10)
    for s in (0.5, 0.5, 0.5):
        box.record(s)
    assert box.plateaued(window=3, min_delta=0.01) is True  # range 0, flat


def test_plateau_slow_monotonic_crawl_is_flagged():
    # Fresh-eyes audit: a MONOTONIC-INCREASING crawl whose total spread stays under
    # min_delta is intentionally treated as a plateau (diminishing returns), even
    # though each step nudges upward. Documents range-based semantics so a future
    # reader doesn't "fix" it expecting any upward climb to be exempt.
    box = gb.GoalBox("g", max_attempts=10)
    for s in (0.500, 0.504, 0.508):        # +0.004/step, total spread 0.008
        box.record(s)
    assert box.plateaued(window=3, min_delta=0.01) is True    # spread 0.008 < 0.01
    assert box.plateaued(window=3, min_delta=0.005) is False  # spread 0.008 >= 0.005
    assert box.decide().action == "continue"  # advisory only — never forces a stop


def test_decision_as_dict():
    box = gb.GoalBox("g", max_attempts=3)
    box.record(0.4)
    d = box.decide().as_dict()
    assert set(d) == {"action", "reason", "best_score", "attempts_used"}
    assert d["attempts_used"] == 1
