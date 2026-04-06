#!/usr/bin/env python3
"""Reasoning snapshot — proactive context persistence for tight-zone checkpointing.

When context enters tight zone (>=65%), the LLM proactively writes a synthesis of its
current reasoning state to a well-known file. This is higher fidelity than WM slots
because it's the LLM's own synthesized understanding, written while context is still fresh.

The postcompact restore reads this file and includes it in the injected message.

Subcommands:
  write — Write a reasoning snapshot from stdin JSON/YAML
  read  — Read the current snapshot
  clear — Delete the snapshot file
"""

import argparse
import json
import os
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml

from _paths import AGENT_DIR

SNAPSHOT_PATH = AGENT_DIR / "session" / "reasoning-snapshot.yaml"


def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def cmd_write(args):
    """Write a reasoning snapshot from stdin."""
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERROR: No input on stdin", file=sys.stderr)
        sys.exit(1)

    # Try JSON first, then YAML
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            print(f"ERROR: Invalid JSON/YAML: {e}", file=sys.stderr)
            sys.exit(1)

    if not isinstance(data, dict):
        print("ERROR: Snapshot must be a dict/object", file=sys.stderr)
        sys.exit(1)

    # Auto-add metadata
    data["snapshot_at"] = now_iso()

    # Read context budget if available
    budget_path = AGENT_DIR / "session" / "context-budget.json"
    if budget_path.exists():
        try:
            budget = json.loads(budget_path.read_text(encoding="utf-8"))
            data["context_used_pct"] = budget.get("used_pct")
            data["context_zone"] = budget.get("zone")
        except Exception:
            pass

    # Atomic write
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_PATH.with_suffix(".tmp")
    tmp.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(SNAPSHOT_PATH))

    print(f"ok: snapshot written at {data['snapshot_at']}")


def cmd_read(args):
    """Read the current reasoning snapshot."""
    if not SNAPSHOT_PATH.exists():
        print("null")
        return

    data = yaml.safe_load(SNAPSHOT_PATH.read_text(encoding="utf-8")) or {}
    if args.json:
        print(json.dumps(data, ensure_ascii=False, default=str))
    else:
        yaml.dump(data, sys.stdout, default_flow_style=False, allow_unicode=True, sort_keys=False)


def cmd_clear(args):
    """Delete the snapshot file."""
    if SNAPSHOT_PATH.exists():
        SNAPSHOT_PATH.unlink()
        print("ok: snapshot cleared")
    else:
        print("no snapshot to clear")


def build_parser():
    parser = argparse.ArgumentParser(description="Reasoning snapshot — tight-zone context persistence")
    sub = parser.add_subparsers(dest="command", required=True)

    # write
    sub.add_parser("write", help="Write snapshot from stdin JSON/YAML")

    # read
    p_read = sub.add_parser("read", help="Read current snapshot")
    p_read.add_argument("--json", action="store_true", help="Output as JSON")

    # clear
    sub.add_parser("clear", help="Delete snapshot file")

    return parser


DISPATCH = {
    "write": cmd_write,
    "read": cmd_read,
    "clear": cmd_clear,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
