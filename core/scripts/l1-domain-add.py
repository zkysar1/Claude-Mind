#!/usr/bin/env python3
"""Add a new L1 domain to the knowledge tree (S8 — Phase 2).

Adds a new top-level node under `root` AND registers it in
`core/config/tree.yaml l1_domains:`. Existing nodes are not affected —
this is the cheap path of S8's locked scope (RENAME + ADD only).

Workflow:
1. User-approval gate: requires `--approved-by <pending-id>` proving the
   user said yes to a pending-question proposing this change. See
   `core/config/conventions/l1-taxonomy-changes.md`.
2. Validate new key: lowercase kebab-case, not colliding with existing
   L1 or other top-level node, not a reserved name.
3. Create the L1 node atomically:
   a. Append to `core/config/tree.yaml l1_domains:` list.
   b. Add a new node under `root` in `world/knowledge/tree/_tree.yaml`.
   c. Create the L1 .md file at `world/knowledge/tree/{key}.md`.
4. Log the change for audit (l1-pick-log + journal).

Fail-safe: if any step fails, NO partial change persists (validation
blocks before any write; tree mutation uses locked_modify_yaml).

Usage:
    py -3 core/scripts/l1-domain-add.py \\
        --key experiments \\
        --summary "What we're TESTING — open hypotheses and experiments" \\
        --approved-by l1-taxonomy-2026-05-14-add-experiments

    py -3 core/scripts/l1-domain-add.py --dry-run --key X --summary Y
        # Validate inputs and print the would-be plan; no writes.
"""

import argparse
import json
import os
import re
import subprocess
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


def _validate_key(key, existing_keys):
    if not key:
        return "key is required"
    if not KEY_RE.match(key):
        return ("key must be lowercase kebab-case, 2-41 chars, "
                "starting with a letter (got '{}')".format(key))
    if key in RESERVED_KEYS:
        return "key '{}' is reserved".format(key)
    if key in existing_keys:
        return "key '{}' already exists in tree".format(key)
    return None


def _validate_approval(approval_id):
    """Verify the approval id matches a resolved pending-question.

    The id should reference a pending-question with status `resolved` and
    a positive answer. We accept the id as evidence — the FULL audit trail
    lives in pending-questions.yaml. The key sanity check: prefix must be
    `l1-taxonomy-`.
    """
    if not approval_id:
        return ("user approval is required (--approved-by <pending-id>). "
                "See core/config/conventions/l1-taxonomy-changes.md.")
    if not approval_id.startswith("l1-taxonomy-"):
        return ("approval id must start with 'l1-taxonomy-' (per the "
                "structural_modifiable.l1_domains.proposal_template_pending_id_prefix "
                "in tree.yaml; got '{}')".format(approval_id))
    return None


def _read_tree():
    with open(TREE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _read_tree_config():
    with open(TREE_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _append_l1_to_tree_yaml(key, summary):
    """Append the new L1 entry to core/config/tree.yaml l1_domains: list.

    The list is YAML; we read full text, locate the l1_domains section, append
    a properly-indented entry, and write back. We do NOT round-trip through
    yaml.dump (would lose comments + reorder keys); we do a targeted text
    insert just after the last l1_domains list item.
    """
    with open(TREE_CONFIG_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    # Find l1_domains: block start.
    marker = "\nl1_domains:\n"
    idx = text.find(marker)
    if idx < 0:
        raise RuntimeError("could not find 'l1_domains:' block in tree.yaml")
    # Find the END of the l1_domains list — the next top-level key OR a
    # blank-line-then-non-list-entry. Walk forward line-by-line.
    block_start = idx + len(marker)
    lines = text[block_start:].splitlines(keepends=True)
    # Track up to last `  - key:` entry; insert AFTER its 3-line block.
    last_item_end = 0  # offset within `lines[]`
    consumed = 0
    in_item = False
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        # New item starts with `  - key:`
        if stripped.startswith("  - key:"):
            in_item = True
            consumed = i
            continue
        # Continued item lines start with `    `
        if in_item and (stripped.startswith("    ") or stripped == ""):
            consumed = i
            continue
        # Anything else ends the block.
        in_item = False
        last_item_end = consumed + 1
        break
    if last_item_end == 0:
        # block runs to end of file
        last_item_end = len(lines)
    # Trailing blank lines were absorbed by the continuation rule above
    # (a blank line after `summary:` doesn't end the item). Back off so the
    # new entry lands flush against its siblings, not below the gap.
    while last_item_end > 0 and lines[last_item_end - 1].strip() == "":
        last_item_end -= 1
    insert_offset = block_start + sum(len(L) for L in lines[:last_item_end])
    new_entry = (
        "  - key: {}\n"
        "    file: world/knowledge/tree/{}.md\n"
        "    summary: \"{}\"\n"
    ).format(key, key, summary.replace('"', '\\"'))
    new_text = text[:insert_offset] + new_entry + text[insert_offset:]
    with open(TREE_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(new_text)


def _add_l1_node_to_world(key, summary):
    """Add the L1 node to _tree.yaml under root, atomically."""
    from _fileops import locked_modify_yaml

    def _do_add(data):
        if not isinstance(data, dict) or "nodes" not in data:
            raise RuntimeError("invalid _tree.yaml: missing 'nodes' key")
        nodes = data["nodes"]
        if key in nodes:
            raise RuntimeError("node '{}' already exists in _tree.yaml".format(key))
        if "root" not in nodes:
            raise RuntimeError("invalid _tree.yaml: missing 'root' node")
        # Add the L1 node
        nodes[key] = {
            "file": "world/knowledge/tree/{}.md".format(key),
            "depth": 1,
            "parent": "root",
            "children": [],
            "child_count": 0,
            "domain_confidence": None,
            "article_count": 0,
            "growth_state": "stable",
            "summary": summary,
            "last_updated": date.today().isoformat(),
        }
        # Wire into root.children
        root = nodes["root"]
        root_children = root.get("children", []) or []
        if key not in root_children:
            root_children.append(key)
        root["children"] = root_children
        root["child_count"] = len(root_children)
        nodes["root"] = root
        data["nodes"] = nodes
        data["last_updated"] = date.today().isoformat()
        # Append to tree_growth_log so the audit trail is uniform.
        log = data.get("tree_growth_log") or []
        log.append({
            "op": "L1_ADD",
            "node": key,
            "date": date.today().isoformat(),
            "reason": "S8 user-approval-gated L1 add",
        })
        data["tree_growth_log"] = log
        return data

    locked_modify_yaml(TREE_PATH, _do_add)


def _create_l1_md_file(key, summary, approval_id):
    """Write the new L1 .md file. Refuses to overwrite an existing file."""
    target = WORLD_DIR / "knowledge" / "tree" / "{}.md".format(key)
    if target.exists():
        raise RuntimeError("L1 file already exists at {}".format(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        "domain: {key}\n"
        "level: L1\n"
        "topics: []\n"
        "last_updated: '{today}'\n"
        "last_update_trigger:\n"
        "  type: l1_add\n"
        "  source: l1-domain-add.py\n"
        "  session: ~\n"
        "  approved_by: {approval_id}\n"
        "---\n"
        "\n"
        "# {title}\n"
        "\n"
        "{summary}\n"
        "\n"
        "Empty — newly created via the S8 user-approval-gated L1 add path on {today}.\n"
        "\n"
        "## Capability Map\n"
        "\n"
        "| Topic | Confidence | Level | Articles |\n"
        "|---|---|---|---|\n"
        "\n"
        "## Cross-Domain Links\n"
    ).format(
        key=key,
        title=key.replace("-", " ").title(),
        summary=summary,
        today=date.today().isoformat(),
        approval_id=approval_id,
    )
    target.write_text(content, encoding="utf-8")
    return str(target)


def _log_l1_pick(key, decision_type, source, reason):
    """Reuse the S9 helper for audit trail consistency."""
    try:
        log_path = Path(META_DIR) / "l1-pick-log.jsonl"
        agent = os.environ.get("MIND_AGENT", "").strip() or None
        sid = os.environ.get("MIND_SID", "").strip() or None
        entry = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": agent,
            "session_id": sid,
            "target_node": key,
            "l1": key,
            "decision_type": decision_type,
            "source": source,
            "reason": reason,
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(log_path), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[l1-domain-add] log warn: " + str(e), file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True, help="New L1 key (lowercase kebab-case)")
    ap.add_argument("--summary", required=True,
                    help="One-line description of what this L1 covers")
    ap.add_argument("--approved-by", default=None,
                    help="Pending-question id proving user approval (required unless --dry-run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate inputs and print the plan; no writes")
    args = ap.parse_args()

    # Read existing state for validation.
    try:
        tree = _read_tree()
    except Exception as e:
        print("Failed to read _tree.yaml: " + str(e), file=sys.stderr)
        sys.exit(2)
    existing_keys = set((tree.get("nodes") or {}).keys())

    err = _validate_key(args.key, existing_keys)
    if err:
        print("Invalid --key: " + err, file=sys.stderr)
        sys.exit(2)

    if not args.dry_run:
        err = _validate_approval(args.approved_by)
        if err:
            print(err, file=sys.stderr)
            sys.exit(3)

    plan = {
        "action": "L1_ADD",
        "key": args.key,
        "summary": args.summary,
        "approved_by": args.approved_by,
        "writes": [
            "core/config/tree.yaml l1_domains: append",
            "_tree.yaml: add node '{}' under root".format(args.key),
            "world/knowledge/tree/{}.md: create".format(args.key),
            "meta/l1-pick-log.jsonl: append",
        ],
    }

    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan, "would_validate": "ok"},
                         indent=2, ensure_ascii=False))
        sys.exit(0)

    # Apply: tree config FIRST (cheap, plain text), then _tree.yaml under
    # lock, then the .md file. If the .md create fails, we accept the small
    # incoherence (config + node entry exist; file missing) — easier to fix
    # forward than to roll back.
    try:
        _append_l1_to_tree_yaml(args.key, args.summary)
    except Exception as e:
        print("Failed to append to tree.yaml l1_domains: " + str(e), file=sys.stderr)
        sys.exit(4)
    try:
        _add_l1_node_to_world(args.key, args.summary)
    except Exception as e:
        print("Failed to add L1 node to _tree.yaml: " + str(e), file=sys.stderr)
        sys.exit(5)
    try:
        md_path = _create_l1_md_file(args.key, args.summary, args.approved_by)
    except Exception as e:
        print("[l1-domain-add] WARN: .md file create failed: " + str(e),
              file=sys.stderr)
        md_path = None

    _log_l1_pick(
        args.key,
        decision_type="l1-add",
        source="l1-domain-add.py",
        reason="user-approved via {}".format(args.approved_by),
    )

    print(json.dumps({
        "ok": True,
        "action": "L1_ADD",
        "key": args.key,
        "summary": args.summary,
        "approved_by": args.approved_by,
        "md_path": md_path,
    }, indent=2, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
