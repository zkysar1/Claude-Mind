#!/usr/bin/env python3
"""Aspiration lifecycle engine for JSONL-based aspiration management.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import MIND_DIR, CORE_ROOT

LIVE_PATH = MIND_DIR / "aspirations.jsonl"
ARCHIVE_PATH = MIND_DIR / "aspirations-archive.jsonl"
META_PATH = MIND_DIR / "aspirations-meta.json"
EVOLUTION_PATH = MIND_DIR / "evolution-log.jsonl"

VALID_ASP_STATUSES = {"active", "paused", "completed", "retired"}
VALID_GOAL_STATUSES = {"pending", "in-progress", "completed", "blocked", "skipped", "expired", "decomposed"}
VALID_PRIORITIES = {"HIGH", "MEDIUM", "LOW"}
VALID_SCOPES = {"sprint", "project", "initiative"}
ASP_ID_RE = re.compile(r"^asp-\d{3}$")
GOAL_ID_RE = re.compile(r"^g-\d{3}-\d{2}(-[a-z])?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Helpers: file I/O
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


def read_json(path):
    """Read a JSON file and return a dict."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    """Atomically write a dict as pretty-printed JSON."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        # ensure_ascii=True: prevents mojibake/surrogates from bricking the file
        json.dump(data, f, indent=2, ensure_ascii=True)
        f.write("\n")
    os.replace(str(tmp), str(p))


# ---------------------------------------------------------------------------
# Helpers: validation
# ---------------------------------------------------------------------------

def validate_aspiration(asp):
    """Validate an aspiration dict. Raises ValueError on invalid."""
    required = {"id", "title", "status", "goals", "priority", "archived"}
    missing = required - set(asp.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not ASP_ID_RE.match(asp["id"]):
        raise ValueError(f"Invalid aspiration ID format: {asp['id']} (expected asp-NNN)")

    if asp["status"] not in VALID_ASP_STATUSES:
        raise ValueError(f"Invalid aspiration status: {asp['status']}")

    if asp["priority"] not in VALID_PRIORITIES:
        raise ValueError(f"Invalid priority: {asp['priority']}")

    if not isinstance(asp["goals"], list):
        raise ValueError("goals must be a list")

    if not isinstance(asp["archived"], bool):
        raise ValueError("archived must be a boolean")

    if "scope" in asp and asp["scope"] not in VALID_SCOPES:
        raise ValueError(f"Invalid scope: {asp['scope']} (expected one of {VALID_SCOPES})")

    if "sessions_active" in asp and not isinstance(asp["sessions_active"], (int, float)):
        raise ValueError("sessions_active must be a number")

    for goal in asp["goals"]:
        validate_goal(goal)


def validate_verification(verification, goal_id):
    """Validate the unified verification field on a goal."""
    if not isinstance(verification, dict):
        raise ValueError(f"Goal {goal_id}: verification must be a dict")
    # outcomes: list of strings (human-readable success criteria)
    outcomes = verification.get("outcomes")
    if outcomes is not None and not isinstance(outcomes, list):
        raise ValueError(f"Goal {goal_id}: verification.outcomes must be a list")
    # checks: list of dicts (machine-verifiable conditions)
    checks = verification.get("checks")
    if checks is not None and not isinstance(checks, list):
        raise ValueError(f"Goal {goal_id}: verification.checks must be a list")
    # preconditions: list of strings (what must be true before execution)
    preconditions = verification.get("preconditions")
    if preconditions is not None and not isinstance(preconditions, list):
        raise ValueError(f"Goal {goal_id}: verification.preconditions must be a list")


def validate_goal(goal):
    """Validate a goal dict within an aspiration.

    Accepts both new unified 'verification' field and legacy 'desiredEndState' +
    'completion_check' fields. Both formats are valid for backward compatibility.
    """
    if "id" not in goal:
        raise ValueError("Goal missing 'id' field")
    if not GOAL_ID_RE.match(goal["id"]):
        raise ValueError(f"Invalid goal ID format: {goal['id']} (expected g-NNN-NN or g-NNN-NN-a)")
    if "status" not in goal:
        raise ValueError(f"Goal {goal['id']} missing 'status' field")
    if goal["status"] not in VALID_GOAL_STATUSES:
        raise ValueError(f"Invalid goal status for {goal['id']}: {goal['status']}")
    # Validate unified verification field if present
    if "verification" in goal:
        validate_verification(goal["verification"], goal["id"])
    # Validate recurring goal fields if present
    if "interval_hours" in goal:
        if not isinstance(goal["interval_hours"], (int, float)) or goal["interval_hours"] <= 0:
            raise ValueError(f"Goal {goal['id']}: interval_hours must be a positive number")
    if "recurring" in goal:
        if not isinstance(goal["recurring"], bool):
            raise ValueError(f"Goal {goal['id']}: recurring must be a boolean")
    # Validate deferred goal fields if present
    if "deferred_until" in goal:
        val = goal["deferred_until"]
        if val is not None:
            try:
                datetime.fromisoformat(str(val))
            except (ValueError, TypeError):
                raise ValueError(f"Goal {goal['id']}: deferred_until must be a valid ISO 8601 timestamp or null")
    if "defer_reason" in goal:
        val = goal["defer_reason"]
        if val is not None and not isinstance(val, str):
            raise ValueError(f"Goal {goal['id']}: defer_reason must be a string or null")


def validate_evolution_event(evt):
    """Validate an evolution event dict. Raises ValueError on invalid."""
    required = {"date", "event", "details"}
    missing = required - set(evt.keys())
    if missing:
        raise ValueError(f"Missing required evolution event fields: {missing}")
    if not DATE_RE.match(str(evt["date"])):
        raise ValueError(f"Invalid date format: {evt['date']} (expected YYYY-MM-DD)")


# ---------------------------------------------------------------------------
# Helpers: search
# ---------------------------------------------------------------------------

def find_aspiration_by_id(items, asp_id):
    """Find an aspiration by ID. Returns (index, aspiration) or None."""
    for i, asp in enumerate(items):
        if asp.get("id") == asp_id:
            return (i, asp)
    return None


def find_goal_in_aspirations(items, goal_id):
    """Find a goal across all aspirations. Returns (asp_index, goal_index, aspiration) or None."""
    for ai, asp in enumerate(items):
        for gi, goal in enumerate(asp.get("goals", [])):
            if goal.get("id") == goal_id:
                return (ai, gi, asp)
    return None


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
# Helpers: duplicate check
# ---------------------------------------------------------------------------

def check_no_duplicate_id(items, asp_id):
    """Raise ValueError if asp_id already exists in items."""
    for item in items:
        if item.get("id") == asp_id:
            raise ValueError(f"Duplicate aspiration ID: {asp_id}")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

COMPACT_GOAL_KEEP = {
    "id", "title", "status", "priority", "category", "skill",
    "recurring", "interval_hours", "lastAchievedAt", "achievedCount",
    "currentStreak", "longestStreak",
    "participants", "blocked_by", "deferred_until", "defer_reason",
    "args", "parent_goal", "discovered_by", "started",
}


def compact_aspiration(asp):
    """Project an aspiration to compact form (no descriptions, no verification)."""
    result = {k: v for k, v in asp.items() if k != "goals"}
    result.pop("description", None)
    result["goals"] = [
        {k: v for k, v in g.items() if k in COMPACT_GOAL_KEEP}
        for g in asp.get("goals", [])
    ]
    return result


def cmd_read(args):
    if args.active_compact:
        items = read_jsonl(LIVE_PATH)
        compact = [compact_aspiration(a) for a in items]
        print(json.dumps(compact, indent=2, ensure_ascii=True))
    elif args.active:
        items = read_jsonl(LIVE_PATH)
        print(json.dumps(items, indent=2, ensure_ascii=False))
    elif args.id:
        # Search live first, then archive
        items = read_jsonl(LIVE_PATH)
        result = find_aspiration_by_id(items, args.id)
        if result is None:
            items = read_jsonl(ARCHIVE_PATH)
            result = find_aspiration_by_id(items, args.id)
        if result is None:
            print(f"Aspiration {args.id} not found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result[1], indent=2, ensure_ascii=False))
    elif args.summary:
        items = read_jsonl(LIVE_PATH)
        for asp in items:
            completed = asp.get("progress", {}).get("completed_goals", 0)
            total = asp.get("progress", {}).get("total_goals", 0)
            status = asp.get("status", "unknown").upper()
            print(f"{asp['id']}: {asp['title']} [{status}] ({completed}/{total} goals)")
    elif args.archive:
        items = read_jsonl(ARCHIVE_PATH)
        print(json.dumps(items, indent=2, ensure_ascii=False))
    elif args.meta:
        if not META_PATH.exists():
            print("{}")
        else:
            data = read_json(META_PATH)
            print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print("Specify one of: --active, --id, --summary, --archive, --meta", file=sys.stderr)
        sys.exit(1)


def recompute_progress(asp):
    """Always derive progress from goals array — single source of truth."""
    goals = asp.get("goals", [])
    asp["progress"] = {
        "completed_goals": sum(1 for g in goals if g.get("status") == "completed"),
        "total_goals": len(goals)
    }


def cmd_add(args):
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    raw = sys.stdin.read().strip()
    if not raw:
        print("No input provided on stdin", file=sys.stderr)
        sys.exit(1)
    try:
        asp = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        validate_aspiration(asp)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(LIVE_PATH)
    try:
        check_no_duplicate_id(items, asp["id"])
    except ValueError as e:
        print(f"Duplicate error: {e}", file=sys.stderr)
        sys.exit(1)

    recompute_progress(asp)
    append_jsonl(LIVE_PATH, asp)
    print(json.dumps(asp, indent=2, ensure_ascii=False))


def cmd_update(args):
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    raw = sys.stdin.read().strip()
    if not raw:
        print("No input provided on stdin", file=sys.stderr)
        sys.exit(1)
    try:
        asp = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        validate_aspiration(asp)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(LIVE_PATH)
    result = find_aspiration_by_id(items, args.asp_id)
    if result is None:
        print(f"Aspiration {args.asp_id} not found in live file", file=sys.stderr)
        sys.exit(1)

    idx = result[0]
    recompute_progress(asp)
    items[idx] = asp
    write_jsonl(LIVE_PATH, items)
    print(json.dumps(asp, indent=2, ensure_ascii=False))


def cmd_update_goal(args):
    # No re-validation — use "null" (not "") to clear date fields like deferred_until.
    goal_id = args.goal_id
    field = args.field
    value = parse_value(args.value)

    items = read_jsonl(LIVE_PATH)
    result = find_goal_in_aspirations(items, goal_id)
    if result is None:
        print(f"Goal {goal_id} not found in any aspiration", file=sys.stderr)
        sys.exit(1)

    asp_idx, goal_idx, asp = result
    asp["goals"][goal_idx][field] = value

    recompute_progress(asp)

    items[asp_idx] = asp
    write_jsonl(LIVE_PATH, items)
    print(json.dumps(asp, indent=2, ensure_ascii=False))


def _clear_stale_blockers(items, archived_goal_ids):
    """Remove blocked_by references to goals that are being archived.

    Called after archiving an aspiration — its goal IDs vanish from live data,
    so any remaining blocked_by references to them would be stale forever.
    """
    for asp in items:
        for goal in asp.get("goals", []):
            bb = goal.get("blocked_by", [])
            if bb:
                cleaned = [b for b in bb if b not in archived_goal_ids]
                if len(cleaned) != len(bb):
                    goal["blocked_by"] = cleaned


def cmd_complete(args):
    items = read_jsonl(LIVE_PATH)
    result = find_aspiration_by_id(items, args.asp_id)
    if result is None:
        print(f"Aspiration {args.asp_id} not found in live file", file=sys.stderr)
        sys.exit(1)

    idx, asp = result

    # Maturity warning: check if aspiration has been active long enough for its scope
    scope = asp.get("scope", "project")
    sessions_active = asp.get("sessions_active", 0)
    min_sessions_map = {"sprint": 1, "project": 2, "initiative": 4}
    min_sessions = min_sessions_map.get(scope, 2)
    if sessions_active < min_sessions and scope != "sprint":
        print(f"MATURITY WARNING: {asp['id']} completing after {sessions_active} session(s) "
              f"but scope={scope} expects {min_sessions}. Consider adding depth goals.",
              file=sys.stderr)

    asp["status"] = "completed"
    asp["completed_at"] = date.today().isoformat()
    asp["archived"] = True

    archived_goal_ids = {g["id"] for g in asp.get("goals", [])}

    # Archive BEFORE removing from live — if crash between writes,
    # aspiration exists in both (benign) rather than neither (data loss).
    append_jsonl(ARCHIVE_PATH, asp)
    items.pop(idx)
    _clear_stale_blockers(items, archived_goal_ids)
    write_jsonl(LIVE_PATH, items)

    print(json.dumps(asp, indent=2, ensure_ascii=False))


def cmd_retire(args):
    items = read_jsonl(LIVE_PATH)
    result = find_aspiration_by_id(items, args.asp_id)
    if result is None:
        print(f"Aspiration {args.asp_id} not found in live file", file=sys.stderr)
        sys.exit(1)

    idx, asp = result
    asp["status"] = "retired"
    asp["completed_at"] = None  # Explicitly clear — retired means never completed
    asp["archived"] = True

    archived_goal_ids = {g["id"] for g in asp.get("goals", [])}

    append_jsonl(ARCHIVE_PATH, asp)
    items.pop(idx)
    _clear_stale_blockers(items, archived_goal_ids)
    write_jsonl(LIVE_PATH, items)

    print(json.dumps(asp, indent=2, ensure_ascii=False))


def cmd_archive_sweep(args):
    items = read_jsonl(LIVE_PATH)
    to_archive = [a for a in items if a.get("status") in ("completed", "retired")]
    remaining = [a for a in items if a.get("status") not in ("completed", "retired")]

    if not to_archive:
        print("0")
        return

    # Collect all goal IDs from aspirations being archived
    archived_goal_ids = set()
    for asp in to_archive:
        for g in asp.get("goals", []):
            archived_goal_ids.add(g["id"])

    # Append to archive
    archive = read_jsonl(ARCHIVE_PATH)
    archive.extend(to_archive)
    write_jsonl(ARCHIVE_PATH, archive)

    # Clean stale blockers before writing
    _clear_stale_blockers(remaining, archived_goal_ids)
    write_jsonl(LIVE_PATH, remaining)

    print(str(len(to_archive)))


def cmd_meta_update(args):
    field = args.field
    value = parse_value(args.value)

    if META_PATH.exists():
        data = read_json(META_PATH)
    else:
        data = {"last_updated": None, "last_evolution": None, "session_count": 0, "readiness_gates": {}}

    data[field] = value
    write_json(META_PATH, data)
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_recompute_all_progress(args):
    """Recompute progress.total_goals for every aspiration in a JSONL file."""
    path = Path(args.path)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)
    items = read_jsonl(path)
    for asp in items:
        recompute_progress(asp)
    write_jsonl(path, items)
    print(f"Recomputed progress for {len(items)} aspiration(s)")


def cmd_add_goal(args):
    """Add a single goal to an existing aspiration."""
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    raw = sys.stdin.read().strip()
    if not raw:
        print("No input provided on stdin", file=sys.stderr)
        sys.exit(1)
    try:
        goal = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(LIVE_PATH)
    result = find_aspiration_by_id(items, args.asp_id)
    if result is None:
        print(f"Aspiration {args.asp_id} not found in live file", file=sys.stderr)
        sys.exit(1)

    idx, asp = result
    goals = asp.get("goals", [])

    # Auto-assign goal ID if not provided
    if "id" not in goal or not goal["id"]:
        asp_num = args.asp_id.replace("asp-", "")
        max_seq = 0
        for g in goals:
            gid = g.get("id", "")
            match = re.match(r"^g-\d{3}-(\d{2})", gid)
            if match:
                max_seq = max(max_seq, int(match.group(1)))
        goal["id"] = f"g-{asp_num}-{max_seq + 1:02d}"

    # Apply defaults for required fields
    goal.setdefault("status", "pending")

    # Auto-assign category via category-suggest if not provided (or "uncategorized")
    if not goal.get("category") or goal.get("category") == "uncategorized":
        text = "{t}. {d}".format(t=goal.get("title", ""), d=goal.get("description", ""))
        try:
            r = subprocess.run(
                [sys.executable, str(CORE_ROOT / "scripts" / "category-suggest.py"),
                 "--text", text, "--top", "1"],
                capture_output=True, timeout=5,
                encoding="utf-8", errors="replace",
            )
            if r.returncode == 0:
                matches = json.loads(r.stdout)
                if matches and matches[0].get("score", 0) > 0:
                    goal["category"] = matches[0]["key"]
        except Exception:
            pass
        if not goal.get("category"):
            goal["category"] = "uncategorized"

    try:
        validate_goal(goal)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    # Check for duplicate goal ID
    for g in goals:
        if g.get("id") == goal["id"]:
            print(f"Duplicate goal ID: {goal['id']} already exists in {args.asp_id}", file=sys.stderr)
            sys.exit(1)

    goals.append(goal)
    asp["goals"] = goals
    recompute_progress(asp)
    items[idx] = asp
    write_jsonl(LIVE_PATH, items)
    print(json.dumps(goal, indent=2, ensure_ascii=False))


def cmd_evolution_append(args):
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    raw = sys.stdin.read().strip()
    if not raw:
        print("No input provided on stdin", file=sys.stderr)
        sys.exit(1)
    try:
        evt = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        validate_evolution_event(evt)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    append_jsonl(EVOLUTION_PATH, evt)
    print(json.dumps(evt, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Aspiration lifecycle engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # read
    p_read = subparsers.add_parser("read", help="Read aspirations")
    read_group = p_read.add_mutually_exclusive_group(required=True)
    read_group.add_argument("--active", action="store_true", help="List active aspirations")
    read_group.add_argument("--active-compact", action="store_true", dest="active_compact",
                            help="Active aspirations with compact goals (no descriptions/verification)")
    read_group.add_argument("--id", type=str, help="Find aspiration by ID")
    read_group.add_argument("--summary", action="store_true", help="One-liner summary per active aspiration")
    read_group.add_argument("--archive", action="store_true", help="List archived aspirations")
    read_group.add_argument("--meta", action="store_true", help="Show aspirations metadata")

    # add
    subparsers.add_parser("add", help="Add aspiration from stdin JSON")

    # update
    p_update = subparsers.add_parser("update", help="Update aspiration from stdin JSON")
    p_update.add_argument("asp_id", type=str, help="Aspiration ID to update")

    # update-goal
    p_ug = subparsers.add_parser("update-goal", help="Update a single goal field")
    p_ug.add_argument("goal_id", type=str, help="Goal ID")
    p_ug.add_argument("field", type=str, help="Field to update")
    p_ug.add_argument("value", type=str, help="New value")

    # complete
    p_complete = subparsers.add_parser("complete", help="Complete and archive an aspiration")
    p_complete.add_argument("asp_id", type=str, help="Aspiration ID to complete")

    # retire
    p_retire = subparsers.add_parser("retire", help="Retire (never-started) and archive an aspiration")
    p_retire.add_argument("asp_id", type=str, help="Aspiration ID to retire")

    # archive-sweep
    subparsers.add_parser("archive-sweep", help="Move completed/retired aspirations to archive")

    # meta-update
    p_meta = subparsers.add_parser("meta-update", help="Update a metadata field")
    p_meta.add_argument("field", type=str, help="Field to update")
    p_meta.add_argument("value", type=str, help="New value")

    # recompute-all-progress
    p_recompute = subparsers.add_parser("recompute-all-progress", help="Recompute progress for all aspirations in a JSONL file")
    p_recompute.add_argument("path", type=str, help="Path to JSONL file")

    # add-goal
    p_ag = subparsers.add_parser("add-goal", help="Add a goal to an existing aspiration from stdin JSON")
    p_ag.add_argument("asp_id", type=str, help="Aspiration ID to add goal to")

    # evolution-append
    subparsers.add_parser("evolution-append", help="Append evolution event from stdin JSON")

    args = parser.parse_args()

    dispatch = {
        "read": cmd_read,
        "add": cmd_add,
        "update": cmd_update,
        "update-goal": cmd_update_goal,
        "add-goal": cmd_add_goal,
        "complete": cmd_complete,
        "retire": cmd_retire,
        "archive-sweep": cmd_archive_sweep,
        "meta-update": cmd_meta_update,
        "recompute-all-progress": cmd_recompute_all_progress,
        "evolution-append": cmd_evolution_append,
    }

    try:
        dispatch[args.command](args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
