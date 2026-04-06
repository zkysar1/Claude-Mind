#!/usr/bin/env python3
"""Execution diary — append-only reasoning breadcrumb trail.

Captures key decision points, failures, findings, and approach changes during
goal execution. Unlike WM slots (which are overwritten), the diary is cumulative.
It survives autocompact because it's on disk, and the postcompact restore reads
the last N entries to inject into fresh context.

Subcommands:
  append  — Add a diary entry from stdin JSON
  read    — Read recent entries (--limit, --since, --goal)
  summary — Generate compressed summary for post-compact injection
  trim    — Remove entries older than N hours
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR

DIARY_PATH = AGENT_DIR / "session" / "execution-diary.jsonl"

VALID_ENTRY_TYPES = {
    "decision", "failure", "finding", "approach_change",
    "observation", "state_update",
}


def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def read_entries():
    """Read all diary entries."""
    if not DIARY_PATH.exists():
        return []
    entries = []
    with open(DIARY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def cmd_append(args):
    """Add a diary entry from stdin JSON."""
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERROR: No input on stdin", file=sys.stderr)
        sys.exit(1)

    try:
        entry = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(entry, dict):
        print("ERROR: Entry must be a JSON object", file=sys.stderr)
        sys.exit(1)

    # Validate entry_type
    etype = entry.get("entry_type", "")
    if etype and etype not in VALID_ENTRY_TYPES:
        print(f"WARNING: Unknown entry_type '{etype}' — valid types: {', '.join(sorted(VALID_ENTRY_TYPES))}",
              file=sys.stderr)

    # Auto-add timestamp if missing
    if "timestamp" not in entry:
        entry["timestamp"] = now_iso()

    # Ensure required fields
    if "content" not in entry:
        print("ERROR: Entry must have 'content' field", file=sys.stderr)
        sys.exit(1)

    # Append atomically
    DIARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DIARY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    print(f"ok: {entry.get('entry_type', '?')} @ {entry['timestamp']}")


def cmd_read(args):
    """Read recent diary entries."""
    entries = read_entries()

    # Filter by goal
    if args.goal:
        entries = [e for e in entries if e.get("goal_id") == args.goal]

    # Filter by time
    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since)
            entries = [e for e in entries if _parse_ts(e) and _parse_ts(e) >= since_dt]
        except ValueError:
            print(f"WARNING: Invalid --since timestamp: {args.since}", file=sys.stderr)

    # Apply limit (from the end)
    if args.limit and args.limit > 0:
        entries = entries[-args.limit:]

    if args.json:
        print(json.dumps(entries, ensure_ascii=False, default=str))
    else:
        for entry in entries:
            ts = entry.get("timestamp", "")
            time_part = ts[11:16] if len(ts) >= 16 else ts
            goal = entry.get("goal_id", "")
            etype = entry.get("entry_type", "")
            content = str(entry.get("content", ""))[:200]
            print(f"[{time_part}] {goal} {etype}: {content}")


def cmd_summary(args):
    """Generate compressed summary of recent entries."""
    entries = read_entries()
    if not entries:
        print("no diary entries")
        return

    # Last N entries
    limit = args.limit or 10
    recent = entries[-limit:]

    # Group by goal
    by_goal = {}
    for e in recent:
        gid = e.get("goal_id", "unknown")
        by_goal.setdefault(gid, []).append(e)

    lines = []
    for gid, goal_entries in by_goal.items():
        types = {}
        for e in goal_entries:
            etype = e.get("entry_type", "?")
            types[etype] = types.get(etype, 0) + 1
        type_str = ", ".join(f"{k}:{v}" for k, v in types.items())
        last_content = str(goal_entries[-1].get("content", ""))[:100]
        lines.append(f"{gid}: {len(goal_entries)} entries ({type_str}) — last: {last_content}")

    print(f"Diary: {len(entries)} total, {len(recent)} recent")
    for line in lines:
        print(f"  {line}")


def cmd_trim(args):
    """Remove entries older than N hours."""
    entries = read_entries()
    if not entries:
        print("no entries to trim")
        return

    hours = args.hours or 8
    cutoff = datetime.now() - timedelta(hours=hours)
    kept = []
    removed = 0

    for entry in entries:
        ts = _parse_ts(entry)
        if ts and ts < cutoff:
            removed += 1
        else:
            kept.append(entry)

    if removed == 0:
        print(f"no entries older than {hours}h")
        return

    # Rewrite file with kept entries
    tmp = DIARY_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for entry in kept:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    os.replace(str(tmp), str(DIARY_PATH))

    print(f"trimmed {removed} entries older than {hours}h, {len(kept)} remaining")


def _parse_ts(entry):
    """Parse timestamp from entry, return datetime or None."""
    ts = entry.get("timestamp", "")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def build_parser():
    parser = argparse.ArgumentParser(description="Execution diary — append-only reasoning trail")
    sub = parser.add_subparsers(dest="command", required=True)

    # append
    sub.add_parser("append", help="Add entry from stdin JSON")

    # read
    p_read = sub.add_parser("read", help="Read recent entries")
    p_read.add_argument("--limit", type=int, default=None, help="Max entries to return (from end)")
    p_read.add_argument("--since", type=str, default=None, help="Only entries after this ISO timestamp")
    p_read.add_argument("--goal", type=str, default=None, help="Filter by goal_id")
    p_read.add_argument("--json", action="store_true", help="Output as JSON array")

    # summary
    p_sum = sub.add_parser("summary", help="Compressed summary of recent entries")
    p_sum.add_argument("--limit", type=int, default=10, help="Max entries to summarize")

    # trim
    p_trim = sub.add_parser("trim", help="Remove entries older than N hours")
    p_trim.add_argument("--hours", type=int, default=8, help="Hours threshold (default: 8)")

    return parser


DISPATCH = {
    "append": cmd_append,
    "read": cmd_read,
    "summary": cmd_summary,
    "trim": cmd_trim,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
