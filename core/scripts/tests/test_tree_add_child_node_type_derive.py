"""test_tree_add_child_node_type_derive.py —  (asp-115).

Pins the fix that makes tree-update add-child / batch add-child flip a parent's
node_type from leaf to interior when it gains its first child. Before the fix,
add-child updated child_count but left node_type stale, so a freshly-created
node that gained children stayed node_type=leaf (witnessed:
runtime-constraint-envelope had child_count=4 but node_type=leaf until a manual
--set). That mislabels interior nodes as leaves, misleading retrieval/decompose
logic and tripping tree-validate (guard-757 documented the manual workaround,
retired by this goal).

The guard is `node_type == "leaf" -> interior` (mirrors cmd_reparent's
new-parent idiom). It only rewrites an EXPLICIT stale leaf — a parent with NO
node_type field is already normalized to interior on read (apply_defaults
L366-367), so these tests seed an explicit node_type="leaf" to exercise the fix
directly. Two changed paths, two subprocess tests (cmd_add_child + cmd_batch);
modeled on test_origin_goal_id_instrumentation.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
TREE_PY = CORE_SCRIPTS / "tree.py"
PYTHON = sys.executable


def _seed_tree(world: Path, parent_node_type: str = "leaf",
               parent_children=None, extra_nodes=None):
    """Seed _tree.yaml with a `parent` node carrying an explicit node_type and a
    consistent child_count. EXPLOIT capability_level keeps the child-limit gate
    well clear for the 1-2 children these tests add."""
    children = list(parent_children or [])
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    nodes = {
        "parent": {
            "file": "world/knowledge/tree/parent.md", "depth": 0,
            "children": children, "child_count": len(children),
            "capability_level": "EXPLOIT", "node_type": parent_node_type,
        },
    }
    if extra_nodes:
        nodes.update(extra_nodes)
    (tree_dir / "_tree.yaml").write_text(
        yaml.safe_dump({"nodes": nodes}), encoding="utf-8")
    return tree_dir / "_tree.yaml"


def _run_tree(args, stdin_text, world: Path, meta: Path):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env.pop("MIND_AGENT", None)
    return subprocess.run(
        [PYTHON, str(TREE_PY)] + args,
        input=stdin_text, text=True, capture_output=True, env=env, timeout=30,
    )


def test_add_child_flips_parent_leaf_to_interior(tmp_path):
    """cmd_add_child: a stored node_type=leaf parent that gains its first child
    becomes node_type=interior (the witnessed bug, now fixed)."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(world, parent_node_type="leaf")
    payload = json.dumps({"key": "child-a", "summary": "a test child node"})
    r = _run_tree(["update", "--add-child", "parent"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    parent = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]["parent"]
    assert parent["node_type"] == "interior"
    assert parent["child_count"] == 1


def test_batch_add_child_flips_parent_leaf_to_interior(tmp_path):
    """cmd_batch add-child: same leaf->interior flip via the batch path."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(world, parent_node_type="leaf")
    payload = json.dumps({"operations": [
        {"op": "add-child", "key": "parent",
         "child": {"key": "child-b", "summary": "a batch child node"}},
    ]})
    r = _run_tree(["update", "--batch"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    parent = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]["parent"]
    assert parent["node_type"] == "interior"
    assert parent["child_count"] == 1


def test_add_child_already_interior_stays_interior(tmp_path):
    """An already-interior parent gaining another child stays interior — the
    guard is idempotent and does not regress correctly-labeled nodes."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(
        world, parent_node_type="interior", parent_children=["child-x"],
        extra_nodes={"child-x": {
            "file": "world/knowledge/tree/child-x.md", "depth": 1,
            "parent": "parent", "children": [], "child_count": 0,
            "capability_level": "EXPLOIT", "node_type": "leaf"}})
    payload = json.dumps({"key": "child-y", "summary": "second child"})
    r = _run_tree(["update", "--add-child", "parent"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    parent = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]["parent"]
    assert parent["node_type"] == "interior"
    assert parent["child_count"] == 2


# ── : symmetric remove-child paths flip interior->leaf on last child ──
# The add-child fix () above made gaining a first child flip
# leaf->interior. The remove paths (cmd_remove_child + cmd_batch remove-child)
# had the symmetric gap: removing the LAST child updated child_count but left
# node_type=interior, so an emptied node stayed mislabeled interior. cmd_reparent's
# old-parent path already had the `if not children: node_type=leaf` idiom; these
# tests pin that the two remove paths now mirror it.


def _seed_interior_with_one_child(world: Path):
    """Seed `parent` as a stored node_type=interior with exactly one leaf child,
    consistent child_count=1. Removing that child should flip parent to leaf."""
    return _seed_tree(
        world, parent_node_type="interior", parent_children=["only-child"],
        extra_nodes={"only-child": {
            "file": "world/knowledge/tree/only-child.md", "depth": 1,
            "parent": "parent", "children": [], "child_count": 0,
            "capability_level": "EXPLOIT", "node_type": "leaf"}})


def test_remove_child_flips_parent_interior_to_leaf(tmp_path):
    """cmd_remove_child: removing a parent's last child flips node_type
    interior->leaf (the symmetric gap to the add-child fix)."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_interior_with_one_child(world)
    r = _run_tree(["update", "--remove-child", "parent", "only-child"], "", world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    parent = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]["parent"]
    assert parent["node_type"] == "leaf"
    assert parent["child_count"] == 0


def test_batch_remove_child_flips_parent_interior_to_leaf(tmp_path):
    """cmd_batch remove-child: same interior->leaf flip via the batch path."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_interior_with_one_child(world)
    payload = json.dumps({"operations": [
        {"op": "remove-child", "key": "parent", "child_key": "only-child"},
    ]})
    r = _run_tree(["update", "--batch"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    parent = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]["parent"]
    assert parent["node_type"] == "leaf"
    assert parent["child_count"] == 0


def test_remove_non_last_child_keeps_parent_interior(tmp_path):
    """Removing a child while OTHERS remain leaves node_type=interior — the
    flip only fires when the last child is removed (guard is `if not children`)."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(
        world, parent_node_type="interior",
        parent_children=["child-1", "child-2"],
        extra_nodes={
            "child-1": {"file": "world/knowledge/tree/child-1.md", "depth": 1,
                        "parent": "parent", "children": [], "child_count": 0,
                        "capability_level": "EXPLOIT", "node_type": "leaf"},
            "child-2": {"file": "world/knowledge/tree/child-2.md", "depth": 1,
                        "parent": "parent", "children": [], "child_count": 0,
                        "capability_level": "EXPLOIT", "node_type": "leaf"}})
    r = _run_tree(["update", "--remove-child", "parent", "child-1"], "", world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    parent = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]["parent"]
    assert parent["node_type"] == "interior"
    assert parent["child_count"] == 1


# ── : the CHILD's OWN node_type is derive-always at create ──────────
#  (above) fixed the PARENT flip; this is the sibling gap where the
# freshly-created CHILD kept a caller-supplied (or default) node_type that
# contradicted its own children. node_type is now derived from child-presence at
# the create path (cmd_add_child + cmd_batch add-child), mirroring child_count: a
# caller-supplied node_type is IGNORED (removed from the copy allowlist), and
# apply_defaults' fill-if-absent is no longer relied on as the deriver (it also
# normalizes reads, which must NOT mask on-disk drift). These tests assert the
# child node's node_type directly (the tests above assert the parent's).


def test_add_child_with_children_is_interior(tmp_path):
    """A child created carrying a non-empty children list is node_type=interior."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(world)
    payload = json.dumps({"key": "child-c", "summary": "child with kids",
                          "children": ["gc-1"]})
    r = _run_tree(["update", "--add-child", "parent"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    child = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]["child-c"]
    assert child["node_type"] == "interior"
    assert child["child_count"] == 1


def test_add_child_leaf_node_type_overridden_when_children_present(tmp_path):
    """THE BUG (): caller passes node_type=leaf with a non-empty
    children list; the create path derives interior anyway (caller value ignored)."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(world)
    payload = json.dumps({"key": "child-d", "summary": "wrongly-typed child",
                          "children": ["gc-1", "gc-2"], "node_type": "leaf"})
    r = _run_tree(["update", "--add-child", "parent"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    child = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]["child-d"]
    assert child["node_type"] == "interior"
    assert child["child_count"] == 2


def test_add_child_no_children_is_leaf(tmp_path):
    """A childless child is node_type=leaf (the common case stays correct)."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(world)
    payload = json.dumps({"key": "child-e", "summary": "childless"})
    r = _run_tree(["update", "--add-child", "parent"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    child = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]["child-e"]
    assert child["node_type"] == "leaf"
    assert child["child_count"] == 0


def test_add_child_interior_node_type_overridden_when_no_children(tmp_path):
    """Symmetric: caller passes node_type=interior with NO children; the create
    path derives leaf (derive-always ignores the wrong caller value)."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(world)
    payload = json.dumps({"key": "child-f", "summary": "wrongly-interior",
                          "node_type": "interior"})
    r = _run_tree(["update", "--add-child", "parent"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    child = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]["child-f"]
    assert child["node_type"] == "leaf"
    assert child["child_count"] == 0


def test_batch_add_child_with_children_is_interior(tmp_path):
    """Batch path: a child carrying a non-empty children list is interior."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(world)
    payload = json.dumps({"operations": [
        {"op": "add-child", "key": "parent",
         "child": {"key": "child-g", "summary": "batch child with kids",
                   "children": ["gc-1"]}},
    ]})
    r = _run_tree(["update", "--batch"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    child = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]["child-g"]
    assert child["node_type"] == "interior"
    assert child["child_count"] == 1


def test_batch_add_child_leaf_node_type_overridden_when_children_present(tmp_path):
    """Batch path, THE BUG: node_type=leaf + children -> derived interior."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(world)
    payload = json.dumps({"operations": [
        {"op": "add-child", "key": "parent",
         "child": {"key": "child-h", "summary": "wrongly-typed batch child",
                   "children": ["gc-1", "gc-2"], "node_type": "leaf"}},
    ]})
    r = _run_tree(["update", "--batch"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    child = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]["child-h"]
    assert child["node_type"] == "interior"
    assert child["child_count"] == 2
