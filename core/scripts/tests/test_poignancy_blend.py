"""test_poignancy_blend.py — unit tests for the poignancy retrieval blend.

Covers g-306-08 (BRD Gap 1a; Generative Agents 2304.03442): a flag-gated,
boost-only, null-safe poignancy factor folded into both the tree-node
effective score (retrieve._score_weight_limit) and the supplementary-store
sort key (retrieve._sort_by_utility).

The two load-bearing invariants under test:
  1. DEFAULT-OFF NEUTRALITY — with the blend flag false (the shipped default),
     the poignancy factor is 1.0 for every record and ranking is byte-identical
     to pre-g-306-08. This is the "default current behavior" verification
     outcome.
  2. BOOST-ONLY — with the blend ON, the factor is always >= 1.0 (min is 1.0),
     so the blend can PROMOTE high-poignancy records but never demote anything
     below its current effective score. A bounded boost (max +0.5 additive on
     the supplementary sort signal) cannot overturn a meaningful utility lead —
     this is the "no known-good knowledge hidden" verification outcome.

Pure stdlib. retrieve.py is imported via importlib against a scratch
MIND_WORLD (same bootstrap as test_retrieve_supplementary_filter.py); the
functions under test are pure and do not touch the live world.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Bind retrieve.py to a scratch world before import (it resolves paths from
# MIND_WORLD at module load). Capture/restore so sibling tests don't inherit.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="poignancy-blend-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_RETRIEVE_PATH = CORE_SCRIPTS / "retrieve.py"
_spec = importlib.util.spec_from_file_location("retrieve_poignancy_mod", _RETRIEVE_PATH)
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

# Restore the captured env immediately after import.
if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

CFG_OFF = {
    "poignancy_blend_enabled": False,
    "poignancy_weight_min": 1.0,
    "poignancy_weight_max": 1.5,
}
CFG_ON = {
    "poignancy_blend_enabled": True,
    "poignancy_weight_min": 1.0,
    "poignancy_weight_max": 1.5,
}


# ── _poignancy_weight: the pure factor ───────────────────────────────────────

def test_factor_flag_off_is_neutral_even_for_max_poignancy():
    # Flag off short-circuits BEFORE reading poignancy — always 1.0.
    assert _retrieve._poignancy_weight({"poignancy": 10}, CFG_OFF) == 1.0


def test_factor_null_degrades_to_floor_when_center_unset():
    # RENAMED from test_factor_null_poignancy_is_neutral (). The
    # assertions are unchanged; the old NAME asserted a contract that is false.
    # 1.0 is poignancy_weight_min — the FLOOR — so this is not "neutral", it is
    # "assume worst". What this test actually pins is the DEGRADE-SAFE property:
    # CFG_ON carries no poignancy_weight_center, and a missing key must reproduce
    # the pre-fix behaviour exactly rather than a value the config cannot vouch
    # for. See test_factor_null_is_centered_when_center_set for the live contract.
    assert _retrieve._poignancy_weight({}, CFG_ON) == 1.0
    assert _retrieve._poignancy_weight({"poignancy": None}, CFG_ON) == 1.0


def test_factor_min_at_poignancy_1():
    assert _retrieve._poignancy_weight({"poignancy": 1}, CFG_ON) == 1.0


def test_factor_max_at_poignancy_10():
    assert _retrieve._poignancy_weight({"poignancy": 10}, CFG_ON) == 1.5


def test_factor_linear_midpoint():
    # p=5 -> 1.0 + (5-1)/9 * (1.5-1.0)
    expected = 1.0 + (5 - 1) / 9.0 * 0.5
    assert abs(_retrieve._poignancy_weight({"poignancy": 5}, CFG_ON) - expected) < 1e-9


def test_factor_clamps_above_10():
    assert _retrieve._poignancy_weight({"poignancy": 15}, CFG_ON) == 1.5


def test_factor_clamps_below_1():
    assert _retrieve._poignancy_weight({"poignancy": 0}, CFG_ON) == 1.0


def test_factor_unparseable_degrades_to_floor_when_center_unset():
    # RENAMED () for the same reason as the null case above: with no
    # center configured an unparseable rating reproduces the pre-fix floor.
    assert _retrieve._poignancy_weight({"poignancy": "high"}, CFG_ON) == 1.0


def test_factor_is_boost_only():
    # Across the whole valid range, the factor is never below 1.0 (min) when on.
    for p in range(1, 11):
        assert _retrieve._poignancy_weight({"poignancy": p}, CFG_ON) >= 1.0


# ── null centering () ──────────────────────────────────────────────
# Null used to short-circuit to 1.0 == poignancy_weight_min, the FLOOR of the
# output range, so an unrated record ranked as if it were the least significant
# thing in the corpus and could never be promoted at any k. These pin the fixed
# contract: null enters the SAME linear map at `center`, leaving the rated curve
# untouched and every factor inside [min, max].

CFG_CENTERED = dict(CFG_ON, poignancy_weight_center=6.5822)


def test_factor_null_is_centered_when_center_set():
    expected = 1.0 + (6.5822 - 1.0) / 9.0 * 0.5
    for record in ({}, {"poignancy": None}, {"poignancy": "high"}):
        assert abs(_retrieve._poignancy_weight(record, CFG_CENTERED) - expected) < 1e-9
    # And it is strictly above the floor — the whole point of the fix.
    assert _retrieve._poignancy_weight({}, CFG_CENTERED) > 1.0


def test_centering_leaves_the_rated_mapping_byte_identical():
    # Shape (a): only the null INPUT moved. A record that carries a real rating
    # must score exactly what it scored before, or this is a ranking change for
    # rated records rather than a null fix.
    for p in (1, 2, 3, 5, 6.8, 7, 9, 10, 0, 15):
        assert (_retrieve._poignancy_weight({"poignancy": p}, CFG_ON)
                == _retrieve._poignancy_weight({"poignancy": p}, CFG_CENTERED))


def test_centering_preserves_boost_only_for_every_input():
    # The A/B harness's displacement bound assumes factor >= min for EVERY
    # record. Includes adversarial inputs and an out-of-range center, which the
    # post-substitution clamp must contain.
    lo, hi = 1.0, 1.5
    for cfg in (CFG_CENTERED, dict(CFG_ON, poignancy_weight_center=99),
                dict(CFG_ON, poignancy_weight_center=-99),
                dict(CFG_ON, poignancy_weight_center=None)):
        for p in (None, "x", -99, 0, 1, 5, 10, 99, 10 ** 6):
            assert lo <= _retrieve._poignancy_weight({"poignancy": p}, cfg) <= hi


def test_centering_never_overrides_the_master_flag():
    # A configured center must not resurrect the blend while it is disabled.
    assert _retrieve._poignancy_weight(
        {"poignancy": None}, dict(CFG_OFF, poignancy_weight_center=6.5822)) == 1.0


def test_shipped_center_is_above_the_floor_and_in_range():
    # The measured constant ships in core/config/tree.yaml; _DEFAULT_RETRIEVAL_CFG
    # deliberately keeps 1.0 so an ABSENT key degrades to today (see the null test
    # above). Guard the shipped value against a rating that is out of the 1-10
    # domain, which would silently clamp instead of failing.
    import yaml
    from pathlib import Path
    # Derived from __file__, never cwd — this test must not depend on where
    # pytest was invoked from (core/scripts/tests/ -> ../../.. == PROJECT_ROOT).
    tree_yaml = Path(__file__).resolve().parents[3] / "core" / "config" / "tree.yaml"
    cfg = yaml.safe_load(tree_yaml.read_text(encoding="utf-8"))["retrieval"]
    center = float(cfg["poignancy_weight_center"])
    assert 1.0 < center <= 10.0, f"shipped center {center} is not a usable poignancy"
    assert _retrieve._DEFAULT_RETRIEVAL_CFG["poignancy_weight_center"] == 1.0


# ── _sort_by_utility: supplementary-store ordering ───────────────────────────

def _with_cached_cfg(cfg, fn):
    """Run fn() with retrieve._RETRIEVAL_CFG_CACHE pinned to cfg, then restore.
    _sort_by_utility reads the cached config (no cfg param), so tests must pin
    it rather than pass it."""
    prev = _retrieve._RETRIEVAL_CFG_CACHE
    _retrieve._RETRIEVAL_CFG_CACHE = cfg
    try:
        return fn()
    finally:
        _retrieve._RETRIEVAL_CFG_CACHE = prev


def test_sort_flag_off_orders_by_utilization_only():
    recs = [
        {"id": "hiP", "poignancy": 10, "utilization": {"utilization_score": 1}, "created": "2026-01-02"},
        {"id": "loP", "poignancy": 1, "utilization": {"utilization_score": 2}, "created": "2026-01-01"},
    ]
    _with_cached_cfg(CFG_OFF, lambda: _retrieve._sort_by_utility(recs))
    # Pure utilization order: loP(2) before hiP(1). Poignancy ignored when off.
    assert [r["id"] for r in recs] == ["loP", "hiP"]


def test_sort_flag_on_promotes_high_poignancy_at_equal_utility():
    recs = [
        {"id": "loP", "poignancy": 1, "utilization": {"utilization_score": 2}, "created": "2026-01-01"},
        {"id": "hiP", "poignancy": 10, "utilization": {"utilization_score": 2}, "created": "2026-01-01"},
    ]
    _with_cached_cfg(CFG_ON, lambda: _retrieve._sort_by_utility(recs))
    assert recs[0]["id"] == "hiP"


def test_sort_flag_on_breaks_zero_utilization_ties_by_poignancy():
    recs = [
        {"id": "z", "poignancy": 2, "utilization": {"utilization_score": 0}, "created": "2026-01-01"},
        {"id": "y", "poignancy": 9, "utilization": {"utilization_score": 0}, "created": "2026-01-01"},
    ]
    _with_cached_cfg(CFG_ON, lambda: _retrieve._sort_by_utility(recs))
    assert recs[0]["id"] == "y"


def test_sort_flag_on_does_not_flip_large_utility_lead():
    # The "no known-good knowledge hidden" invariant: a bounded boost (max +0.5)
    # cannot overturn a meaningful utility gap. strong=5+0 beats weak=1+0.5.
    recs = [
        {"id": "strong", "poignancy": 1, "utilization": {"utilization_score": 5}, "created": "2026-01-01"},
        {"id": "weak", "poignancy": 10, "utilization": {"utilization_score": 1}, "created": "2026-01-01"},
    ]
    _with_cached_cfg(CFG_ON, lambda: _retrieve._sort_by_utility(recs))
    assert recs[0]["id"] == "strong"


def test_sort_flag_on_null_poignancy_records_unaffected():
    # Guardrails / pattern signatures carry no poignancy — they must sort exactly
    # as before (null -> +0.0 bonus).
    recs = [
        {"id": "a", "utilization": {"utilization_score": 3}, "created": "2026-01-01"},
        {"id": "b", "utilization": {"utilization_score": 5}, "created": "2026-01-01"},
    ]
    _with_cached_cfg(CFG_ON, lambda: _retrieve._sort_by_utility(recs))
    assert [r["id"] for r in recs] == ["b", "a"]


# ── _score_weight_limit: tree-node effective-score integration path ──────────
#
#  (discovered_by ): the suite above covers _poignancy_weight
# (the pure factor) and _sort_by_utility (the supplementary-store sort) but NOT
# _score_weight_limit, the tree-node scoring path where the poignancy factor
# multiplies base*utility into the effective score. _score_weight_limit reads
# config via _load_retrieval_config() (no cfg param) and passes that cfg to BOTH
# _utility_weight and _poignancy_weight, so the pinned cache must carry the
# utility keys too. Two nodes identical in every score-bearing field except
# poignancy isolate the factor: base*utility is equal, so the only effective-
# score difference is the poignancy multiplier.

_SWL_CFG_OFF = {
    "utility_weight_min": 0.5,
    "utility_weight_max": 1.5,
    "utility_weight_neutral_below_retrievals": 5,
    "poignancy_blend_enabled": False,
    "poignancy_weight_min": 1.0,
    "poignancy_weight_max": 1.5,
}
_SWL_CFG_ON = {**_SWL_CFG_OFF, "poignancy_blend_enabled": True}


def _two_nodes_identical_except_poignancy():
    # retrieval_count=0 is below utility_weight_neutral_below_retrievals (5), so
    # _utility_weight returns the neutral 1.0 for both nodes -> base*utility is
    # equal and the poignancy factor is the sole differentiator.
    common = {
        "depth": 2,
        "confidence": 0.5,
        "capability_level": "EXPLOIT",
        "retrieval_count": 0,
    }
    hi = {**common, "poignancy": 10}
    lo = {**common, "poignancy": 1}
    matched = [("hiP", hi), ("loP", lo)]
    channels = {"hiP": "exact", "loP": "exact"}
    return matched, channels


def _effective_by_key(scored):
    # _score_weight_limit returns (key, node, effective, channel, base, w) tuples.
    return {key: eff for key, _node, eff, _ch, _base, _w in scored}


def test_score_weight_limit_flag_off_leaves_effective_score_identical():
    matched, channels = _two_nodes_identical_except_poignancy()
    scored = _with_cached_cfg(
        _SWL_CFG_OFF,
        lambda: _retrieve._score_weight_limit(matched, channels, limit=10),
    )
    eff = _effective_by_key(scored)
    # Flag off -> poignancy factor 1.0 for both -> byte-identical effective score.
    assert eff["hiP"] == eff["loP"]


def test_score_weight_limit_flag_on_boosts_high_poignancy_node():
    matched, channels = _two_nodes_identical_except_poignancy()
    scored = _with_cached_cfg(
        _SWL_CFG_ON,
        lambda: _retrieve._score_weight_limit(matched, channels, limit=10),
    )
    eff = _effective_by_key(scored)
    # Flag on -> poignancy-10 factor 1.5 vs poignancy-1 factor 1.0; identical
    # base*utility means hiP's effective score is exactly 1.5x loP's, and it
    # sorts to the top.
    assert eff["hiP"] > eff["loP"]
    assert abs(eff["hiP"] - eff["loP"] * 1.5) < 1e-9
    assert scored[0][0] == "hiP"


# ── Config + schema defaults ─────────────────────────────────────────────────

def test_default_retrieval_cfg_ships_flag_off():
    cfg = _retrieve._DEFAULT_RETRIEVAL_CFG
    assert cfg["poignancy_blend_enabled"] is False
    assert cfg["poignancy_weight_min"] == 1.0
    assert cfg["poignancy_weight_max"] == 1.5


def test_rb_default_fields_carry_null_poignancy():
    # The reasoning-bank store ships poignancy=None by default (null-safe,
    # additive — the rb validator has no unknown-field gate).
    # CORE_SCRIPTS = core/scripts -> .parent.parent = PROJECT_ROOT.
    sys.path.insert(0, str(CORE_SCRIPTS.parent.parent / "mind_api" / "src"))
    import store_registry as S
    assert "poignancy" in S.RB_DEFAULT_FIELDS
    assert S.RB_DEFAULT_FIELDS["poignancy"] is None
