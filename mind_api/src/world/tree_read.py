"""GET /v1/tree/read — parity with `tree.py read --<flag>` for the SIMPLE
lookups (no cross-file computation).

Query params (mutually exclusive — exactly one):
    node=<key>            full node JSON with defaults applied
    path=<key>            file path string for a node
    ancestors=<key>       parent chain array (node -> root)
    children=<key>        immediate children JSON
    leaves=1              all leaf nodes
    leaves_under=<key>    leaf descendants of a subtree
    stats=1               node counts by depth
    child_path=<parent>,<slug>     compute path for new child
    summary=1             compact tree overview
    maintenance=1         top-level maintenance cadence block

NOT served on the daemon path (fall through to fallback python):
    --validate
    --decompose-candidates
    --redistribute-candidates
    --distill-candidates
    --active-content       (reads node .md files; subprocess-bound)
    --find                 (use /v1/tree/find-node instead)

The deferred flags either read node .md files heavily (multiplied by
the tree size — would dominate the daemon's request loop) or run
cross-record validation that the daemon doesn't need yet. They keep
the same fallback semantics as the pre-migration CLI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..yaml_cache import cache
from ..endpoints._jsonl_common import flag, missing_flag_error, json_response_pretty


# Reach into core/scripts for the pure-function tree helpers.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "core" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from tree import (  # noqa: E402
    apply_defaults,
    get_node,
    walk_ancestors,
    get_children,
    get_all_leaves,
    get_leaves_under,
    compute_stats,
    compute_child_path,
)


def _tree_path(ctx):
    return ctx.paths.world / "knowledge" / "tree" / "_tree.yaml"


def _load_tree(ctx):
    tree = cache().get(_tree_path(ctx))
    if tree is None or not isinstance(tree, dict) or "nodes" not in tree:
        return None
    return tree


def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    tree = _load_tree(ctx)
    if tree is None:
        return Response.error(404, "tree_not_found", "Tree file not found or invalid")

    key = q.get("node")
    if key:
        try:
            node = get_node(tree, key)
        except SystemExit:
            return Response.error(404, "node_not_found", f"Node {key} not found")
        out = apply_defaults(node)
        out["key"] = key
        return json_response_pretty(out)

    key = q.get("path")
    if key:
        try:
            node = get_node(tree, key)
        except SystemExit:
            return Response.error(404, "node_not_found", f"Node {key} not found")
        return Response.text(node.get("file", ""), content_type="text/plain")

    key = q.get("ancestors")
    if key:
        try:
            chain = walk_ancestors(tree, key)
        except SystemExit:
            return Response.error(404, "node_not_found", f"Node {key} not found")
        return json_response_pretty(chain)

    key = q.get("children")
    if key:
        try:
            result = get_children(tree, key)
        except SystemExit:
            return Response.error(404, "node_not_found", f"Node {key} not found")
        return json_response_pretty(result)

    if flag(q, "leaves"):
        return json_response_pretty(get_all_leaves(tree))

    key = q.get("leaves_under")
    if key:
        try:
            result = get_leaves_under(tree, key)
        except SystemExit:
            return Response.error(404, "node_not_found", f"Node {key} not found")
        return json_response_pretty(result)

    if flag(q, "stats"):
        return json_response_pretty(compute_stats(tree))

    # child_path takes two args, comma-separated.
    cp = q.get("child_path")
    if cp:
        parts = cp.split(",", 1)
        if len(parts) != 2:
            return Response.error(400, "invalid_param",
                                  "child_path requires '<parent>,<slug>'")
        parent_key, slug = parts
        try:
            parent_node = get_node(tree, parent_key)
        except SystemExit:
            return Response.error(404, "node_not_found",
                                  f"Parent {parent_key} not found")
        return Response.text(
            compute_child_path(parent_node.get("file", ""), slug),
            content_type="text/plain",
        )

    if flag(q, "summary"):
        nodes = tree.get("nodes", {})
        compact = {}
        for key, node in nodes.items():
            compact[key] = {
                "file": node.get("file", ""),
                "summary": node.get("summary", ""),
                "depth": node.get("depth", 0),
                "capability_level": node.get("capability_level", ""),
                "confidence": node.get("confidence"),
                # last_updated + article_count: consumed by strategic-scan S2
                # (knowledge-frontier staleness/thin detection). MIRROR of
                # core/scripts/tree.py --summary projection — keep in sync.
                # .
                "last_updated": node.get("last_updated"),
                "article_count": node.get("article_count", 0),
                "children": node.get("children", []),
            }
        return json_response_pretty({"nodes": compact, "total": len(compact)})

    if flag(q, "maintenance"):
        return json_response_pretty(tree.get("maintenance") or {})

    return missing_flag_error([
        "node", "path", "ancestors", "children", "leaves", "leaves_under",
        "stats", "child_path", "summary", "maintenance",
    ])


def register(routes) -> None:
    routes[("GET", "/v1/tree/read")] = read
