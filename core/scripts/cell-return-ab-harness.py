#!/usr/bin/env python3
# domain-leak-exempt: framework cell-return A/B harness; the entity-ID prefixes
#   (rb-, guard-, g-, cat:, tag:, node:) are framework record identifiers and the
#   knowledge-graph's pseudo-node namespaces -- functional data, not domain examples.
"""cell-return-ab-harness.py -- A/B compare baseline vs cell-return-boosted selection.

g-306-50 (BRD Gap 17, child D/4 of g-306-16). Sibling to ppr-blend-ab-harness.py
(g-306-44/45): that harness validates the retrieve.py PPR blend; THIS one validates
the goal-selector.py cell-return boost (apply_cell_return_boost, g-306-49).

It runs apply_cell_return_boost over a candidate goal set TWICE -- once with
cell_return.enabled OFF (the production default; byte-identical baseline) and once
ON (Personalized PageRank seeded from the top-N highest-value archived Go-Explore
cells) -- and reports how the post-boost ranking changed. The boost is boost-only
(see goal-selector.apply_cell_return_boost), so a candidate can only move UP when
its record-id entities are PPR-proximate to the high-value cells' entities.

Validated against the REAL Mind knowledge graph (the g-306-45 discipline: a toy
tmp graph would hide an inert-feature defect; only the real corpus exposes whether
candidate entities actually land as graph nodes).

Scenarios:
  --scenario demo  (default): a controlled REAL-GRAPH demonstration. A tmp archive
      is seeded with a high-value cell whose signature entities are real,
      well-connected graph nodes (guard-832, rb-245). Three candidate goals -- one
      whose signature shares a graph-proximate id, one whose id is graph-distant,
      one with no graph entity -- are scored so the BASELINE winner has NO cell
      proximity. Shows the boost re-orders selection (changes the winner) by graph
      proximity alone.
  --scenario empty : the real (live) per-agent archive. When the archive is
      unpopulated (Go-Explore has not run) this is FLAT -- the honest
      "dormant without seeds" boundary. A flat result is a FINDING, not a pass.
  --scenario coverage : diagnostic only. For each candidate, report the entities
      _extract_refs pulls from its signature and whether each is a real graph node
      (the g-306-45 cross-component key-match check applied to candidate goals).

The harness reads the real graph read-only and writes only a self-managed tmp
archive dir that it removes on exit. It never mutates the on-disk default
(cell_return stays OFF). Reversible + bounded (self.md).

Usage:
  cell-return-ab-harness.py
  cell-return-ab-harness.py --scenario empty --output json
  cell-return-ab-harness.py --scenario coverage
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(alias, filename):
    """importlib-load a hyphen-named sibling script by file path (cache-free)."""
    path = os.path.join(_HERE, filename)
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cfg(enabled, *, seed_top_n=5, bonus_scale=3.0, bonus_max=1.5,
         enrich_signature=False):
    return {"enabled": enabled, "seed_top_n": seed_top_n,
            "bonus_scale": bonus_scale, "bonus_max": bonus_max,
            "enrich_signature": enrich_signature}


def _rank(gs, scored, enabled, *, cells_dir=None, agent=None, graph_path=None,
          bonus_scale=3.0, bonus_max=1.5, enrich_signature=False):
    """Apply apply_cell_return_boost with the flag toggled, then sort like the
    selector does (descending score, tie-break ascending goal_id). Returns the
    ordered list of (goal_id, score) plus the per-candidate boost detail."""
    work = copy.deepcopy(scored)
    gs.apply_cell_return_boost(
        work, _cfg(enabled, bonus_scale=bonus_scale, bonus_max=bonus_max,
                   enrich_signature=enrich_signature),
        cells_dir=cells_dir, agent=agent, graph_path=graph_path)
    work.sort(key=lambda s: (-s.get("score", 0.0), s.get("goal_id", "")))
    order = [(s.get("goal_id", ""), s.get("score", 0.0)) for s in work]
    detail = {s.get("goal_id", ""): {
        "bonus": (s.get("breakdown", {}) or {}).get("cell_return_bonus", 0.0),
        "ppr_mass": (s.get("raw", {}) or {}).get("cell_return_ppr_mass", 0.0),
        "overlap": (s.get("raw", {}) or {}).get("cell_return_overlap", 0),
    } for s in work}
    return order, detail


def _compare(gs, scored, *, cells_dir=None, agent=None, graph_path=None, top_k=5,
             bonus_scale=3.0, bonus_max=1.5, enrich_signature=False):
    base_order, _ = _rank(gs, scored, False, cells_dir=cells_dir, agent=agent,
                          graph_path=graph_path)
    blend_order, detail = _rank(gs, scored, True, cells_dir=cells_dir, agent=agent,
                                graph_path=graph_path, bonus_scale=bonus_scale,
                                bonus_max=bonus_max, enrich_signature=enrich_signature)
    base_keys = [k for k, _ in base_order]
    blend_keys = [k for k, _ in blend_order]
    base_pos = {k: i for i, k in enumerate(base_keys)}
    blend_pos = {k: i for i, k in enumerate(blend_keys)}
    promotions = []
    for k in blend_keys:
        if k in base_pos and blend_pos[k] < base_pos[k]:
            promotions.append({"goal_id": k, "baseline_rank": base_pos[k] + 1,
                               "blended_rank": blend_pos[k] + 1,
                               "delta": base_pos[k] - blend_pos[k],
                               "bonus": detail.get(k, {}).get("bonus", 0.0)})
    promotions.sort(key=lambda p: -p["delta"])
    base_topk, blend_topk = set(base_keys[:top_k]), set(blend_keys[:top_k])
    jac = (len(base_topk & blend_topk) / len(base_topk | blend_topk)
           if (base_topk | blend_topk) else 1.0)
    return {
        "baseline_winner": base_keys[0] if base_keys else None,
        "blended_winner": blend_keys[0] if blend_keys else None,
        "winner_changed": bool(base_keys and blend_keys
                               and base_keys[0] != blend_keys[0]),
        "reordered": sum(1 for k in blend_keys
                         if k in base_pos and blend_pos[k] != base_pos[k]),
        "promotions": promotions,
        "topk_jaccard": round(jac, 4),
        "baseline_order": [{"goal_id": k, "score": v} for k, v in base_order],
        "blended_order": [{"goal_id": k, "score": v, **detail.get(k, {})}
                          for k, v in blend_order],
    }


def _demo_scored():
    """Three candidates scored so the BASELINE winner (C) has no cell proximity.
    Entities are REAL graph nodes (verified in-graph by the g-306-50 probe):
      A: id+title carry guard-832 -> graph-proximate to the seeded cell -> boosted
      B: title carries rb-810 (a distant real node) -> little/no boost
      C: no record-id entity -> never boosted; baseline winner by score
    Base scores: C(10.40) > B(10.20) > A(10.00). If the boost lifts A past C, the
    cell-return mechanism has CHANGED THE SELECTED GOAL purely by graph proximity.
    """
    return [
        {"goal_id": "g-115-100", "title": "Apply guard-832 partner-file commit-flow fix",
         "category": "framework-architecture", "score": 10.00,
         "breakdown": {"priority": 2.0}, "raw": {"priority": 2}},
        {"goal_id": "g-115-101", "title": "Fix rb-810 Vertx null-guard regression",
         "category": "framework-architecture", "score": 10.20,
         "breakdown": {"priority": 2.0}, "raw": {"priority": 2}},
        {"goal_id": "g-115-102", "title": "Generate weekly progress report",
         "category": "reporting", "score": 10.40,
         "breakdown": {"priority": 2.0}, "raw": {"priority": 2}},
    ]


def _seed_demo_archive(cells, cells_dir):
    """Upsert one high-value Go-Explore cell whose signature entities are real,
    well-connected graph nodes (guard-832, rb-245). This is the 'cell worth
    RETURNING to' that seeds PPR when cell_return is ON."""
    cells.upsert_cell(
        "hot-commit-flow", "framework-architecture",
        state_signature="high-value trajectory through guard-832 and rb-245",
        trajectory=["hot-commit-flow"], score=9.5,
        cells_dir=cells_dir, now="2026-06-21T00:00:00")


def _coverage(gs, scored, graph_path):
    """Diagnostic: per candidate, the entities _extract_refs pulls from its
    signature and whether each is a real graph node (g-306-45 key-match check)."""
    sim = gs._load_cell_sim_module()
    build = sim._build_module()
    ppr = sim._ppr_module()
    # Build the full graph node set once (seed from a known hub; the ranking
    # spans every node, so its keys ARE the node set).
    ranked, _meta = ppr.compute(["cat:system"], path=graph_path, top_k=None,
                                exclude_pseudo=False)
    nodes = set(n for n, _ in ranked)
    rows = []
    for s in scored:
        sig = "{} {} {}".format(s.get("goal_id", ""), s.get("title", ""),
                                s.get("category", ""))
        ents = sim.signature_entities(sig)
        rows.append({"goal_id": s.get("goal_id"),
                     "entities": ents,
                     "in_graph": {e: (e in nodes) for e in ents}})
    return {"graph_nodes": len(nodes), "candidates": rows}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="A/B compare baseline vs cell-return-boosted goal selection (g-306-50).")
    ap.add_argument("--scenario", choices=("demo", "empty", "coverage"),
                    default="demo")
    ap.add_argument("--graph-path", default=None,
                    help="Knowledge-graph path (default: live graph).")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--bonus-scale", type=float, default=3.0)
    ap.add_argument("--bonus-max", type=float, default=1.5)
    ap.add_argument("--enrich-signature", action="store_true",
                    help="g-306-51: enrich the ON arm's candidate signatures with "
                         "cat:/tag: pseudo-node entities (A/B the enriched signal).")
    ap.add_argument("--output", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)

    gs = _load("goal_selector_abh", "goal-selector.py")
    scored = _demo_scored()

    if args.scenario == "coverage":
        rep = {"scenario": "coverage",
               **_coverage(gs, scored, args.graph_path)}
        _emit(rep, args.output)
        return 0

    if args.scenario == "empty":
        # Real live archive (no tmp dir, no agent override): with an unpopulated
        # archive _high_value_cell_seeds returns [] -> boost is a no-op.
        rep = {"scenario": "empty (live archive)",
               **_compare(gs, scored, graph_path=args.graph_path, top_k=args.top_k,
                          bonus_scale=args.bonus_scale, bonus_max=args.bonus_max,
                          enrich_signature=args.enrich_signature)}
        _emit(rep, args.output)
        return 0

    # demo: seed a tmp archive with one real-entity high-value cell.
    cells = _load("cells_archive_abh", "cells-archive.py")
    tmpdir = tempfile.mkdtemp(prefix="cell-return-ab-")
    try:
        cells_dir = os.path.join(tmpdir, "cells")
        os.makedirs(cells_dir, exist_ok=True)
        _seed_demo_archive(cells, cells_dir)
        rep = {"scenario": "demo (seeded tmp archive, real graph)",
               "seed_cell_entities": gs._load_cell_sim_module().signature_entities(
                   "high-value trajectory through guard-832 and rb-245"),
               **_compare(gs, scored, cells_dir=cells_dir, graph_path=args.graph_path,
                          top_k=args.top_k, bonus_scale=args.bonus_scale,
                          bonus_max=args.bonus_max,
                          enrich_signature=args.enrich_signature)}
        _emit(rep, args.output)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


def _emit(rep, output):
    if output == "json":
        print(json.dumps(rep, ensure_ascii=True, indent=2))
        return
    print("=== cell-return A/B: %s ===" % rep.get("scenario"))
    if rep["scenario"].startswith("coverage"):
        print("  graph nodes: %d" % rep["graph_nodes"])
        for c in rep["candidates"]:
            print("  %s: entities=%s" % (c["goal_id"], c["entities"]))
            for e, present in c["in_graph"].items():
                print("      %s in-graph=%s" % (e, present))
        return
    if "seed_cell_entities" in rep:
        print("  seed-cell entities: %s" % rep["seed_cell_entities"])
    print("  baseline winner: %s  ->  blended winner: %s  (changed=%s)"
          % (rep["baseline_winner"], rep["blended_winner"], rep["winner_changed"]))
    print("  top-%d Jaccard(baseline,blended) = %s | %d reordered"
          % (len(rep["blended_order"]), rep["topk_jaccard"], rep["reordered"]))
    print("  baseline order: %s"
          % ["%s=%.2f" % (o["goal_id"], o["score"]) for o in rep["baseline_order"]])
    print("  blended order:  %s"
          % ["%s=%.2f(+%.2f)" % (o["goal_id"], o["score"], o.get("bonus", 0.0))
             for o in rep["blended_order"]])
    if rep["promotions"]:
        print("  promotions (rank improved by cell-return proximity):")
        for p in rep["promotions"]:
            print("    %s: #%d -> #%d (+%d, bonus=%.2f)"
                  % (p["goal_id"], p["baseline_rank"], p["blended_rank"],
                     p["delta"], p["bonus"]))
    else:
        print("  no rank changes (no candidate was PPR-proximate to the seed cells)")


if __name__ == "__main__":
    sys.exit(main())
