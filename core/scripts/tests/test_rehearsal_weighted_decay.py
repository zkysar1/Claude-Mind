"""test_rehearsal_weighted_decay.py -- M-4 rehearsal-adaptive TAU.

Covers:
  - TAU stays at base when retrieval_count < neutral threshold
  - TAU grows linearly with retrieval_count above threshold
  - TAU is clamped at rehearsal_tau_max
  - High-rehearsal nodes retain recency bonus longer than low-rehearsal
  - cfg=None falls back to module-level defaults
  - Custom cfg dict overrides defaults
  - Integration: _compute_match_score threads cfg to _recency_bonus
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Bootstrap a clean WORLD env for tree_match imports.
# Capture-restore pattern: stash env before module-level mutation
# so subsequent tests in the same pytest session don't inherit changes.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="rehearsal-decay-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

import tree_match  # noqa: E402

# Restore env so downstream tests inherit clean conftest defaults.
if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


def _node(last_updated=None, retrieval_count=0, **extras):
    """Helper -- minimal node dict with optional last_updated and retrieval_count."""
    n = {"depth": 3, "children": [], "child_count": 0,
         "retrieval_count": retrieval_count}
    if last_updated is not None:
        n["last_updated"] = last_updated
    n.update(extras)
    return n


# ---------------------------------------------------------------------------
# TAU stays at base when retrieval_count < neutral
# ---------------------------------------------------------------------------

def test_tau_at_base_when_under_neutral():
    """retrieval_count=3 (< REHEARSAL_NEUTRAL_BELOW=5), TAU should equal base.

    Verify by checking that the bonus exactly matches the pre-M-4 formula:
    RECENCY_MAX_BONUS * exp(-age_days / REHEARSAL_TAU_BASE).
    """
    age_days = 60
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = _node(last_updated=d, retrieval_count=3)
    bonus = tree_match._recency_bonus(node)
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(
        -age_days / tree_match.REHEARSAL_TAU_BASE
    )
    assert abs(bonus - expected) < 1e-9, f"expected {expected}, got {bonus}"


def test_tau_at_base_when_zero_retrievals():
    """retrieval_count=0, TAU stays at base (same as pre-M-4 behavior)."""
    age_days = 30
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = _node(last_updated=d, retrieval_count=0)
    bonus = tree_match._recency_bonus(node)
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(
        -age_days / tree_match.REHEARSAL_TAU_BASE
    )
    assert abs(bonus - expected) < 1e-9


# ---------------------------------------------------------------------------
# TAU grows linearly with retrieval_count above threshold
# ---------------------------------------------------------------------------

def test_tau_grows_with_retrieval_count():
    """retrieval_count=50 (> neutral=5), TAU = 30 + 50/5.0 = 40."""
    age_days = 60
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = _node(last_updated=d, retrieval_count=50)
    bonus = tree_match._recency_bonus(node)
    expected_tau = 30.0 + 50 / 5.0  # = 40
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(-age_days / expected_tau)
    assert abs(bonus - expected) < 1e-9, f"expected {expected}, got {bonus}"


def test_tau_at_boundary_neutral():
    """retrieval_count exactly at neutral (5) triggers rehearsal effect.

    TAU = 30 + 5/5.0 = 31 (above base).
    """
    age_days = 60
    d = (date.today() - timedelta(days=age_days)).isoformat()
    # rc=5 is NOT < 5, so rehearsal kicks in
    node = _node(last_updated=d, retrieval_count=5)
    bonus = tree_match._recency_bonus(node)
    expected_tau = 30.0 + 5 / 5.0  # = 31
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(-age_days / expected_tau)
    assert abs(bonus - expected) < 1e-9


# ---------------------------------------------------------------------------
# TAU clamped at max
# ---------------------------------------------------------------------------

def test_tau_clamped_at_max():
    """retrieval_count=1000, TAU = min(30 + 1000/5.0, 120) = 120."""
    age_days = 60
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = _node(last_updated=d, retrieval_count=1000)
    bonus = tree_match._recency_bonus(node)
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(
        -age_days / tree_match.REHEARSAL_TAU_MAX
    )
    assert abs(bonus - expected) < 1e-9, f"expected {expected}, got {bonus}"


def test_tau_clamped_at_exact_ceiling():
    """retrieval_count=450, TAU = min(30+90, 120) = 120 exactly at ceiling."""
    age_days = 90
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = _node(last_updated=d, retrieval_count=450)
    bonus = tree_match._recency_bonus(node)
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(
        -age_days / tree_match.REHEARSAL_TAU_MAX
    )
    assert abs(bonus - expected) < 1e-9


# ---------------------------------------------------------------------------
# High-rehearsal nodes retain bonus longer
# ---------------------------------------------------------------------------

def test_high_rehearsal_retains_bonus_longer():
    """Two nodes same age=60 days, one with rc=0, one with rc=200.

    The rc=200 node should have a higher recency bonus because its TAU is
    longer (slower decay).
    """
    age_days = 60
    d = (date.today() - timedelta(days=age_days)).isoformat()
    low = _node(last_updated=d, retrieval_count=0)
    high = _node(last_updated=d, retrieval_count=200)
    bonus_low = tree_match._recency_bonus(low)
    bonus_high = tree_match._recency_bonus(high)
    assert bonus_high > bonus_low, (
        f"high-rehearsal bonus {bonus_high} should exceed "
        f"low-rehearsal bonus {bonus_low}"
    )


def test_rehearsal_effect_vanishes_at_age_zero():
    """At age=0, both low and high rehearsal nodes get the same MAX_BONUS.

    Rehearsal affects decay RATE, not the starting value.
    """
    today = date.today().isoformat()
    low = _node(last_updated=today, retrieval_count=0)
    high = _node(last_updated=today, retrieval_count=500)
    bonus_low = tree_match._recency_bonus(low)
    bonus_high = tree_match._recency_bonus(high)
    assert abs(bonus_low - tree_match.RECENCY_MAX_BONUS) < 1e-9
    assert abs(bonus_high - tree_match.RECENCY_MAX_BONUS) < 1e-9


# ---------------------------------------------------------------------------
# cfg=None falls back to module-level defaults
# ---------------------------------------------------------------------------

def test_cfg_none_uses_defaults():
    """Calling _recency_bonus without cfg uses REHEARSAL_* module constants."""
    age_days = 60
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = _node(last_updated=d, retrieval_count=50)
    # Explicit cfg=None
    bonus = tree_match._recency_bonus(node, cfg=None)
    expected_tau = tree_match.REHEARSAL_TAU_BASE + 50 / tree_match.REHEARSAL_TAU_SCALE
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(-age_days / expected_tau)
    assert abs(bonus - expected) < 1e-9


def test_no_cfg_argument_same_as_none():
    """Positional call without cfg keyword matches cfg=None behavior."""
    age_days = 45
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = _node(last_updated=d, retrieval_count=100)
    bonus_positional = tree_match._recency_bonus(node)
    bonus_kwarg = tree_match._recency_bonus(node, cfg=None)
    assert abs(bonus_positional - bonus_kwarg) < 1e-12


# ---------------------------------------------------------------------------
# Custom cfg dict overrides defaults
# ---------------------------------------------------------------------------

def test_custom_cfg_override():
    """Pass cfg dict with different tau_base/max/scale, verify formula uses them."""
    cfg = {
        "rehearsal_tau_base": 10,
        "rehearsal_tau_max": 50,
        "rehearsal_tau_scale": 2.0,
        "rehearsal_neutral_below": 3,
    }
    age_days = 30
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = _node(last_updated=d, retrieval_count=20)
    bonus = tree_match._recency_bonus(node, cfg=cfg)
    # TAU = min(10 + 20/2.0, 50) = min(20, 50) = 20
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(-age_days / 20.0)
    assert abs(bonus - expected) < 1e-9, f"expected {expected}, got {bonus}"


def test_custom_cfg_clamped_at_custom_max():
    """Custom cfg with low tau_max should clamp."""
    cfg = {
        "rehearsal_tau_base": 10,
        "rehearsal_tau_max": 25,
        "rehearsal_tau_scale": 1.0,
        "rehearsal_neutral_below": 0,
    }
    age_days = 50
    d = (date.today() - timedelta(days=age_days)).isoformat()
    # rc=100 -> unclamped tau = 10 + 100/1.0 = 110, clamped to 25
    node = _node(last_updated=d, retrieval_count=100)
    bonus = tree_match._recency_bonus(node, cfg=cfg)
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(-age_days / 25.0)
    assert abs(bonus - expected) < 1e-9


def test_custom_cfg_neutral_below_respected():
    """Custom neutral_below=10 means rc=8 uses base TAU."""
    cfg = {
        "rehearsal_tau_base": 20,
        "rehearsal_tau_max": 100,
        "rehearsal_tau_scale": 5.0,
        "rehearsal_neutral_below": 10,
    }
    age_days = 40
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = _node(last_updated=d, retrieval_count=8)  # < 10
    bonus = tree_match._recency_bonus(node, cfg=cfg)
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(-age_days / 20.0)
    assert abs(bonus - expected) < 1e-9


# ---------------------------------------------------------------------------
# Integration: _compute_match_score threads cfg to _recency_bonus
# ---------------------------------------------------------------------------

def test_compute_match_score_without_cfg_backward_compat():
    """_compute_match_score(key, node, channel) without cfg still works."""
    today = date.today().isoformat()
    node = _node(last_updated=today, retrieval_count=50,
                 capability_level="EXPLORE")
    # Should not raise
    score = tree_match._compute_match_score("k", node, "substring")
    assert isinstance(score, float)
    assert score > 0


def test_compute_match_score_with_cfg_threads_to_recency():
    """When cfg is passed, _compute_match_score threads it to _recency_bonus,
    producing a different score than cfg=None would for the same node when
    cfg parameters differ from module defaults."""
    age_days = 60
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = _node(last_updated=d, retrieval_count=100,
                 capability_level="EXPLORE")
    custom_cfg = {
        "rehearsal_tau_base": 10,
        "rehearsal_tau_max": 200,
        "rehearsal_tau_scale": 2.0,
        "rehearsal_neutral_below": 0,
    }
    score_default = tree_match._compute_match_score("k", node, "substring")
    score_custom = tree_match._compute_match_score(
        "k", node, "substring", cfg=custom_cfg
    )
    # Custom cfg: TAU = min(10 + 100/2.0, 200) = 60
    # Default:    TAU = min(30 + 100/5.0, 120) = 50
    # Different TAU -> different recency bonus -> different total score
    assert score_default != score_custom, (
        f"scores should differ: default={score_default}, custom={score_custom}"
    )


def test_compute_match_score_rehearsal_only_affects_recency_term():
    """The difference between two _compute_match_score calls with different
    cfg should equal exactly the difference in recency bonuses, verifying
    that cfg only threads to the recency term, not other score components."""
    age_days = 45
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = _node(last_updated=d, retrieval_count=80,
                 capability_level="CALIBRATE", confidence=0.7)
    cfg_a = {
        "rehearsal_tau_base": 30,
        "rehearsal_tau_max": 120,
        "rehearsal_tau_scale": 5.0,
        "rehearsal_neutral_below": 5,
    }
    cfg_b = {
        "rehearsal_tau_base": 15,
        "rehearsal_tau_max": 60,
        "rehearsal_tau_scale": 10.0,
        "rehearsal_neutral_below": 5,
    }
    score_a = tree_match._compute_match_score("k", node, "substring", cfg=cfg_a)
    score_b = tree_match._compute_match_score("k", node, "substring", cfg=cfg_b)
    recency_a = tree_match._recency_bonus(node, cfg=cfg_a)
    recency_b = tree_match._recency_bonus(node, cfg=cfg_b)
    score_delta = score_a - score_b
    recency_delta = recency_a - recency_b
    assert abs(score_delta - recency_delta) < 1e-9, (
        f"score delta {score_delta} should equal recency delta {recency_delta}"
    )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_retrieval_count_none_treated_as_zero():
    """retrieval_count=None (YAML null) is treated as 0 -> TAU stays at base."""
    age_days = 30
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = _node(last_updated=d)
    node["retrieval_count"] = None
    bonus = tree_match._recency_bonus(node)
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(
        -age_days / tree_match.REHEARSAL_TAU_BASE
    )
    assert abs(bonus - expected) < 1e-9


def test_missing_retrieval_count_treated_as_zero():
    """Node dict missing retrieval_count entirely -> treated as 0."""
    age_days = 30
    d = (date.today() - timedelta(days=age_days)).isoformat()
    node = {"depth": 3, "last_updated": d}  # no retrieval_count key
    bonus = tree_match._recency_bonus(node)
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(
        -age_days / tree_match.REHEARSAL_TAU_BASE
    )
    assert abs(bonus - expected) < 1e-9


def _run_all():
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures.append((t.__name__, str(e) or "<no message>"))
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failures.append((t.__name__, f"{type(e).__name__}: {e}"))
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(_run_all())
