"""test_tree_node_field_prevention.py — regression tests for the 2026-05-10
prevention layer that closes the capability_level / last_updated gap.

The audit found 66.2% of tree nodes missing capability_level and 74.2%
missing last_updated. Root cause:
  - apply_defaults didn't default capability_level
  - cmd_add_child / batch add-child never stamped last_updated
  - cmd_set / batch set required manual `--set <key> last_updated <today>`
    which SKILL.md authors had to remember and routinely forgot

These tests exercise the prevention paths directly:
  - apply_defaults backfills capability_level="EXPLORE" when missing
  - cmd_add_child inherits capability_level from parent + stamps last_updated
  - cmd_set does NOT auto-bump per-node last_updated (g-115-1683 reverted the
    2026-05-10 auto-bump: firing on every non-date field marched the _tree.yaml
    index AHEAD of the node .md front matter — the source of truth — producing
    index-ahead drift on 250 nodes / g-115-1612. last_updated is now synced to
    the .md fm only by tree-front-matter-sync.py and at node creation.)

End-to-end via subprocess so we exercise argparse + stdin pathways the LLM
actually uses, not in-process function calls that bypass arg parsing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
TREE_PY = CORE_SCRIPTS / "tree.py"
PYTHON = sys.executable

_TMPDIR = Path(tempfile.mkdtemp(prefix="tree-prevention-test-"))
_WORLD = _TMPDIR / "world"
# Isolate META_DIR too (not just WORLD): tree.py's _append_l1_pick_log writes
# META_DIR/l1-pick-log.jsonl on every add-child/set. Without MIND_META set in
# _run_tree, those writes hit the REAL meta dir — the 'new-child' test key
# polluted 840/896 production l1-pick-log entries, starving the S9/S7 L1-skew
# signal (0).
_META = _TMPDIR / "meta"
_TREE_DIR = _WORLD / "knowledge" / "tree"
_TREE_DIR.mkdir(parents=True, exist_ok=True)
_TREE_PATH = _TREE_DIR / "_tree.yaml"


def _seed_tree(nodes_dict, top_level=None):
    """Write a minimal _tree.yaml. nodes_dict is {key: node_dict}."""
    import yaml
    tree = {"nodes": nodes_dict}
    if top_level:
        tree.update(top_level)
    with open(_TREE_PATH, "w", encoding="utf-8") as f:
        yaml.dump(tree, f)


def _read_tree():
    import yaml
    with open(_TREE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _run_tree(args, stdin_text=None):
    """Invoke tree.py with the configured WORLD_DIR pointing at our tmp."""
    env = dict(os.environ)
    env["MIND_WORLD"] = str(_WORLD)
    env["MIND_META"] = str(_META)  # isolate l1-pick-log writes to tmp (0)
    env.pop("MIND_AGENT", None)
    return subprocess.run(
        [PYTHON, str(TREE_PY)] + args,
        input=stdin_text, text=True, capture_output=True,
        env=env, timeout=30,
    )


# ---------------------------------------------------------------------------
# apply_defaults — capability_level
# ---------------------------------------------------------------------------

def test_apply_defaults_sets_capability_level_explore_when_missing():
    """A read of a node lacking capability_level returns it with EXPLORE
    in the apply_defaults output. On-disk stays unchanged (read-side default)."""
    _seed_tree({
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": ["child"], "child_count": 1},
        "child": {"file": "world/knowledge/tree/root/child.md",
                  "depth": 1, "parent": "root", "children": []},
    })
    r = _run_tree(["read", "--node", "child"])
    assert r.returncode == 0, f"stderr={r.stderr}"
    out = json.loads(r.stdout)
    assert out["capability_level"] == "EXPLORE"


def test_apply_defaults_preserves_existing_capability_level():
    """If a node already has capability_level set, apply_defaults leaves it."""
    _seed_tree({
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": ["child"], "child_count": 1},
        "child": {"file": "world/knowledge/tree/root/child.md",
                  "depth": 1, "parent": "root", "children": [],
                  "capability_level": "EXPLOIT"},
    })
    r = _run_tree(["read", "--node", "child"])
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["capability_level"] == "EXPLOIT"


# ---------------------------------------------------------------------------
# cmd_add_child — parent inheritance + last_updated stamp
# ---------------------------------------------------------------------------

def test_cmd_add_child_inherits_capability_level_from_parent():
    """When the new-child JSON omits capability_level, the child inherits
    the parent's value (matches _enforce_child_limit's defaulting rule)."""
    _seed_tree({
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": [], "child_count": 0,
                 "capability_level": "EXPLOIT"},
    })
    payload = json.dumps({
        "key": "new-child",
        "summary": "test child without explicit capability_level",
        # NOTE: no capability_level here on purpose
    })
    r = _run_tree(["update", "--add-child", "root"], stdin_text=payload)
    assert r.returncode == 0, f"stderr={r.stderr}"

    tree = _read_tree()
    child = tree["nodes"]["new-child"]
    assert child["capability_level"] == "EXPLOIT", (
        f"expected inherited EXPLOIT, got {child['capability_level']}"
    )


def test_cmd_add_child_explicit_capability_level_wins_over_parent():
    """An explicit capability_level in the input JSON overrides parent inheritance."""
    _seed_tree({
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": [], "child_count": 0,
                 "capability_level": "EXPLOIT"},
    })
    payload = json.dumps({
        "key": "new-child",
        "summary": "test child with explicit capability_level",
        "capability_level": "EXPLORE",
    })
    r = _run_tree(["update", "--add-child", "root"], stdin_text=payload)
    assert r.returncode == 0, f"stderr={r.stderr}"

    tree = _read_tree()
    child = tree["nodes"]["new-child"]
    assert child["capability_level"] == "EXPLORE"


def test_cmd_add_child_stamps_last_updated_today():
    """Every newly-created child gets last_updated stamped to today's
    date — no more 74% missing-last_updated for new nodes."""
    _seed_tree({
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": [], "child_count": 0,
                 "capability_level": "EXPLORE"},
    })
    payload = json.dumps({"key": "new-child", "summary": "test"})
    r = _run_tree(["update", "--add-child", "root"], stdin_text=payload)
    assert r.returncode == 0

    tree = _read_tree()
    child = tree["nodes"]["new-child"]
    assert child["last_updated"] == date.today().isoformat()


def test_cmd_add_child_falls_back_to_explore_when_parent_lacks_capability():
    """If the parent ALSO lacks capability_level (legacy data), apply_defaults'
    EXPLORE fallback kicks in. apply_defaults runs at WRITE time too (not
    only read), so EXPLORE is persisted to disk for new children — this is
    the intended forward-looking behavior. Legacy nodes that bypassed
    cmd_add_child are the only ones missing capability_level on disk;
    backfill-tree-node-fields.py handles those."""
    _seed_tree({
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": [], "child_count": 0},  # no capability_level
    })
    payload = json.dumps({"key": "new-child", "summary": "test"})
    r = _run_tree(["update", "--add-child", "root"], stdin_text=payload)
    assert r.returncode == 0, f"stderr={r.stderr}"

    tree = _read_tree()
    child = tree["nodes"]["new-child"]
    assert child["capability_level"] == "EXPLORE", (
        f"expected EXPLORE persisted on disk via apply_defaults at write "
        f"time, got {child.get('capability_level')}"
    )


# ---------------------------------------------------------------------------
# cmd_set — auto-bump last_updated
# ---------------------------------------------------------------------------

def test_cmd_set_does_not_bump_last_updated():
    """3 (Option B): a metadata `--set <key> summary <new>` does NOT
    auto-bump per-node last_updated — it stays at its seeded value. The node
    .md front matter is the single source of truth (g-001-67); the _tree.yaml
    index last_updated is synced to it ONLY by tree-front-matter-sync.py (the
    Edit/Write PostToolUse hook, which writes both stores) and at node creation.
    The prior auto-bump fired on every non-date field, marching the index AHEAD
    of the .md fm (index-ahead drift, 250 nodes; g-115-1612 audit)."""
    _seed_tree({
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": [], "child_count": 0,
                 "summary": "old summary",
                 "last_updated": "2026-01-01"},
    })
    r = _run_tree(["update", "--set", "root", "summary", "new summary"])
    assert r.returncode == 0, f"stderr={r.stderr}"

    tree = _read_tree()
    assert tree["nodes"]["root"]["summary"] == "new summary"
    assert tree["nodes"]["root"]["last_updated"] == "2026-01-01", (
        f"expected last_updated UNCHANGED (no auto-bump, g-115-1683), got "
        f"{tree['nodes']['root']['last_updated']}"
    )


def test_cmd_set_explicit_last_updated_is_not_clobbered():
    """When the field BEING SET is last_updated itself, the explicit value
    wins — auto-bump must not overwrite an explicit date set."""
    _seed_tree({
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": [], "child_count": 0,
                 "last_updated": "2026-01-01"},
    })
    r = _run_tree(["update", "--set", "root", "last_updated", "2025-06-15"])
    assert r.returncode == 0, f"stderr={r.stderr}"

    tree = _read_tree()
    assert tree["nodes"]["root"]["last_updated"] == "2025-06-15", (
        f"expected explicit 2025-06-15, got "
        f"{tree['nodes']['root']['last_updated']}"
    )


def _run_all():
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures.append((t.__name__, str(e) or "<no message>"))
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failures.append((t.__name__, f"{type(e).__name__}: {e}"))
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(_run_all())
