#!/usr/bin/env python3
"""Journal session index engine for JSONL-based journal management.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.
Follows the same patterns as experience.py and pipeline.py.

Manages mind/journal.jsonl (session index only — .md content files stay as-is).
"""

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import MIND_DIR

LIVE_PATH = MIND_DIR / "journal.jsonl"

JOURNAL_FILE_RE = re.compile(r"^mind/journal/\d{4}/\d{2}/\d{4}-\d{2}-\d{2}\.md$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

REQUIRED_FIELDS = {"session", "date", "journal_file"}
DEFAULT_FIELDS = {
    "goals_completed": [],
    "hypotheses_resolved": 0,
    "hypotheses_created": 0,
    "key_events": [],
    "tags": [],
}


# ---------------------------------------------------------------------------
# Helpers: file I/O (same as experience.py / pipeline.py)
# ---------------------------------------------------------------------------

def read_jsonl(path):
    """Read a JSONL file and return a list of dicts. Returns [] if missing/empty."""
    p = Path(path)
    if not p.exists():
        return []
    items = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                items.append(json.loads(stripped))
    return items


def write_jsonl(path, items):
    """Atomically write a list of dicts as JSONL (one JSON object per line)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for item in items:
            # ensure_ascii=True: prevents mojibake/surrogates from bricking the file
            f.write(json.dumps(item, ensure_ascii=True) + "\n")
    os.replace(str(tmp), str(p))


def append_jsonl(path, item):
    """Append one JSON line to a JSONL file, creating it if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        # ensure_ascii=True: prevents mojibake/surrogates from bricking the file
        f.write(json.dumps(item, ensure_ascii=True) + "\n")


def parse_value(value_str):
    """Parse a string value into the appropriate Python type."""
    if value_str == "true":
        return True
    if value_str == "false":
        return False
    if value_str == "null":
        return None
    if value_str == "[]":
        return []
    # Try JSON parse for complex values (objects, arrays)
    if value_str.startswith("{") or value_str.startswith("["):
        try:
            return json.loads(value_str)
        except json.JSONDecodeError:
            pass
    # Try int
    try:
        return int(value_str)
    except ValueError:
        pass
    # Try float
    try:
        return float(value_str)
    except ValueError:
        pass
    return value_str


# ---------------------------------------------------------------------------
# Helpers: validation
# ---------------------------------------------------------------------------

def validate_record(rec):
    """Validate a journal record dict. Raises ValueError on invalid."""
    missing = REQUIRED_FIELDS - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not isinstance(rec["session"], int) or rec["session"] < 1:
        raise ValueError(f"Invalid session: {rec['session']} (must be a positive integer)")

    if not DATE_RE.match(rec["date"]):
        raise ValueError(f"Invalid date format: {rec['date']} (expected YYYY-MM-DD)")

    # Validate date is parseable
    try:
        date.fromisoformat(rec["date"])
    except ValueError:
        raise ValueError(f"Invalid date: {rec['date']}")

    if not JOURNAL_FILE_RE.match(rec["journal_file"]):
        raise ValueError(
            f"Invalid journal_file: {rec['journal_file']} "
            f"(expected mind/journal/YYYY/MM/YYYY-MM-DD.md)"
        )

    if not isinstance(rec.get("goals_completed", []), list):
        raise ValueError("goals_completed must be an array of strings")
    for item in rec.get("goals_completed", []):
        if not isinstance(item, str):
            raise ValueError("goals_completed must be an array of strings")

    if not isinstance(rec.get("key_events", []), list):
        raise ValueError("key_events must be an array of strings")
    for item in rec.get("key_events", []):
        if not isinstance(item, str):
            raise ValueError("key_events must be an array of strings")

    if not isinstance(rec.get("tags", []), list):
        raise ValueError("tags must be an array of strings")
    for item in rec.get("tags", []):
        if not isinstance(item, str):
            raise ValueError("tags must be an array of strings")


def normalize_record(rec):
    """Apply defaults for missing fields. Mutates and returns rec."""
    for field, default in DEFAULT_FIELDS.items():
        if field not in rec:
            if isinstance(default, (dict, list)):
                rec[field] = json.loads(json.dumps(default))  # deep copy
            else:
                rec[field] = default
    return rec


# ---------------------------------------------------------------------------
# Helpers: search
# ---------------------------------------------------------------------------

def find_record_by_session(items, session_num):
    """Find a record by session number. Returns (index, record) or None."""
    for i, rec in enumerate(items):
        if rec.get("session") == session_num:
            return (i, rec)
    return None


def find_records_by_date(items, target_date):
    """Find all records matching a date. Returns list of records."""
    return [rec for rec in items if rec.get("date") == target_date]


def get_max_session(items):
    """Return the highest session number, or 0 if no records."""
    if not items:
        return 0
    return max(rec.get("session", 0) for rec in items)


# ---------------------------------------------------------------------------
# Subcommands: read
# ---------------------------------------------------------------------------

def cmd_read(args):
    if args.session is not None:
        items = read_jsonl(LIVE_PATH)
        result = find_record_by_session(items, args.session)
        if result is None:
            print(f"Session {args.session} not found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result[1], indent=2, ensure_ascii=False))

    elif args.date:
        items = read_jsonl(LIVE_PATH)
        filtered = find_records_by_date(items, args.date)
        print(json.dumps(filtered, indent=2, ensure_ascii=False))

    elif args.summary:
        items = read_jsonl(LIVE_PATH)
        for rec in items:
            session = rec.get("session", "?")
            d = rec.get("date", "?")
            goals = len(rec.get("goals_completed", []))
            events = len(rec.get("key_events", []))
            tags = rec.get("tags", [])
            tags_str = ", ".join(tags) if tags else ""
            print(f"Session {session} ({d}): {goals} goals, {events} events [{tags_str}]")

    elif args.meta:
        items = read_jsonl(LIVE_PATH)
        if not items:
            meta = {"total_sessions": 0, "last_updated": None, "date_range": []}
        else:
            dates = [rec.get("date", "") for rec in items if rec.get("date")]
            dates.sort()
            meta = {
                "total_sessions": len(items),
                "last_updated": dates[-1] if dates else None,
                "date_range": [dates[0], dates[-1]] if dates else [],
            }
        print(json.dumps(meta, indent=2, ensure_ascii=False))

    elif args.latest:
        items = read_jsonl(LIVE_PATH)
        if not items:
            print("No journal records found", file=sys.stderr)
            sys.exit(1)
        latest = max(items, key=lambda r: r.get("session", 0))
        print(json.dumps(latest, indent=2, ensure_ascii=False))

    elif args.recent is not None:
        items = read_jsonl(LIVE_PATH)
        sorted_items = sorted(items, key=lambda r: r.get("session", 0), reverse=True)
        recent = sorted_items[:args.recent]
        print(json.dumps(recent, indent=2, ensure_ascii=False))

    else:
        print("Specify one of: --session, --date, --summary, --meta, --latest, --recent",
              file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommands: write
# ---------------------------------------------------------------------------

def cmd_add(args):
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    raw = sys.stdin.read().strip()
    if not raw:
        print("No input provided on stdin", file=sys.stderr)
        sys.exit(1)
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(LIVE_PATH)

    # Auto-set date to today if not provided
    if "date" not in rec:
        rec["date"] = date.today().isoformat()

    # Auto-increment session if not provided
    if "session" not in rec:
        rec["session"] = get_max_session(items) + 1

    rec = normalize_record(rec)

    try:
        validate_record(rec)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    # Check for duplicate session number
    existing = find_record_by_session(items, rec["session"])
    if existing is not None:
        print(f"Duplicate session number: {rec['session']}", file=sys.stderr)
        sys.exit(1)

    append_jsonl(LIVE_PATH, rec)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_update(args):
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    raw = sys.stdin.read().strip()
    if not raw:
        print("No input provided on stdin", file=sys.stderr)
        sys.exit(1)
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # Parse session number from argument (format: session-N or just N)
    session_arg = args.session_id
    if session_arg.startswith("session-"):
        session_num = int(session_arg.split("-", 1)[1])
    else:
        session_num = int(session_arg)

    rec = normalize_record(rec)

    # Stdin record session must match the target — prevent silent session mutation
    if rec.get("session") != session_num:
        print(f"Session mismatch: stdin has {rec.get('session')} but target is {session_num}", file=sys.stderr)
        sys.exit(1)

    try:
        validate_record(rec)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(LIVE_PATH)
    result = find_record_by_session(items, session_num)
    if result is None:
        print(f"Session {session_num} not found", file=sys.stderr)
        sys.exit(1)

    idx = result[0]
    items[idx] = rec
    write_jsonl(LIVE_PATH, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_merge(args):
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    raw = sys.stdin.read().strip()
    if not raw:
        print("No input provided on stdin", file=sys.stderr)
        sys.exit(1)
    try:
        merge_data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # Parse session number from argument (format: session-N or just N)
    session_arg = args.session_id
    if session_arg.startswith("session-"):
        session_num = int(session_arg.split("-", 1)[1])
    else:
        session_num = int(session_arg)

    items = read_jsonl(LIVE_PATH)
    result = find_record_by_session(items, session_num)
    if result is None:
        print(f"Session {session_num} not found", file=sys.stderr)
        sys.exit(1)

    idx, rec = result

    # Merge fields
    for key, val in merge_data.items():
        if key in ("goals_completed", "tags"):
            # Union-merge: append new items not already present
            existing = rec.get(key, [])
            if not isinstance(existing, list):
                existing = []
            for item in (val if isinstance(val, list) else [val]):
                if item not in existing:
                    existing.append(item)
            rec[key] = existing
        elif key == "key_events":
            # Always append (events are chronological, duplicates are fine)
            existing = rec.get(key, [])
            if not isinstance(existing, list):
                existing = []
            if isinstance(val, list):
                existing.extend(val)
            else:
                existing.append(val)
            rec[key] = existing
        else:
            # Scalar fields: overwrite
            rec[key] = val

    items[idx] = rec
    write_jsonl(LIVE_PATH, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Journal session index engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # read
    p_read = subparsers.add_parser("read", help="Read journal records")
    read_group = p_read.add_mutually_exclusive_group(required=True)
    read_group.add_argument("--session", type=int, help="Find record by session number")
    read_group.add_argument("--date", type=str, help="Find record(s) by date")
    read_group.add_argument("--summary", action="store_true", help="One-liner summary per record")
    read_group.add_argument("--meta", action="store_true", help="Computed metadata")
    read_group.add_argument("--latest", action="store_true", help="Most recent session record")
    read_group.add_argument("--recent", type=int, nargs="?", const=5, help="Last N session records (default 5)")

    # add
    subparsers.add_parser("add", help="Add record from stdin JSON")

    # update
    p_update = subparsers.add_parser("update", help="Full replace of session record from stdin JSON")
    p_update.add_argument("session_id", type=str, help="Session ID (e.g., session-14 or 14)")

    # merge
    p_merge = subparsers.add_parser("merge", help="Merge new data into existing session record")
    p_merge.add_argument("session_id", type=str, help="Session ID (e.g., session-14 or 14)")

    args = parser.parse_args()

    dispatch = {
        "read": cmd_read,
        "add": cmd_add,
        "update": cmd_update,
        "merge": cmd_merge,
    }

    try:
        dispatch[args.command](args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
