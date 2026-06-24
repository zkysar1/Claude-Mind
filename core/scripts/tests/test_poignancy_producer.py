"""test_poignancy_producer.py —  (asp-306, BRD Gap 1a producer).

g-306-08 shipped the `poignancy` field (RB + tree) and the flag-gated retrieve
blend, but NO write path assigned a rating — every poignancy was null, so the
blend was a permanent no-op. g-306-26 wires the producers. The RB write path
already passes the field through (no unknown-field gate; rb-1691 dogfood). The
TREE write path did NOT: cmd_add_child / cmd_batch add-child copy only an
explicit field allowlist, which omitted `poignancy` — so a poignancy in the
child JSON was silently dropped, then defaulted to null by apply_defaults.

This test pins the tree-side code change: `poignancy` is now in BOTH add-child
allowlists, so the tree-node creation path assigns poignancy AT WRITE TIME.
Regression guard: absent poignancy still defaults to null (existing readers
parse unchanged). Modeled on test_origin_goal_id_instrumentation.py (the
sibling field-copy mechanism whose allowlist entry sits right beside the new one).
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


def _seed_tree(world: Path):
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    tree = {"nodes": {
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": [], "child_count": 0, "capability_level": "EXPLOIT"},
    }}
    (tree_dir / "_tree.yaml").write_text(yaml.safe_dump(tree), encoding="utf-8")
    return tree_dir / "_tree.yaml"


def _run_tree(args, stdin_text, world: Path, meta: Path):
    """Invoke tree.py with WORLD/META isolated to tmp. No MIND_AGENT needed —
    poignancy is caller-supplied in the child JSON, not injected from team-state
    (unlike origin_goal_id), so _read_in_flight_goal_id returning None is fine."""
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env.pop("MIND_AGENT", None)
    return subprocess.run(
        [PYTHON, str(TREE_PY)] + args,
        input=stdin_text, text=True, capture_output=True, env=env, timeout=30,
    )


def test_add_child_preserves_poignancy_at_write(tmp_path):
    """The core  change: a poignancy in the child JSON lands on the new
    node at creation (was silently dropped by the allowlist before this goal)."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    tree_path = _seed_tree(world)
    payload = json.dumps({"key": "child-poig", "summary": "a node", "poignancy": 8})
    r = _run_tree(["update", "--add-child", "root"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    assert tree["nodes"]["child-poig"]["poignancy"] == 8


def test_add_child_absent_poignancy_defaults_null(tmp_path):
    """Regression: a child JSON without poignancy still yields a null poignancy
    (apply_defaults) — existing /tree add callers parse unchanged."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    tree_path = _seed_tree(world)
    payload = json.dumps({"key": "child-nopoig", "summary": "a node"})
    r = _run_tree(["update", "--add-child", "root"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    assert tree["nodes"]["child-nopoig"]["poignancy"] is None


def test_batch_add_child_preserves_poignancy(tmp_path):
    """The batch add-child path (second allowlist) also preserves poignancy, so
    decompose/sprout batch writes populate the field at creation too."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    tree_path = _seed_tree(world)
    payload = json.dumps({"operations": [
        {"op": "add-child", "key": "root",
         "child": {"key": "batch-poig", "summary": "batch node", "poignancy": 6}},
    ]})
    r = _run_tree(["update", "--batch"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    assert tree["nodes"]["batch-poig"]["poignancy"] == 6
