"""test_provenance_confidence.py -- M-5 provenance-based confidence modulation.

Covers:
  - DIRECT provenance: confidence term unchanged (weight=1.0)
  - INFERRED provenance: confidence term reduced to 70%
  - SYNTHESIZED provenance: confidence term reduced to 80%
  - HEARSAY provenance: confidence term halved (weight=0.5)
  - Missing provenance (legacy): confidence term at 90% (DEFAULT)
  - Unknown provenance string: falls back to DEFAULT (0.9)
  - Case-insensitive lookup
  - Null confidence yields zero regardless of provenance
  - Integration: _compute_match_score applies provenance to confidence only
  - _sort_by_utility tie-breaking by provenance weight
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Bootstrap a clean WORLD env for tree_match imports.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="provenance-confidence-test-")
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


def _node(confidence=0.8, provenance=None, **extras):
    """Helper -- minimal node dict with confidence and optional provenance."""
    n = {"depth": 3, "children": [], "child_count": 0,
         "confidence": confidence, "retrieval_count": 0}
    if provenance is not None:
        n["provenance"] = provenance
    n.update(extras)
    return n


# ---------------------------------------------------------------------------
# _provenance_weight unit tests
# ---------------------------------------------------------------------------

def test_provenance_weight_direct():
    """DIRECT provenance -> weight 1.0."""
    node = _node(provenance="DIRECT")
    assert tree_match._provenance_weight(node) == 1.0


def test_provenance_weight_inferred():
    """INFERRED provenance -> weight 0.7."""
    node = _node(provenance="INFERRED")
    assert tree_match._provenance_weight(node) == 0.7


def test_provenance_weight_synthesized():
    """SYNTHESIZED provenance -> weight 0.8."""
    node = _node(provenance="SYNTHESIZED")
    assert tree_match._provenance_weight(node) == 0.8


def test_provenance_weight_hearsay():
    """HEARSAY provenance -> weight 0.5."""
    node = _node(provenance="HEARSAY")
    assert tree_match._provenance_weight(node) == 0.5


def test_provenance_weight_missing():
    """No provenance field -> DEFAULT_PROVENANCE_WEIGHT (0.9)."""
    node = _node()  # no provenance key
    assert tree_match._provenance_weight(node) == tree_match.DEFAULT_PROVENANCE_WEIGHT


def test_provenance_weight_none_value():
    """provenance=None (YAML null) -> DEFAULT_PROVENANCE_WEIGHT."""
    node = _node(provenance=None)
    # provenance key is absent when None is passed to _node helper
    # (it only sets the key when not None). Test with explicit None:
    node["provenance"] = None
    assert tree_match._provenance_weight(node) == tree_match.DEFAULT_PROVENANCE_WEIGHT


def test_provenance_weight_unknown_string():
    """Unknown provenance string -> DEFAULT_PROVENANCE_WEIGHT."""
    node = _node()
    node["provenance"] = "RUMOR"
    assert tree_match._provenance_weight(node) == tree_match.DEFAULT_PROVENANCE_WEIGHT


def test_provenance_weight_case_insensitive():
    """Lowercase provenance string still matches."""
    node = _node()
    node["provenance"] = "direct"
    assert tree_match._provenance_weight(node) == 1.0


def test_provenance_weight_mixed_case():
    """Mixed-case provenance string still matches."""
    node = _node()
    node["provenance"] = "Inferred"
    assert tree_match._provenance_weight(node) == 0.7


# ---------------------------------------------------------------------------
# Confidence contribution via _compute_match_score
# ---------------------------------------------------------------------------

def test_direct_full_confidence():
    """DIRECT provenance: confidence contribution = confidence * 1.0."""
    node = _node(confidence=0.8, provenance="DIRECT")
    score = tree_match._compute_match_score("k", node, "substring")
    # Reconstruct expected: channel(3.0) + depth(1.5) + confidence(0.8*1.0)
    #   + capability(0) + recency(0, no last_updated)
    expected = 3.0 + 1.5 + 0.8 * 1.0 + 0 + 0
    assert abs(score - expected) < 1e-9, f"expected {expected}, got {score}"


def test_inferred_discounted():
    """INFERRED provenance: confidence contribution = confidence * 0.7."""
    node = _node(confidence=0.8, provenance="INFERRED")
    score = tree_match._compute_match_score("k", node, "substring")
    expected = 3.0 + 1.5 + 0.8 * 0.7 + 0 + 0
    assert abs(score - expected) < 1e-9, f"expected {expected}, got {score}"


def test_synthesized_partial():
    """SYNTHESIZED provenance: confidence contribution = confidence * 0.8."""
    node = _node(confidence=0.8, provenance="SYNTHESIZED")
    score = tree_match._compute_match_score("k", node, "substring")
    expected = 3.0 + 1.5 + 0.8 * 0.8 + 0 + 0
    assert abs(score - expected) < 1e-9, f"expected {expected}, got {score}"


def test_hearsay_halved():
    """HEARSAY provenance: confidence contribution = confidence * 0.5."""
    node = _node(confidence=0.8, provenance="HEARSAY")
    score = tree_match._compute_match_score("k", node, "substring")
    expected = 3.0 + 1.5 + 0.8 * 0.5 + 0 + 0
    assert abs(score - expected) < 1e-9, f"expected {expected}, got {score}"


def test_legacy_node_default():
    """No provenance field: confidence contribution = confidence * 0.9."""
    node = _node(confidence=0.8)  # no provenance
    score = tree_match._compute_match_score("k", node, "substring")
    expected = 3.0 + 1.5 + 0.8 * 0.9 + 0 + 0
    assert abs(score - expected) < 1e-9, f"expected {expected}, got {score}"


def test_null_confidence_zero_regardless():
    """confidence=None with any provenance -> confidence contribution = 0."""
    for prov in ("DIRECT", "INFERRED", "SYNTHESIZED", "HEARSAY", None):
        node = _node(confidence=None)
        if prov is not None:
            node["provenance"] = prov
        score = tree_match._compute_match_score("k", node, "substring")
        # channel(3.0) + depth(1.5) + confidence(0) + capability(0) + recency(0)
        expected = 3.0 + 1.5 + 0 + 0 + 0
        assert abs(score - expected) < 1e-9, (
            f"prov={prov}: expected {expected}, got {score}"
        )


def test_zero_confidence_zero_regardless():
    """confidence=0.0 with any provenance -> confidence contribution = 0."""
    node = _node(confidence=0.0, provenance="DIRECT")
    score = tree_match._compute_match_score("k", node, "substring")
    expected = 3.0 + 1.5 + 0 + 0 + 0
    assert abs(score - expected) < 1e-9


# ---------------------------------------------------------------------------
# Full score: only confidence term affected by provenance
# ---------------------------------------------------------------------------

def test_full_score_only_confidence_affected():
    """Two nodes identical except provenance: score difference equals exactly
    (confidence * weight_a) - (confidence * weight_b).
    """
    conf = 0.8
    node_direct = _node(confidence=conf, provenance="DIRECT",
                        capability_level="CALIBRATE")
    node_hearsay = _node(confidence=conf, provenance="HEARSAY",
                         capability_level="CALIBRATE")
    score_d = tree_match._compute_match_score("k", node_direct, "substring")
    score_h = tree_match._compute_match_score("k", node_hearsay, "substring")
    expected_delta = conf * 1.0 - conf * 0.5
    actual_delta = score_d - score_h
    assert abs(actual_delta - expected_delta) < 1e-9, (
        f"expected delta {expected_delta}, got {actual_delta}"
    )


def test_provenance_does_not_affect_channel_or_depth():
    """Changing provenance on a node with confidence=0 has no effect on score,
    proving provenance only modulates the confidence term."""
    node_a = _node(confidence=0.0, provenance="DIRECT",
                   capability_level="EXPLOIT")
    node_b = _node(confidence=0.0, provenance="HEARSAY",
                   capability_level="EXPLOIT")
    score_a = tree_match._compute_match_score("k", node_a, "exact_key")
    score_b = tree_match._compute_match_score("k", node_b, "exact_key")
    assert abs(score_a - score_b) < 1e-9, (
        f"with zero confidence, scores should be identical: {score_a} vs {score_b}"
    )


# ---------------------------------------------------------------------------
# _sort_by_utility provenance tie-breaking
# ---------------------------------------------------------------------------

def test_sort_by_utility_provenance_tiebreak():
    """Two entries with same utilization_score and same created date but
    different provenance: DIRECT sorts before HEARSAY."""
    # Import retrieve module -- needs env setup similar to tree_match
    _orig_world = os.environ.get("MIND_WORLD")
    _orig_agent = os.environ.get("MIND_AGENT")
    _tmpdir = tempfile.mkdtemp(prefix="prov-sort-test-")
    os.environ["MIND_WORLD"] = _tmpdir
    os.environ.pop("MIND_AGENT", None)
    try:
        # Force reimport of retrieve module with clean env
        import retrieve as _retrieve
        entry_hearsay = {
            "id": "rb-1",
            "provenance": "HEARSAY",
            "utilization": {"utilization_score": 5},
            "created": "2026-01-01",
        }
        entry_direct = {
            "id": "rb-2",
            "provenance": "DIRECT",
            "utilization": {"utilization_score": 5},
            "created": "2026-01-01",
        }
        entries = [entry_hearsay, entry_direct]
        _retrieve._sort_by_utility(entries)
        assert entries[0]["id"] == "rb-2", (
            f"DIRECT should sort first, got {entries[0]['id']}"
        )
        assert entries[1]["id"] == "rb-1"
    finally:
        if _orig_world is not None:
            os.environ["MIND_WORLD"] = _orig_world
        elif "MIND_WORLD" in os.environ:
            del os.environ["MIND_WORLD"]
        if _orig_agent is not None:
            os.environ["MIND_AGENT"] = _orig_agent


def test_sort_by_utility_provenance_legacy_default():
    """Entry without provenance field gets DEFAULT weight (0.9), which ranks
    above HEARSAY (0.5) and below DIRECT (1.0) at equal utility."""
    _orig_world = os.environ.get("MIND_WORLD")
    _orig_agent = os.environ.get("MIND_AGENT")
    _tmpdir = tempfile.mkdtemp(prefix="prov-sort-legacy-test-")
    os.environ["MIND_WORLD"] = _tmpdir
    os.environ.pop("MIND_AGENT", None)
    try:
        import retrieve as _retrieve
        entry_direct = {
            "id": "rb-d",
            "provenance": "DIRECT",
            "utilization": {"utilization_score": 5},
            "created": "2026-01-01",
        }
        entry_legacy = {
            "id": "rb-l",
            # no provenance field
            "utilization": {"utilization_score": 5},
            "created": "2026-01-01",
        }
        entry_hearsay = {
            "id": "rb-h",
            "provenance": "HEARSAY",
            "utilization": {"utilization_score": 5},
            "created": "2026-01-01",
        }
        entries = [entry_hearsay, entry_legacy, entry_direct]
        _retrieve._sort_by_utility(entries)
        assert entries[0]["id"] == "rb-d", "DIRECT first"
        assert entries[1]["id"] == "rb-l", "legacy (0.9) second"
        assert entries[2]["id"] == "rb-h", "HEARSAY last"
    finally:
        if _orig_world is not None:
            os.environ["MIND_WORLD"] = _orig_world
        elif "MIND_WORLD" in os.environ:
            del os.environ["MIND_WORLD"]
        if _orig_agent is not None:
            os.environ["MIND_AGENT"] = _orig_agent


def test_sort_by_utility_score_still_dominates():
    """Higher utilization_score still dominates provenance weight."""
    _orig_world = os.environ.get("MIND_WORLD")
    _orig_agent = os.environ.get("MIND_AGENT")
    _tmpdir = tempfile.mkdtemp(prefix="prov-sort-dom-test-")
    os.environ["MIND_WORLD"] = _tmpdir
    os.environ.pop("MIND_AGENT", None)
    try:
        import retrieve as _retrieve
        entry_high_util_hearsay = {
            "id": "rb-high",
            "provenance": "HEARSAY",
            "utilization": {"utilization_score": 10},
            "created": "2026-01-01",
        }
        entry_low_util_direct = {
            "id": "rb-low",
            "provenance": "DIRECT",
            "utilization": {"utilization_score": 3},
            "created": "2026-01-01",
        }
        entries = [entry_low_util_direct, entry_high_util_hearsay]
        _retrieve._sort_by_utility(entries)
        assert entries[0]["id"] == "rb-high", (
            "higher utilization_score should dominate provenance"
        )
    finally:
        if _orig_world is not None:
            os.environ["MIND_WORLD"] = _orig_world
        elif "MIND_WORLD" in os.environ:
            del os.environ["MIND_WORLD"]
        if _orig_agent is not None:
            os.environ["MIND_AGENT"] = _orig_agent


# ---------------------------------------------------------------------------
# Constants consistency
# ---------------------------------------------------------------------------

def test_provenance_weights_all_present():
    """All four provenance tags are in PROVENANCE_WEIGHTS."""
    for tag in ("DIRECT", "INFERRED", "SYNTHESIZED", "HEARSAY"):
        assert tag in tree_match.PROVENANCE_WEIGHTS, f"missing {tag}"


def test_provenance_weights_values_in_range():
    """All weights are in (0, 1]."""
    for tag, w in tree_match.PROVENANCE_WEIGHTS.items():
        assert 0 < w <= 1.0, f"{tag} weight {w} out of range"


def test_default_provenance_weight_in_range():
    """DEFAULT_PROVENANCE_WEIGHT is in (0, 1]."""
    w = tree_match.DEFAULT_PROVENANCE_WEIGHT
    assert 0 < w <= 1.0, f"DEFAULT weight {w} out of range"


def test_ordering_direct_gt_synthesized_gt_inferred_gt_hearsay():
    """Provenance weights are ordered: DIRECT > SYNTHESIZED > INFERRED > HEARSAY."""
    w = tree_match.PROVENANCE_WEIGHTS
    assert w["DIRECT"] > w["SYNTHESIZED"] > w["INFERRED"] > w["HEARSAY"]


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
