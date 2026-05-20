#!/usr/bin/env python3
"""Shared team state for multi-agent situational awareness.

Manages world/team-state.yaml — a single document both agents maintain
with strategic focus, recent completions, active blockers, and agent status.
Locked writes via _fileops prevent concurrent modification.

Subcommands:
  read              — Read the full team state or a specific field
  update            — Update a specific field (set, append, remove)
  init              — Create team-state.yaml with empty structure if missing
  in-flight         — Mark agent as in-flight on a goal (auto-stamps claimed_at)
  clear-in-flight   — Remove the in_flight block from an agent's status
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
from _fileops import locked_modify_yaml, locked_write_yaml

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

MAX_RECENT_COMPLETIONS = 50

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

def _stamp_metadata(data, agent_name):
    """Stamp last_updated + last_updated_by on every write. Called by
    modifier closures passed to locked_modify_yaml so these fields are
    inside the lock alongside the real mutation."""
    data["last_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    data["last_updated_by"] = agent_name
    return data

def _agent_name():
    return os.environ.get("MIND_AGENT", "system")

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_update(args):
    """Update a specific field in team state."""
    _validate_field_path(args.field)
    agent = args.author or _agent_name()
    field = args.field
    value = args.value

    # Parse value as JSON if possible, else keep as string
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = value

    def _modifier(state):
        # Schema-migration backfill (previously in read_state); we must do
        # it here because locked_modify_yaml reads the raw file without
        # knowing about EMPTY_STATE.
        for key, default in EMPTY_STATE.items():
            if key not in state:
                state[key] = default if not isinstance(default, (list, dict)) else type(default)()
        if args.operation == "set":
            _set_nested(state, field, parsed)
        elif args.operation == "append":
            _append_nested(state, field, parsed)
        elif args.operation == "remove":
            _remove_nested(state, field, parsed)
        # Enforce ring buffer on recent_completions
        if "recent_completions" in state:
            state["recent_completions"] = state["recent_completions"][-MAX_RECENT_COMPLETIONS:]
        return _stamp_metadata(state, agent)

    locked_modify_yaml(TEAM_STATE_PATH, _modifier, initial=dict(EMPTY_STATE))
    print(f"Updated {field}")

def cmd_init(args):
    """Initialize team-state.yaml if it doesn't exist."""
    if TEAM_STATE_PATH.exists():
        print("team-state.yaml already exists")
        return
    TEAM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    agent = args.author or _agent_name()
    state = _stamp_metadata(dict(EMPTY_STATE), agent)
    # Init is single-writer (we just checked the file doesn't exist, and
    # two racing inits would both see False before either wrote), so use
    # locked_write_yaml. If a race DID happen, the second writer would
    # just overwrite with identical empty state — no data loss.
    locked_write_yaml(TEAM_STATE_PATH, state)
    print(f"Created {TEAM_STATE_PATH}")

def cmd_in_flight(args):
    """Mark an agent as in-flight on a goal. Auto-stamps claimed_at AND last_active.

    Bumping last_active here closes the silence-detection drift where a long-running
    Phase-4 goal made the running agent look silent for hours: last_active was only
    written by iteration-close.sh do_state_update (Phase 8), so a partner reading
    team-state.yaml mid-execution saw a stale timestamp from the previous goal's
    completion. Claim is unambiguous proof of liveness — write both fields together.
    """
    _validate_agent_name(args.agent, "in-flight")
    target_agent = args.agent
    agent_author = args.author or _agent_name()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    def _modifier(state):
        for key, default in EMPTY_STATE.items():
            if key not in state:
                state[key] = default if not isinstance(default, (list, dict)) else type(default)()
        if "agent_status" not in state or not isinstance(state["agent_status"], dict):
            state["agent_status"] = {}
        if target_agent not in state["agent_status"] or not isinstance(state["agent_status"][target_agent], dict):
            state["agent_status"][target_agent] = {}
        state["agent_status"][target_agent]["in_flight"] = {
            "goal_id": args.goal_id,
            "title": args.title,
            "claimed_at": now,
            "phase": args.phase,
        }
        state["agent_status"][target_agent]["last_active"] = now
        return _stamp_metadata(state, agent_author)

    locked_modify_yaml(TEAM_STATE_PATH, _modifier, initial=dict(EMPTY_STATE))
    print(f"in_flight set for {target_agent}: {args.goal_id} phase={args.phase}")

def cmd_clear_in_flight(args):
    """Remove the in_flight block from an agent's status. Bumps last_active.

    Symmetric to cmd_in_flight: completing/releasing/skipping a goal is also
    liveness evidence. Without this, the only writer of last_active is
    iteration-close.sh do_state_update — which fires on completed goals but NOT
    on release/skip paths, leaving last_active stale after a release.
    """
    _validate_agent_name(args.agent, "clear-in-flight")
    target_agent = args.agent
    agent_author = args.author or _agent_name()
    # Track whether any mutation happened so we can print the right message
    # AFTER the locked_modify_yaml call. Using a closure variable avoids
    # doing a second read just to check.
    status = {"cleared": False}

    def _modifier(state):
        for key, default in EMPTY_STATE.items():
            if key not in state:
                state[key] = default if not isinstance(default, (list, dict)) else type(default)()
        agent_status = state.get("agent_status") or {}
        entry = agent_status.get(target_agent) or {}
        if "in_flight" in entry:
            entry.pop("in_flight")
            entry["last_active"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            agent_status[target_agent] = entry
            state["agent_status"] = agent_status
            status["cleared"] = True
            return _stamp_metadata(state, agent_author)
        # No in_flight to clear — still write nothing. Return state
        # unchanged; locked_modify_yaml will still re-write the file
        # (harmless — yaml round-trip), but we skip the metadata stamp
        # so "last_updated" doesn't move on a no-op call.
        return state

    locked_modify_yaml(TEAM_STATE_PATH, _modifier, initial=dict(EMPTY_STATE))
    if status["cleared"]:
        print(f"in_flight cleared for {target_agent}")
    else:
        print(f"in_flight already absent for {target_agent}")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Write-boundary guards. Every write path validates here rather than trusting
# callers, because a shell caller with an unset $MIND_AGENT produces an
# empty-string agent name or a "agent_status..field" dot-path that would
# silently corrupt the YAML (create a "" key under agent_status). Fail loudly
# at the boundary so no new caller can reintroduce the gap.

def _validate_field_path(field):
    if not field:
        sys.exit("team-state: empty --field")
    if any(p == "" for p in field.split(".")):
        sys.exit(
            f"team-state: malformed --field {field!r} — empty segment "
            f"(likely unset env var like $MIND_AGENT)"
        )

def _validate_agent_name(agent, cmd_name):
    if not agent:
        sys.exit(
            f"team-state {cmd_name}: empty --agent "
            f"(likely unset env var like $MIND_AGENT)"
        )

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

    # update
    update_p = sub.add_parser("update", help="Update a field in team state")
    update_p.add_argument("--field", required=True,
                          help="Dot-notation field path")
    update_p.add_argument("--value", required=True,
                          help="Value to set/append/remove (JSON or string)")
    update_p.add_argument("--operation", choices=["set", "append", "remove"],
                          default="set", help="Operation type (default: set)")
    update_p.add_argument("--author", help="Author name (defaults to MIND_AGENT)")

    # init
    init_p = sub.add_parser("init", help="Initialize team-state.yaml")
    init_p.add_argument("--author", help="Author name (defaults to MIND_AGENT)")

    # in-flight
    inflight_p = sub.add_parser("in-flight",
                                help="Mark agent as in-flight on a goal (auto-stamps claimed_at)")
    inflight_p.add_argument("--agent", required=True,
                            help="Agent name to mark in-flight (alpha, bravo, ...)")
    inflight_p.add_argument("--goal-id", required=True, help="Goal id (e.g., g-001-99)")
    inflight_p.add_argument("--title", required=True, help="Short goal title")
    inflight_p.add_argument("--phase", required=True,
                            help="Aspirations-loop phase number (e.g., 4)")
    inflight_p.add_argument("--author", help="Author name (defaults to MIND_AGENT)")

    # clear-in-flight
    clear_p = sub.add_parser("clear-in-flight",
                             help="Remove the in_flight block from an agent's status")
    clear_p.add_argument("--agent", required=True,
                         help="Agent name to clear (alpha, bravo, ...)")
    clear_p.add_argument("--author", help="Author name (defaults to MIND_AGENT)")

    return parser

def main():
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "update": cmd_update,
        "init": cmd_init,
        "in-flight": cmd_in_flight,
        "clear-in-flight": cmd_clear_in_flight,
    }
    dispatch[args.command](args)

if __name__ == "__main__":
    main()
