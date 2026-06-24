#!/usr/bin/env python3
# domain-leak-exempt: framework knowledge-graph retriever; the entity-ID prefixes
#   (rb-, guard-, node:, cat:, tag:) are framework record identifiers and the
#   graph's own pseudo-node namespaces -- functional data, not domain examples.
"""knowledge-graph-ppr.py -- Personalized PageRank over the Mind knowledge-graph.

Reads the triple-store built by knowledge-graph-build.py (meta/knowledge-graph.jsonl),
constructs an undirected weighted graph over the framework's record/relation
entities, and ranks nodes by Personalized PageRank seeded from a set of query
entities. The high-PPR record nodes (rb-/guard-/node:) are the multi-hop-relevant
source records that a downstream retriever surfaces.

BRD Gap 1b+1c, child 2/4 of g-306-15. Evidence: HippoRAG (2405.14831) -- after the
OpenIE->graph step (child 1, knowledge-graph-build.py), PPR seeded from recognized
query entities ranks graph nodes so that records reachable in 1-2 hops from the seeds
(e.g. records sharing a category/tag hub, or directly cross-referencing one another)
surface above lexically-unrelated records. Child 3 (g-306-44) blends this ranking into
retrieve.sh; child 4 (g-306-45) validates multi-hop retrieval.

MIND-SCOPED -- READ THIS BEFORE EXTENDING.
  This ranks the Mind FRAMEWORK's OWN symbolic record graph (rb-/guard-/g-/node-keys
  and their cross-references). It is NOT the NPC behavior pipeline's all-MiniLM-L6-v2
  sentence embeddings (a separate product-side artifact). No neural embedding is
  involved: PPR here is a purely structural/lexical ranking over inspectable record
  identifiers, deterministic and reproducible (the framework's design value:
  "structured, inspectable, evolvable reasoning").

GRAPH MODEL.
  Each triple (s, p, o) becomes an UNDIRECTED edge s--o with weight = multiplicity
  (number of triples asserting that s--o connection). Undirected is deliberate: a
  category/tag node (cat:X / tag:Y) is a HUB that connects every record carrying it,
  so two records sharing a tag are 2 hops apart through the tag node and PPR mass
  flows between them. A `references`/`derived_from` edge directly connects two record
  nodes. Restart (teleport) mass returns to the seed set every step -- that is what
  makes the ranking PERSONALIZED to the query rather than global importance.

PURITY / DETERMINISM (goal contract).
  Pure: the core functions take triples/adjacency as arguments and perform only
  in-memory arithmetic -- no external-service calls, no network, no daemon. The only
  I/O is reading the local graph file in the CLI/convenience path. Deterministic:
  dict-based sparse power iteration with a fixed damping/tolerance/iteration cap and
  sorted, tie-broken output -- identical inputs yield byte-identical rankings. Bounded:
  max_iters caps the loop so a caller (incl. a future daemon endpoint) never blocks
  the event loop on an unbounded computation.

Usage:
  knowledge-graph-ppr.py --seeds "rb-245,guard-832"          # rank from query entities
  knowledge-graph-ppr.py --seeds "rb-245" --exclude-pseudo   # record nodes only (no cat:/tag:)
  knowledge-graph-ppr.py --seeds "rb-245" --top-k 10 --output json
  knowledge-graph-ppr.py                                     # no seeds -> standard PageRank
"""

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _fileops import read_jsonl_with_recovery  # noqa: E402
from _paths import META_DIR  # noqa: E402

_GRAPH_NAME = "knowledge-graph.jsonl"

# Pseudo-node namespaces: relation HUBS, not retrievable source records. The graph
# build emits has_category objects as "cat:<category>" and has_tag objects as
# "tag:<tag>". They carry and propagate PPR mass (connecting records that share a
# category/tag) but are filtered out of the ranked OUTPUT under --exclude-pseudo,
# leaving only the records a retriever can actually surface.
_PSEUDO_PREFIXES = ("cat:", "tag:")

# Defaults: damping 0.5 (restart probability 0.5). This is DELIBERATELY lower than
# standard PageRank's 0.85. Standard 0.85 is calibrated for GLOBAL importance (web
# link analysis) and lets mass diffuse far from the seeds -- a degree-1 query entity
# is then outranked by its more-central neighbor, defeating personalization. A 0.5
# restart keeps mass LOCAL to the query entities (the HippoRAG-family choice for
# PPR-based retrieval): the seed ranks highest and scores decay monotonically with
# hop-distance, while multi-hop neighbors still receive proportional mass. Verified
# on the fixture graphs in test_knowledge_graph_ppr.py (g-306-43). tol/max_iters bound
# the power iteration -- at ~12k nodes it converges well under the cap, and the cap
# guarantees a hard upper bound on work (event-loop safety).
_DEFAULT_DAMPING = 0.5
_DEFAULT_MAX_ITERS = 100
_DEFAULT_TOL = 1e-10


def _graph_path() -> Path:
    return Path(META_DIR) / _GRAPH_NAME


def is_pseudo_node(node: str) -> bool:
    """True for relation-hub pseudo-nodes (cat:/tag:), False for record nodes."""
    return node.startswith(_PSEUDO_PREFIXES)


def load_triples(path=None, include_retracted: bool = False):
    """Load triples from the graph JSONL.

    Returns a list of {s, p, o, ...} dicts. By default only CURRENTLY-VALID triples
    are returned (valid_to is null) -- the bi-temporal substrate (Gap 5) records a
    retracted/superseded triple by stamping valid_to, and a retrieval ranking should
    reflect the current view. Pass include_retracted=True for the full history.
    """
    p = Path(path) if path else _graph_path()
    if not p.exists():
        return []
    records = read_jsonl_with_recovery(str(p))
    triples = []
    for r in records:
        if not isinstance(r, dict):
            continue
        if r.get("s") is None or r.get("o") is None:
            continue
        if not include_retracted and r.get("valid_to") is not None:
            continue
        triples.append(r)
    return triples


def build_adjacency(triples, include_retracted: bool = False):
    """Build undirected weighted adjacency from triples.

    Returns (adjacency, out_weight) where:
      adjacency  : dict[node -> dict[neighbor -> weight]]  (symmetric)
      out_weight : dict[node -> total incident edge weight]

    Self-loops (s == o) are skipped. Repeated (s, o) pairs accumulate weight so a
    stronger / multiply-asserted connection carries proportionally more PPR mass.
    `triples` may be raw record dicts (with s/o/valid_to keys) or pre-filtered;
    include_retracted is honored when records carry valid_to.
    """
    adjacency = {}
    out_weight = {}

    def _add_edge(a, b):
        nbrs = adjacency.setdefault(a, {})
        nbrs[b] = nbrs.get(b, 0.0) + 1.0
        out_weight[a] = out_weight.get(a, 0.0) + 1.0

    for r in triples:
        s = r.get("s")
        o = r.get("o")
        if s is None or o is None or s == o:
            continue
        if not include_retracted and r.get("valid_to") is not None:
            continue
        _add_edge(s, o)
        _add_edge(o, s)

    # Guarantee every node appears in both maps (isolated nodes never arise from
    # edge construction, but a caller may inject one -- keep the maps consistent).
    for node in list(adjacency.keys()):
        out_weight.setdefault(node, 0.0)
    return adjacency, out_weight


def personalized_pagerank(
    adjacency,
    seeds,
    out_weight=None,
    damping: float = _DEFAULT_DAMPING,
    max_iters: int = _DEFAULT_MAX_ITERS,
    tol: float = _DEFAULT_TOL,
):
    """Compute Personalized PageRank over an undirected weighted graph.

    adjacency : dict[node -> dict[neighbor -> weight]]
    seeds     : iterable of query-entity node ids. Seeds absent from the graph are
                ignored. If NO seed is in the graph (empty or all-missing), the
                personalization falls back to UNIFORM -> standard PageRank (global
                importance), which is the sensible "no recognized query entity" view.
    Returns dict[node -> score]; scores are a probability distribution (sum ~= 1.0).

    Mass-conserving fixed point:
      r[v] = (1-d) * p[v]                         # restart/teleport to seeds
             + d * sum_{u~v} r[u]*w(u,v)/W(u)     # weighted neighbor flow
             + d * (dangling mass) * p[v]         # isolated-node mass back to seeds
    where p is the personalization vector (uniform over valid seeds, or uniform over
    all nodes when none), W(u) the total incident weight of u, d the damping factor.
    """
    nodes = sorted(adjacency.keys())  # sorted -> deterministic iteration + tie-break
    n = len(nodes)
    if n == 0:
        return {}
    if out_weight is None:
        out_weight = {u: sum(w.values()) for u, w in adjacency.items()}

    node_set = set(nodes)
    valid_seeds = [s for s in dict.fromkeys(seeds or []) if s in node_set]
    if valid_seeds:
        pv = 1.0 / len(valid_seeds)
        personalization = {s: pv for s in valid_seeds}
    else:
        # No recognized query entity -> uniform teleport == standard PageRank.
        pv = 1.0 / n
        personalization = {v: pv for v in nodes}

    dangling = [u for u in nodes if out_weight.get(u, 0.0) <= 0.0]

    # Start from the personalization vector (concentrated on seeds) and spread.
    rank = {v: personalization.get(v, 0.0) for v in nodes}

    for _ in range(max_iters):
        dangling_mass = 0.0
        for u in dangling:
            dangling_mass += rank[u]
        teleport_share = (1.0 - damping) + damping * dangling_mass
        new_rank = {v: teleport_share * personalization.get(v, 0.0) for v in nodes}
        for u in nodes:
            ru = rank[u]
            if ru == 0.0:
                continue
            wu = out_weight.get(u, 0.0)
            if wu <= 0.0:
                continue  # dangling mass already folded into teleport_share
            share = damping * ru / wu
            for v, w in adjacency[u].items():
                new_rank[v] += share * w
        delta = 0.0
        for v in nodes:
            delta += abs(new_rank[v] - rank[v])
        rank = new_rank
        if delta < tol:
            break
    return rank


def rank_nodes(scores, exclude_pseudo: bool = False, top_k=None):
    """Sort PPR scores descending, deterministic tie-break by node id ascending.

    exclude_pseudo : drop cat:/tag: hub nodes, returning only retrievable records.
    top_k          : keep only the top K (None = all).
    Returns a list of (node, score) tuples.
    """
    items = [
        (node, score)
        for node, score in scores.items()
        if not (exclude_pseudo and is_pseudo_node(node))
    ]
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    if top_k is not None:
        items = items[:top_k]
    return items


def compute(
    seeds,
    path=None,
    include_retracted: bool = False,
    exclude_pseudo: bool = False,
    top_k=None,
    damping: float = _DEFAULT_DAMPING,
    max_iters: int = _DEFAULT_MAX_ITERS,
    tol: float = _DEFAULT_TOL,
):
    """Convenience: load graph -> build adjacency -> PPR -> ranked list.

    Returns (ranked, meta) where ranked is [(node, score), ...] and meta carries
    node/edge/seed counts for telemetry.
    """
    # Normalize seeds to a list ONCE: compute() consumes `seeds` twice (via
    # personalized_pagerank below AND matched_seeds further down). A one-shot
    # iterable (generator expression) would be exhausted by the first pass,
    # silently zeroing matched_seeds / meta['personalized'] while the PPR ranking
    # itself stayed correct -- an inconsistency a downstream caller reading
    # meta['personalized'] would be misled by (fresh-eyes review of g-306-43).
    seeds = list(seeds or [])
    triples = load_triples(path=path, include_retracted=include_retracted)
    adjacency, out_weight = build_adjacency(triples, include_retracted=include_retracted)
    scores = personalized_pagerank(
        adjacency, seeds, out_weight=out_weight,
        damping=damping, max_iters=max_iters, tol=tol,
    )
    node_set = set(adjacency.keys())
    matched_seeds = sorted(s for s in dict.fromkeys(seeds or []) if s in node_set)
    ranked = rank_nodes(scores, exclude_pseudo=exclude_pseudo, top_k=top_k)
    meta = {
        "nodes": len(node_set),
        "edges": sum(len(v) for v in adjacency.values()) // 2,
        "seeds_requested": list(dict.fromkeys(seeds or [])),
        "seeds_matched": matched_seeds,
        "personalized": bool(matched_seeds),
    }
    return ranked, meta


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Personalized PageRank over the Mind knowledge-graph triple-store."
    )
    ap.add_argument(
        "--seeds", default="",
        help="Comma-separated query-entity node ids (e.g. 'rb-245,guard-832'). "
             "Empty -> standard PageRank (global importance).",
    )
    ap.add_argument("--top-k", type=int, default=20, help="Keep top K ranked nodes (default 20).")
    ap.add_argument(
        "--exclude-pseudo", action="store_true",
        help="Drop cat:/tag: hub nodes; rank only retrievable record nodes.",
    )
    ap.add_argument("--damping", type=float, default=_DEFAULT_DAMPING)
    ap.add_argument("--max-iters", type=int, default=_DEFAULT_MAX_ITERS)
    ap.add_argument("--tol", type=float, default=_DEFAULT_TOL)
    ap.add_argument(
        "--include-retracted", action="store_true",
        help="Include triples with a non-null valid_to (historical view).",
    )
    ap.add_argument("--path", default=None, help="Override graph path (default meta/knowledge-graph.jsonl).")
    ap.add_argument("--output", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)

    seeds = [s.strip() for s in args.seeds.split(",") if s.strip()]
    ranked, meta = compute(
        seeds,
        path=args.path,
        include_retracted=args.include_retracted,
        exclude_pseudo=args.exclude_pseudo,
        top_k=args.top_k,
        damping=args.damping,
        max_iters=args.max_iters,
        tol=args.tol,
    )

    if not meta["nodes"]:
        msg = "knowledge-graph empty or missing -- run knowledge-graph-build.py --apply first"
        if args.output == "json":
            print(json.dumps({"error": msg, "meta": meta}))
        else:
            print(msg, file=sys.stderr)
        return 1

    if args.output == "json":
        print(json.dumps({
            "meta": meta,
            "ranked": [{"node": node, "score": score} for node, score in ranked],
        }, ensure_ascii=True))
    else:
        seed_note = (
            f"seeds matched {meta['seeds_matched']}" if meta["personalized"]
            else "no seeds matched -> standard PageRank"
        )
        print(f"knowledge-graph PPR: {meta['nodes']} nodes / {meta['edges']} edges; {seed_note}")
        for rank_i, (node, score) in enumerate(ranked, start=1):
            print(f"  {rank_i:3d}. {score:.8f}  {node}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
