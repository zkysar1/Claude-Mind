"""test_tree_kd4_locality.py -- tests for the K=D=4 retrieval-locality contract
(g-306-13, BRD Gap 2): structural decompose/regroup candidate triggers + the
advisory validate_tree locality reporter.

Evidence base: Zhong et al., Phys Rev Lett 134:237402 (2025) -- random-tree
recall is bounded by K=4 children, D=4 retrieval depth, saturating at
K^(D-1)=64 leaves under any retrieval entry point. Design decision (HYBRID,
report-mode-first + enforce-going-forward; advisory exit 0): world board
msg-20260619-075228-bravo-086.

Covers:
  1. _subtree_leaf_counts -- pure post-order leaf counting (leaf=1, interior=sum,
     dangling-child-safe, cycle-safe).
  2. leaf_cap derivation from K_max / D_retrieval (single source of truth) + the
     explicit-override escape hatch.
  3. get_redistribute_candidates -- structural children>K_max (regroup) trigger,
     line-count trigger retired (outcome 3).
  4. get_decompose_candidates -- structural subtree-leaves>leaf_cap (decompose)
     trigger, root excluded, line-count trigger retired (outcome 3).
  5. validate_tree locality advisory -- signal computation, K/D/leaf counts,
     D grandfathering, and the ADVISORY invariant (violations never set
     valid=False / never become errors -> tree --validate exits 0).
"""

from __future__ import annotations

import sys
from pathlib import Path

# conftest.py already inserts core/scripts on sys.path; add it defensively so
# the module imports cleanly when run in isolation too.
SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import _paths  # noqa: E402
import tree  # noqa: E402


# --------------------------------------------------------------------------
# _subtree_leaf_counts (pure -- no config, no filesystem)
# --------------------------------------------------------------------------

def test_subtree_leaf_counts_leaf_is_one():
    assert tree._subtree_leaf_counts({"a": {"children": []}})["a"] == 1


def test_subtree_leaf_counts_interior_sums_children():
    nodes = {
        "root": {"children": ["a", "b"]},
        "a": {"children": ["a1", "a2"]},
        "b": {"children": []},
        "a1": {"children": []},
        "a2": {"children": []},
    }
    counts = tree._subtree_leaf_counts(nodes)
    assert counts["a"] == 2       # a1, a2
    assert counts["b"] == 1       # itself (leaf)
    assert counts["root"] == 3    # a1, a2, b


def test_subtree_leaf_counts_dangling_child_contributes_zero():
    # child 'ghost' absent from nodes -> contributes 0, no crash.
    nodes = {"root": {"children": ["real", "ghost"]}, "real": {"children": []}}
    assert tree._subtree_leaf_counts(nodes)["root"] == 1


def test_subtree_leaf_counts_cycle_safe():
    # a -> b -> a (malformed). Must not raise or infinite-loop; the absence of
    # an exception is the assertion (validate_tree owns flagging the cycle).
    counts = tree._subtree_leaf_counts({"a": {"children": ["b"]}, "b": {"children": ["a"]}})
    assert "a" in counts and "b" in counts


# --------------------------------------------------------------------------
# leaf_cap derivation
# --------------------------------------------------------------------------

def test_leaf_cap_derives_from_k_and_d():
    # Real config: K_max=4, D_retrieval=4 -> 4^3 = 64 (Zhong saturation point).
    assert tree._config_leaf_cap() == tree._config_k_max() ** (tree._config_d_retrieval() - 1)
    assert tree._config_leaf_cap() == 64


def test_leaf_cap_explicit_override(monkeypatch):
    monkeypatch.setattr(tree, "_merged_config",
                        lambda: {"config": {"K_max": 4, "D_retrieval": 4, "leaf_cap": 100}})
    assert tree._config_leaf_cap() == 100


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _patch_caps(monkeypatch, k_max=2, leaf_cap=2, d_retrieval=2):
    """Shrink the Zhong caps so tiny in-memory fixtures trip the triggers."""
    monkeypatch.setattr(tree, "_config_k_max", lambda: k_max)
    monkeypatch.setattr(tree, "_config_leaf_cap", lambda: leaf_cap)
    monkeypatch.setattr(tree, "_config_d_retrieval", lambda: d_retrieval)


def _patch_world(monkeypatch, tmp_path):
    # resolve_file_path reads _paths.WORLD_DIR; validate_tree's file/xref blocks
    # read tree.WORLD_DIR. file:None nodes skip both, but patch defensively.
    monkeypatch.setattr(_paths, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(tree, "WORLD_DIR", tmp_path)


# --------------------------------------------------------------------------
# get_redistribute_candidates -- structural children > K_max (regroup)
# --------------------------------------------------------------------------

def test_redistribute_flags_k_overflow(monkeypatch):
    _patch_caps(monkeypatch, k_max=2)
    nodes = {"nodes": {
        "p": {"depth": 1, "children": ["a", "b", "c"], "file": "f"},   # 3 > 2
        "a": {"depth": 2, "children": []},
        "b": {"depth": 2, "children": []},
        "c": {"depth": 2, "children": []},
    }}
    cands = tree.get_redistribute_candidates(nodes)
    assert [c["key"] for c in cands] == ["p"]
    assert cands[0]["reason"] == "k_overflow"
    assert cands[0]["recommended_action"] == "regroup"
    assert cands[0]["child_count"] == 3
    # line-count trigger retired (outcome 3): no line_count field is emitted.
    assert "line_count" not in cands[0]


def test_redistribute_within_cap_not_flagged(monkeypatch):
    _patch_caps(monkeypatch, k_max=4)
    nodes = {"nodes": {
        "p": {"depth": 1, "children": ["a", "b"], "file": "f"},        # 2 <= 4
        "a": {"depth": 2, "children": []},
        "b": {"depth": 2, "children": []},
    }}
    assert tree.get_redistribute_candidates(nodes) == []


def test_redistribute_leaves_skipped(monkeypatch):
    _patch_caps(monkeypatch, k_max=2)
    nodes = {"nodes": {"leaf": {"depth": 1, "children": []}}}
    res = tree.get_redistribute_candidates(nodes, include_skipped=True)
    assert res["candidates"] == []
    assert any(s["skip_reason"] == "no_children" for s in res["skipped"])


# --------------------------------------------------------------------------
# get_decompose_candidates -- structural subtree-leaves > leaf_cap (decompose)
# --------------------------------------------------------------------------

def test_decompose_flags_leaf_overflow(monkeypatch):
    _patch_caps(monkeypatch, k_max=10, leaf_cap=2)   # k_max high so only leaf rule fires
    nodes = {"nodes": {
        "root": {"depth": 0, "children": ["p"]},
        "p": {"depth": 1, "children": ["a", "b", "c"], "file": "f"},   # 3 leaves > 2
        "a": {"depth": 2, "children": []},
        "b": {"depth": 2, "children": []},
        "c": {"depth": 2, "children": []},
    }}
    cands = tree.get_decompose_candidates(nodes)
    pc = [c for c in cands if c["key"] == "p"]
    assert pc, "expected 'p' flagged as decompose candidate"
    assert pc[0]["reason"] == "leaf_overflow"
    assert pc[0]["recommended_action"] == "decompose"
    assert pc[0]["subtree_leaves"] == 3
    assert "line_count" not in pc[0]   # outcome 3: line-count trigger retired


def test_decompose_excludes_root(monkeypatch):
    _patch_caps(monkeypatch, leaf_cap=2)
    # root holds 3 leaves > cap but is the routing index (depth 0) -> excluded.
    nodes = {"nodes": {
        "root": {"depth": 0, "children": ["a", "b", "c"]},
        "a": {"depth": 1, "children": []},
        "b": {"depth": 1, "children": []},
        "c": {"depth": 1, "children": []},
    }}
    res = tree.get_decompose_candidates(nodes, include_skipped=True)
    assert "root" not in [c["key"] for c in res["candidates"]]
    assert any(s["node_key"] == "root" and s["skip_reason"] == "is_root" for s in res["skipped"])


def test_decompose_within_cap_not_flagged(monkeypatch):
    _patch_caps(monkeypatch, leaf_cap=64)
    nodes = {"nodes": {
        "root": {"depth": 0, "children": ["p"]},
        "p": {"depth": 1, "children": ["a"], "file": "f"},
        "a": {"depth": 2, "children": []},
    }}
    assert tree.get_decompose_candidates(nodes) == []


# --------------------------------------------------------------------------
# validate_tree locality advisory
# --------------------------------------------------------------------------

def test_validate_locality_signal_ok(monkeypatch, tmp_path):
    _patch_world(monkeypatch, tmp_path)
    _patch_caps(monkeypatch, k_max=4, leaf_cap=64)
    nodes = {"nodes": {
        "root": {"file": None, "depth": 0, "parent": None, "children": ["a"],
                 "child_count": 1, "node_type": "interior"},
        "a": {"file": None, "depth": 1, "parent": "root", "children": [],
              "child_count": 0, "node_type": "leaf"},
    }}
    res = tree.validate_tree(nodes)
    assert res["locality"]["signal"] == "ok"
    assert res["locality"]["k_violations"]["count"] == 0
    assert res["locality"]["leaf_violations"]["count"] == 0


def test_validate_locality_is_advisory_not_error(monkeypatch, tmp_path):
    _patch_world(monkeypatch, tmp_path)
    _patch_caps(monkeypatch, k_max=2, leaf_cap=2)
    # 'p' has 3 children (>k_max) AND 3 leaves (>leaf_cap) -> both violations.
    nodes = {"nodes": {
        "root": {"file": None, "depth": 0, "parent": None, "children": ["p"],
                 "child_count": 1, "node_type": "interior"},
        "p": {"file": None, "depth": 1, "parent": "root", "children": ["a", "b", "c"],
              "child_count": 3, "node_type": "interior"},
        "a": {"file": None, "depth": 2, "parent": "p", "children": [], "child_count": 0, "node_type": "leaf"},
        "b": {"file": None, "depth": 2, "parent": "p", "children": [], "child_count": 0, "node_type": "leaf"},
        "c": {"file": None, "depth": 2, "parent": "p", "children": [], "child_count": 0, "node_type": "leaf"},
    }}
    res = tree.validate_tree(nodes)
    # ADVISORY invariant: locality violations MUST NOT invalidate the tree.
    assert res["valid"] is True
    assert res["locality"]["signal"] == "decompose+regroup"
    assert res["locality"]["k_violations"]["count"] == 1
    assert res["locality"]["leaf_violations"]["count"] == 1
    # Violations surface as warnings only -- never errors.
    assert not any("K=D=4 locality" in e for e in res["errors"])
    assert any("K=D=4 locality" in w for w in res["warnings"])


def test_validate_locality_d_grandfathered(monkeypatch, tmp_path):
    _patch_world(monkeypatch, tmp_path)
    _patch_caps(monkeypatch, k_max=4, leaf_cap=64, d_retrieval=2)
    # 'c' at depth 3 > d_retrieval(2) -> D violation, grandfathered. No K/leaf
    # violation -> signal stays "ok" (D is advisory only, not actionable).
    nodes = {"nodes": {
        "root": {"file": None, "depth": 0, "parent": None, "children": ["a"], "child_count": 1, "node_type": "interior"},
        "a": {"file": None, "depth": 1, "parent": "root", "children": ["b"], "child_count": 1, "node_type": "interior"},
        "b": {"file": None, "depth": 2, "parent": "a", "children": ["c"], "child_count": 1, "node_type": "interior"},
        "c": {"file": None, "depth": 3, "parent": "b", "children": [], "child_count": 0, "node_type": "leaf"},
    }}
    res = tree.validate_tree(nodes)
    assert res["valid"] is True
    assert res["locality"]["d_violations"]["count"] == 1
    assert res["locality"]["d_violations"]["grandfathered"] is True
    assert res["locality"]["signal"] == "ok"
