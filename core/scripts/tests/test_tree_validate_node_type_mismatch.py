"""test_tree_validate_node_type_mismatch.py -- regression test for the
present-but-wrong node_type detective check added to validate_tree
(g-115-1486, 2026-06-16).

Background: each tree node carries a node_type field whose canonical value is
'interior' (has children) or 'leaf' (no children). apply_defaults DERIVES
node_type when ABSENT but does NOT mask a PRESENT-but-wrong value on reads, so
legacy 'branch'/'fact' values (24 normalized 2026-06-16) or any future
create-path asymmetry would otherwise sit undetected on every read. validate_tree
is the right surface for the present-but-wrong case -- symmetric to the
long-standing child_count mismatch ERROR (both assert a stored field matches the
actual node structure), and an ERROR (not a warning) to match that sibling.

This test exercises:
  1. A present-but-wrong node_type (1 child, labelled 'leaf')      -> ERROR.
  2. A legacy value ('branch', never canonical)                    -> ERROR.
  3. A childless node mislabelled 'interior'                       -> ERROR.
  4. Correct node_type (interior-with-children, leaf-without)      -> no error.
  5. ABSENT node_type                                              -> no ERROR
     (the missing-field path is a WARNING; the present-but-wrong check skips None).
"""

from __future__ import annotations

import sys
from pathlib import Path

# conftest.py already inserts core/scripts on sys.path; add it defensively so
# the module imports cleanly when run in isolation too.
SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import _paths  # noqa: E402
import tree  # noqa: E402


def _seed_files(tree_root: Path) -> None:
    """Write the .md files the structural fixture references, so validate_tree's
    physical-file + dangling-xref checks stay clean (bodies carry no backtick
    refs). Isolates the node_type assertion from file-existence noise."""
    (tree_root / "fiction").mkdir(parents=True, exist_ok=True)
    (tree_root / "fiction.md").write_text("# Fiction (L1)\n", encoding="utf-8")
    (tree_root / "fiction" / "leaf1.md").write_text("# Leaf1\n", encoding="utf-8")


def _tree_dict(fiction_node_type):
    """root -> fiction(L1, 1 child) -> leaf1 (no children). Structurally
    consistent (child_count/depth/parent/children all correct) so the ONLY
    error a test can provoke is the node_type one under test. fiction's
    node_type is the variable."""
    return {"nodes": {
        "root": {"file": None, "depth": 0, "parent": None,
                 "children": ["fiction"], "child_count": 1,
                 "node_type": "interior", "summary": "root"},
        "fiction": {"file": "world/knowledge/tree/fiction.md", "depth": 1,
                    "parent": "root", "children": ["leaf1"], "child_count": 1,
                    "node_type": fiction_node_type, "summary": "fiction"},
        "leaf1": {"file": "world/knowledge/tree/fiction/leaf1.md", "depth": 2,
                  "parent": "fiction", "children": [], "child_count": 0,
                  "node_type": "leaf", "summary": "leaf1"},
    }}


def _patch(monkeypatch, tmp_path):
    # resolve_file_path reads _paths.WORLD_DIR; validate_tree's own file/xref
    # blocks read tree.WORLD_DIR. Patch both; monkeypatch auto-reverts.
    monkeypatch.setattr(_paths, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(tree, "WORLD_DIR", tmp_path)


def _node_type_errors(result):
    # The check's message is the only one containing "node_type=" (child_count's
    # is "child_count="), so this cleanly isolates the node_type errors.
    return [e for e in result["errors"] if "node_type=" in e]


def test_present_but_wrong_node_type_is_error(tmp_path, monkeypatch):
    _seed_files(tmp_path / "knowledge" / "tree")
    _patch(monkeypatch, tmp_path)
    # fiction has 1 child but is labelled 'leaf' -> wrong (expected 'interior')
    result = tree.validate_tree(_tree_dict("leaf"))
    errs = _node_type_errors(result)
    assert len(errs) == 1, "expected one node_type error, got: " + repr(result["errors"])
    assert "fiction" in errs[0] and "leaf" in errs[0] and "interior" in errs[0], errs[0]
    assert result["valid"] is False, "errors: " + repr(result["errors"])


def test_legacy_branch_value_is_error(tmp_path, monkeypatch):
    _seed_files(tmp_path / "knowledge" / "tree")
    _patch(monkeypatch, tmp_path)
    # legacy 'branch' never equals canonical interior/leaf -> error (the exact
    # class normalized 2026-06-16: 22 'branch'/'fact' nodes with children).
    result = tree.validate_tree(_tree_dict("branch"))
    errs = _node_type_errors(result)
    assert len(errs) == 1, repr(result["errors"])
    assert "branch" in errs[0] and "interior" in errs[0], errs[0]


def test_childless_node_mislabelled_interior_is_error(tmp_path, monkeypatch):
    _seed_files(tmp_path / "knowledge" / "tree")
    _patch(monkeypatch, tmp_path)
    # leaf1 has 0 children but mislabel it 'interior' -> wrong (expected 'leaf').
    d = _tree_dict("interior")  # fiction correct
    d["nodes"]["leaf1"]["node_type"] = "interior"
    result = tree.validate_tree(d)
    errs = _node_type_errors(result)
    assert len(errs) == 1, repr(result["errors"])
    assert "leaf1" in errs[0] and "leaf" in errs[0], errs[0]


def test_correct_node_type_no_error(tmp_path, monkeypatch):
    _seed_files(tmp_path / "knowledge" / "tree")
    _patch(monkeypatch, tmp_path)
    # fiction correctly labelled 'interior' (has a child); all nodes consistent.
    result = tree.validate_tree(_tree_dict("interior"))
    assert _node_type_errors(result) == [], (
        "correct node_type must not error: " + repr(result["errors"]))


def test_absent_node_type_is_not_error(tmp_path, monkeypatch):
    _seed_files(tmp_path / "knowledge" / "tree")
    _patch(monkeypatch, tmp_path)
    d = _tree_dict("interior")
    del d["nodes"]["fiction"]["node_type"]  # absent -> present-but-wrong check skips None
    result = tree.validate_tree(d)
    assert _node_type_errors(result) == [], (
        "absent node_type must not be an ERROR (missing-field is a warning): "
        + repr(result["errors"]))
