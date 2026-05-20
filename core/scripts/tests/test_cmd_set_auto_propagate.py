"""test_cmd_set_auto_propagate.py — regression test for tree.py cmd_set
auto-propagation added 2026-05-07 (fix #7 from the 2026-05-07 tree audit).

Pre-fix, `tree.py --set <key> confidence 0.9` updated only the source node.
Callers had to follow with `tree.py --propagate <key>` to lift ancestor
confidence + capability_level. The 2026-05-07 audit found callers often
forgot, leaving ancestors stale: leaves at MASTER capability under parents
still at EXPLORE.

This test:
  1. Seeds a 3-level chain: root → mid → leaf.
  2. Sets leaf.confidence=0.9 via cmd_set.
  3. Asserts the source's capability_level graduated AND mid+root saw
     ancestor confidence updates AND capability_level changes.

The test invokes tree.py via Python import (not subprocess) so it runs
fast and doesn't need the Git Bash detection scaffolding.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from io import StringIO
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

# Bind tree.py to a temp world BEFORE import (tree.py reads paths from _paths.py
# at module load time).
#
# Capture-restore pattern: stash the conftest-set MIND_AGENT (and any prior
# MIND_WORLD) before mutating, then restore AFTER the fresh import. Without
# the restore, this test file contaminates every subsequent test in the
# pytest session — popping MIND_AGENT at module level was the contaminator
# behind 18 cross-test failures (test_auto_contract, test_inactivity_detector,
# test_inferred_unknown_autoflag, test_streak_break_reflector,
# test_unblock_parent_status_sweep, test_window_streak, test_wm_cmd_set_auto_init).
# tree_mod retains the temp paths internally via its own module-level _paths
# snapshot; later tests inherit a clean conftest env.
_TMP = tempfile.mkdtemp(prefix="cmd-set-prop-test-")
_SAVED_AGENT = os.environ.get("MIND_AGENT")
_SAVED_WORLD = os.environ.get("MIND_WORLD")
os.environ["MIND_WORLD"] = _TMP
os.environ.pop("MIND_AGENT", None)

_TREE_PATH = CORE_SCRIPTS / "tree.py"
_spec = importlib.util.spec_from_file_location("tree_mod", _TREE_PATH)
_tree_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tree_mod)

# Restore conftest's env so this file's import doesn't poison other tests.
if _SAVED_AGENT is not None:
    os.environ["MIND_AGENT"] = _SAVED_AGENT
if _SAVED_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _SAVED_WORLD


def _seed_tree() -> Path:
    """Build a 3-level chain inside the temp WORLD with low starting confidence
    so a single 0.9 leaf write triggers visible threshold crossings."""
    tree_dir = Path(_TMP) / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    tree = {
        "last_updated": "2026-05-07",
        "tree_growth_log": [],
        "nodes": {
            "root": {
                "file": None, "depth": 0, "parent": None,
                "children": ["mid"], "child_count": 1,
                "summary": "root", "confidence": 0.10,
                "domain_confidence": 0.10, "capability_level": "EXPLORE",
            },
            "mid": {
                "file": "world/knowledge/tree/mid.md",
                "depth": 1, "parent": "root",
                "children": ["leaf"], "child_count": 1,
                "summary": "mid", "confidence": 0.10,
                "domain_confidence": 0.10, "capability_level": "EXPLORE",
            },
            "leaf": {
                "file": "world/knowledge/tree/mid/leaf.md",
                "depth": 2, "parent": "mid",
                "children": [], "child_count": 0,
                "summary": "leaf", "confidence": 0.10,
                "domain_confidence": 0.10, "capability_level": "EXPLORE",
            },
        },
    }
    path = tree_dir / "_tree.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(tree, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return path


class _Args:
    """Mimic argparse Namespace shape that cmd_set reads."""
    def __init__(self, set_args):
        self.set = set_args


def main() -> int:
    tree_path = _seed_tree()

    # _tree_mod was loaded BEFORE we wrote the tree, but TREE_PATH is read
    # lazily at cmd_set runtime. Sanity check: TREE_PATH points at our temp.
    if Path(_tree_mod.TREE_PATH) != tree_path:
        print(f"FAIL: tree-mod TREE_PATH = {_tree_mod.TREE_PATH}, "
              f"expected {tree_path}", file=sys.stderr)
        return 1

    # Capture cmd_set's stdout — it prints the result JSON we want to inspect.
    saved_stdout = sys.stdout
    sys.stdout = capture = StringIO()
    try:
        _tree_mod.cmd_set(_Args(["leaf", "confidence", "0.9"]))
    finally:
        sys.stdout = saved_stdout

    out = capture.getvalue()
    try:
        result = json.loads(out)
    except json.JSONDecodeError as e:
        print(f"FAIL: cmd_set did not emit valid JSON: {e}", file=sys.stderr)
        print(f"output={out}", file=sys.stderr)
        return 1

    # Assertion 1: cmd_set's JSON output must include propagation results
    # when the field is `confidence` (the auto-propagate signal).
    if "ancestors_updated" not in result:
        print(f"FAIL: cmd_set should include ancestors_updated when "
              f"setting confidence; got keys={sorted(result.keys())}",
              file=sys.stderr)
        return 1
    if "capability_changes" not in result:
        print(f"FAIL: cmd_set should include capability_changes; "
              f"got keys={sorted(result.keys())}", file=sys.stderr)
        return 1

    # Assertion 2: BOTH mid and root should have been updated.
    updated_keys = {a["key"] for a in result["ancestors_updated"]}
    if {"mid", "root"} != updated_keys:
        print(f"FAIL: ancestors_updated should include mid AND root, "
              f"got {updated_keys}", file=sys.stderr)
        return 1

    # Assertion 3: re-read the tree and confirm mid + root confidence moved.
    with open(tree_path, "r", encoding="utf-8") as f:
        tree = yaml.safe_load(f)
    nodes = tree["nodes"]
    if nodes["leaf"]["confidence"] != 0.9:
        print(f"FAIL: leaf.confidence should be 0.9, got "
              f"{nodes['leaf']['confidence']}", file=sys.stderr)
        return 1
    if nodes["mid"]["confidence"] != 0.9:
        print(f"FAIL: mid.confidence should propagate to 0.9 (single child), "
              f"got {nodes['mid']['confidence']}", file=sys.stderr)
        return 1
    if nodes["root"]["confidence"] != 0.9:
        print(f"FAIL: root.confidence should propagate to 0.9, "
              f"got {nodes['root']['confidence']}", file=sys.stderr)
        return 1

    # Assertion 4: capability_level should have crossed thresholds.
    # Default thresholds: EXPLORE 0.25, CALIBRATE 0.50, EXPLOIT 0.75, MASTER 1.00.
    # Confidence 0.9 → EXPLOIT.
    for k in ("leaf", "mid", "root"):
        if nodes[k].get("capability_level") != "EXPLOIT":
            print(f"FAIL: {k}.capability_level should be EXPLOIT (conf=0.9), "
                  f"got {nodes[k].get('capability_level')}", file=sys.stderr)
            return 1

    # Assertion 5: capability_changes should record at least the source-node
    # graduation (leaf EXPLORE → EXPLOIT). The ancestor changes should also
    # appear (mid + root crossed thresholds in the same direction).
    changed_keys = {c["key"] for c in result["capability_changes"]}
    if "leaf" not in changed_keys:
        print(f"FAIL: capability_changes should include leaf "
              f"(self-graduation), got {changed_keys}", file=sys.stderr)
        return 1
    if not ({"mid", "root"} & changed_keys):
        print(f"FAIL: capability_changes should include at least one ancestor "
              f"(mid or root); got {changed_keys}", file=sys.stderr)
        return 1

    print(f"PASS: cmd_set --set leaf confidence 0.9 auto-propagated to "
          f"{updated_keys} and graduated capability_level on {changed_keys}.")
    return 0


if __name__ == "__main__":
    rc = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(rc)
