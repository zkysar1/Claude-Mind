"""test_knowledge_graph_ppr.py --  (BRD Gap 1b+1c, child 2/4 of ).

Pins Personalized PageRank over the Mind knowledge-graph
(core/scripts/knowledge-graph-ppr.py): undirected weighted adjacency construction
(symmetry, edge-multiplicity weighting, self-loop guard), the PPR fixed point
(seed-is-top, monotonic decay along a path, symmetric-graph symmetric scores,
mass conservation, determinism), the pseudo-node (cat:/tag:) output filter,
empty-/missing-seed fallback to standard PageRank, and the temporal (valid_to)
load filter -- all on deterministic small-graph fixtures with hand-verifiable
expected orderings.

MIND-SCOPED: ranks the framework's OWN symbolic record graph (rb-/guard-/node-keys),
NOT the NPC all-MiniLM-L6-v2 embedding pipeline.

Daemon-safe (no daemon spawn, no daemon_integration marker): the PPR core is a pure
function set taking adjacency/triples as arguments; only the load_triples/compute
file-path tests touch disk, and they pass an explicit tmp path (module-level META_DIR
is never needed).

Cross-references:
  - g-306-42 / test_knowledge_graph_build.py -- child 1/4 (builds the graph this ranks)
  - HippoRAG 2405.14831 -- PPR-from-query-entities retrieval this implements
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

# Hyphen-named script -> importlib load (the test_knowledge_graph_build pattern).
_PPR_PATH = CORE_SCRIPTS / "knowledge-graph-ppr.py"
_spec = importlib.util.spec_from_file_location("knowledge_graph_ppr", _PPR_PATH)
ppr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ppr)


# --- helpers ----------------------------------------------------------------

def _triples(*pairs):
    """[(s, o), ...] -> [{'s':s,'o':o,'valid_to':None}, ...]."""
    return [{"s": s, "p": "references", "o": o, "valid_to": None} for s, o in pairs]


def _ppr_from_pairs(pairs, seeds, **kw):
    adjacency, out_weight = ppr.build_adjacency(_triples(*pairs))
    return ppr.personalized_pagerank(adjacency, seeds, out_weight=out_weight, **kw)


# --- adjacency construction -------------------------------------------------

def test_build_adjacency_undirected_and_symmetric():
    adjacency, out_weight = ppr.build_adjacency(_triples(("A", "B")))
    assert adjacency["A"] == {"B": 1.0}
    assert adjacency["B"] == {"A": 1.0}  # undirected: edge added both ways
    assert out_weight["A"] == 1.0 and out_weight["B"] == 1.0


def test_build_adjacency_accumulates_edge_weight():
    # The same (s, o) asserted twice -> weight 2 (stronger connection).
    adjacency, out_weight = ppr.build_adjacency(_triples(("A", "B"), ("A", "B")))
    assert adjacency["A"]["B"] == 2.0
    assert adjacency["B"]["A"] == 2.0
    assert out_weight["A"] == 2.0


def test_build_adjacency_skips_self_loops():
    adjacency, out_weight = ppr.build_adjacency(_triples(("A", "A"), ("A", "B")))
    assert "A" not in adjacency.get("A", {})  # self-loop dropped
    assert adjacency["A"] == {"B": 1.0}


def test_build_adjacency_honors_valid_to_filter():
    triples = [
        {"s": "A", "o": "B", "valid_to": None},
        {"s": "C", "o": "D", "valid_to": "2026-01-01"},  # retracted
    ]
    adj_current, _ = ppr.build_adjacency(triples)
    assert set(adj_current.keys()) == {"A", "B"}  # retracted edge excluded
    adj_all, _ = ppr.build_adjacency(triples, include_retracted=True)
    assert set(adj_all.keys()) == {"A", "B", "C", "D"}


# --- PPR fixed point --------------------------------------------------------

def test_seed_is_top_ranked():
    scores = _ppr_from_pairs([("A", "B"), ("B", "C"), ("C", "D")], ["A"])
    ranked = ppr.rank_nodes(scores)
    assert ranked[0][0] == "A"


def test_linear_chain_monotonic_decay():
    # Undirected path A-B-C-D seeded at A: PPR decays strictly with graph distance.
    scores = _ppr_from_pairs([("A", "B"), ("B", "C"), ("C", "D")], ["A"])
    assert scores["A"] > scores["B"] > scores["C"] > scores["D"]


def test_symmetric_graph_yields_symmetric_scores():
    # A--B, B--C, A--tag:x, D--tag:x : swapping (B<->tag:x, C<->D) is an automorphism
    # fixing the seed A, so PPR(A) must assign equal scores to the swapped pairs.
    pairs = [("A", "B"), ("B", "C"), ("A", "tag:x"), ("D", "tag:x")]
    scores = _ppr_from_pairs(pairs, ["A"])
    assert abs(scores["B"] - scores["tag:x"]) < 1e-7   # 1-hop pair
    assert abs(scores["C"] - scores["D"]) < 1e-7       # 2-hop pair
    assert scores["A"] > scores["B"]                   # seed dominates
    assert scores["B"] > scores["C"]                   # 1-hop over 2-hop


def test_mass_is_conserved():
    scores = _ppr_from_pairs([("A", "B"), ("B", "C"), ("A", "tag:x"), ("D", "tag:x")], ["A"])
    assert abs(sum(scores.values()) - 1.0) < 1e-9


def test_determinism_identical_across_runs():
    pairs = [("A", "B"), ("B", "C"), ("C", "D"), ("A", "tag:x"), ("D", "tag:x")]
    run1 = _ppr_from_pairs(pairs, ["A", "C"])
    run2 = _ppr_from_pairs(pairs, ["A", "C"])
    assert run1 == run2  # byte-identical dicts (same insertion order + values)


def test_multi_seed_elevates_both_seeds():
    # Path A-B-C-D-E seeded at both ends: endpoints outrank the center, and by the
    # A<->E symmetry score equally.
    scores = _ppr_from_pairs([("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")], ["A", "E"])
    assert scores["A"] > scores["C"]
    assert scores["E"] > scores["C"]
    assert abs(scores["A"] - scores["E"]) < 1e-7


# --- seed fallback ----------------------------------------------------------

def test_empty_seeds_is_standard_pagerank():
    # Star graph, no seeds -> uniform teleport -> the high-degree hub ranks first.
    pairs = [("hub", "L1"), ("hub", "L2"), ("hub", "L3"), ("hub", "L4")]
    scores = _ppr_from_pairs(pairs, [])
    ranked = ppr.rank_nodes(scores)
    assert ranked[0][0] == "hub"


def test_seed_absent_from_graph_falls_back_to_uniform():
    # A seed absent from the graph contributes no personalization; with no valid
    # seed left the teleport falls back to uniform (standard PageRank).
    adjacency, out_weight = ppr.build_adjacency(_triples(("A", "B")))
    scores = ppr.personalized_pagerank(adjacency, ["ZZZ-missing"], out_weight=out_weight)
    assert set(scores.keys()) == {"A", "B"}            # still ranks the whole graph
    assert abs(sum(scores.values()) - 1.0) < 1e-9      # valid distribution (uniform teleport)


# --- output shaping ---------------------------------------------------------

def test_exclude_pseudo_filters_hub_nodes():
    pairs = [("A", "B"), ("A", "tag:x"), ("B", "cat:framework")]
    scores = _ppr_from_pairs(pairs, ["A"])
    full = {n for n, _ in ppr.rank_nodes(scores)}
    filtered = {n for n, _ in ppr.rank_nodes(scores, exclude_pseudo=True)}
    assert "tag:x" in full and "cat:framework" in full
    assert "tag:x" not in filtered and "cat:framework" not in filtered
    assert {"A", "B"} <= filtered


def test_rank_nodes_top_k_and_tie_break():
    # Equal scores tie-break by node id ascending; top_k truncates.
    scores = {"b": 0.5, "a": 0.5, "c": 0.2}
    ranked = ppr.rank_nodes(scores, top_k=2)
    assert ranked == [("a", 0.5), ("b", 0.5)]  # a before b (ascending), c dropped


def test_is_pseudo_node():
    assert ppr.is_pseudo_node("cat:framework-patterns")
    assert ppr.is_pseudo_node("tag:deployment")
    assert not ppr.is_pseudo_node("rb-245")
    assert not ppr.is_pseudo_node("node:system/foo")


def test_empty_graph_returns_empty_scores():
    assert ppr.personalized_pagerank({}, ["x"]) == {}


# --- end-to-end from a file -------------------------------------------------

def test_load_triples_filters_retracted(tmp_path):
    gp = tmp_path / "knowledge-graph.jsonl"
    gp.write_text(
        json.dumps({"s": "A", "p": "references", "o": "B", "valid_to": None}) + "\n"
        + json.dumps({"s": "C", "p": "references", "o": "D", "valid_to": "2026-01-01"}) + "\n",
        encoding="utf-8",
    )
    current = ppr.load_triples(path=str(gp))
    assert {(t["s"], t["o"]) for t in current} == {("A", "B")}
    allt = ppr.load_triples(path=str(gp), include_retracted=True)
    assert {(t["s"], t["o"]) for t in allt} == {("A", "B"), ("C", "D")}


def test_compute_end_to_end_from_file(tmp_path):
    gp = tmp_path / "knowledge-graph.jsonl"
    gp.write_text(
        "\n".join(
            json.dumps({"s": s, "p": "references", "o": o, "valid_to": None})
            for s, o in [("A", "B"), ("B", "C"), ("C", "D")]
        ) + "\n",
        encoding="utf-8",
    )
    ranked, meta = ppr.compute(["A"], path=str(gp), top_k=2)
    assert meta["nodes"] == 4 and meta["edges"] == 3
    assert meta["seeds_matched"] == ["A"] and meta["personalized"] is True
    assert len(ranked) == 2
    assert ranked[0][0] == "A"  # seed top-ranked


def test_compute_missing_seed_reports_unpersonalized(tmp_path):
    gp = tmp_path / "knowledge-graph.jsonl"
    gp.write_text(
        json.dumps({"s": "A", "p": "references", "o": "B", "valid_to": None}) + "\n",
        encoding="utf-8",
    )
    ranked, meta = ppr.compute(["ZZZ"], path=str(gp))
    assert meta["personalized"] is False
    assert meta["seeds_matched"] == []
    assert len(ranked) == 2  # still ranks A, B under uniform teleport
