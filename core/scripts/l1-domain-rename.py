#!/usr/bin/env python3
"""Rename an existing L1 domain (S8 — Phase 2).

Renames an L1 in the tree AND reparents every descendant file path so the
existing subtree continues to resolve. The descendant graph keeps its
parent links (recomputed via tree-update reparent logic); only file paths
move.

This is the higher-blast-radius half of S8's locked scope (RENAME + ADD).
It atomically:
  1. Renames the L1 entry in `core/config/tree.yaml l1_domains:`
  2. Renames the node key in `_tree.yaml` under root.children
  3. Walks all descendants and recomputes their `file:` path from the new
     parent path
  4. Moves the L1 .md file and the L1's subtree directory on disk
  5. Updates every descendant's parent reference + file moves
  6. Logs the rename to tree_growth_log + l1-pick-log

If ANY step fails after the YAML mutation begins, the operation must abort
and surface the inconsistent state — partial rename is worse than failed
rename because subsequent operations can't safely retry. We use locked_modify
for the _tree.yaml step; the .md/directory move is the LAST step.

Workflow:
1. User-approval gate: requires `--approved-by <pending-id>` (l1-taxonomy- prefix).
2. Validate new key: distinct from existing keys, valid kebab-case.
3. Apply.

Usage:
    py -3 core/scripts/l1-domain-rename.py \\
        --old-key system \\
        --new-key infrastructure \\
        --summary "HOW we work — system infrastructure and meta-knowledge" \\
        --approved-by l1-taxonomy-2026-05-14-rename-system

    py -3 core/scripts/l1-domain-rename.py --dry-run \\
        --old-key system --new-key infrastructure --summary "..."
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, date
from pathlib import Path

from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import PROJECT_ROOT, WORLD_DIR, META_DIR, CONFIG_DIR


TREE_PATH = str(WORLD_DIR / "knowledge" / "tree" / "_tree.yaml")
TREE_CONFIG_PATH = str(CONFIG_DIR / "tree.yaml")
RESERVED_KEYS = {"root", "_tree", "archive", "archived"}
KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


def _validate_keys(old_key, new_key, existing_keys):
    if not old_key or not new_key:
        return "both --old-key and --new-key are required"
    if old_key == new_key:
        return "--old-key and --new-key must differ"
    if not KEY_RE.match(new_key):
        return ("--new-key must be lowercase kebab-case, 2-41 chars, "
                "starting with a letter (got '{}')".format(new_key))
    if new_key in RESERVED_KEYS:
        return "--new-key '{}' is reserved".format(new_key)
    if new_key in existing_keys:
        return "--new-key '{}' already exists in tree".format(new_key)
    if old_key not in existing_keys:
        return "--old-key '{}' does not exist in tree".format(old_key)
    return None


def _validate_l1(tree, old_key):
    node = (tree.get("nodes") or {}).get(old_key)
    if not node:
        return "node '{}' not found".format(old_key)
    if node.get("depth") != 1:
        return ("node '{}' is not an L1 (depth={}). This script renames "
                "L1s only — use /tree reparent for non-L1 moves.".format(
                    old_key, node.get("depth")))
    if node.get("parent") != "root":
        return ("node '{}' parent is '{}', expected 'root' for L1".format(
            old_key, node.get("parent")))
    return None


def _validate_approval(approval_id):
    if not approval_id:
        return ("user approval is required (--approved-by <pending-id>). "
                "See core/config/conventions/l1-taxonomy-changes.md.")
    if not approval_id.startswith("l1-taxonomy-"):
        return "approval id must start with 'l1-taxonomy-'"
    return None


def _rename_in_tree_yaml(old_key, new_key, new_summary):
    """Targeted text edit on core/config/tree.yaml l1_domains: list.

    Same surgical-text approach as l1-domain-add.py — avoids round-tripping
    through yaml.dump which would strip comments and reorder keys.
    """
    with open(TREE_CONFIG_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    # Match the block:
    #   - key: <old_key>
    #     file: world/knowledge/tree/<old_key>.md
    #     summary: "..."
    pattern = re.compile(
        r"(\s*- key: )" + re.escape(old_key) + r"(\s*\n"
        r"\s*file: world/knowledge/tree/)" + re.escape(old_key) + r"(\.md\s*\n"
        r"\s*summary: )\"[^\"]*\"",
        re.MULTILINE,
    )
    replacement = (
        r"\1" + new_key + r"\2" + new_key + r"\3"
        + "\"" + new_summary.replace('"', '\\"') + "\""
    )
    new_text, n = pattern.subn(replacement, text, count=1)
    if n == 0:
        raise RuntimeError(
            "could not find '- key: {}' entry in tree.yaml l1_domains".format(old_key))
    with open(TREE_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(new_text)


def _rename_in_world_tree(old_key, new_key, new_summary):
    """Atomically rename the L1 node + recompute every descendant's file path.

    Returns a list of (old_abs_path, new_abs_path) file move tuples for the
    caller to execute on disk. The YAML is fully updated when this returns.
    """
    from _fileops import locked_modify_yaml

    captured = {"file_moves": [], "old_l1_file": None, "new_l1_file": None}

    def _do(data):
        if not isinstance(data, dict) or "nodes" not in data:
            raise RuntimeError("invalid _tree.yaml: missing 'nodes'")
        nodes = data["nodes"]
        if old_key not in nodes:
            raise RuntimeError("old key '{}' missing from _tree.yaml".format(old_key))
        if new_key in nodes:
            raise RuntimeError("new key '{}' already exists in _tree.yaml".format(new_key))
        # 1. Rename root.children entry
        root = nodes.get("root") or {}
        root_children = root.get("children", []) or []
        if old_key not in root_children:
            raise RuntimeError("old key '{}' not in root.children".format(old_key))
        root_children = [new_key if c == old_key else c for c in root_children]
        root["children"] = root_children
        nodes["root"] = root
        # 2. Move the L1 node entry under its new key
        old_node = nodes.pop(old_key)
        old_l1_file = old_node.get("file", "world/knowledge/tree/{}.md".format(old_key))
        new_l1_file = "world/knowledge/tree/{}.md".format(new_key)
        old_node["file"] = new_l1_file
        if new_summary:
            old_node["summary"] = new_summary
        old_node["last_updated"] = date.today().isoformat()
        nodes[new_key] = old_node
        captured["old_l1_file"] = old_l1_file
        captured["new_l1_file"] = new_l1_file
        captured["file_moves"].append((old_l1_file, new_l1_file))

        # 3. Recursively recompute child file paths.
        def _walk(key, parent_file):
            n = nodes.get(key)
            if not n:
                return
            for child_key in n.get("children", []) or []:
                child = nodes.get(child_key)
                if not child:
                    continue
                old_child_file = child.get("file", "")
                # New file path: parent's .md stripped of `.md`, + child slug + .md
                parent_dir = parent_file[:-3] if parent_file.endswith(".md") else parent_file
                new_child_file = "{}/{}.md".format(parent_dir, child_key)
                if old_child_file != new_child_file:
                    child["file"] = new_child_file
                    nodes[child_key] = child
                    captured["file_moves"].append((old_child_file, new_child_file))
                # Recurse with updated parent_file for grandchildren
                _walk(child_key, new_child_file)

        _walk(new_key, new_l1_file)

        # 4. Update children of any node that referenced old_key as parent
        # (in case parent fields use the L1 key — they do for direct children).
        for nk, node in nodes.items():
            if node.get("parent") == old_key:
                node["parent"] = new_key
                nodes[nk] = node

        data["nodes"] = nodes
        data["last_updated"] = date.today().isoformat()
        log = data.get("tree_growth_log") or []
        log.append({
            "op": "L1_RENAME",
            "from": old_key,
            "to": new_key,
            "date": date.today().isoformat(),
            "reason": "S8 user-approval-gated L1 rename",
        })
        data["tree_growth_log"] = log
        return data

    locked_modify_yaml(TREE_PATH, _do)
    return captured["file_moves"]


def _execute_file_moves(file_moves):
    """Execute physical file moves on disk.

    Strategy: move the L1 directory (carrying every descendant in one
    rename), then move the L1 .md file. Because every child's file is
    derived from its parent's file, the directory rename handles the bulk
    of file_moves implicitly.
    """
    if not file_moves:
        return
    old_l1_file, new_l1_file = file_moves[0]
    old_l1_abs = (Path(PROJECT_ROOT) / old_l1_file).resolve() if not Path(old_l1_file).is_absolute() else Path(old_l1_file)
    new_l1_abs = (Path(PROJECT_ROOT) / new_l1_file).resolve() if not Path(new_l1_file).is_absolute() else Path(new_l1_file)
    # Resolve via WORLD_DIR when the path is virtual (world/...)
    if old_l1_file.startswith("world/"):
        old_l1_abs = Path(WORLD_DIR) / Path(old_l1_file).relative_to("world")
    if new_l1_file.startswith("world/"):
        new_l1_abs = Path(WORLD_DIR) / Path(new_l1_file).relative_to("world")
    # Move the directory first (carries descendants)
    old_dir = old_l1_abs.parent / old_l1_abs.stem
    new_dir = new_l1_abs.parent / new_l1_abs.stem
    if old_dir.exists() and old_dir.is_dir():
        if new_dir.exists():
            raise RuntimeError("target directory '{}' already exists".format(new_dir))
        shutil.move(str(old_dir), str(new_dir))
    # Move the L1 .md file
    if old_l1_abs.exists():
        if new_l1_abs.exists():
            raise RuntimeError("target file '{}' already exists".format(new_l1_abs))
        shutil.move(str(old_l1_abs), str(new_l1_abs))


def _log_l1_pick(old_key, new_key, approval_id):
    try:
        log_path = Path(META_DIR) / "l1-pick-log.jsonl"
        agent = os.environ.get("MIND_AGENT", "").strip() or None
        sid = os.environ.get("MIND_SID", "").strip() or None
        entry = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": agent,
            "session_id": sid,
            "target_node": new_key,
            "l1": new_key,
            "decision_type": "l1-rename",
            "source": "l1-domain-rename.py",
            "reason": "renamed from '{}' via {}".format(old_key, approval_id),
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(log_path), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[l1-domain-rename] log warn: " + str(e), file=sys.stderr)


def _read_tree():
    with open(TREE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old-key", required=True, help="Existing L1 key to rename")
    ap.add_argument("--new-key", required=True, help="New L1 key (lowercase kebab-case)")
    ap.add_argument("--summary", default=None,
                    help="New one-line description (defaults to existing summary)")
    ap.add_argument("--approved-by", default=None,
                    help="Pending-question id proving user approval (required unless --dry-run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate inputs and print the plan; no writes")
    args = ap.parse_args()

    try:
        tree = _read_tree()
    except Exception as e:
        print("Failed to read _tree.yaml: " + str(e), file=sys.stderr)
        sys.exit(2)
    existing_keys = set((tree.get("nodes") or {}).keys())

    err = _validate_keys(args.old_key, args.new_key, existing_keys)
    if err:
        print("Invalid input: " + err, file=sys.stderr)
        sys.exit(2)
    err = _validate_l1(tree, args.old_key)
    if err:
        print("Invalid input: " + err, file=sys.stderr)
        sys.exit(2)

    if not args.dry_run:
        err = _validate_approval(args.approved_by)
        if err:
            print(err, file=sys.stderr)
            sys.exit(3)

    # If --summary not given, keep existing.
    existing_summary = (tree.get("nodes") or {}).get(args.old_key, {}).get("summary", "")
    new_summary = args.summary if args.summary is not None else existing_summary

    # Count descendants so the plan reports blast radius.
    nodes = tree.get("nodes") or {}
    descendants = []
    stack = [args.old_key]
    while stack:
        cur = stack.pop()
        for ch in nodes.get(cur, {}).get("children", []) or []:
            descendants.append(ch)
            stack.append(ch)

    plan = {
        "action": "L1_RENAME",
        "from": args.old_key,
        "to": args.new_key,
        "summary": new_summary,
        "approved_by": args.approved_by,
        "descendant_count": len(descendants),
        "writes": [
            "core/config/tree.yaml l1_domains: rename entry",
            "_tree.yaml: rename node + reparent {} descendants".format(len(descendants)),
            "world/knowledge/tree/{}.md → world/knowledge/tree/{}.md".format(
                args.old_key, args.new_key),
            "world/knowledge/tree/{}/ → world/knowledge/tree/{}/".format(
                args.old_key, args.new_key),
            "meta/l1-pick-log.jsonl: append",
        ],
    }

    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan, "would_validate": "ok"},
                         indent=2, ensure_ascii=False))
        sys.exit(0)

    # Apply: config text first, then _tree.yaml under lock, then file moves.
    try:
        _rename_in_tree_yaml(args.old_key, args.new_key, new_summary)
    except Exception as e:
        print("Failed to rename in tree.yaml: " + str(e), file=sys.stderr)
        sys.exit(4)
    try:
        file_moves = _rename_in_world_tree(args.old_key, args.new_key, new_summary)
    except Exception as e:
        print("Failed to update _tree.yaml: " + str(e), file=sys.stderr)
        print("CRITICAL: tree.yaml l1_domains: was renamed but _tree.yaml was not. "
              "Run `git diff core/config/tree.yaml` and revert manually.",
              file=sys.stderr)
        sys.exit(5)
    try:
        _execute_file_moves(file_moves)
    except Exception as e:
        print("Failed to move files on disk: " + str(e), file=sys.stderr)
        print("CRITICAL: YAML state is renamed but on-disk files still use the "
              "old layout. Run the file moves manually:",
              file=sys.stderr)
        for old, new in file_moves:
            print("  mv '{}' '{}'".format(old, new), file=sys.stderr)
        sys.exit(6)

    _log_l1_pick(args.old_key, args.new_key, args.approved_by)

    print(json.dumps({
        "ok": True,
        "action": "L1_RENAME",
        "from": args.old_key,
        "to": args.new_key,
        "summary": new_summary,
        "approved_by": args.approved_by,
        "descendant_count": len(descendants),
        "file_moves_executed": len(file_moves),
    }, indent=2, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
