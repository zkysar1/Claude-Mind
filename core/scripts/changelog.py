#!/usr/bin/env python3
"""Changelog reader for world/changelog.jsonl.

The changelog is auto-appended by _fileops.py locked write functions.
This script provides read/query operations.

Subcommands:
  read   — Read recent changelog entries
  stats  — Show per-agent and per-file statistics
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import WORLD_DIR

CHANGELOG_PATH = WORLD_DIR / "changelog.jsonl"


def read_entries():
    """Read all changelog entries."""
    if not CHANGELOG_PATH.exists():
        return []
    entries = []
    with open(CHANGELOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))
    return entries


def parse_duration(duration_str):
    """Parse a duration string like '1h', '30m', '2d' into a timedelta."""
    if not duration_str:
        return None
    unit = duration_str[-1].lower()
    try:
        value = int(duration_str[:-1])
    except ValueError:
        return None
    if unit == "m":
        return timedelta(minutes=value)
    elif unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    return None


def cmd_read(args):
    """Read recent changelog entries."""
    entries = read_entries()

    # Filter by --since
    if args.since:
        delta = parse_duration(args.since)
        if delta:
            cutoff = datetime.now() - delta
            entries = [e for e in entries
                       if datetime.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%S") >= cutoff]

    # Filter by --agent
    if args.agent:
        entries = [e for e in entries if e.get("agent") == args.agent]

    # Filter by --file
    if args.file:
        entries = [e for e in entries if args.file in e.get("file", "")]

    # Limit by --last
    if args.last:
        entries = entries[-args.last:]

    if args.json_output:
        for e in entries:
            print(json.dumps(e, ensure_ascii=False))
    else:
        for e in entries:
            summary = f" — {e['summary']}" if e.get("summary") else ""
            lines = f" ({e['lines_changed']} lines)" if e.get("lines_changed") else ""
            print(f"[{e['timestamp']}] {e.get('agent', '?')} {e.get('action', '?')} {e.get('file', '?')}{lines}{summary}")


def cmd_stats(args):
    """Show changelog statistics."""
    entries = read_entries()

    if not entries:
        print("No changelog entries.")
        return

    # Filter by --since
    if args.since:
        delta = parse_duration(args.since)
        if delta:
            cutoff = datetime.now() - delta
            entries = [e for e in entries
                       if datetime.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%S") >= cutoff]

    agent_counts = Counter(e.get("agent", "unknown") for e in entries)
    file_counts = Counter(e.get("file", "unknown") for e in entries)
    action_counts = Counter(e.get("action", "unknown") for e in entries)

    period = f" (last {args.since})" if args.since else " (all time)"
    print(f"Changelog stats{period}: {len(entries)} entries")
    print()

    print("By agent:")
    for agent, count in agent_counts.most_common():
        print(f"  {agent}: {count}")
    print()

    print("By action:")
    for action, count in action_counts.most_common():
        print(f"  {action}: {count}")
    print()

    print("Top files:")
    for file, count in file_counts.most_common(10):
        print(f"  {file}: {count}")


def build_parser():
    parser = argparse.ArgumentParser(description="Changelog reader")
    sub = parser.add_subparsers(dest="command", required=True)

    # read
    read_p = sub.add_parser("read", help="Read recent changelog entries")
    read_p.add_argument("--since", help="Duration filter (e.g., 1h, 30m, 2d)")
    read_p.add_argument("--agent", help="Filter by agent name")
    read_p.add_argument("--file", help="Filter by file path (substring match)")
    read_p.add_argument("--last", type=int, help="Show only last N entries")
    read_p.add_argument("--json", dest="json_output", action="store_true",
                        help="Output as JSONL")

    # stats
    stats_p = sub.add_parser("stats", help="Show changelog statistics")
    stats_p.add_argument("--since", help="Duration filter (e.g., 1h, 30m, 2d)")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "read": cmd_read,
        "stats": cmd_stats,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
