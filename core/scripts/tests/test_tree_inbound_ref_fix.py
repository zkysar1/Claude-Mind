"""test_tree_inbound_ref_fix.py — regression test for the post-reparent inbound
cross-reference repair tool (tree-inbound-ref-fix.py, g-115-1830).

Background: `tree-update.sh --reparent` updates `_tree.yaml` and physically
moves a node's .md, reporting `file_moves` (old->new). But it NEVER rewrites the
inbound prose cross-references in OTHER nodes' bodies that hardcode the moved
node's OLD path. Left unrepaired those refs dangle, surfacing only later as a
validate warning (g-115-1419) — the silent-breakage gap that made the g-115-398
regroup break 8 inbound refs across 7 nodes (all fixed by hand).

The tool consumes a reparent's `file_moves` and rewrites each inbound ref
old_path -> new_path, backtick-scoped and form-preserving. It reuses
`tree._iter_body_md_refs` (the same iterator that drives validate's g-115-1419
detection), so detection and repair can never drift apart.

This test exercises:
  1. build_moved_map normalizes the world/knowledge/tree/ prefix and drops
     no-op moves.
  2. find_inbound_refs surfaces refs to a moved node (both L1-first and
     world/knowledge/tree/-prefixed forms), preserving the author's ref form,
     and IGNORES a sibling ref that was NOT moved.
  3. apply_fix rewrites only the backtick-delimited refs (a bare-prose mention
     of the same path is left untouched) and reports the fixed count.
  4. Re-running find after apply returns zero (idempotent — the old refs are
     gone, so a second reparent-repair pass is a no-op).
  5. Empty/degenerate moves are a clean no-op.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# conftest.py already inserts core/scripts on sys.path; add it defensively so
# the module imports cleanly when run in isolation too.
SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import _paths  # noqa: E402
import _fileops  # noqa: E402
import tree  # noqa: E402

# tree-inbound-ref-fix.py has a hyphenated name — load it via importlib.
_spec = importlib.util.spec_from_file_location(
    "tree_inbound_ref_fix", str(CORE_SCRIPTS / "tree-inbound-ref-fix.py"))
tirf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tirf)

# The reparent that moved `beta` out from directly under fiction into a new
# `methods` intermediate category (the exact shape of the  regroup).
MOVES = [{
    "key": "beta",
    "old": "world/knowledge/tree/fiction/beta.md",
    "new": "world/knowledge/tree/fiction/methods/beta.md",
}]


def _seed(tree_root: Path) -> None:
    """Write the POST-move tree to disk (beta already at its new path — the tool
    runs AFTER file_moves are physically applied). Inbound refs in alpha/delta
    still point at beta's OLD path (that is what the tool repairs)."""
    (tree_root / "fiction" / "methods").mkdir(parents=True, exist_ok=True)
    (tree_root / "fiction.md").write_text(
        "# Fiction (L1)\nChildren: alpha, gamma, delta, methods.\n", encoding="utf-8")
    # alpha: L1-first ref to the moved node + a sibling ref that did NOT move +
    # a bare-prose mention of the moved path (must survive untouched).
    (tree_root / "fiction" / "alpha.md").write_text(
        "# Alpha\n"
        "Depends on `fiction/beta.md` for methodology.\n"
        "Also see `fiction/gamma.md` (sibling, not moved).\n"
        "Prose mention of fiction/beta.md without backticks must stay untouched.\n",
        encoding="utf-8")
    (tree_root / "fiction" / "gamma.md").write_text(
        "# Gamma\nNo outbound refs.\n", encoding="utf-8")
    # delta: the SAME moved node referenced in the world/knowledge/tree/-prefixed
    # form (form preservation must keep the prefix on rewrite).
    (tree_root / "fiction" / "delta.md").write_text(
        "# Delta\nCross-refs `world/knowledge/tree/fiction/beta.md` in prefixed form.\n",
        encoding="utf-8")
    (tree_root / "fiction" / "methods.md").write_text(
        "# Methods (intermediate)\nRegroup category node.\n", encoding="utf-8")
    (tree_root / "fiction" / "methods" / "beta.md").write_text(
        "# Beta\nMoved here by the regroup.\n", encoding="utf-8")


def _tree_dict() -> dict:
    return {"nodes": {
        "root": {"file": None, "depth": 0, "parent": None,
                 "children": ["fiction"], "child_count": 1, "summary": "root"},
        "fiction": {"file": "world/knowledge/tree/fiction.md", "depth": 1,
                    "parent": "root", "children": ["alpha", "gamma", "delta", "methods"],
                    "child_count": 4, "summary": "fiction"},
        "alpha": {"file": "world/knowledge/tree/fiction/alpha.md", "depth": 2,
                  "parent": "fiction", "children": [], "child_count": 0, "summary": "alpha"},
        "gamma": {"file": "world/knowledge/tree/fiction/gamma.md", "depth": 2,
                  "parent": "fiction", "children": [], "child_count": 0, "summary": "gamma"},
        "delta": {"file": "world/knowledge/tree/fiction/delta.md", "depth": 2,
                  "parent": "fiction", "children": [], "child_count": 0, "summary": "delta"},
        "methods": {"file": "world/knowledge/tree/fiction/methods.md", "depth": 2,
                    "parent": "fiction", "children": ["beta"], "child_count": 1,
                    "summary": "methods"},
        # beta already at its NEW path (post-move).
        "beta": {"file": "world/knowledge/tree/fiction/methods/beta.md", "depth": 3,
                 "parent": "methods", "children": [], "child_count": 0, "summary": "beta"},
    }}


def _patch_world(tmp_path, monkeypatch):
    """Redirect both WORLD_DIR globals to the temp tree (resolve_file_path reads
    _paths.WORLD_DIR at call time; _iter_body_md_refs reads tree.WORLD_DIR)."""
    monkeypatch.setattr(_paths, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(tree, "WORLD_DIR", tmp_path)


def test_build_moved_map_normalizes_and_filters():
    # prefix stripped on both sides; the no-op move (same old/new) is dropped.
    moved = tirf.build_moved_map(MOVES + [
        {"key": "noop", "old": "world/knowledge/tree/x/y.md",
         "new": "world/knowledge/tree/x/y.md"},
        {"key": "junk", "old": "", "new": "world/knowledge/tree/z.md"},
        "not-a-dict",
    ])
    assert moved == {"fiction/beta.md": "fiction/methods/beta.md"}


def test_find_surfaces_moved_inbound_refs(tmp_path, monkeypatch):
    _seed(tmp_path / "knowledge" / "tree")
    _patch_world(tmp_path, monkeypatch)

    moved = tirf.build_moved_map(MOVES)
    refs = tirf.find_inbound_refs(moved, nodes=_tree_dict()["nodes"])

    by_node = {r["node"]: r for r in refs}
    # exactly the two referencing nodes (alpha L1-first, delta prefixed) — NOT
    # gamma (its ref did not move), NOT beta/methods/fiction (no moved refs).
    assert set(by_node) == {"alpha", "delta"}, "unexpected referencing set: " + repr(sorted(by_node))

    # alpha: L1-first form preserved
    assert by_node["alpha"]["old_ref"] == "fiction/beta.md"
    assert by_node["alpha"]["new_ref"] == "fiction/methods/beta.md"

    # delta: world/knowledge/tree/-prefixed form preserved on rewrite
    assert by_node["delta"]["old_ref"] == "world/knowledge/tree/fiction/beta.md"
    assert by_node["delta"]["new_ref"] == "world/knowledge/tree/fiction/methods/beta.md"

    # the un-moved sibling ref (fiction/gamma.md) is never surfaced
    assert all(r["old_ref"] != "fiction/gamma.md" for r in refs)


def test_apply_rewrites_only_backtick_refs(tmp_path, monkeypatch):
    _seed(tmp_path / "knowledge" / "tree")
    _patch_world(tmp_path, monkeypatch)
    # Isolate the ref-rewrite from the history/changelog machinery (separately
    # tested): no base_dir -> save_history/append_changelog are skipped, only the
    # atomic body write runs.
    monkeypatch.setattr(_fileops, "resolve_base_dir", lambda p: None)
    monkeypatch.setattr(_fileops, "_agent_name", lambda: "test")

    moved = tirf.build_moved_map(MOVES)
    refs = tirf.find_inbound_refs(moved, nodes=_tree_dict()["nodes"])
    fixed = tirf.apply_fix(refs)
    assert len(fixed) == 2, "both alpha and delta refs should be rewritten"

    alpha_body = (tmp_path / "knowledge" / "tree" / "fiction" / "alpha.md").read_text(encoding="utf-8")
    # backtick-delimited ref rewritten to the new path
    assert "`fiction/methods/beta.md`" in alpha_body
    assert "`fiction/beta.md`" not in alpha_body
    # the non-moved sibling ref is untouched
    assert "`fiction/gamma.md`" in alpha_body
    # the BARE-PROSE mention of the old path is NOT rewritten (backtick-scoped)
    assert "Prose mention of fiction/beta.md without backticks must stay untouched." in alpha_body

    delta_body = (tmp_path / "knowledge" / "tree" / "fiction" / "delta.md").read_text(encoding="utf-8")
    assert "`world/knowledge/tree/fiction/methods/beta.md`" in delta_body
    assert "`world/knowledge/tree/fiction/beta.md`" not in delta_body


def test_apply_is_idempotent(tmp_path, monkeypatch):
    _seed(tmp_path / "knowledge" / "tree")
    _patch_world(tmp_path, monkeypatch)
    monkeypatch.setattr(_fileops, "resolve_base_dir", lambda p: None)
    monkeypatch.setattr(_fileops, "_agent_name", lambda: "test")

    moved = tirf.build_moved_map(MOVES)
    tirf.apply_fix(tirf.find_inbound_refs(moved, nodes=_tree_dict()["nodes"]))
    # second pass: the old refs are gone, so nothing matches the (old-path) moves
    refs2 = tirf.find_inbound_refs(moved, nodes=_tree_dict()["nodes"])
    assert refs2 == [], "second repair pass must be a no-op: " + repr(refs2)


def test_empty_moves_is_noop(tmp_path, monkeypatch):
    _seed(tmp_path / "knowledge" / "tree")
    _patch_world(tmp_path, monkeypatch)
    assert tirf.build_moved_map([]) == {}
    assert tirf.find_inbound_refs({}, nodes=_tree_dict()["nodes"]) == []


def test_apply_chain_moves_no_corruption(tmp_path, monkeypatch):
    """Finding 1 regression (5): two moves forming a CHAIN — move A's
    NEW path equals move B's OLD path — must not corrupt. A body citing BOTH old
    paths must map each to ITS OWN target, never double-rewrite through the chain.
    The retired per-ref sequential str.replace produced `gamma` for BOTH refs:
    after alpha->beta inserted a second `beta`, the beta->gamma pass hit it too."""
    tree_root = tmp_path / "knowledge" / "tree"
    (tree_root / "fiction").mkdir(parents=True, exist_ok=True)
    _patch_world(tmp_path, monkeypatch)
    # isolate the rewrite from the history/changelog machinery (separately tested)
    monkeypatch.setattr(_fileops, "resolve_base_dir", lambda p: None)
    monkeypatch.setattr(_fileops, "_agent_name", lambda: "test")

    # cite references BOTH alpha (moves -> beta) AND beta (moves -> gamma).
    (tree_root / "fiction" / "cite.md").write_text(
        "# Cite\nSee `fiction/alpha.md` and also `fiction/beta.md` here.\n",
        encoding="utf-8")

    nodes = {"cite": {"file": "world/knowledge/tree/fiction/cite.md", "depth": 2,
                      "parent": "fiction", "children": [], "child_count": 0,
                      "summary": "cite"}}
    moves = [
        {"key": "alpha", "old": "world/knowledge/tree/fiction/alpha.md",
         "new": "world/knowledge/tree/fiction/beta.md"},
        {"key": "beta", "old": "world/knowledge/tree/fiction/beta.md",
         "new": "world/knowledge/tree/fiction/gamma.md"},
    ]
    moved = tirf.build_moved_map(moves)
    assert moved == {"fiction/alpha.md": "fiction/beta.md",
                     "fiction/beta.md": "fiction/gamma.md"}
    refs = tirf.find_inbound_refs(moved, nodes=nodes)
    fixed = tirf.apply_fix(refs)
    assert len(fixed) == 2, "both chained refs should be rewritten: " + repr(fixed)

    body = (tree_root / "fiction" / "cite.md").read_text(encoding="utf-8")
    # each old ref maps to ITS OWN target — NO chain corruption:
    assert "`fiction/beta.md`" in body, body     # alpha -> beta (NOT gamma)
    assert "`fiction/gamma.md`" in body, body    # beta -> gamma
    assert "`fiction/alpha.md`" not in body, body
