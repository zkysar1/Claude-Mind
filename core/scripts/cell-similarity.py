#!/usr/bin/env python3
# domain-leak-exempt: framework cell-similarity matcher; the entity-ID prefixes
#   (rb-, guard-, g-, sig-, node:, cat:, tag:) are framework record identifiers and
#   the knowledge-graph's own pseudo-node namespaces -- functional data, not domain
#   examples.
"""cell-similarity.py -- KG+PPR cell matching for the Go-Explore archive.

Given a query Mind state, return the most-similar archived cell
(core/scripts/cells-archive.py, g-306-47) using the Mind knowledge-graph +
Personalized PageRank substrate (knowledge-graph-build.py + knowledge-graph-ppr.py,
g-306-42/43/44) -- NOT the product NPC pipeline's sentence embeddings.

BRD Gap 17, child 2/4 of g-306-16 (g-306-48). Consumes child 1/4 (g-306-47, the
cell archive). Mirrors the g-306-43 PPR-module child's composition pattern: a thin
deterministic layer over the shared HippoRAG substrate, no new graph machinery.

MIND-SCOPED -- READ THIS BEFORE EXTENDING.
  Similarity here is computed over the framework's OWN symbolic record graph
  (rb-/guard-/g-/node-keys and their cross-references) via Personalized PageRank.
  NO neural embedding is involved -- this is deliberately the symbolic,
  deterministic, inspectable alternative to the product NPC pipeline's
  all-MiniLM-L6-v2 cell-reuse cascade (a SEPARATE product-side artifact; see tree
  node cell-reuse-algorithm, whose `embedding-similarity` entity is the product
  path, NOT this one). The two never share storage, vocabulary, or code.

ALGORITHM (HippoRAG passage ranking, 2405.14831).
  1. Extract record-identifier ENTITIES from the query state's signature using the
     SAME extractor that built the graph (knowledge-graph-build._extract_refs over
     _stringify) -- so query entities are guaranteed to live in the graph's node
     vocabulary. Re-implementing the regex here would risk drift from the builder
     (single-source-of-truth: communication-clarity rule 5).
  2. Seed Personalized PageRank from those query entities -> a score per graph node
     (knowledge-graph-ppr.compute). High-PPR nodes are the records reachable in 1-2
     hops from the query (a shared category/tag hub, or a direct cross-reference).
  3. Score each archived cell by SUMMING the PPR mass landing on ITS OWN signature
     entities -- the standard HippoRAG passage score. The cell whose entities
     capture the most query-seeded PPR mass is the most similar.
  4. Return the argmax cell, tie-broken by cell_id ascending (determinism).

PURITY / DETERMINISM (goal contract).
  The PPR core is deterministic (sorted nodes, fixed damping/tol/iters upstream);
  the cell aggregation is a sum; the final ranking tie-breaks by cell_id ascending
  -- identical (query, archive, graph) inputs yield a byte-identical winner. No
  network, no daemon, no event-loop block (PPR is iteration-capped upstream). When
  the query has no graph-recognized entity, OR no cell shares positive PPR mass with
  it, the matcher returns NO match (an honest "no KG signal") rather than an
  arbitrary cell -- so a downstream Go-Explore caller archives a fresh cell instead
  of falsely reusing an unrelated trajectory.

Usage:
  cell-similarity.py --query "state referencing rb-245 and guard-832" --category planning
  cell-similarity.py --query-entities "rb-245,guard-832" --top-k 5 --output json
  cell-similarity.py --query "..."                 # search ALL categories (namespaced)
"""

import argparse
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# --- sibling hyphen-named module loaders ------------------------------------
# core/scripts hosts hyphen-named scripts (cells-archive.py, knowledge-graph-*.py)
# that `import` cannot reach by name. importlib-load each once per process, cache
# the result, and FAIL-OPEN: a missing/broken sibling removes the signal it
# provides (-> no match) but never raises. Mirrors retrieve.py::_load_ppr_module.
_MODULE_CACHE = {}


def _load_sibling(mod_alias, filename):
    """Load a hyphen-named sibling script by file path; cache module-or-False."""
    if mod_alias in _MODULE_CACHE:
        return _MODULE_CACHE[mod_alias] or None
    try:
        path = os.path.join(_HERE, filename)
        spec = importlib.util.spec_from_file_location(mod_alias, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODULE_CACHE[mod_alias] = mod
        return mod
    except Exception:
        _MODULE_CACHE[mod_alias] = False  # tried + failed; do not retry this process
        return None


def _ppr_module():
    return _load_sibling("knowledge_graph_ppr", "knowledge-graph-ppr.py")


def _build_module():
    return _load_sibling("knowledge_graph_build", "knowledge-graph-build.py")


def _cells_module():
    return _load_sibling("cells_archive", "cells-archive.py")


# --- entity extraction (graph-vocabulary SSOT) ------------------------------

def signature_entities(signature):
    """Record-identifier entities in an (any-typed) state signature.

    Reuses knowledge-graph-build's extractor (_stringify -> _extract_refs) so the
    returned ids are guaranteed to match the graph's node vocabulary -- the build
    module is the single source of truth for what counts as an entity. Handles any
    signature shape (str / list / dict / scalar) because _stringify flattens all of
    them to searchable text. Returns a SORTED unique list (sorted for determinism).
    Fail-open to [] if the build module cannot be loaded (a real env breakage, not a
    normal path).
    """
    build = _build_module()
    if build is None:
        return []
    text = build._stringify(signature)
    return sorted(dict.fromkeys(build._extract_refs(text)))


# --- pure scoring -----------------------------------------------------------

def score_entities(entities, ppr_scores):
    """SUM of PPR mass over a precomputed entity list (HippoRAG passage score).

    Pure scoring half of score_cell: takes an entity iterable + a
    {node: ppr_score} map and returns (score, overlap_count). Entities are
    de-duplicated (order-preserving) so a caller that concatenates several entity
    sources cannot double-count. Lets a caller score a signature it assembled
    DIRECTLY -- e.g. a candidate enriched with cat:/tag: pseudo-node entities that
    the _extract_refs regex never emits from a bare category/tag string (g-306-51)
    -- without round-tripping through text extraction.
    """
    ents = list(dict.fromkeys(entities))
    overlap = [e for e in ents if e in ppr_scores]
    score = sum(ppr_scores.get(e, 0.0) for e in overlap)
    return score, len(overlap)


def score_cell(cell_record, ppr_scores):
    """SUM of PPR mass over a cell's signature entities (HippoRAG passage score).

    Pure: takes a precomputed {node: ppr_score} map (the query-seeded ranking).
    Returns (score, overlap_count) where overlap_count is how many of the cell's
    entities appear in the PPR ranking -- surfaced for inspection/diagnostics.
    """
    ents = signature_entities((cell_record or {}).get("state_signature"))
    return score_entities(ents, ppr_scores)


def rank_cells(candidates, ppr_scores):
    """Deterministic descending ranking of candidate cells by query-relevance.

    candidates : {cell_key: cell_record}. Returns
    [(cell_key, score, overlap_count, record), ...] sorted by (-score, cell_key) so
    ties break by ascending key -- identical inputs yield a byte-identical order.
    """
    rows = []
    for ckey, rec in candidates.items():
        score, overlap = score_cell(rec, ppr_scores)
        rows.append((ckey, score, overlap, rec))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


# --- archive + ppr access ---------------------------------------------------

def _collect_candidates(category, agent, cells_dir):
    """Load candidate cells from the archive.

    category given -> only that category's cells (keyed by bare cell_id). category
    None -> every category, keyed "category/cell_id" to avoid the documented
    cross-category cell_id collision (test_per_category_isolation). The record
    itself always carries the true cell_id + category, so a namespaced key never
    loses identity. Returns ({cell_key: record}, categories_searched).
    """
    cells = _cells_module()
    if cells is None:
        return {}, []
    if category:
        cats = [category]
    else:
        cats = cells.list_categories(agent=agent, cells_dir=cells_dir)
    merged = {}
    for cat in cats:
        loaded = cells.load_category(cat, agent=agent, cells_dir=cells_dir)
        for cid, rec in loaded.items():
            key = cid if category else f"{cat}/{cid}"
            merged[key] = rec
    return merged, cats


def _ppr_scores_for(query_entities, graph_path):
    """Full PPR ranking seeded from query_entities -> ({node: score}, personalized).

    personalized is False when no query entity matched the graph (the upstream
    uniform-teleport fallback). top_k=None keeps the WHOLE ranking so every cell
    entity can be looked up. Fail-open to ({}, False) on any PPR error.
    """
    ppr = _ppr_module()
    if ppr is None or not query_entities:
        return {}, False
    try:
        ranked, meta = ppr.compute(
            list(query_entities), path=graph_path, top_k=None, exclude_pseudo=False
        )
    except Exception:
        return {}, False
    scores = {node: score for node, score in ranked}
    return scores, bool(meta.get("personalized"))


# --- the matcher ------------------------------------------------------------

def match(query_state, *, category=None, agent=None, cells_dir=None, graph_path=None,
          min_similarity=0.0, top_k=None):
    """Return the most-similar archived cell to query_state via KG+PPR.

    Returns a result dict (never raises on a normal no-signal path):
      match                 : the winning cell record, or None
      cell_id / category    : the winner's true identity (None when no match)
      similarity            : the winner's summed PPR mass (0.0 when no match)
      personalized          : whether the query seeded the graph (else uniform PR)
      query_entities        : entities extracted from the query signature
      categories_searched   : category names scanned
      candidates_considered : cell count scanned
      ranked                : top-k inspection view [{cell_id, category,
                              similarity, overlap}, ...]
      reason                : matched | no-query-entities | no-candidate-cells |
                              no-ppr-signal | no-overlap

    A cell wins only when its summed PPR mass is STRICTLY greater than
    min_similarity (default 0.0): zero-overlap candidates never win, so the
    matcher reports "no-overlap" instead of returning an arbitrary tie-broken cell.
    """
    query_entities = signature_entities(query_state)
    candidates, cats = _collect_candidates(category, agent, cells_dir)
    result = {
        "match": None,
        "cell_id": None,
        "category": None,
        "similarity": 0.0,
        "personalized": False,
        "query_entities": query_entities,
        "categories_searched": cats,
        "candidates_considered": len(candidates),
        "ranked": [],
        "reason": None,
    }

    if not query_entities:
        result["reason"] = "no-query-entities"
        return result
    if not candidates:
        result["reason"] = "no-candidate-cells"
        return result

    ppr_scores, personalized = _ppr_scores_for(query_entities, graph_path)
    result["personalized"] = personalized
    if not ppr_scores:
        result["reason"] = "no-ppr-signal"
        return result

    rows = rank_cells(candidates, ppr_scores)
    view_rows = rows[:top_k] if top_k else rows
    result["ranked"] = [
        {
            "cell_id": rec.get("cell_id", ckey),
            "category": rec.get("category"),
            "similarity": score,
            "overlap": overlap,
        }
        for (ckey, score, overlap, rec) in view_rows
    ]

    best_key, best_score, _best_overlap, best_rec = rows[0]
    if best_score <= min_similarity:
        result["reason"] = "no-overlap"
        return result

    result["match"] = best_rec
    result["cell_id"] = best_rec.get("cell_id", best_key)
    result["category"] = best_rec.get("category")
    result["similarity"] = best_score
    result["reason"] = "matched"
    return result


# --- CLI --------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Most-similar archived cell to a query Mind state via KG+PPR "
                    "(no NPC embeddings)."
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--query", help="Query state signature (any text; record ids are extracted).")
    src.add_argument("--query-entities", help="Comma-separated record ids to use as the query directly.")
    ap.add_argument("--category", default=None, help="Restrict to one cell category (default: all).")
    ap.add_argument("--agent", default=None, help="Agent whose archive to read (default: bound agent).")
    ap.add_argument("--cells-dir", default=None, help="Override cells dir (test injection).")
    ap.add_argument("--graph-path", default=None, help="Override knowledge-graph path (test injection).")
    ap.add_argument("--min-similarity", type=float, default=0.0,
                    help="A cell wins only with summed PPR mass strictly above this (default 0.0).")
    ap.add_argument("--top-k", type=int, default=5, help="Ranked candidates to display (default 5).")
    ap.add_argument("--output", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)

    query_state = args.query if args.query is not None else args.query_entities
    result = match(
        query_state,
        category=args.category,
        agent=args.agent,
        cells_dir=args.cells_dir,
        graph_path=args.graph_path,
        min_similarity=args.min_similarity,
        top_k=args.top_k,
    )

    if args.output == "json":
        print(json.dumps(result, ensure_ascii=True))
        return 0

    qe = ", ".join(result["query_entities"]) or "(none)"
    print(f"cell-similarity: query entities [{qe}] | "
          f"{result['candidates_considered']} candidate(s) over "
          f"{len(result['categories_searched'])} categor(ies) | "
          f"personalized={result['personalized']}")
    if result["reason"] != "matched":
        print(f"  no match ({result['reason']})")
        return 0
    print(f"  BEST: {result['cell_id']} (category={result['category']}) "
          f"similarity={result['similarity']:.8f}")
    for rank_i, row in enumerate(result["ranked"], start=1):
        print(f"  {rank_i:3d}. sim={row['similarity']:.8f}  overlap={row['overlap']:2d}  "
              f"{row['cell_id']} [{row['category']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
