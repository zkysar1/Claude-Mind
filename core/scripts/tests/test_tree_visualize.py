"""Unit tests for tree-visualize.py core logic.

Pins the non-trivial data-shaping: full-path -> bare-leaf key mapping (the
graph keys nodes by full path while _tree.yaml keys by bare leaf -- the bug
that silently dropped 668/705 reference subjects), shared-entity backlink
derivation, and orphan-target filtering.
"""
import importlib.util
import json
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "tree_visualize", CORE_SCRIPTS / "tree-visualize.py"
)
tv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tv)


def test_load_tree_references_extracts_leaf_key(tmp_path):
    """Full-path 'node:l1/.../leaf' subjects map to the bare leaf key."""
    graph = tmp_path / "knowledge-graph.jsonl"
    rows = [
        {"s": "node:intelligence/core/server-memory", "p": "references", "o": "rb-1", "store": "tree"},
        {"s": "node:intelligence/core/server-memory", "p": "references", "o": "g-2", "store": "tree"},
        {"s": "node:system/hooks/hook-platform", "p": "references", "o": "rb-1", "store": "tree"},
        # non-tree store + non-references predicate must be ignored
        {"s": "node:x/y", "p": "has_tag", "o": "tag:z", "store": "tree"},
        {"s": "rb-9", "p": "references", "o": "rb-1", "store": "reasoning-bank"},
    ]
    graph.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    n2e, e2n = tv.load_tree_references(graph)
    assert set(n2e.keys()) == {"server-memory", "hook-platform"}
    assert n2e["server-memory"] == {"rb-1", "g-2"}
    # reversed edge: rb-1 is cited by both leaves
    assert e2n["rb-1"] == {"server-memory", "hook-platform"}


def test_compute_backlinks_ranks_by_shared_entity_count():
    n2e = {
        "a": {"rb-1", "g-2", "g-3"},
        "b": {"rb-1", "g-2"},      # shares 2 with a
        "c": {"rb-1"},             # shares 1 with a and b
    }
    e2n = {}
    for node, ents in n2e.items():
        for e in ents:
            e2n.setdefault(e, set()).add(node)
    bl = tv.compute_backlinks(n2e, e2n, max_coref=12)
    # a's strongest co-reference is b (2 shared), then c (1 shared)
    assert bl["a"][0] == ["b", 2]
    assert ["c", 1] in bl["a"]
    # self never appears
    assert all(other != "a" for other, _ in bl["a"])


def test_build_payload_filters_orphan_backlinks_and_sets_structure():
    nodes = {
        "root": {"parent": None, "children": ["l1"], "summary": "root"},
        "l1": {"parent": "root", "children": ["leaf"], "summary": "domain"},
        "leaf": {"parent": "l1", "children": [], "summary": "a leaf", "capability_level": "EXPLOIT"},
    }
    n2e = {"leaf": {"rb-1"}}
    # backlinks reference a stale node "ghost" not present in nodes -> must drop
    backlinks = {"leaf": [["ghost", 5], ["l1", 1]]}
    payload = tv.build_payload(nodes, n2e, backlinks, max_coref=12)
    assert payload["roots"] == ["root"]
    assert payload["stats"]["node_count"] == 3
    assert payload["stats"]["hierarchy_edges"] == 2
    leaf = payload["nodes"]["leaf"]
    assert leaf["l1"] == "l1"                       # walked up to top-level under root
    assert leaf["refs"] == ["rb-1"]
    assert leaf["backlinks"] == [["l1", 1]]         # ghost filtered out


def test_render_html_is_self_contained():
    payload = {"nodes": {"root": {"parent": None, "children": []}}, "roots": ["root"],
               "stats": {"node_count": 1, "hierarchy_edges": 0,
                         "nodes_with_refs": 0, "nodes_with_backlinks": 0, "max_coref": 12}}
    html = tv.render_html(payload)
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    # no network / external dependency
    for bad in ('src="http', 'href="http', "fetch(", "XMLHttpRequest"):
        assert bad not in html, f"self-contained violation: {bad}"
    assert "const DATA =" in html
