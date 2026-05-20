"""test_tree_mmr_diversity.py — MMR (Maximal Marginal Relevance) diversity
re-ranker (P2 #11).

Covers:
  - _path_chain walks parent pointers to root, root-first, cycle-safe
  - _path_similarity: siblings ≈ 0.83, cousins lower, different branches lowest
  - _mmr_rerank no-op when len(scored) <= limit
  - MMR demotes a same-parent sibling when a non-sibling alternative is close
    in relevance (the canonical "5 siblings crowding out alternatives" case)
  - High-relevance sibling still wins when alternative is far below in score
  - Top-1 always preserved (highest relevance picked first)
  - Lambda=1.0 → pure relevance (degenerates to sort-by-score)
  - Lambda=0.0 → pure diversity (top-1 still by score; rest by anti-similarity)
  - Integration into _score_and_limit: MMR runs only when all_nodes provided
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

#  capture-restore pattern: stash env before module-level mutation
# so subsequent tests in the same pytest session don't inherit a popped
# MIND_AGENT. See test_applies_to_required.py for full rationale.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="tree-mmr-test-")
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


def _make_tree(spec):
    """spec: dict[key] = parent_key (None for root). Returns nodes dict
    suitable for _path_chain etc."""
    nodes = {}
    for key, parent in spec.items():
        n = {"children": [], "child_count": 0}
        if parent is not None:
            n["parent"] = parent
        nodes[key] = n
    # Fill children lists for completeness (not used by MMR but realistic)
    for key, parent in spec.items():
        if parent and parent in nodes:
            nodes[parent]["children"].append(key)
            nodes[parent]["child_count"] = len(nodes[parent]["children"])
    return nodes


# ---------------------------------------------------------------------------
# _path_chain
# ---------------------------------------------------------------------------

def test_path_chain_walks_root_first():
    nodes = _make_tree({
        "root": None,
        "L1": "root",
        "L2": "L1",
        "leaf": "L2",
    })
    chain = tree_match._path_chain(nodes, "leaf")
    assert chain == ["root", "L1", "L2", "leaf"]


def test_path_chain_root_node_is_single_element():
    nodes = _make_tree({"root": None})
    assert tree_match._path_chain(nodes, "root") == ["root"]


def test_path_chain_cycle_safe():
    """A malformed tree with a cycle must terminate, not loop forever."""
    nodes = {
        "a": {"parent": "b"},
        "b": {"parent": "a"},
    }
    chain = tree_match._path_chain(nodes, "a", max_hops=10)
    assert "a" in chain and "b" in chain
    assert len(chain) <= 10


# ---------------------------------------------------------------------------
# _path_similarity
# ---------------------------------------------------------------------------

def test_path_similarity_siblings_high():
    nodes = _make_tree({
        "root": None, "L1": "root", "L2": "L1",
        "sib_a": "L2", "sib_b": "L2",
    })
    chain_a = tree_match._path_chain(nodes, "sib_a")  # [root, L1, L2, sib_a]
    chain_b = tree_match._path_chain(nodes, "sib_b")  # [root, L1, L2, sib_b]
    sim = tree_match._path_similarity(chain_a, chain_b)
    assert abs(sim - 3 / 4) < 1e-9, f"siblings sim should be 0.75, got {sim}"


def test_path_similarity_cousins_lower():
    nodes = _make_tree({
        "root": None, "L1": "root", "L2x": "L1", "L2y": "L1",
        "cous_a": "L2x", "cous_b": "L2y",
    })
    chain_a = tree_match._path_chain(nodes, "cous_a")  # [root, L1, L2x, cous_a]
    chain_b = tree_match._path_chain(nodes, "cous_b")  # [root, L1, L2y, cous_b]
    sim = tree_match._path_similarity(chain_a, chain_b)
    assert abs(sim - 2 / 4) < 1e-9, f"cousins sim should be 0.5, got {sim}"


def test_path_similarity_different_l1_branches_low():
    nodes = _make_tree({
        "root": None, "L1x": "root", "L1y": "root",
        "a": "L1x", "b": "L1y",
    })
    chain_a = tree_match._path_chain(nodes, "a")  # [root, L1x, a]
    chain_b = tree_match._path_chain(nodes, "b")  # [root, L1y, b]
    sim = tree_match._path_similarity(chain_a, chain_b)
    assert abs(sim - 1 / 3) < 1e-9, f"different-branch sim 1/3, got {sim}"


def test_path_similarity_identical_is_one():
    chain = ["root", "L1", "L2"]
    assert tree_match._path_similarity(chain, chain) == 1.0


# ---------------------------------------------------------------------------
# _mmr_rerank
# ---------------------------------------------------------------------------

def test_mmr_noop_when_under_limit():
    """If candidate count <= limit, MMR returns the input unchanged."""
    nodes = _make_tree({"root": None, "a": "root", "b": "root"})
    scored = [("a", nodes["a"], 5.0, "ch"), ("b", nodes["b"], 4.0, "ch")]
    out = tree_match._mmr_rerank(scored, nodes, limit=10)
    assert out == scored


def test_mmr_top_1_always_highest_relevance():
    """The first selected item is always the highest-relevance candidate,
    regardless of lambda."""
    nodes = _make_tree({
        "root": None, "L1": "root", "L2": "L1",
        "best": "L2", "rest1": "L2", "rest2": "L2",
    })
    scored = [
        ("best",  nodes["best"],  9.0, "ch"),
        ("rest1", nodes["rest1"], 6.0, "ch"),
        ("rest2", nodes["rest2"], 5.0, "ch"),
    ]
    out = tree_match._mmr_rerank(scored, nodes, limit=2, lambda_=0.0)
    assert out[0][0] == "best", f"top-1 must be highest rel, got {out[0][0]}"


def test_mmr_demotes_close_score_sibling_in_favor_of_diverse_alt():
    """Canonical case: top result, then a near-tie sibling vs a slightly-lower
    different-branch alternative. With λ=0.7 the diverse alt should win the
    second slot when scores are close."""
    nodes = _make_tree({
        "root": None,
        "L1a": "root", "L1b": "root",
        "L2a": "L1a", "L2b": "L1b",
        # Two siblings under L2a (high relevance)
        "topA":     "L2a",  # rel 6.0
        "siblingA": "L2a",  # rel 5.5  — slight drop
        # An alternative under a different L2 branch
        "altB":     "L2b",  # rel 5.3  — even further drop, but no overlap
    })
    scored = [
        ("topA",     nodes["topA"],     6.0, "ch"),
        ("siblingA", nodes["siblingA"], 5.5, "ch"),
        ("altB",     nodes["altB"],     5.3, "ch"),
    ]
    out = tree_match._mmr_rerank(scored, nodes, limit=2, lambda_=0.7)
    assert out[0][0] == "topA"
    # With λ=0.7, sibling at 5.5 (sim ≈ 0.75 with topA) competes against
    # altB at 5.3 (sim ≈ 0.25 with topA via shared root only).
    # MMR(sibling) = 0.7*(5.5/6.0) - 0.3*0.75  = 0.642 - 0.225  = 0.417
    # MMR(altB)    = 0.7*(5.3/6.0) - 0.3*0.25  = 0.618 - 0.075  = 0.543
    # → altB wins the 2nd slot.
    assert out[1][0] == "altB", \
        f"diverse alt should win 2nd slot under λ=0.7, got {out[1][0]}"


def test_mmr_keeps_high_relevance_sibling_when_alt_is_far_below():
    """If the diverse alternative is much lower in relevance, MMR should
    still pick the high-relevance sibling. Diversity is a tiebreaker, not
    a free lunch."""
    nodes = _make_tree({
        "root": None,
        "L1a": "root", "L1b": "root",
        "topA":     "L1a",  # rel 9.0
        "siblingA": "L1a",  # rel 8.5
        "altB":     "L1b",  # rel 2.0  — way lower
    })
    scored = [
        ("topA",     nodes["topA"],     9.0, "ch"),
        ("siblingA", nodes["siblingA"], 8.5, "ch"),
        ("altB",     nodes["altB"],     2.0, "ch"),
    ]
    out = tree_match._mmr_rerank(scored, nodes, limit=2, lambda_=0.7)
    # MMR(sibA) = 0.7*(8.5/9.0) - 0.3*sim(sibA,topA)
    # sim(sibA,topA): chain sibA=[root,L1a,sibA], chain topA=[root,L1a,topA]
    #   common=2, max=3, sim=0.667
    # MMR(sibA) = 0.7*0.944 - 0.3*0.667 = 0.661 - 0.200 = 0.461
    # MMR(altB) = 0.7*(2.0/9.0) - 0.3*sim(altB,topA)
    # sim(altB,topA): common=1 (root), max=3, sim=0.333
    # MMR(altB) = 0.7*0.222 - 0.3*0.333 = 0.156 - 0.100 = 0.056
    # → siblingA wins despite the diversity penalty.
    assert out[1][0] == "siblingA"


def test_mmr_lambda_one_is_pure_relevance():
    """λ=1 means the diversity term is zero — MMR degenerates to relevance
    sort. Useful sanity check for the formula."""
    nodes = _make_tree({
        "root": None, "L1": "root",
        "a": "L1", "b": "L1", "c": "L1",
    })
    scored = [
        ("a", nodes["a"], 9.0, "ch"),
        ("b", nodes["b"], 7.0, "ch"),
        ("c", nodes["c"], 3.0, "ch"),
    ]
    out = tree_match._mmr_rerank(scored, nodes, limit=3, lambda_=1.0)
    assert [item[0] for item in out] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Integration into _score_and_limit
# ---------------------------------------------------------------------------

def test_score_and_limit_skips_mmr_without_all_nodes():
    """Pre-MMR callers (no all_nodes) get the same sort-by-score-desc result.
    Regression-safe."""
    matched = [
        ({"key": "a", "depth": 3}),
        ({"key": "b", "depth": 3}),
    ]
    # Call with the (key, node) tuple shape _score_and_limit expects
    matched_pairs = [("a", {"depth": 3}), ("b", {"depth": 3})]
    channels = {"a": "exact_key", "b": "substring"}
    out = tree_match._score_and_limit(matched_pairs, channels, limit=10)
    # Without all_nodes MMR doesn't run; result is sort-by-score-desc.
    # exact_key=4.0 base > substring=3.0, both d3=+1.5, so a > b.
    assert out[0][0] == "a"
    assert out[1][0] == "b"


def test_score_and_limit_runs_mmr_with_all_nodes_and_overflow():
    """When all_nodes is provided AND len(matched) > limit, MMR re-ranks
    the over-limit pool. Match topology: 3 siblings + 1 cousin under
    different L2, limit=2 — the cousin should make it into top-2 even if
    the third sibling scores higher."""
    nodes = _make_tree({
        "root": None, "L1": "root",
        "L2x": "L1", "L2y": "L1",
        # Three siblings under L2x, all with similar (channel + d3) scores
        "sx_top":    "L2x",
        "sx_mid":    "L2x",
        "sx_low":    "L2x",
        # One cousin under L2y, scores similarly
        "cousin_y":  "L2y",
    })
    # Use exact_key for sx_top (4.0) and substring for the rest (3.0) +
    # depth bonus d3 (1.5) for all = 5.5 / 4.5 effective scores, before
    # cosine/recency. We'll skip query/IDF (set query="" but pass nodes
    # to enable MMR via the len-check + all_nodes branch).
    matched = [
        ("sx_top",    nodes["sx_top"]),
        ("sx_mid",    nodes["sx_mid"]),
        ("sx_low",    nodes["sx_low"]),
        ("cousin_y",  nodes["cousin_y"]),
    ]
    for k in nodes:
        nodes[k]["depth"] = 3  # d3 bonus uniformly
    channels = {
        "sx_top":   "exact_key",
        "sx_mid":   "substring",
        "sx_low":   "substring",
        "cousin_y": "substring",
    }
    out = tree_match._score_and_limit(
        matched, channels, limit=2, query_text="", all_nodes=nodes
    )
    keys = [item[0] for item in out]
    assert keys[0] == "sx_top", f"top-1 should be highest score, got {keys}"
    # Second slot: MMR picks cousin_y over sx_mid because cousin_y has
    # lower path overlap with sx_top despite identical relevance score.
    assert keys[1] == "cousin_y", \
        f"MMR should pick diverse cousin_y, got {keys}"


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
