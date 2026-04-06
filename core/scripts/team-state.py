#!/usr/bin/env python3
"""Shared team state for multi-agent situational awareness.

Manages world/team-state.yaml — a single document both agents maintain
with strategic focus, recent completions, active blockers, and agent status.
Locked writes via _fileops prevent concurrent modification.

Subcommands:
  read     — Read the full team state or a specific field
  update   — Update a specific field (set, append, remove)
  init     — Create team-state.yaml with empty structure if missing
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml

from _paths import WORLD_DIR
from _fileops import locked_write_yaml

TEAM_STATE_PATH = WORLD_DIR / "team-state.yaml"

EMPTY_STATE = {
    "last_updated": None,
    "last_updated_by": None,
    "strategic_focus": {
        "primary": None,
        "rationale": None,
        "set_by": None,
        "set_at": None,
        "acknowledged_by": [],
    },
    "active_blockers": [],
    "recent_completions": [],
    "agent_status": {},
    "critical_blockers": [],
}

MAX_RECENT_COMPLETIONS = 10


def read_state():
    """Read the current team state, returning empty structure if missing."""
    if not TEAM_STATE_PATH.exists():
        return dict(EMPTY_STATE)
    with open(TEAM_STATE_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        return dict(EMPTY_STATE)
    # Schema migration: backfill any keys added to EMPTY_STATE since file was created
    for key, default in EMPTY_STATE.items():
        if key not in data:
            data[key] = default if not isinstance(default, (list, dict)) else type(default)()
    return data


def write_state(data, agent_name):
    """Write team state with locking, history, and changelog."""
    data["last_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    data["last_updated_by"] = agent_name
    locked_write_yaml(TEAM_STATE_PATH, data)


def _agent_name():
    return os.environ.get("AYOAI_AGENT", "system")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_read(args):
    """Read team state — full or a specific field."""
    state = read_state()
    if args.field:
        # Dot-notation field access: e.g., "strategic_focus.primary" or "agent_status.alpha"
        parts = args.field.split(".")
        val = state
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                val = None
                break
        if args.json_output:
            print(json.dumps(val, ensure_ascii=False, default=str))
        else:
            if isinstance(val, (dict, list)):
                print(yaml.dump(val, default_flow_style=False, allow_unicode=True).rstrip())
            elif val is not None:
                print(val)
    else:
        if args.json_output:
            print(json.dumps(state, ensure_ascii=False, default=str))
        else:
            print(yaml.dump(state, default_flow_style=False, allow_unicode=True).rstrip())


def cmd_update(args):
    """Update a specific field in team state."""
    state = read_state()
    agent = args.author or _agent_name()
    field = args.field
    value = args.value

    # Parse value as JSON if possible, else keep as string
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = value

    if args.operation == "set":
        _set_nested(state, field, parsed)
    elif args.operation == "append":
        _append_nested(state, field, parsed)
    elif args.operation == "remove":
        _remove_nested(state, field, parsed)

    # Enforce ring buffer on recent_completions
    if "recent_completions" in state:
        state["recent_completions"] = state["recent_completions"][-MAX_RECENT_COMPLETIONS:]

    write_state(state, agent)
    print(f"Updated {field}")


def cmd_init(args):
    """Initialize team-state.yaml if it doesn't exist."""
    if TEAM_STATE_PATH.exists():
        print("team-state.yaml already exists")
        return
    TEAM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    agent = args.author or _agent_name()
    state = dict(EMPTY_STATE)
    write_state(state, agent)
    print(f"Created {TEAM_STATE_PATH}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_nested(data, field, value):
    """Set a value at a dot-notation path, creating intermediate dicts."""
    parts = field.split(".")
    target = data
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value


def _append_nested(data, field, value):
    """Append a value to a list at a dot-notation path."""
    parts = field.split(".")
    target = data
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    key = parts[-1]
    if key not in target or not isinstance(target[key], list):
        target[key] = []
    target[key].append(value)


def _remove_nested(data, field, value):
    """Remove an item from a list at a dot-notation path (by id or value match)."""
    parts = field.split(".")
    target = data
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            return
        target = target[part]
    key = parts[-1]
    if key not in target or not isinstance(target[key], list):
        return
    lst = target[key]
    # Remove by id if value is a string and items have id fields
    if isinstance(value, str):
        target[key] = [item for item in lst
                       if not (isinstance(item, dict) and item.get("id") == value)
                       and item != value]
    else:
        target[key] = [item for item in lst if item != value]


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Shared team state management")
    sub = parser.add_subparsers(dest="command", required=True)

    # read
    read_p = sub.add_parser("read", help="Read team state")
    read_p.add_argument("--field", help="Dot-notation field path (e.g., strategic_focus.primary)")
    read_p.add_argument("--json", dest="json_output", action="store_true",
                        help="Output as JSON")

    # update
    update_p = sub.add_parser("update", help="Update a field in team state")
    update_p.add_argument("--field", required=True,
                          help="Dot-notation field path")
    update_p.add_argument("--value", required=True,
                          help="Value to set/append/remove (JSON or string)")
    update_p.add_argument("--operation", choices=["set", "append", "remove"],
                          default="set", help="Operation type (default: set)")
    update_p.add_argument("--author", help="Author name (defaults to AYOAI_AGENT)")

    # init
    init_p = sub.add_parser("init", help="Initialize team-state.yaml")
    init_p.add_argument("--author", help="Author name (defaults to AYOAI_AGENT)")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "read": cmd_read,
        "update": cmd_update,
        "init": cmd_init,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
