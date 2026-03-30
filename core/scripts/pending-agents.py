#!/usr/bin/env python3
"""Pending background agent tracker for <agent>/session/pending-agents.yaml.

Tracks dispatched background agents so the stop hook and aspirations loop
can handle idle-while-agents-work scenarios. Agents are registered before
dispatch and deregistered after result collection.

Subcommands:
  register       — Add an agent entry
  deregister     — Remove by agent_id (deletes file if list empty)
  deregister-team — Remove all agents with a given team_name
  list           — Print all registered agents (prunes stale first)
  has-pending    — Exit 0 if non-stale agents remain, exit 1 otherwise
  prune-stale    — Remove agents past their timeout_minutes
  clear          — Delete file entirely
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml

from _paths import AGENT_DIR

PENDING_PATH = AGENT_DIR / "session" / "pending-agents.yaml"

DEFAULT_TIMEOUT_MINUTES = 10


def log(msg):
    print(f"[pending-agents] {msg}", file=sys.stderr)


def read_data():
    """Read pending-agents.yaml, return dict with 'agents' list."""
    if not PENDING_PATH.exists():
        return {"agents": [], "last_updated": None}
    try:
        data = yaml.safe_load(PENDING_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"agents": [], "last_updated": None}
    if "agents" not in data or not isinstance(data["agents"], list):
        data["agents"] = []
    return data


def write_data(data):
    """Atomic write to pending-agents.yaml."""
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["last_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    tmp = PENDING_PATH.with_suffix(".tmp")
    tmp.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(PENDING_PATH))


def delete_file():
    """Remove the tracking file entirely."""
    PENDING_PATH.unlink(missing_ok=True)


def prune_stale(agents):
    """Remove agents past their timeout. Returns (kept, pruned_count)."""
    now = datetime.now()
    kept = []
    pruned = 0
    for agent in agents:
        dispatched_str = agent.get("dispatched_at", "")
        timeout = agent.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES)
        try:
            dispatched = datetime.fromisoformat(dispatched_str)
            if now - dispatched > timedelta(minutes=timeout):
                pruned += 1
                continue
        except (ValueError, TypeError):
            # Bad timestamp — treat as stale
            pruned += 1
            continue
        kept.append(agent)
    return kept, pruned


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_register(args):
    """Add an agent entry to the tracking file."""
    data = read_data()
    # Prevent duplicate registration
    for agent in data["agents"]:
        if agent.get("agent_id") == args.id:
            log(f"already registered: {args.id}")
            return
    entry = {
        "agent_id": args.id,
        "team_name": args.team,
        "goal_id": args.goal,
        "dispatched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "purpose": args.purpose,
        "timeout_minutes": args.timeout,
    }
    data["agents"].append(entry)
    write_data(data)
    log(f"registered: {args.id} (team={args.team}, goal={args.goal}, timeout={args.timeout}m)")


def cmd_deregister(args):
    """Remove an agent by agent_id. Delete file if list becomes empty."""
    data = read_data()
    before = len(data["agents"])
    data["agents"] = [a for a in data["agents"] if a.get("agent_id") != args.id]
    after = len(data["agents"])
    if before == after:
        log(f"not found: {args.id}")
        return
    if not data["agents"]:
        delete_file()
        log(f"deregistered: {args.id} (no agents remaining, file deleted)")
    else:
        write_data(data)
        log(f"deregistered: {args.id} ({after} remaining)")


def cmd_deregister_team(args):
    """Remove all agents with a given team_name."""
    data = read_data()
    before = len(data["agents"])
    data["agents"] = [a for a in data["agents"] if a.get("team_name") != args.team]
    after = len(data["agents"])
    removed = before - after
    if removed == 0:
        log(f"no agents found for team: {args.team}")
        return
    if not data["agents"]:
        delete_file()
        log(f"deregistered {removed} agents from team {args.team} (file deleted)")
    else:
        write_data(data)
        log(f"deregistered {removed} agents from team {args.team} ({after} remaining)")


def cmd_list(args):
    """Print all registered agents (prunes stale before returning)."""
    data = read_data()
    agents = data.get("agents", [])

    # Prune stale agents before returning — same as has-pending.
    # Without this, Phase -0.5a could try to collect from agents that timed out.
    if agents:
        kept, pruned = prune_stale(agents)
        if pruned > 0:
            if kept:
                data["agents"] = kept
                write_data(data)
            else:
                delete_file()
            log(f"list: pruned {pruned} stale agent(s), {len(kept)} remaining")
            agents = kept

    if args.json:
        # Required: when all agents were stale, delete_file() runs but data["agents"]
        # still holds the original list. This line is the single source of truth for output.
        data["agents"] = agents
        print(json.dumps(data, indent=2, default=str))
    else:
        if not agents:
            print("No pending agents.")
            return
        for a in agents:
            print(f"  {a.get('agent_id', '?')} | team={a.get('team_name', '?')} | "
                  f"goal={a.get('goal_id', '?')} | dispatched={a.get('dispatched_at', '?')} | "
                  f"timeout={a.get('timeout_minutes', '?')}m")
            if a.get("purpose"):
                print(f"    purpose: {a['purpose']}")


def cmd_has_pending(args):
    """Check for non-stale pending agents. Exit 0 if any, exit 1 if none."""
    data = read_data()
    agents = data.get("agents", [])
    if not agents:
        sys.exit(1)
    kept, pruned = prune_stale(agents)
    if pruned > 0:
        if kept:
            data["agents"] = kept
            write_data(data)
        else:
            delete_file()
        log(f"pruned {pruned} stale agent(s), {len(kept)} remaining")
    if kept:
        sys.exit(0)
    else:
        sys.exit(1)


def cmd_prune_stale(args):
    """Remove agents past their timeout_minutes."""
    data = read_data()
    agents = data.get("agents", [])
    if not agents:
        print("No agents to prune.")
        return
    kept, pruned = prune_stale(agents)
    if pruned == 0:
        print("No stale agents found.")
        return
    if kept:
        data["agents"] = kept
        write_data(data)
    else:
        delete_file()
    print(f"Pruned {pruned} stale agent(s), {len(kept)} remaining.")


def cmd_clear(args):
    """Delete the tracking file entirely."""
    if PENDING_PATH.exists():
        delete_file()
        log("cleared all pending agents")
    else:
        log("no pending agents file to clear")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Pending background agent tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    # register
    reg = sub.add_parser("register", help="Register a background agent")
    reg.add_argument("--id", required=True, help="Agent identifier")
    reg.add_argument("--team", required=True, help="Team name")
    reg.add_argument("--goal", required=True, help="Goal ID this agent serves")
    reg.add_argument("--purpose", default="", help="Short description of agent's task")
    reg.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MINUTES,
                     help=f"Staleness timeout in minutes (default: {DEFAULT_TIMEOUT_MINUTES})")

    # deregister
    dereg = sub.add_parser("deregister", help="Remove an agent by ID")
    dereg.add_argument("--id", required=True, help="Agent identifier to remove")

    # deregister-team
    dereg_team = sub.add_parser("deregister-team", help="Remove all agents from a team")
    dereg_team.add_argument("--team", required=True, help="Team name")

    # list
    lst = sub.add_parser("list", help="List all registered agents (prunes stale first)")
    lst.add_argument("--json", action="store_true", help="Output as JSON")

    # has-pending
    sub.add_parser("has-pending", help="Exit 0 if non-stale agents exist, exit 1 otherwise")

    # prune-stale
    sub.add_parser("prune-stale", help="Remove agents past their timeout")

    # clear
    sub.add_parser("clear", help="Delete tracking file entirely")

    return parser


DISPATCH = {
    "register": cmd_register,
    "deregister": cmd_deregister,
    "deregister-team": cmd_deregister_team,
    "list": cmd_list,
    "has-pending": cmd_has_pending,
    "prune-stale": cmd_prune_stale,
    "clear": cmd_clear,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    fn = DISPATCH.get(args.command)
    if fn is None:
        parser.error(f"Unknown command: {args.command}")
    fn(args)


if __name__ == "__main__":
    main()
