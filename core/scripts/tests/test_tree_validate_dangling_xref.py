"""test_tree_validate_dangling_xref.py — regression test for the node-body
dangling cross-reference check added to validate_tree (g-115-1419, 2026-06-13).

Background: validate_tree's physical-file check validates each node's OWN
`file` field, but historically NEVER read node .md BODIES. When a node was
relocated, every OTHER node whose body referenced the old path was left with
a dangling inbound ref pointing at a path no longer on disk. That class of
bug surfaced only via the 96h recurring tree-correctness goal (g-115-399 +
g-115-1417: one relocation left 6 dangling inbound refs across 5 files) — never
via `tree validate`, because no check read node bodies.

The new block scans each node body for backtick-quoted tree-node references
(paths ending in .md whose first path segment is a live L1 tree directory) and
warns when the target does not exist on disk. rb-8: treat every cross-reference
as a positive-state assertion; probe target existence before trusting.

This test exercises:
  1. A dangling body ref (target missing on disk) → exactly one warning.
  2. Valid body refs (targets present) → no warning.
  3. Out-of-scope refs (non-.md, and world/ files outside the tree) → ignored,
     so the check stays low-false-positive.
  4. Structural validity is unaffected (warnings only, no new errors).
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


def _seed(tree_root: Path) -> None:
    """Write a small, structurally-consistent tree to disk under tree_root.

    Layout:  root -> fiction(L1) -> {alpha, beta}
    Body refs (synthetic, domain-free):
      - alpha body  -> `fiction/beta.md`   (exists)            -> no warn
      - beta  body  -> `fiction/ghost.md`  (MISSING, relocated)-> WARN
                       `fiction/alpha.md`  (exists)            -> no warn
                       `core/scripts/none.py`  (non-.md / non-tree) -> ignored
                       `world/conventions/ghost-conv.md` (world non-tree) -> ignored
    """
    (tree_root / "fiction").mkdir(parents=True, exist_ok=True)
    (tree_root / "fiction.md").write_text(
        "# Fiction (L1)\nSee `fiction/alpha.md`.\n", encoding="utf-8")
    (tree_root / "fiction" / "alpha.md").write_text(
        "# Alpha\nSee `fiction/beta.md` for details.\n", encoding="utf-8")
    (tree_root / "fiction" / "beta.md").write_text(
        "# Beta\n"
        "Relocated from `fiction/ghost.md` (now missing).\n"
        "See also `fiction/alpha.md`.\n"
        "Out-of-scope: `core/scripts/none.py` and "
        "`world/conventions/ghost-conv.md` must be ignored.\n"
        "Illustrative path `fiction/sub/.../buried.md` is a prose placeholder "
        "(ellipsis) and must be ignored.\n",
        encoding="utf-8")
    # ghost.md is intentionally NOT written — it is the dangling target.


def _tree_dict() -> dict:
    return {"nodes": {
        "root": {"file": None, "depth": 0, "parent": None,
                 "children": ["fiction"], "child_count": 1, "summary": "root"},
        "fiction": {"file": "world/knowledge/tree/fiction.md", "depth": 1,
                    "parent": "root", "children": ["alpha", "beta"],
                    "child_count": 2, "summary": "fiction"},
        "alpha": {"file": "world/knowledge/tree/fiction/alpha.md", "depth": 2,
                  "parent": "fiction", "children": [], "child_count": 0,
                  "summary": "alpha"},
        "beta": {"file": "world/knowledge/tree/fiction/beta.md", "depth": 2,
                 "parent": "fiction", "children": [], "child_count": 0,
                 "summary": "beta"},
    }}


def test_validate_detects_dangling_body_xref(tmp_path, monkeypatch):
    tree_root = tmp_path / "knowledge" / "tree"
    _seed(tree_root)

    # Redirect both WORLD_DIR globals to the temp tree. resolve_file_path reads
    # _paths.WORLD_DIR at call time; validate_tree's own block reads
    # tree.WORLD_DIR. Patch both; monkeypatch auto-reverts after the test.
    monkeypatch.setattr(_paths, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(tree, "WORLD_DIR", tmp_path)

    result = tree.validate_tree(_tree_dict())
    warns = result["warnings"]

    # 1. exactly one dangling-ref warning, naming the right node + target
    dangling = [w for w in warns if "body references tree node" in w]
    assert len(dangling) == 1, (
        "expected exactly one dangling body-xref warning, got: " + repr(dangling))
    assert "beta" in dangling[0] and "fiction/ghost.md" in dangling[0], (
        "warning should name node 'beta' and target 'fiction/ghost.md': "
        + dangling[0])

    # 2. valid body refs (alpha->beta, beta->alpha) must NOT warn
    assert not any("fiction/beta.md" in w and "body references" in w for w in warns)
    assert not any("fiction/alpha.md" in w and "body references" in w for w in warns)

    # 3. out-of-scope refs ignored (non-.md python; world/ file outside the tree)
    assert not any("none.py" in w for w in warns), "non-.md ref must be ignored"
    assert not any("ghost-conv" in w for w in warns), (
        "world/ file outside the tree must be ignored")

    # 3b. ellipsis paths are prose placeholders ('...'), never real targets
    assert not any("buried.md" in w for w in warns), (
        "ellipsis ('...') path must be ignored (g-115-1419 live-validation finding)")

    # 4. structural validity unaffected — the check adds warnings, not errors
    assert result["valid"] is True, "errors: " + repr(result["errors"])


def test_validate_clean_tree_has_no_dangling_warning(tmp_path, monkeypatch):
    """All inbound refs resolve -> zero dangling-ref warnings (no false positive)."""
    tree_root = tmp_path / "knowledge" / "tree"
    (tree_root / "fiction").mkdir(parents=True, exist_ok=True)
    (tree_root / "fiction.md").write_text(
        "# Fiction\nSee `fiction/alpha.md` and `fiction/beta.md`.\n", encoding="utf-8")
    (tree_root / "fiction" / "alpha.md").write_text(
        "# Alpha\nrefs `fiction/beta.md`\n", encoding="utf-8")
    (tree_root / "fiction" / "beta.md").write_text(
        "# Beta\nrefs `fiction/alpha.md`\n", encoding="utf-8")

    monkeypatch.setattr(_paths, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(tree, "WORLD_DIR", tmp_path)

    result = tree.validate_tree(_tree_dict())
    dangling = [w for w in result["warnings"] if "body references tree node" in w]
    assert dangling == [], "clean tree should produce no dangling-ref warnings: " + repr(dangling)
