"""Tests for tree-adjudication-scan.py ().

Verifies the earned-low calibration-adjudication marker scan: a node whose .md
body carries a `## Confidence Rationale` heading is reported (so g-115-400's
under-encoded sweep can exclude it); a plain node is not.
"""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "tree_adjudication_scan",
    Path(__file__).resolve().parents[1] / "tree-adjudication-scan.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _node(dir_, name, body):
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / f"{name}.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_finds_marked_nodes_front_matter_key_and_stem_fallback(tmp_path):
    tree = tmp_path / "knowledge" / "tree"
    sub = tree / "intelligence"
    # marked, key from front matter
    _node(sub, "adjudicated-a",
          "---\nkey: adjudicated-a\n---\n# A\n\n## Confidence Rationale: 0.3 Is Earned\nbody\n")
    # plain, no marker -> excluded
    _node(sub, "plain-b", "---\nkey: plain-b\n---\n# B\n\njust content, no marker\n")
    # marked, no `key:` front matter -> stem fallback; extra-spaces heading
    _node(tree, "adjudicated-c-stemkey",
          "---\ntopic: C\n---\n# C\n\n##   Confidence Rationale\nextra spaces tolerated\n")
    rows = mod.scan(tree_root=tree)
    assert sorted(r["key"] for r in rows) == ["adjudicated-a", "adjudicated-c-stemkey"]


def test_marker_must_be_a_heading_not_inline_mention(tmp_path):
    tree = tmp_path / "knowledge" / "tree"
    # an inline mention of the phrase (not a level-2 heading) must NOT match
    _node(tree, "mentions-only",
          "# X\n\nThis node discusses the Confidence Rationale convention but is not adjudicated.\n")
    assert mod.scan(tree_root=tree) == []


def test_no_marker_returns_empty(tmp_path):
    tree = tmp_path / "knowledge" / "tree"
    _node(tree, "x", "# X\n\nno marker here\n")
    assert mod.scan(tree_root=tree) == []


def test_missing_tree_root_returns_empty(tmp_path):
    assert mod.scan(tree_root=tmp_path / "does-not-exist") == []
