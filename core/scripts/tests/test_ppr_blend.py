"""test_ppr_blend.py -- unit tests for the PPR retrieval blend ().

Covers BRD Gap 1b+1c (child 3/4 of g-306-15; HippoRAG 2405.14831): a flag-gated,
boost-only, null-safe Personalized-PageRank factor folded into the tree-node
effective score (retrieve._score_weight_limit), seeded from the top baseline
(token-overlap) matches over the Mind knowledge-graph.

The two load-bearing invariants under test mirror the poignancy blend (g-306-08):
  1. DEFAULT-OFF NEUTRALITY -- with ppr_blend_enabled false (the shipped
     default), _ppr_weight is 1.0 for every node AND _score_weight_limit skips
     the PPR pass entirely, so ranking is byte-identical to pre-g-306-44. This
     is the "existing retrieve.sh tests still pass with flag off / no regression"
     verification outcome.
  2. BOOST-ONLY -- with the blend ON, the factor is always >= 1.0 (min is 1.0),
     so the blend can PROMOTE graph-proximate records but never demote anything.

Pure stdlib. retrieve.py is imported via importlib against a scratch MIND_WORLD
(same bootstrap as test_poignancy_blend.py); the functions under test are pure
and do not touch the live world. The graph-dependent path (_compute_ppr_scores
calling the real knowledge-graph) is exercised with a mocked PPR module so the
tests are deterministic and do not depend on meta/knowledge-graph.jsonl.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Bind retrieve.py to a scratch world before import (resolves paths from
# MIND_WORLD at module load). Capture/restore so sibling tests don't inherit.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="ppr-blend-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_RETRIEVE_PATH = CORE_SCRIPTS / "retrieve.py"
_spec = importlib.util.spec_from_file_location("retrieve_ppr_mod", _RETRIEVE_PATH)
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

PPR_CFG_OFF = {"ppr_blend_enabled": False, "ppr_weight_min": 1.0, "ppr_weight_max": 1.5}
PPR_CFG_ON = {"ppr_blend_enabled": True, "ppr_weight_min": 1.0, "ppr_weight_max": 1.5}


# ── _ppr_weight: the pure factor ─────────────────────────────────────────────

def test_ppr_weight_flag_off_is_neutral_even_for_max_score():
    # Flag off short-circuits BEFORE reading the score -- always 1.0.
    assert _retrieve._ppr_weight("node:x", {"node:x": 1.0}, PPR_CFG_OFF) == 1.0


def test_ppr_weight_empty_scores_is_neutral():
    assert _retrieve._ppr_weight("node:x", {}, PPR_CFG_ON) == 1.0
    assert _retrieve._ppr_weight("node:x", None, PPR_CFG_ON) == 1.0


def test_ppr_weight_absent_node_is_neutral():
    assert _retrieve._ppr_weight("node:y", {"node:x": 1.0}, PPR_CFG_ON) == 1.0


def test_ppr_weight_min_at_norm_0():
    assert _retrieve._ppr_weight("node:x", {"node:x": 0.0}, PPR_CFG_ON) == 1.0


def test_ppr_weight_max_at_norm_1():
    assert _retrieve._ppr_weight("node:x", {"node:x": 1.0}, PPR_CFG_ON) == 1.5


def test_ppr_weight_linear_midpoint():
    expected = 1.0 + 0.5 * (1.5 - 1.0)
    assert abs(_retrieve._ppr_weight("node:x", {"node:x": 0.5}, PPR_CFG_ON) - expected) < 1e-9


def test_ppr_weight_is_boost_only():
    for norm in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert _retrieve._ppr_weight("node:x", {"node:x": norm}, PPR_CFG_ON) >= 1.0


# ── _compute_ppr_scores: seeding + normalization + fail-open ─────────────────

def test_compute_ppr_scores_flag_off_is_empty():
    assert _retrieve._compute_ppr_scores(["node:x"], PPR_CFG_OFF) == {}


def test_compute_ppr_scores_no_seeds_is_empty():
    assert _retrieve._compute_ppr_scores([], PPR_CFG_ON) == {}


def _patch_ppr_module(mod):
    prev = _retrieve._load_ppr_module
    _retrieve._load_ppr_module = lambda: mod
    return prev


def test_compute_ppr_scores_fail_open_when_module_missing():
    prev = _patch_ppr_module(None)
    try:
        assert _retrieve._compute_ppr_scores(["node:x"], PPR_CFG_ON) == {}
    finally:
        _retrieve._load_ppr_module = prev


def test_compute_ppr_scores_fail_open_when_compute_raises():
    def _boom(seeds, exclude_pseudo=False):
        raise RuntimeError("graph unavailable")
    prev = _patch_ppr_module(types.SimpleNamespace(compute=_boom))
    try:
        assert _retrieve._compute_ppr_scores(["node:x"], PPR_CFG_ON) == {}
    finally:
        _retrieve._load_ppr_module = prev


def test_compute_ppr_scores_normalizes_to_max():
    fake = types.SimpleNamespace(
        compute=lambda seeds, exclude_pseudo=False: (
            [("a", 0.6), ("b", 0.3), ("c", 0.0)], {}))
    prev = _patch_ppr_module(fake)
    try:
        scores = _retrieve._compute_ppr_scores(["a"], PPR_CFG_ON)
    finally:
        _retrieve._load_ppr_module = prev
    assert scores["a"] == 1.0               # 0.6 / 0.6
    assert abs(scores["b"] - 0.5) < 1e-9    # 0.3 / 0.6
    assert scores["c"] == 0.0


def test_compute_ppr_scores_empty_ranking_is_empty():
    fake = types.SimpleNamespace(compute=lambda seeds, exclude_pseudo=False: ([], {}))
    prev = _patch_ppr_module(fake)
    try:
        assert _retrieve._compute_ppr_scores(["a"], PPR_CFG_ON) == {}
    finally:
        _retrieve._load_ppr_module = prev


def test_load_ppr_module_loads_real_module():
    # The real hyphen-named knowledge-graph-ppr.py loads + exposes compute().
    mod = _retrieve._load_ppr_module()
    assert mod is not None
    assert hasattr(mod, "compute")
    assert hasattr(mod, "personalized_pagerank")


# ── _score_weight_limit: tree-node integration path ──────────────────────────

def _with_cached_cfg(cfg, fn):
    prev = _retrieve._RETRIEVAL_CFG_CACHE
    _retrieve._RETRIEVAL_CFG_CACHE = cfg
    try:
        return fn()
    finally:
        _retrieve._RETRIEVAL_CFG_CACHE = prev


_SWL_CFG_OFF = {
    "utility_weight_min": 0.5,
    "utility_weight_max": 1.5,
    "utility_weight_neutral_below_retrievals": 5,
    "poignancy_blend_enabled": False,
    "poignancy_weight_min": 1.0,
    "poignancy_weight_max": 1.5,
    "ppr_blend_enabled": False,
    "ppr_weight_min": 1.0,
    "ppr_weight_max": 1.5,
    "ppr_seed_top_n": 5,
}
_SWL_CFG_ON = {**_SWL_CFG_OFF, "ppr_blend_enabled": True}


def _two_nodes_equal_base():
    # retrieval_count=0 < neutral threshold -> _utility_weight is 1.0 for both;
    # poignancy off -> 1.0; identical fields -> equal base*util*poignancy, so the
    # PPR factor is the sole differentiator.
    common = {"depth": 2, "confidence": 0.5,
              "capability_level": "EXPLOIT", "retrieval_count": 0}
    matched = [("aaa", dict(common)), ("bbb", dict(common))]
    channels = {"aaa": "exact", "bbb": "exact"}
    return matched, channels


def _effective_by_key(scored):
    return {key: eff for key, _node, eff, _ch, _base, _w in scored}


def test_score_weight_limit_ppr_off_leaves_effective_score_identical():
    matched, channels = _two_nodes_equal_base()
    scored = _with_cached_cfg(
        _SWL_CFG_OFF,
        lambda: _retrieve._score_weight_limit(matched, channels, limit=10))
    eff = _effective_by_key(scored)
    # Flag off -> PPR pass skipped -> identical effective scores (no regression).
    assert eff["aaa"] == eff["bbb"]


def test_score_weight_limit_ppr_on_boosts_proximate_node():
    matched, channels = _two_nodes_equal_base()

    # Mock the graph: bbb is PPR-proximate to the seeds (norm 1.0), aaa is not.
    def _fake_scores(seed_keys, cfg=None):
        return {"node:bbb": 1.0, "node:aaa": 0.0}

    prev = _retrieve._compute_ppr_scores
    _retrieve._compute_ppr_scores = _fake_scores
    try:
        scored = _with_cached_cfg(
            _SWL_CFG_ON,
            lambda: _retrieve._score_weight_limit(matched, channels, limit=10))
    finally:
        _retrieve._compute_ppr_scores = prev
    eff = _effective_by_key(scored)
    # bbb boosted x1.5 (norm 1.0 -> max), aaa x1.0 (norm 0.0 -> min); equal base
    # means bbb's effective is exactly 1.5x aaa's and it sorts to the top.
    assert eff["bbb"] > eff["aaa"]
    assert abs(eff["bbb"] - eff["aaa"] * 1.5) < 1e-9
    assert scored[0][0] == "bbb"


def test_score_weight_limit_ppr_on_empty_scores_no_reorder():
    # Flag on but PPR returns {} (graph unavailable) -> baseline order preserved.
    matched, channels = _two_nodes_equal_base()
    prev = _retrieve._compute_ppr_scores
    _retrieve._compute_ppr_scores = lambda seed_keys, cfg=None: {}
    try:
        scored = _with_cached_cfg(
            _SWL_CFG_ON,
            lambda: _retrieve._score_weight_limit(matched, channels, limit=10))
    finally:
        _retrieve._compute_ppr_scores = prev
    eff = _effective_by_key(scored)
    assert eff["aaa"] == eff["bbb"]


# ── Config + schema defaults ─────────────────────────────────────────────────

def test_default_retrieval_cfg_ships_ppr_flag_off():
    cfg = _retrieve._DEFAULT_RETRIEVAL_CFG
    assert cfg["ppr_blend_enabled"] is False
    assert cfg["ppr_weight_min"] == 1.0
    assert cfg["ppr_weight_max"] == 1.5
    assert cfg["ppr_seed_top_n"] == 5


# ── Graph-key derivation ( inert-blend fix) ──────────────────────────
# knowledge-graph-build.py keys tree nodes by tree-root-relative POSIX path, but
# load_tree_nodes keys them by basename. The blend first shipped seeding
# "node:"+basename, which matched ZERO graph nodes -> the blend was silently
# inert on real data. 's multi-hop validation found this; the fix derives
# the graph key from the candidate's `file` field. These guard the regression.

def test_graph_node_key_candidates_path_form_first():
    node = {"file": "world/knowledge/tree/execution/ayoai-development-patterns/framework-patterns.md"}
    cands = _retrieve._graph_node_key_candidates("framework-patterns", node)
    # Path form (matches the graph) first, basename fallback second.
    assert cands[0] == "node:execution/ayoai-development-patterns/framework-patterns"
    assert "node:framework-patterns" in cands


def test_graph_node_key_candidates_no_file_basename_only():
    # Synthetic test nodes (and any node without a `file`) fall back to basename,
    # preserving the unit-test path the rest of this file exercises.
    assert _retrieve._graph_node_key_candidates("x", {}) == ["node:x"]
    assert _retrieve._graph_node_key_candidates("x", None) == ["node:x"]


def test_graph_node_key_candidates_normalizes_backslashes():
    node = {"file": r"world\knowledge\tree\system\foo\bar.md"}
    cands = _retrieve._graph_node_key_candidates("bar", node)
    assert cands[0] == "node:system/foo/bar"


def test_resolve_ppr_key_prefers_present_path_form():
    node = {"file": "world/knowledge/tree/cat/sub/aaa.md"}
    # Only the path form is in the ranking (mirrors the real graph namespace).
    ppr = {"node:cat/sub/aaa": 0.7}
    assert _retrieve._resolve_ppr_key("aaa", node, ppr) == "node:cat/sub/aaa"


def test_resolve_ppr_key_falls_back_to_first_candidate_when_absent():
    node = {"file": "world/knowledge/tree/cat/sub/aaa.md"}
    assert _retrieve._resolve_ppr_key("aaa", node, {}) == "node:cat/sub/aaa"


def test_score_weight_limit_ppr_boosts_via_path_key_not_basename():
    # Regression guard for the inert-blend bug (): a candidate whose
    # PATH-form graph key is in the PPR ranking -- but whose basename form is NOT
    # -- must still be boosted. The pre-fix code looked up "node:"+basename, found
    # nothing, and applied no boost (the silent no-op on real data).
    common = {"depth": 2, "confidence": 0.5, "capability_level": "EXPLOIT",
              "retrieval_count": 0, "file": "world/knowledge/tree/cat/sub/aaa.md"}
    other = {"depth": 2, "confidence": 0.5, "capability_level": "EXPLOIT",
             "retrieval_count": 0, "file": "world/knowledge/tree/cat/sub/bbb.md"}
    matched = [("aaa", dict(common)), ("bbb", dict(other))]
    channels = {"aaa": "exact", "bbb": "exact"}

    # Ranking keyed by PATH form only; basename forms ("node:aaa"/"node:bbb")
    # are intentionally absent -- exactly how the real knowledge-graph keys nodes.
    def _fake_scores(seed_keys, cfg=None):
        return {"node:cat/sub/bbb": 1.0, "node:cat/sub/aaa": 0.0}

    prev = _retrieve._compute_ppr_scores
    _retrieve._compute_ppr_scores = _fake_scores
    try:
        scored = _with_cached_cfg(
            _SWL_CFG_ON,
            lambda: _retrieve._score_weight_limit(matched, channels, limit=10))
    finally:
        _retrieve._compute_ppr_scores = prev
    eff = _effective_by_key(scored)
    # bbb resolves to node:cat/sub/bbb (norm 1.0 -> x1.5); aaa to node:cat/sub/aaa
    # (norm 0.0 -> x1.0). Pre-fix both resolved to absent basename keys -> x1.0,
    # x1.0 -> no reorder. This asserts the path-keyed boost fires.
    assert eff["bbb"] > eff["aaa"]
    assert abs(eff["bbb"] - eff["aaa"] * 1.5) < 1e-9
    assert scored[0][0] == "bbb"
