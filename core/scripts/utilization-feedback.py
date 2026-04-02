#!/usr/bin/env python3
"""Utilization feedback engine — single-command replacement for Phase 4.26.

Reads <agent>/session/retrieval-session.json (auto-written by retrieve.py) and
applies utilization feedback to all retrieved tree nodes and supplementary items.

Usage:
    utilization-feedback.sh --goal <goal-id> --helpful "node1,node2,rb-001"
    utilization-feedback.sh --goal <goal-id> --all-helpful
    utilization-feedback.sh --goal <goal-id> --all-noise

The --helpful flag marks named items as helpful and everything else as noise.
The --all-helpful and --all-noise flags apply uniformly. The hook fallback
(utilization-gate.sh) uses --all-noise as the conservative default.

Idempotent: if utilization_pending is already false, exits with no changes.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import PROJECT_ROOT, WORLD_DIR, AGENT_DIR, CORE_ROOT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TREE_PATH = WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"
SESSION_PATH = AGENT_DIR / "session" / "retrieval-session.json" if AGENT_DIR else None


def read_yaml(path):
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def write_yaml(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=None, sort_keys=False,
                  allow_unicode=True, width=200)
    os.replace(str(tmp), str(p))


def now_str():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Tree node batch update (single atomic read-modify-write)
# ---------------------------------------------------------------------------

def _recompute_utility_ratio(node):
    """Canonical formula lives in tree.py cmd_increment (line ~739).
    Must stay in sync: utility_ratio = times_helpful / max(retrieval_count, 1)."""
    rc = node.get("retrieval_count", 0)
    th = node.get("times_helpful", 0)
    node["utility_ratio"] = round(th / max(rc, 1), 4)


def update_tree_nodes(helpful_keys, noise_keys):
    """Increment times_helpful/times_noise and recompute utility_ratio atomically."""
    if not TREE_PATH.exists():
        return 0, 0

    tree = read_yaml(TREE_PATH)
    nodes = tree.get("nodes", {})
    h_count = 0
    n_count = 0

    for key in helpful_keys:
        if key in nodes:
            node = nodes[key]
            node["times_helpful"] = node.get("times_helpful", 0) + 1
            _recompute_utility_ratio(node)
            h_count += 1

    for key in noise_keys:
        if key in nodes:
            node = nodes[key]
            node["times_noise"] = node.get("times_noise", 0) + 1
            _recompute_utility_ratio(node)
            n_count += 1

    if h_count > 0 or n_count > 0:
        write_yaml(TREE_PATH, tree)

    return h_count, n_count


# ---------------------------------------------------------------------------
# Supplementary item feedback (reasoning bank + guardrails)
# ---------------------------------------------------------------------------

def increment_supplementary(item_id, item_type, field):
    """Call the appropriate increment script for a supplementary item."""
    if item_type == "reasoning_bank":
        script = str(CORE_ROOT / "scripts" / "reasoning-bank-increment.sh")
    elif item_type == "guardrail":
        script = str(CORE_ROOT / "scripts" / "guardrails-increment.sh")
    else:
        return  # pattern_signatures don't have utilization increment scripts

    try:
        subprocess.run(
            ["bash", script, item_id, f"utilization.{field}"],
            capture_output=True, timeout=10, cwd=str(PROJECT_ROOT)
        )
    except Exception as e:
        print(f"[utilization-feedback] Warning: increment failed for {item_id}: {e}",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Apply utilization feedback from retrieval-session.json"
    )
    parser.add_argument("--goal", required=True, help="Goal ID (must match session file)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--helpful", type=str, default=None,
                       help="Comma-separated IDs of helpful items (others marked noise)")
    group.add_argument("--all-helpful", action="store_true",
                       help="Mark all items as helpful")
    group.add_argument("--all-noise", action="store_true",
                       help="Mark all items as noise (hook fallback)")
    args = parser.parse_args()

    if not SESSION_PATH or not SESSION_PATH.exists():
        print(json.dumps({"status": "no_session_file", "message": "No retrieval-session.json found"}))
        sys.exit(0)

    with open(SESSION_PATH, "r", encoding="utf-8") as f:
        session = json.load(f)

    # Validate goal ID matches (prevents stale feedback)
    if session.get("goal_id") != args.goal:
        print(json.dumps({
            "status": "goal_mismatch",
            "session_goal": session.get("goal_id"),
            "requested_goal": args.goal,
        }))
        sys.exit(0)

    # Idempotency guard
    if not session.get("utilization_pending", False):
        print(json.dumps({
            "status": "already_processed",
            "completed_at": session.get("utilization_completed_at"),
        }))
        sys.exit(0)

    # Determine which items are helpful vs noise
    tree_nodes = session.get("tree_nodes_loaded", [])
    supp_items = session.get("supplementary_items", [])
    all_ids = set(tree_nodes + [s["id"] for s in supp_items])

    if args.all_helpful:
        helpful_ids = all_ids
    elif args.all_noise:
        helpful_ids = set()
    else:  # --helpful "key1,key2"
        helpful_ids = {k.strip() for k in args.helpful.split(",") if k.strip()}

    # Partition tree nodes
    tree_helpful = [k for k in tree_nodes if k in helpful_ids]
    tree_noise = [k for k in tree_nodes if k not in helpful_ids]

    # Apply tree node feedback (atomic batch)
    h_count, n_count = update_tree_nodes(tree_helpful, tree_noise)

    # Apply supplementary item feedback
    supp_h = 0
    supp_n = 0
    for item in supp_items:
        iid = item["id"]
        itype = item["type"]
        if iid in helpful_ids:
            increment_supplementary(iid, itype, "times_helpful")
            supp_h += 1
        else:
            increment_supplementary(iid, itype, "times_noise")
            supp_n += 1

    # Mark as processed (atomic write)
    session["utilization_pending"] = False
    session["utilization_completed_at"] = now_str()
    tmp = Path(str(SESSION_PATH) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)
    os.replace(str(tmp), str(SESSION_PATH))

    result = {
        "status": "completed",
        "goal_id": args.goal,
        "tree_nodes": {"helpful": h_count, "noise": n_count},
        "supplementary": {"helpful": supp_h, "noise": supp_n},
    }
    print(json.dumps(result, indent=2))
    print(f"[utilization-feedback] {args.goal}: {h_count} tree helpful, {n_count} tree noise, "
          f"{supp_h} supp helpful, {supp_n} supp noise", file=sys.stderr)


if __name__ == "__main__":
    main()
