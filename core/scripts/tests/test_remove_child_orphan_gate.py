"""test_remove_child_orphan_gate.py — regression test for the
subtree-orphaning gate added 2026-05-07 (fix #8 from the tree audit).

Pre-fix, `tree.py --remove-child <parent> <child>` silently deleted the
child even when it had grandchildren. The grandchildren's `parent` field
still pointed at the deleted key, making them unreachable from root,
invisible to retrieval, and flagged as orphans by validate_tree. The
audit (2026-05-07) found this was the cause of two stale subtrees in
the live alpha tree.

Post-fix, the operation refuses with exit code 2 and a message naming
the descendants. Callers must remove descendants depth-first.

This test exercises both:
  1. cmd_remove_child (single-op CLI path)
  2. cmd_batch's remove-child branch (atomic batch path)
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
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

#  capture-restore pattern: stash env before module-level mutation
# so subsequent tests in the same pytest session don't inherit a popped
# MIND_AGENT. See test_applies_to_required.py for full rationale.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMP = tempfile.mkdtemp(prefix="rmchild-orphan-test-")
os.environ["MIND_WORLD"] = _TMP
os.environ.pop("MIND_AGENT", None)

_TREE_PATH = CORE_SCRIPTS / "tree.py"
_spec = importlib.util.spec_from_file_location("tree_mod", _TREE_PATH)
_tree_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tree_mod)

# Restore env so downstream tests inherit clean conftest defaults.
if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


def _seed_tree() -> Path:
    tree_dir = Path(_TMP) / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    tree = {
        "last_updated": "2026-05-07",
        "tree_growth_log": [],
        "nodes": {
            "root": {"file": None, "depth": 0, "parent": None,
                     "children": ["mid"], "child_count": 1, "summary": "root"},
            "mid": {
                "file": "world/knowledge/tree/mid.md",
                "depth": 1, "parent": "root",
                "children": ["leaf-a", "leaf-b"], "child_count": 2,
                "summary": "mid",
            },
            "leaf-a": {
                "file": "world/knowledge/tree/mid/leaf-a.md",
                "depth": 2, "parent": "mid", "children": [], "child_count": 0,
                "summary": "leaf a",
            },
            "leaf-b": {
                "file": "world/knowledge/tree/mid/leaf-b.md",
                "depth": 2, "parent": "mid", "children": [], "child_count": 0,
                "summary": "leaf b",
            },
        },
    }
    path = tree_dir / "_tree.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(tree, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return path


class _Args:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _read_tree(tree_path: Path) -> dict:
    with open(tree_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _scenario_refuse_with_descendants(tree_path: Path) -> bool:
    """Removing 'mid' with leaf-a + leaf-b as descendants must be refused."""
    saved_stderr = sys.stderr
    sys.stderr = capture = StringIO()
    rc_seen = None
    try:
        _tree_mod.cmd_remove_child(_Args(remove_child=["root", "mid"]))
    except SystemExit as e:
        rc_seen = e.code
    finally:
        sys.stderr = saved_stderr

    err = capture.getvalue()
    if rc_seen != 2:
        print(f"FAIL[scenario 1]: expected SystemExit(2), got rc={rc_seen}",
              file=sys.stderr)
        return False
    if "leaf-a" not in err or "leaf-b" not in err:
        print(f"FAIL[scenario 1]: error message should name descendants; "
              f"got: {err}", file=sys.stderr)
        return False

    # Verify nothing changed on disk
    tree = _read_tree(tree_path)
    if "mid" not in tree["nodes"]:
        print("FAIL[scenario 1]: 'mid' was deleted despite the refusal",
              file=sys.stderr)
        return False
    if "mid" not in tree["nodes"]["root"]["children"]:
        print("FAIL[scenario 1]: 'mid' was unlinked from root despite refusal",
              file=sys.stderr)
        return False
    return True


def _scenario_allow_leaf_removal(tree_path: Path) -> bool:
    """Removing a leaf (no descendants) must succeed."""
    saved_stdout = sys.stdout
    sys.stdout = capture = StringIO()
    rc_seen = None
    try:
        _tree_mod.cmd_remove_child(_Args(remove_child=["mid", "leaf-a"]))
    except SystemExit as e:
        rc_seen = e.code
    finally:
        sys.stdout = saved_stdout

    if rc_seen is not None and rc_seen != 0:
        print(f"FAIL[scenario 2]: leaf removal exited rc={rc_seen}, "
              f"stdout={capture.getvalue()}", file=sys.stderr)
        return False
    out = capture.getvalue()
    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        print(f"FAIL[scenario 2]: leaf removal stdout not JSON: {out}",
              file=sys.stderr)
        return False
    if result.get("removed") != "leaf-a":
        print(f"FAIL[scenario 2]: removed field should be 'leaf-a', got {result}",
              file=sys.stderr)
        return False

    tree = _read_tree(tree_path)
    if "leaf-a" in tree["nodes"]:
        print("FAIL[scenario 2]: leaf-a still in nodes after successful remove",
              file=sys.stderr)
        return False
    if "leaf-a" in tree["nodes"]["mid"]["children"]:
        print("FAIL[scenario 2]: leaf-a still in mid's children after remove",
              file=sys.stderr)
        return False
    return True


def _scenario_batch_refuses(tree_path: Path) -> bool:
    """cmd_batch's remove-child must apply the same gate.
    State on entry: root → mid → [leaf-b] (leaf-a removed in scenario 2).
    Submit a batch trying to remove 'mid' (still has leaf-b as descendant)."""
    batch_payload = {
        "operations": [
            {"op": "remove-child", "key": "root", "child_key": "mid"},
        ],
    }
    from _bash_helpers import BASH as bash  # rb-1472: bin-first, clean-PATH-safe
    env = os.environ.copy()
    env["MIND_WORLD"] = _TMP
    env.pop("MIND_AGENT", None)
    update_rel = (CORE_SCRIPTS / "tree-update.sh").relative_to(PROJECT_ROOT).as_posix()
    result = subprocess.run(
        [bash, update_rel, "--batch"],
        input=json.dumps(batch_payload),
        capture_output=True, text=True, env=env,
        cwd=str(PROJECT_ROOT), timeout=15,
    )
    if result.returncode != 2:
        print(f"FAIL[scenario 3]: batch should exit 2, got rc={result.returncode}",
              file=sys.stderr)
        print(f"stdout={result.stdout}", file=sys.stderr)
        print(f"stderr={result.stderr}", file=sys.stderr)
        return False
    if "leaf-b" not in result.stderr:
        print(f"FAIL[scenario 3]: batch stderr should name descendants; "
              f"got: {result.stderr}", file=sys.stderr)
        return False

    # Verify nothing changed on disk (atomic refuse)
    tree = _read_tree(tree_path)
    if "mid" not in tree["nodes"]:
        print("FAIL[scenario 3]: batch refuse should be atomic; "
              "'mid' was deleted", file=sys.stderr)
        return False
    return True


def main() -> int:
    tree_path = _seed_tree()
    if Path(_tree_mod.TREE_PATH) != tree_path:
        print(f"FAIL: tree-mod TREE_PATH = {_tree_mod.TREE_PATH}, "
              f"expected {tree_path}", file=sys.stderr)
        return 1

    if not _scenario_refuse_with_descendants(tree_path):
        return 1
    if not _scenario_allow_leaf_removal(tree_path):
        return 1
    if not _scenario_batch_refuses(tree_path):
        return 1

    print("PASS: cmd_remove_child refuses subtree orphaning; leaf removal "
          "still works; cmd_batch's remove-child applies the same gate atomically.")
    return 0


if __name__ == "__main__":
    rc = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(rc)
