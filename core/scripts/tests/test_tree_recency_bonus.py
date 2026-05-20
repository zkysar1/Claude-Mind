"""test_tree_recency_bonus.py — recency-decay scoring layer (P2 #10).

Covers:
  - exp-decay shape: full bonus at age 0, falls toward 0 over TAU_DAYS
  - Missing last_updated → 0 bonus (legacy / unbackfilled nodes)
  - Malformed last_updated → 0 bonus (no crash)
  - Future-dated entries clamped to age=0 (clock skew safety)
  - Asymmetry: stale nodes get 0 bonus, never a penalty
  - Integration into _compute_match_score: total score includes recency
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
#  capture-restore pattern: stash env before module-level mutation
# so subsequent tests in the same pytest session don't inherit a popped
# MIND_AGENT. See test_applies_to_required.py for full rationale.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="tree-recency-test-")
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


def _node(last_updated=None, **extras):
    """Helper — minimal node dict with optional last_updated and extras."""
    n = {"depth": 3, "children": [], "child_count": 0}
    if last_updated is not None:
        n["last_updated"] = last_updated
    n.update(extras)
    return n


# ---------------------------------------------------------------------------
# _recency_bonus shape
# ---------------------------------------------------------------------------

def test_recency_bonus_full_at_age_zero():
    """A node updated today gets the full RECENCY_MAX_BONUS."""
    today = date.today().isoformat()
    bonus = tree_match._recency_bonus(_node(last_updated=today))
    assert abs(bonus - tree_match.RECENCY_MAX_BONUS) < 1e-9


def test_recency_bonus_decays_to_about_one_e_at_tau():
    """At age == TAU_DAYS, bonus ≈ MAX_BONUS / e (exponential half-life-ish)."""
    age_at_tau = date.today() - timedelta(days=tree_match.RECENCY_TAU_DAYS)
    bonus = tree_match._recency_bonus(_node(last_updated=age_at_tau.isoformat()))
    expected = tree_match.RECENCY_MAX_BONUS * math.exp(-1.0)
    assert abs(bonus - expected) < 1e-6


def test_recency_bonus_near_zero_at_5x_tau():
    """At 5×TAU days, bonus is essentially zero (~MAX_BONUS * exp(-5) ≈ 0.003)."""
    far_back = date.today() - timedelta(days=5 * tree_match.RECENCY_TAU_DAYS)
    bonus = tree_match._recency_bonus(_node(last_updated=far_back.isoformat()))
    assert bonus < 0.01


def test_recency_bonus_missing_last_updated_is_zero():
    """Legacy nodes without last_updated get no bonus — neutral, not penalty."""
    bonus = tree_match._recency_bonus(_node())  # no last_updated
    assert bonus == 0.0


def test_recency_bonus_null_last_updated_is_zero():
    """YAML null comes through as Python None — same as missing."""
    bonus = tree_match._recency_bonus(_node(last_updated=None))
    assert bonus == 0.0


def test_recency_bonus_empty_string_last_updated_is_zero():
    """Empty string is also treated as missing."""
    bonus = tree_match._recency_bonus(_node(last_updated=""))
    assert bonus == 0.0


def test_recency_bonus_malformed_date_is_zero():
    """Garbage date string returns 0, doesn't crash."""
    bonus = tree_match._recency_bonus(_node(last_updated="not-a-date"))
    assert bonus == 0.0


def test_recency_bonus_iso_datetime_handled():
    """ISO datetime (with T...) is parsed via the [:10] slice — date portion only."""
    today_iso = date.today().isoformat() + "T15:30:00"
    bonus = tree_match._recency_bonus(_node(last_updated=today_iso))
    assert abs(bonus - tree_match.RECENCY_MAX_BONUS) < 1e-9


def test_recency_bonus_future_date_clamped():
    """A future-dated last_updated (clock skew between agents) is clamped to
    age=0, not negative — bonus capped at MAX_BONUS, never amplified."""
    future = date.today() + timedelta(days=10)
    bonus = tree_match._recency_bonus(_node(last_updated=future.isoformat()))
    assert abs(bonus - tree_match.RECENCY_MAX_BONUS) < 1e-9


# ---------------------------------------------------------------------------
# Asymmetry: stale never penalized
# ---------------------------------------------------------------------------

def test_recency_bonus_never_negative():
    """For any valid date — past, present, future — bonus is in [0, MAX_BONUS]."""
    for delta_days in [-365, -90, -30, -7, 0, 7, 30, 90, 365]:
        d = date.today() + timedelta(days=delta_days)
        bonus = tree_match._recency_bonus(_node(last_updated=d.isoformat()))
        assert 0.0 <= bonus <= tree_match.RECENCY_MAX_BONUS + 1e-9, \
            f"bonus {bonus} out of range for delta {delta_days}"


# ---------------------------------------------------------------------------
# Integration into _compute_match_score
# ---------------------------------------------------------------------------

def test_compute_match_score_includes_recency():
    """A fresh node and a stale node, identical otherwise, differ by the
    recency bonus."""
    today = date.today().isoformat()
    long_ago = (date.today() - timedelta(days=180)).isoformat()
    fresh = _node(last_updated=today, capability_level="EXPLORE")
    stale = _node(last_updated=long_ago, capability_level="EXPLORE")
    s_fresh = tree_match._compute_match_score("k", fresh, "substring")
    s_stale = tree_match._compute_match_score("k", stale, "substring")
    delta = s_fresh - s_stale
    # ≈ MAX_BONUS at age=0 minus near-zero at 180d ≈ MAX_BONUS itself
    assert tree_match.RECENCY_MAX_BONUS * 0.95 <= delta <= tree_match.RECENCY_MAX_BONUS + 1e-9, \
        f"expected ~{tree_match.RECENCY_MAX_BONUS} delta, got {delta}"


def test_compute_match_score_missing_last_updated_no_penalty():
    """A node missing last_updated has the SAME score as it would after
    recency was added — i.e. recency contributes 0, no implicit penalty."""
    n = _node(capability_level="EXPLORE")  # no last_updated
    score = tree_match._compute_match_score("k", n, "substring")
    # Manually compute expected: substring 3.0 + d3 1.5 + cap 0 (EXPLORE) +
    # confidence 0 + recency 0 = 4.5
    assert score == 4.5, f"expected 4.5 (no recency contribution), got {score}"


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
