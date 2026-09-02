"""Scoring-path type robustness (): a malformed record costs ITSELF,
never the whole query.

Found live on a peer deployment: one store record with a sequence-typed value
where the scorer expects a number made /v1/retrieve DETERMINISTICALLY return
{"error": "internal_error", "detail": "TypeError: can't multiply sequence by
non-int of type float"} for any query whose candidate set included that record
— while adjacent queries succeeded. Two sites can produce that exact message
(tree confidence * provenance in tree_match._compute_match_score; supplementary
utilization_score * poignancy factor in retrieve._sort_by_utility), and five
sibling sites crash with different messages on the same record-shape class
(depth/retrieval_count comparisons, float(utility_ratio), unhashable
capability_level, mixed-type sort tuples).

The remedy is tree_match.safe_num: coerce to float where possible (a quoted
"0.85" still scores as 0.85), else WARN naming the record id + field and score
the term as its default. These tests pin both halves: survival + the warning,
and coercion-not-skip for values that are numeric-in-a-string.

The server half (sanitized internal_error responses previously logged NOTHING
server-side) is pinned via mind_api.src.server._log_internal_error.
"""
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from tree_match import _compute_match_score, _recency_bonus, safe_num  # noqa: E402


# --- the helper itself ------------------------------------------------------

def test_safe_num_coerces_and_defaults(capsys):
    assert safe_num(0.7, 0.0) == 0.7
    assert safe_num("0.85", 0.0) == 0.85          # coerce, not skip
    assert safe_num(None, 0.3) == 0.3             # silent (absent is normal)
    assert capsys.readouterr().err == ""
    assert safe_num([0.9], 0.0, rid="node-x", field="confidence") == 0.0
    err = capsys.readouterr().err
    assert "confidence" in err and "node-x" in err and "g-357-49" in err


# --- tree front matter through _compute_match_score -------------------------

MALFORMED_NODE = {
    "confidence": [0.9],            # sequence * float — THE live crash shape
    "depth": ["3"],                 # sequence >= int
    "capability_level": ["forged"],  # unhashable dict key
    "retrieval_count": "seven",     # str / int comparison in recency TAU
    "last_updated": "2026-08-01",
    "provenance": "DIRECT",
}


def test_compute_match_score_survives_malformed_node(capsys):
    score = _compute_match_score("bad-node", MALFORMED_NODE, "direct")
    assert isinstance(score, float)
    err = capsys.readouterr().err
    assert "confidence" in err and "bad-node" in err


def test_compute_match_score_coerces_quoted_numerics():
    """A quoted numeric front-matter value must score IDENTICALLY to the bare
    number — coercion, never a silent zero."""
    bare = _compute_match_score(
        "n", {"confidence": 0.8, "depth": 3, "provenance": "DIRECT"}, "direct")
    quoted = _compute_match_score(
        "n", {"confidence": "0.8", "depth": "3", "provenance": "DIRECT"},
        "direct")
    assert quoted == bare


def test_recency_bonus_survives_malformed_retrieval_count():
    node = {"last_updated": "2026-08-01", "retrieval_count": ["9"],
            "key": "bad-node"}
    assert isinstance(_recency_bonus(node), float)


# --- supplementary stores through _sort_by_utility --------------------------

def _records():
    return [
        {"id": "rb-1", "utilization": {"utilization_score": 0.4},
         "created": "2026-01-01"},
        {"id": "rb-bad", "utilization": {"utilization_score": ["high"]},
         "created": 20260101},          # sequence score AND non-str created
        {"id": "rb-2", "utilization": {"utilization_score": 0.9},
         "created": "2026-02-01"},
    ]


def test_sort_by_utility_survives_malformed_record_blend_off(capsys, monkeypatch):
    import retrieve
    monkeypatch.setattr(retrieve, "_load_retrieval_config",
                        lambda: {"poignancy_blend_enabled": False})
    out = retrieve._sort_by_utility(_records())
    assert [r["id"] for r in out][:2] == ["rb-2", "rb-1"]   # numerics ordered
    assert out[-1]["id"] == "rb-bad"                        # bad one costs itself
    err = capsys.readouterr().err
    assert "utilization_score" in err and "rb-bad" in err


def test_sort_by_utility_survives_malformed_record_blend_on(capsys, monkeypatch):
    """The blend multiplies util * poignancy factor — the second site that
    produced the live 'sequence * float' message."""
    import retrieve
    monkeypatch.setattr(retrieve, "_load_retrieval_config",
                        lambda: {"poignancy_blend_enabled": True,
                                 "poignancy_weight_min": 1.0,
                                 "poignancy_weight_max": 1.5})
    out = retrieve._sort_by_utility(_records())
    assert len(out) == 3 and out[-1]["id"] == "rb-bad"
    assert "utilization_score" in capsys.readouterr().err


def test_utility_weight_survives_malformed_fields(capsys):
    import retrieve
    cfg = {"utility_weight_neutral_below_retrievals": 5,
           "utility_weight_min": 0.5, "utility_weight_max": 1.5,
           "utility_weight_center": 0.0}
    node = {"key": "bad-node", "retrieval_count": ["12"],
            "times_helpful": 3, "utility_ratio": {"a": 1}}
    w = retrieve._utility_weight(node, cfg)
    assert isinstance(w, float)     # malformed rc -> 0.0 -> neutral 1.0
    node2 = {"key": "bad-node", "retrieval_count": 12,
             "times_helpful": 3, "utility_ratio": ["0.4"]}
    w2 = retrieve._utility_weight(node2, cfg)
    assert isinstance(w2, float)
    assert "utility_ratio" in capsys.readouterr().err


def test_load_experiences_sort_key_shape():
    """The experiences sort key must not crash on a null retrieval_stats or a
    non-numeric count (mixed-type sort keys crash the whole sort)."""
    import retrieve
    recs = [
        {"id": "e1", "retrieval_stats": {"retrieval_count": 3}},
        {"id": "e2", "retrieval_stats": None},
        {"id": "e3", "retrieval_stats": {"retrieval_count": "many"}},
    ]
    recs.sort(key=lambda r: retrieve.safe_num(
        (r.get("retrieval_stats") or {}).get("retrieval_count", 0), 0.0,
        rid=r.get("id", ""), field="retrieval_stats.retrieval_count"),
        reverse=True)
    assert recs[0]["id"] == "e1"


# --- server-side traceback logging ------------------------------------------

def test_internal_error_logs_full_traceback_server_side(capsys):
    sys.path.insert(0, str(SCRIPT_DIR.parent.parent.parent))
    from mind_api.src.server import _log_internal_error
    try:
        raise TypeError("can't multiply sequence by non-int of type float")
    except TypeError:
        _log_internal_error("GET", "/v1/retrieve")
    err = capsys.readouterr().err
    assert "internal_error on GET /v1/retrieve" in err
    assert "Traceback (most recent call last)" in err
    assert "can't multiply sequence by non-int of type float" in err
