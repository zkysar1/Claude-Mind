"""test_cell_similarity.py --  (BRD Gap 17, child 2/4 of ).

Pins the KG+PPR cell matcher (core/scripts/cell-similarity.py): record-id entity
extraction in the graph's own vocabulary, the HippoRAG passage-score aggregation
(SUM of query-seeded PPR mass over a cell's entities), deterministic argmax with
cell_id tie-break, the honest no-signal paths (no query entities / no candidates /
no overlap), all-category vs single-category search, and -- the goal's headline
contract -- that similarity reuses the KG+PPR substrate BY CODE PATH, not NPC
embeddings.

MIND-INTERNAL: matches the framework's OWN Go-Explore cells (state_signature +
trajectory + score) over its OWN symbolic record graph. NOT the NPC
all-MiniLM-L6-v2 cell-reuse cascade (a separate product-side artifact).

Daemon-safe (no daemon_integration marker): every test injects an explicit
cells_dir=tmp_path AND graph_path=tmp file, so neither the real
agents/<agent>/cells/ tree nor the real meta/knowledge-graph.jsonl is touched and
no daemon is spawned. Pure file-ops + in-memory PPR on tmp directories.

Cross-references:
  - g-306-47 / test_cells_archive.py -- child 1/4 (the cell store this consumes)
  - g-306-43 / test_knowledge_graph_ppr.py -- the PPR substrate this seeds
  - HippoRAG 2405.14831 -- PPR-from-query-entities passage ranking this implements
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


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sim = _load("cell_similarity", "cell-similarity.py")
cells = _load("cells_archive_t", "cells-archive.py")


# --- fixtures ---------------------------------------------------------------

def _write_graph(tmp_path, pairs):
    """[(s, o), ...] -> a knowledge-graph.jsonl file; returns its path string.

    Mirrors test_knowledge_graph_ppr.test_compute_end_to_end_from_file.
    """
    gp = tmp_path / "knowledge-graph.jsonl"
    gp.write_text(
        "\n".join(
            json.dumps({"s": s, "p": "references", "o": o, "valid_to": None})
            for s, o in pairs
        )
        + "\n",
        encoding="utf-8",
    )
    return str(gp)


def _cells_dir(tmp_path):
    d = tmp_path / "cells"
    d.mkdir(exist_ok=True)
    return d


def _upsert(cells_dir, cell_id, category, score, sig, trajectory=None):
    return cells.upsert_cell(
        cell_id, category,
        state_signature=sig,
        trajectory=(trajectory if trajectory is not None else [cell_id]),
        score=score, cells_dir=cells_dir, now="2026-06-21T00:00:00",
    )


# --- entity extraction (graph-vocabulary SSOT) ------------------------------

def test_signature_entities_extracts_record_ids_sorted_unique():
    ents = sim.signature_entities("state at rb-245 after guard-832 and rb-245 again")
    assert ents == ["guard-832", "rb-245"]  # sorted, deduped


def test_signature_entities_handles_list_and_dict_shapes():
    assert sim.signature_entities(["rb-1", "guard-2"]) == ["guard-2", "rb-1"]
    assert sim.signature_entities({"a": "rb-1", "b": ["g-306-48"]}) == ["g-306-48", "rb-1"]


def test_signature_entities_empty_when_no_record_ids():
    # A discretized state with no framework record id yields no entities.
    assert sim.signature_entities("grid:3x3:filled top-left") == []
    assert sim.signature_entities(None) == []


# --- pure scoring -----------------------------------------------------------

def test_score_cell_sums_ppr_mass_over_entities():
    ppr_scores = {"rb-1": 0.5, "rb-2": 0.3, "guard-9": 0.1}
    rec = {"state_signature": "touches rb-1 and rb-2"}
    score, overlap = sim.score_cell(rec, ppr_scores)
    assert abs(score - 0.8) < 1e-12
    assert overlap == 2


def test_score_cell_zero_when_no_overlap():
    score, overlap = sim.score_cell({"state_signature": "rb-999"}, {"rb-1": 0.5})
    assert score == 0.0 and overlap == 0


def test_rank_cells_deterministic_tiebreak_by_id():
    ppr = {"rb-1": 0.5}
    candidates = {
        "cB": {"state_signature": "rb-1", "cell_id": "cB"},
        "cA": {"state_signature": "rb-1", "cell_id": "cA"},
        "cC": {"state_signature": "rb-999", "cell_id": "cC"},  # 0 overlap
    }
    rows = sim.rank_cells(candidates, ppr)
    # equal-scoring cA, cB tie-break ascending; cC (0.0) last
    assert [r[0] for r in rows] == ["cA", "cB", "cC"]


# --- end-to-end matcher -----------------------------------------------------

def test_match_returns_most_similar_cell(tmp_path):
    # Graph: query seed rb-100 -- rb-101 (1 hop); rb-200 is a disconnected component.
    graph = _write_graph(tmp_path, [("rb-100", "rb-101"), ("rb-200", "rb-300")])
    cd = _cells_dir(tmp_path)
    # cellX references rb-101 (1 hop from the query seed -> high PPR mass).
    _upsert(cd, "cX", "planning", 5.0, sig="reached state via rb-101", trajectory=["a", "b"])
    # cellY references rb-200 (disconnected from the seed -> ~0 PPR mass).
    _upsert(cd, "cY", "planning", 9.0, sig="reached state via rb-200")

    r = sim.match("query at rb-100", category="planning", cells_dir=cd, graph_path=graph)
    assert r["reason"] == "matched"
    assert r["personalized"] is True
    # cX wins on KG proximity DESPITE cY's higher raw archive score -- similarity is
    # graph-structural, not the cell's own reward.
    assert r["cell_id"] == "cX"
    assert r["category"] == "planning"
    assert r["similarity"] > 0.0
    # the winning record is the real archived cell (trajectory preserved)
    assert r["match"]["trajectory"] == ["a", "b"]


def test_match_is_deterministic(tmp_path):
    graph = _write_graph(tmp_path, [("rb-100", "rb-101"), ("rb-101", "rb-102")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "cX", "planning", 1.0, sig="rb-101")
    _upsert(cd, "cY", "planning", 1.0, sig="rb-102")
    r1 = sim.match("rb-100", category="planning", cells_dir=cd, graph_path=graph)
    r2 = sim.match("rb-100", category="planning", cells_dir=cd, graph_path=graph)
    assert r1["cell_id"] == r2["cell_id"]
    assert r1["similarity"] == r2["similarity"]
    assert r1["ranked"] == r2["ranked"]


def test_match_no_query_entities(tmp_path):
    graph = _write_graph(tmp_path, [("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "cX", "planning", 1.0, sig="rb-101")
    r = sim.match("just plain prose, no ids", category="planning", cells_dir=cd, graph_path=graph)
    assert r["reason"] == "no-query-entities"
    assert r["match"] is None


def test_match_no_candidate_cells(tmp_path):
    graph = _write_graph(tmp_path, [("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)  # empty archive
    r = sim.match("rb-100", category="planning", cells_dir=cd, graph_path=graph)
    assert r["reason"] == "no-candidate-cells"
    assert r["match"] is None


def test_match_no_overlap_returns_no_match(tmp_path):
    # Query entity is in the graph, but every cell references a node with ~0 mass.
    graph = _write_graph(tmp_path, [("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "cZ", "planning", 1.0, sig="rb-555 unrelated")  # rb-555 not in graph
    r = sim.match("rb-100", category="planning", cells_dir=cd, graph_path=graph)
    assert r["reason"] == "no-overlap"
    assert r["match"] is None
    assert r["cell_id"] is None


def test_match_searches_all_categories_when_none(tmp_path):
    graph = _write_graph(tmp_path, [("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "c1", "planning", 1.0, sig="rb-999")   # no graph mass
    _upsert(cd, "c1", "retrieval", 1.0, sig="rb-101")  # same id, other category, on-graph
    r = sim.match("rb-100", category=None, cells_dir=cd, graph_path=graph)
    assert r["reason"] == "matched"
    assert r["category"] == "retrieval"  # the on-graph cell wins across categories
    assert r["candidates_considered"] == 2  # both categories scanned, no id collision


# --- the headline contract: KG+PPR by code path, NOT embeddings -------------

def test_matcher_reuses_kg_ppr_substrate_by_code_path():
    import ast

    src = (CORE_SCRIPTS / "cell-similarity.py").read_text(encoding="utf-8")
    # Composes the three shared substrates explicitly (the filenames appear in the
    # _load_sibling calls -- positive code-path evidence).
    assert "knowledge-graph-ppr.py" in src
    assert "knowledge-graph-build.py" in src
    assert "cells-archive.py" in src
    # And does NOT reach for any neural-embedding path (the product NPC artifact).
    # Scan EXECUTABLE CODE ONLY: the module docstring legitimately NAMES the forbidden
    # terms to document that this matcher deliberately avoids them (the MIND-SCOPED
    # disambiguation), so a whole-file substring scan would false-positive on its own
    # warning. Strip the module docstring by its line span, then scan the rest.
    tree = ast.parse(src)
    lines = src.splitlines()
    body = tree.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)):
        doc = body[0]
        code_lines = lines[: doc.lineno - 1] + lines[doc.end_lineno:]
    else:
        code_lines = lines
    code_only = "\n".join(code_lines).lower()
    # Embedding-LIBRARY fingerprints -- these never appear in legitimate KG+PPR code.
    # (The bare word "embedding" is deliberately NOT a token: it shows up in honest
    # "no NPC embeddings" documentation-of-absence -- e.g. the CLI --description --
    # so guarding on it would false-positive on the contract's own statement. The
    # affirmative KG+PPR code path is proven by the 3 substrate imports above plus
    # test_matcher_invokes_real_ppr_compute's compute() spy.)
    for forbidden in ("minilm", "sentence-transformer", "sentencetransformer",
                      "sentence_transformers", "import torch"):
        assert forbidden not in code_only, f"matcher code must not use {forbidden!r} (KG+PPR only)"


def test_matcher_invokes_real_ppr_compute(monkeypatch, tmp_path):
    # Code-path proof: monkeypatch knowledge-graph-ppr.compute and assert the matcher
    # actually routes through it (not an embedding similarity).
    graph = _write_graph(tmp_path, [("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "cX", "planning", 1.0, sig="rb-101")

    ppr_mod = sim._ppr_module()
    calls = {"n": 0}
    real_compute = ppr_mod.compute

    def _spy(*a, **k):
        calls["n"] += 1
        return real_compute(*a, **k)

    monkeypatch.setattr(ppr_mod, "compute", _spy)
    r = sim.match("rb-100", category="planning", cells_dir=cd, graph_path=graph)
    assert calls["n"] == 1  # the matcher seeded PPR exactly once
    assert r["reason"] == "matched"
