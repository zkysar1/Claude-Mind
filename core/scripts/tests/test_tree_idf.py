"""test_tree_idf.py — TF-IDF + cosine similarity for tree retrieval ranking.

Covers:
  - Tokenization aligns with tree_match.py P0 #2 (word-boundary regex)
  - IDF weighting penalizes common tokens, rewards rare tokens
  - Smoothed IDF formula stays positive (no zero-weight terms)
  - Cosine similarity correctness on synthetic vectors
  - Empty-query and empty-corpus edge cases
  - Integration: a query with rare-token overlap ranks above a query with
    only common-token overlap (the NOISY-leaf fix in action)
  - The cosine bonus is layered onto channel-based matching, not replacing
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Bootstrap with a clean WORLD env so tree_match imports don't try to read
# the live world directory.
#  capture-restore pattern: stash env before module-level mutation
# so subsequent tests in the same pytest session don't inherit a popped
# MIND_AGENT. See test_applies_to_required.py for full rationale.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="tree-idf-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

import tree_idf  # noqa: E402
import tree_match  # noqa: E402

# Restore env so downstream tests inherit clean conftest defaults.
if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

def test_tokenize_word_boundary_split():
    """Same regex as tree_match P0 #2 — splits on hyphens, spaces, underscores."""
    assert tree_idf.tokenize("npc-intelligence pipeline") == ["npc", "intelligence", "pipeline"]
    assert tree_idf.tokenize("memory_consolidation") == ["memory", "consolidation"]
    assert tree_idf.tokenize("how does NPC selection work") == ["how", "does", "npc", "selection", "work"]


def test_tokenize_min_length_filter():
    """Tokens shorter than 3 chars dropped — matches the corpus-side discipline."""
    assert tree_idf.tokenize("a bc abc abcd") == ["abc", "abcd"]


def test_tokenize_empty_and_none():
    """Empty / None text returns empty list, never crashes."""
    assert tree_idf.tokenize("") == []
    assert tree_idf.tokenize(None) == []


# ---------------------------------------------------------------------------
# IDF math
# ---------------------------------------------------------------------------

def test_idf_common_term_lower_than_rare():
    """A term in every doc gets the lowest IDF; a term in one doc gets highest."""
    nodes = {
        "alpha": {"summary": "common rare-only-here"},
        "beta": {"summary": "common"},
        "gamma": {"summary": "common"},
    }
    idx = tree_idf.build_index(nodes)
    # "common" appears in 3 docs (and "alpha"/"beta"/"gamma" each appear in 1
    # via the key tokenization). "rare" appears in 1 doc.
    assert idx["idf"]["common"] < idx["idf"]["rare"], \
        f"common IDF {idx['idf']['common']} should be < rare IDF {idx['idf']['rare']}"


def test_idf_smoothed_never_zero():
    """Smoothed IDF (log((N+1)/(df+1))+1) keeps every term strictly positive
    even when df==N (term in every doc) or df==1 (term in only one doc)."""
    nodes = {
        f"key{i:03d}": {"summary": "shared term"} for i in range(5)
    }
    idx = tree_idf.build_index(nodes)
    for term, w in idx["idf"].items():
        assert w > 0.0, f"term {term} has IDF {w}, expected > 0"


def test_idf_n_docs_matches():
    """n_docs reflects total node count, not just docs with tokens."""
    nodes = {f"k{i}": {"summary": "x" * (i % 3)} for i in range(10)}
    idx = tree_idf.build_index(nodes)
    assert idx["n_docs"] == 10


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------

def test_cosine_identical_vectors_is_one():
    """cosine(v, v) == 1.0 for any non-empty vector."""
    v = ({"a": 1.0, "b": 2.0, "c": 3.0}, math.sqrt(1 + 4 + 9))
    assert abs(tree_idf.cosine(v, v) - 1.0) < 1e-9


def test_cosine_orthogonal_is_zero():
    """Vectors with no terms in common have cosine 0."""
    v1 = ({"a": 1.0, "b": 1.0}, math.sqrt(2))
    v2 = ({"c": 1.0, "d": 1.0}, math.sqrt(2))
    assert tree_idf.cosine(v1, v2) == 0.0


def test_cosine_scaled_vectors_is_one():
    """cosine is scale-invariant: scaling a vector doesn't change cosine."""
    v1 = ({"a": 1.0, "b": 2.0}, math.sqrt(5))
    v2 = ({"a": 10.0, "b": 20.0}, math.sqrt(500))
    assert abs(tree_idf.cosine(v1, v2) - 1.0) < 1e-9


def test_cosine_empty_returns_zero():
    """Either side empty → 0 (neutral), not error."""
    empty = ({}, 0.0)
    nonempty = ({"a": 1.0}, 1.0)
    assert tree_idf.cosine(empty, nonempty) == 0.0
    assert tree_idf.cosine(nonempty, empty) == 0.0
    assert tree_idf.cosine(empty, empty) == 0.0


# ---------------------------------------------------------------------------
# query_vector
# ---------------------------------------------------------------------------

def test_query_vector_uses_corpus_idf():
    """Query terms get the IDF weight from the supplied corpus table.
    Unknown terms get 0 weight (no contribution)."""
    nodes = {
        "doc-a": {"summary": "common shared"},
        "doc-b": {"summary": "common"},
        "doc-c": {"summary": "common"},
    }
    idx = tree_idf.build_index(nodes)
    q_vec, q_mag = tree_idf.query_vector("shared unknown-term", idx["idf"])
    assert "shared" in q_vec
    assert q_vec["shared"] > 0
    # "unknown-term" tokenizes to ["unknown", "term"] — neither in corpus
    assert q_vec.get("unknown", 0.0) == 0.0
    assert q_vec.get("term", 0.0) == 0.0


def test_query_vector_empty_query():
    """Empty query → empty vector, magnitude 0."""
    idx = tree_idf.build_index({"doc-a": {"summary": "x"}})
    q_vec, q_mag = tree_idf.query_vector("", idx["idf"])
    assert q_vec == {}
    assert q_mag == 0.0


# ---------------------------------------------------------------------------
# Integration: NOISY-leaf precision check
# ---------------------------------------------------------------------------

def test_noisy_leaf_specific_outranks_generic():
    """The audit-driven scenario: a query with two tokens — one common
    ('intelligence'), one specific ('bottleneck') — should rank a node
    that covers BOTH tokens above a node that only covers the common token.
    Pre-IDF the channel-based scorer treated both as 'word_prefix matches'
    with similar scores; with cosine the rare-token-bearing node wins.
    """
    nodes = {
        # 6 nodes containing "intelligence" — makes "intelligence" common
        "intelligence-pipeline": {"summary": "Intelligence pipeline overview"},
        "intelligence-platform": {"summary": "Platform intelligence stuff"},
        "intelligence-overview": {"summary": "Intelligence summary"},
        "intelligence-survey": {"summary": "Survey of intelligence work"},
        "intelligence-roadmap": {"summary": "Roadmap for intelligence"},
        "intelligence-comms": {"summary": "Comms about intelligence"},
        # The specific leaf — has the rare token "bottleneck"
        "runtime-bottleneck-analysis": {
            "summary": "Bottleneck analysis for intelligence runtime"
        },
        # Also-ran with no overlap
        "unrelated-node": {"summary": "Totally different topic"},
    }
    idx = tree_idf.build_index(nodes)
    q_vm = tree_idf.query_vector("intelligence bottleneck", idx["idf"])

    scored = []
    for key in nodes:
        d_vm = idx["vectors"][key]
        scored.append((tree_idf.cosine(q_vm, d_vm), key))
    scored.sort(reverse=True)

    top_key = scored[0][1]
    assert top_key == "runtime-bottleneck-analysis", \
        f"specific match should win, got top={top_key}, ranks={scored}"

    # And the generic-only nodes rank below it
    generic_rank = next(i for i, (_, k) in enumerate(scored)
                        if k == "intelligence-pipeline")
    specific_rank = next(i for i, (_, k) in enumerate(scored)
                         if k == "runtime-bottleneck-analysis")
    assert specific_rank < generic_rank, \
        f"specific (rank {specific_rank}) should beat generic (rank {generic_rank})"


# ---------------------------------------------------------------------------
# Integration: layered onto channel scoring (regression check)
# ---------------------------------------------------------------------------

def test_score_and_limit_layers_cosine_onto_channel_scores():
    """_score_and_limit with query+nodes adds cosine bonus on top of channel
    scoring. Without query+nodes, behavior is identical to the pre-IDF
    helper (regression-safe)."""
    nodes = {
        "specific-rare-token": {
            "summary": "Rare specific content here",
            "depth": 3, "confidence": 0.5,
        },
        "generic-common-token": {
            "summary": "Generic common term term term",
            "depth": 3, "confidence": 0.5,
        },
    }
    matched = [(k, v) for k, v in nodes.items()]
    channels = {k: "word_prefix" for k in nodes}

    # Without IDF — both score identically (same channel, depth, confidence).
    no_idf = tree_match._score_and_limit(matched, channels, limit=10)
    no_idf_scores = {k: s for k, n, s, c in no_idf}
    assert no_idf_scores["specific-rare-token"] == no_idf_scores["generic-common-token"]

    # With IDF — querying for "rare" should boost specific-rare-token only.
    with_idf = tree_match._score_and_limit(
        matched, channels, limit=10, query_text="rare", all_nodes=nodes
    )
    with_idf_scores = {k: s for k, n, s, c in with_idf}
    assert with_idf_scores["specific-rare-token"] > with_idf_scores["generic-common-token"], \
        f"IDF bonus should boost rare-token match: {with_idf_scores}"
    # And the boost is bounded by COSINE_BONUS_WEIGHT
    bonus = with_idf_scores["specific-rare-token"] - no_idf_scores["specific-rare-token"]
    assert 0 < bonus <= tree_match.COSINE_BONUS_WEIGHT + 1e-6, \
        f"bonus {bonus} should be in (0, {tree_match.COSINE_BONUS_WEIGHT}]"


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
