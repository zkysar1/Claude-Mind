"""test_goal_selector_cell_return.py --  (BRD Gap 17, child C of ).

Pins the Go-Explore cell-return boost wired into goal-selector.py
(apply_cell_return_boost): the flag-gated, boost-only selection adjustment that
promotes candidate goals graph-proximate to the highest-value archived cells
(the cells worth RETURNING to). Mirrors the g-306-44 retrieve.py PPR blend.

Contracts pinned:
  * DEFAULT OFF (cell_return.enabled false) -> apply_cell_return_boost is a
    byte-identical no-op: ``scored`` is returned untouched, no key added. This is
    the "flag off => selection unchanged (no regression by construction)" outcome.
  * ON + a high-value cell whose signature shares record-id entities with a
    candidate -> that candidate gets a positive cell_return_bonus and a higher
    score, deterministically; a candidate whose entities are absent from the graph
    gets nothing. This is the "deterministically return to a matching cell
    (Go-Explore return policy)" outcome.
  * boost-only: no candidate's score is ever lowered.
  * reuse BY CODE PATH: the boost routes through the g-306-48 cell-similarity
    matcher, which routes through the g-306-42/43 KG+PPR substrate -- proven with a
    compute() spy + a source scan, NOT a re-implementation.

Daemon-safe (no daemon_integration marker): every test injects an explicit
cells_dir=tmp_path AND graph_path=tmp file, so neither the real
agents/<agent>/cells/ tree nor the real meta/knowledge-graph.jsonl is touched and
no daemon is spawned. Pure file-ops + in-memory PPR on tmp directories.

Cross-references:
  - g-306-44 / retrieve.py _ppr_weight -- the flag-gated boost-only template
  - g-306-47 / test_cells_archive.py -- the cell store this seeds from
  - g-306-48 / test_cell_similarity.py -- the matcher this reuses (match primitives)
  - HippoRAG 2405.14831 -- the PPR-from-seed-cells passage ranking this implements
"""
from __future__ import annotations

import copy
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


gs = _load("goal_selector_crt", "goal-selector.py")
cells = _load("cells_archive_crt", "cells-archive.py")


# --- fixtures ---------------------------------------------------------------

def _write_graph(tmp_path, pairs):
    """[(s, o), ...] -> a knowledge-graph.jsonl file; returns its path string."""
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


def _upsert(cells_dir, cell_id, category, score, sig):
    return cells.upsert_cell(
        cell_id, category,
        state_signature=sig, trajectory=[cell_id],
        score=score, cells_dir=cells_dir, now="2026-06-21T00:00:00",
    )


def _cfg(enabled, **over):
    base = {"enabled": enabled, "seed_top_n": 5, "bonus_scale": 3.0, "bonus_max": 1.5}
    base.update(over)
    return base


def _scored(entries):
    """entries: [(goal_id, title, score), ...] -> scored-shaped dicts."""
    return [
        {"goal_id": gid, "aspiration_id": "asp-306", "title": title,
         "category": "planning", "score": float(sc),
         "breakdown": {"priority": 2.0}, "raw": {"priority": 2}}
        for gid, title, sc in entries
    ]


# --- flag OFF: byte-identical no-op -----------------------------------------

def test_default_config_is_off():
    assert gs.CELL_RETURN_CONFIG["enabled"] is False  # no-regression default


def test_flag_off_is_byte_identical(tmp_path):
    graph = _write_graph(tmp_path, [("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "cX", "planning", 9.0, sig="rb-100")
    scored = _scored([("g-306-49", "wire rb-101 path", 12.0),
                      ("g-001-10", "generate hypotheses", 14.0)])
    before = copy.deepcopy(scored)
    out = gs.apply_cell_return_boost(scored, _cfg(False), cells_dir=cd, graph_path=graph)
    assert out is scored                       # mutate-in-place contract
    assert scored == before                    # nothing changed, no key added
    assert all("cell_return_bonus" not in s["breakdown"] for s in scored)


# --- flag ON: deterministic, boost-only, matching-cell promotion ------------

def test_on_promotes_candidate_matching_high_value_cell(tmp_path):
    # Graph: seed rb-100 -- rb-101 (1 hop, gets PPR mass); rb-999 is NOT a node.
    graph = _write_graph(tmp_path, [("rb-100", "rb-101"), ("rb-200", "rb-300")])
    cd = _cells_dir(tmp_path)
    # The single HIGH-value cell -- its signature rb-100 is the PPR seed.
    _upsert(cd, "hot", "planning", 9.0, sig="reached via rb-100")
    scored = _scored([
        ("g-306-49", "wire rb-101 into selection", 12.0),  # rb-101 -> on-graph -> boosted
        ("g-306-50", "touches rb-999 only", 12.0),          # rb-999 -> off-graph -> no boost
    ])
    out = gs.apply_cell_return_boost(scored, _cfg(True), cells_dir=cd, graph_path=graph)
    a = next(s for s in out if s["goal_id"] == "g-306-49")
    b = next(s for s in out if s["goal_id"] == "g-306-50")
    # A matched a high-value cell (shares rb-101 graph proximity) -> boosted.
    assert "cell_return_bonus" in a["breakdown"]
    assert a["breakdown"]["cell_return_bonus"] > 0.0
    assert a["score"] > 12.0
    assert a["raw"]["cell_return_overlap"] >= 1
    # B's entity is absent from the graph -> zero PPR mass -> no boost, unchanged.
    assert "cell_return_bonus" not in b["breakdown"]
    assert b["score"] == 12.0


def test_on_is_deterministic(tmp_path):
    graph = _write_graph(tmp_path, [("rb-100", "rb-101"), ("rb-101", "rb-102")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "hot", "planning", 5.0, sig="rb-100")
    base = _scored([("g-306-49", "rb-101 here", 12.0), ("g-306-50", "rb-102 here", 11.0)])
    out1 = gs.apply_cell_return_boost(copy.deepcopy(base), _cfg(True), cells_dir=cd, graph_path=graph)
    out2 = gs.apply_cell_return_boost(copy.deepcopy(base), _cfg(True), cells_dir=cd, graph_path=graph)
    assert [s["score"] for s in out1] == [s["score"] for s in out2]
    assert [s["breakdown"].get("cell_return_bonus") for s in out1] == \
           [s["breakdown"].get("cell_return_bonus") for s in out2]


def test_on_is_boost_only_never_demotes(tmp_path):
    graph = _write_graph(tmp_path, [("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "hot", "planning", 5.0, sig="rb-100")
    base = _scored([("g-306-49", "rb-101", 12.0), ("g-306-50", "rb-999", 11.0)])
    out = gs.apply_cell_return_boost(copy.deepcopy(base), _cfg(True), cells_dir=cd, graph_path=graph)
    for orig, new in zip(base, out):
        assert new["score"] >= orig["score"]    # never lowered


def test_on_bonus_is_capped(tmp_path):
    graph = _write_graph(tmp_path, [("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "hot", "planning", 5.0, sig="rb-100")
    scored = _scored([("g-306-49", "rb-100 rb-101", 12.0)])
    out = gs.apply_cell_return_boost(scored, _cfg(True, bonus_scale=1000.0, bonus_max=1.5),
                                     cells_dir=cd, graph_path=graph)
    bonus = out[0]["breakdown"].get("cell_return_bonus", 0.0)
    assert bonus <= 1.5 + 1e-9                  # bonus_max cap honored


def test_on_empty_archive_no_boost(tmp_path):
    graph = _write_graph(tmp_path, [("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)                   # empty archive
    scored = _scored([("g-306-49", "rb-101", 12.0)])
    before = copy.deepcopy(scored)
    out = gs.apply_cell_return_boost(scored, _cfg(True), cells_dir=cd, graph_path=graph)
    assert out == before                        # no seeds -> graceful no-op


# --- the headline contract: reuse KG+PPR matcher BY CODE PATH ---------------

def test_reuses_cell_similarity_and_kg_ppr_by_code_path(tmp_path, monkeypatch):
    # Code-path proof: spy on the KG+PPR compute() the matcher uses and assert the
    # boost actually routes through it (not an embedding / re-implemented scorer).
    graph = _write_graph(tmp_path, [("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "hot", "planning", 5.0, sig="rb-100")

    sim = gs._load_cell_sim_module()
    assert sim is not None
    ppr_mod = sim._ppr_module()
    calls = {"n": 0}
    real_compute = ppr_mod.compute

    def _spy(*a, **k):
        calls["n"] += 1
        return real_compute(*a, **k)

    monkeypatch.setattr(ppr_mod, "compute", _spy)
    scored = _scored([("g-306-49", "rb-101", 12.0)])
    gs.apply_cell_return_boost(scored, _cfg(True), cells_dir=cd, graph_path=graph)
    assert calls["n"] == 1                       # seeded PPR exactly once (one run/round)


def test_source_loads_the_cell_similarity_matcher():
    # Positive code-path evidence: goal-selector composes the shared matcher (the
    # filename appears in the loader), it does not re-implement cell scoring.
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    assert "cell-similarity.py" in src
    assert "_load_cell_sim_module" in src
    assert "apply_cell_return_boost" in src


# --- : signature enrichment (cat:/tag: pseudo-node entities) ---------
# The _extract_refs regex only emits rb-/guard-/g- record ids, so a bare category
# or tag never becomes a candidate entity -- a candidate's reliable graph footprint
# is just its (often graph-distant) leaf goal-id. enrich_signature injects the
# cat:<category> + tag:<t> pseudo-node entities the graph already carries
# has_category/has_tag edges to, so a same-category high-value cell boosts
# same-category candidates. Default-off (no-regression by construction).

def test_enrich_signature_default_off_in_config():
    assert gs.CELL_RETURN_CONFIG.get("enrich_signature") is False  # no-regression default


def test_enrich_off_bare_category_is_not_an_entity(tmp_path):
    # cat:planning carries PPR mass (seed rb-100 -> cat:planning), but the
    # candidate's only text entity is its graph-distant goal-id. With enrich OFF
    # (the current ON behavior) the bare category never becomes an entity -> no
    # boost. This pins that the cat-boost requires the enrich flag, not enabled alone.
    graph = _write_graph(tmp_path, [("rb-100", "cat:planning"), ("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "hot", "planning", 9.0, sig="rb-100")
    scored = _scored([("g-999-99", "no graph entity in title", 12.0)])
    out = gs.apply_cell_return_boost(scored, _cfg(True), cells_dir=cd, graph_path=graph)
    assert "cell_return_bonus" not in out[0]["breakdown"]
    assert out[0]["score"] == 12.0


def test_enrich_on_category_entity_boosts_same_category(tmp_path):
    # Same graph/cell/candidate, enrich ON: cat:planning is injected as a candidate
    # entity -> it captures the seed cell's same-category PPR mass -> boost fires
    # even though the candidate's goal-id is graph-distant ( headline).
    graph = _write_graph(tmp_path, [("rb-100", "cat:planning"), ("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "hot", "planning", 9.0, sig="rb-100")
    scored = _scored([("g-999-99", "no graph entity in title", 12.0)])
    out = gs.apply_cell_return_boost(scored, _cfg(True, enrich_signature=True),
                                     cells_dir=cd, graph_path=graph)
    assert "cell_return_bonus" in out[0]["breakdown"]
    assert out[0]["breakdown"]["cell_return_bonus"] > 0.0
    assert out[0]["score"] > 12.0
    assert out[0]["raw"]["cell_return_overlap"] >= 1


def test_enrich_on_tag_entity_boosts_same_tag(tmp_path):
    # tag: pseudo-node injection: seed entity -> tag:hot carries PPR mass; a
    # candidate carrying tags=["hot"] captures it under enrich. cat:planning is
    # absent from this graph, so the boost is attributable to the tag alone.
    graph = _write_graph(tmp_path, [("rb-100", "tag:hot"), ("rb-100", "rb-101")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "hot", "planning", 9.0, sig="rb-100")
    scored = _scored([("g-999-99", "no graph entity", 12.0)])
    scored[0]["tags"] = ["hot"]   # selector now propagates goal tags into scored entries
    out = gs.apply_cell_return_boost(scored, _cfg(True, enrich_signature=True),
                                     cells_dir=cd, graph_path=graph)
    assert out[0]["breakdown"].get("cell_return_bonus", 0.0) > 0.0
    assert out[0]["raw"].get("cell_return_overlap", 0) >= 1


def test_enrich_on_is_still_boost_only(tmp_path):
    # enrich preserves the boost-only invariant: no candidate score is ever lowered.
    graph = _write_graph(tmp_path, [("rb-100", "cat:planning")])
    cd = _cells_dir(tmp_path)
    _upsert(cd, "hot", "planning", 9.0, sig="rb-100")
    base = _scored([("g-999-99", "x", 12.0), ("g-888-88", "y", 11.0)])
    out = gs.apply_cell_return_boost(copy.deepcopy(base), _cfg(True, enrich_signature=True),
                                     cells_dir=cd, graph_path=graph)
    for orig, new in zip(base, out):
        assert new["score"] >= orig["score"]
