"""GET /v1/tree/find-node — pre-migration `tree.py read --find` parity.

Query parameters:
    text=<query>       required, free-text search
    top=N              max results (default: 3)
    leaf_only=1        restrict to leaf nodes only

Response is application/json: a list of dicts shaped like
    {key, score, file, depth, summary, node_type}

Equivalence target: stdout of
    python3 core/scripts/tree.py read --find "<query>" [--top N] [--leaf-only]
parsed as JSON.

What this endpoint does NOT do (Phase B scope-cut):
  - Update retrieval counters. `find_nodes` itself doesn't write them
    (that's `retrieve.py`'s job). Parity with cmd_find on this point.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from ..yaml_cache import cache


# Make core/scripts/ importable so we can call tree_match.find_nodes
# directly. NOT path-stable on its own: tree_match's node-body resolver falls
# back to _paths.WORLD_DIR, which this process bound ONCE at import — None if
# the daemon started before any world existed, or agent A's world while
# serving agent B. Every call below therefore passes ctx.paths.world
# explicitly ().
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "core" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Imported once at module load. Safe across per-request agents ONLY because
# the world root is supplied per call (see above).
from tree_match import build_concept_index, _match_nodes, _score_and_limit  # noqa: E402


# Concept-index cache. build_concept_index reads each node's .md file from
# disk (cloud-synced filesystem), costing ~250-5000ms per call. find_nodes
# rebuilds it on EVERY invocation — but the index is fully determined by
# the nodes dict identity. Cache by id(nodes); a yaml_cache reload returns
# a different dict object, naturally invalidating this cache.
#
# CRITICAL: do not key by tree_path. Two daemon threads serving different
# agents that happen to point at the same path would race. id(nodes) keys
# by the actual loaded object — yaml_cache returns the same object for
# concurrent readers of the same unchanged file.
#
# Each entry is (idx, anchor) where `anchor` is a strong reference to the
# nodes dict that produced this idx. Holding the dict strongly is LOAD-BEARING:
# without it, after yaml_cache invalidates an entry, the old dict becomes
# GC-eligible. CPython's small-object pool reuses freed addresses, so a new
# dict can land at the same memory address — at which point id(new_nodes)
# would collide with our cache key and return the STALE idx. Do not remove
# the anchor. Same fix shipped in endpoints/retrieve.py.
_concept_cache: dict = {}
_concept_cache_lock = threading.Lock()
_CONCEPT_CACHE_MAX = 8  # bounded; expect ~1 entry per agent's tree


def _cached_concept_index(nodes: dict, world_root) -> dict:
    # The index is determined by the nodes dict AND the world the bodies are
    # read from; key on both so a stale root can never serve a fresh request.
    cache_key = (id(nodes), str(world_root))
    with _concept_cache_lock:
        entry = _concept_cache.get(cache_key)
    if entry is not None:
        return entry[0]
    # Build outside the lock — file I/O can be slow on cold caches.
    idx = build_concept_index(nodes, world_root=world_root)
    with _concept_cache_lock:
        _concept_cache[cache_key] = (idx, nodes)  # anchor pins the dict
        if len(_concept_cache) > _CONCEPT_CACHE_MAX:
            _concept_cache.pop(next(iter(_concept_cache)))
    return idx


def find(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    text = (q.get("text") or "").strip()
    if not text:
        return Response.error(400, "missing_param", "query parameter 'text' is required")

    try:
        top = int(q.get("top", "3"))
    except ValueError:
        return Response.error(400, "invalid_param", "top must be an integer")

    leaf_only = _flag(q, "leaf_only")

    tree_path = ctx.paths.world / "knowledge" / "tree" / "_tree.yaml"
    tree = cache().get(tree_path)
    if tree is None:
        return Response.error(404, "tree_not_found",
                              f"_tree.yaml not found at {tree_path}")
    if not isinstance(tree, dict) or "nodes" not in tree:
        return Response.error(500, "invalid_tree", "missing 'nodes' key")

    full_nodes = tree["nodes"]
    entity_index = tree.get("entity_index", {}) or {}

    # Concept index is keyed by FULL-nodes identity — leaf_only filtering
    # below relies on `_match_nodes`'s `if key in nodes` check to exclude
    # interior nodes from matches (semantically equivalent to building the
    # index on filtered nodes; see CLI's tree_match.find_nodes for the
    # canonical shape).
    concept_index = _cached_concept_index(full_nodes, ctx.paths.world)

    if leaf_only:
        nodes = {k: v for k, v in full_nodes.items() if not v.get("children")}
    else:
        nodes = full_nodes

    matched, _matched_keys, channels = _match_nodes(text, nodes, entity_index, concept_index)
    scored = _score_and_limit(matched, channels, top, query_text=text, all_nodes=nodes)

    # Mirror find_nodes' output shape verbatim.
    result = []
    for key, node, match_score, _channel in scored:
        children = node.get("children", [])
        result.append({
            "key": key,
            "score": round(match_score, 2),
            "file": node.get("file", ""),
            "depth": node.get("depth", 0),
            "summary": node.get("summary", ""),
            "node_type": node.get("node_type", "leaf" if not children else "interior"),
        })

    return Response.text(
        json.dumps(result, indent=2, ensure_ascii=False),
        content_type="application/json",
    )


def _flag(q, name: str) -> bool:
    v = q.get(name)
    if v is None:
        return False
    return v.lower() not in ("", "0", "false", "no")


def register(routes) -> None:
    routes[("GET", "/v1/tree/find-node")] = find
