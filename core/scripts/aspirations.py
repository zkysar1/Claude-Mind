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

from _paths import WORLD_DIR, AGENT_DIR, META_DIR, CORE_ROOT

# Default paths point to world/ (collective task queue).
# Overridden to agent/ at runtime when --source agent is passed.
LIVE_PATH = WORLD_DIR / "aspirations.jsonl"
ARCHIVE_PATH = WORLD_DIR / "aspirations-archive.jsonl"
META_PATH = WORLD_DIR / "aspirations-meta.json"
EVOLUTION_PATH = META_DIR / "evolution-log.jsonl"

# World-only subcommands — reject --source agent.
# complete-by is NOT here: agents need it for their own recurring goals.
WORLD_ONLY_COMMANDS = {"claim", "release"}

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
# Lock ordering: LIVE_PATH.lock first, ARCHIVE_PATH.lock second.
# Never reverse this or commands that touch both files will deadlock.
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
    """Atomically write a list of dicts as JSONL with locking and history."""
    from _fileops import locked_write_jsonl
    locked_write_jsonl(path, items)


def append_jsonl(path, item):
    """Append one JSON line to a JSONL file with locking and history."""
    from _fileops import locked_append_jsonl
    locked_append_jsonl(path, item)


def _write_live_under_lock(items, action_desc, agent_name=None):
    """Write items to LIVE_PATH when the caller already holds its lock.

    Performs: save_history → atomic tempfile write → os.replace → changelog.
    MUST only be called while holding LIVE_PATH.with_suffix('.lock').
    """
    import tempfile
    from _fileops import save_history, append_changelog, resolve_base_dir
    base_dir = resolve_base_dir(LIVE_PATH)
    agent = agent_name or (AGENT_DIR.name if AGENT_DIR else "unknown")
    if base_dir:
        save_history(LIVE_PATH, base_dir, agent, action_desc)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir=LIVE_PATH.parent, suffix=".tmp",
        delete=False, encoding="utf-8")
    try:
        for item in items:
            tmp.write(json.dumps(item, ensure_ascii=True) + "\n")
        tmp.close()
        os.replace(tmp.name, str(LIVE_PATH))
    except Exception:
        os.unlink(tmp.name)
        raise
    if base_dir:
        append_changelog(base_dir, agent, LIVE_PATH, "update", action_desc)


def _check_not_archived(asp_id, *, action="modify"):
    """Refuse if asp_id exists in the archive. Call under lock."""
    archived = read_jsonl(ARCHIVE_PATH)
    if any(a.get("id") == asp_id for a in archived):
        if action == "add":
            print(f"REFUSED: {asp_id} already exists in archive — pick a higher ID.",
                  file=sys.stderr)
        else:
            print(f"REFUSED: {asp_id} is already archived — cannot modify.",
                  file=sys.stderr)
        sys.exit(1)


def read_json(path):
    """Read a JSON file and return a dict."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    """Atomically write a dict as pretty-printed JSON with locking and history."""
    from _fileops import locked_write_json
    locked_write_json(path, data)


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

    # Parallelizability classification (multi-agent coordination)
    valid_coordination_modes = ("parallel", "serial", "mixed")
    if "coordination_mode" in asp and asp["coordination_mode"] not in valid_coordination_modes:
        raise ValueError(
            f"Invalid coordination_mode: {asp['coordination_mode']} "
            f"(expected one of {valid_coordination_modes})")

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
    # Validate reallocatable field (multi-agent straggler mitigation)
    if "reallocatable" in goal:
        if not isinstance(goal["reallocatable"], bool):
            raise ValueError(f"Goal {goal['id']}: reallocatable must be a boolean")
    # Validate depends_on field (output-passing dependencies, arXiv 2603.28990)
    if "depends_on" in goal:
        deps = goal["depends_on"]
        if not isinstance(deps, list):
            raise ValueError(f"Goal {goal['id']}: depends_on must be a list")
        raw_blocked = goal.get("blocked_by", [])
        blocked_by = set(raw_blocked if isinstance(raw_blocked, list) else [raw_blocked])
        for dep in deps:
            if not isinstance(dep, dict) or "goal_id" not in dep:
                raise ValueError(f"Goal {goal['id']}: each depends_on entry must have 'goal_id'")
            if dep["goal_id"] not in blocked_by:
                raise ValueError(f"Goal {goal['id']}: depends_on goal_id '{dep['goal_id']}' must also appear in blocked_by")
    # Validate abstained_by field (self-abstention, arXiv 2603.28990)
    if "abstained_by" in goal:
        val = goal["abstained_by"]
        if val is not None and not isinstance(val, str):
            raise ValueError(f"Goal {goal['id']}: abstained_by must be a string or null")


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


def find_recurring_goals(asp):
    """Return list of goals with recurring: true in an aspiration."""
    return [g for g in asp.get("goals", []) if g.get("recurring")]


def find_unfinished_goals(asp):
    """Return non-recurring goals not in a terminal status."""
    terminal = {"completed", "skipped", "expired", "decomposed"}
    return [g for g in asp.get("goals", [])
            if not g.get("recurring") and g.get("status") not in terminal]


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
    "depends_on", "abstained_by",
}   # claimed_by intentionally excluded — use aspirations-query.sh to find claimed goals


def compact_aspiration(asp, source="world"):
    """Project an aspiration to compact form (no descriptions, no verification)."""
    result = {k: v for k, v in asp.items() if k != "goals"}
    result.pop("description", None)
    result["source"] = source  # routing tag — skills use this to select correct queue
    result["goals"] = [
        {k: v for k, v in g.items() if k in COMPACT_GOAL_KEEP}
        for g in asp.get("goals", [])
    ]
    return result


def cmd_read(args):
    if args.active_compact:
        items = read_jsonl(LIVE_PATH)
        compact = [compact_aspiration(a, source=args.source) for a in items]
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
            recurring = asp.get("progress", {}).get("recurring_goals", 0)
            status = asp.get("status", "unknown").upper()
            rec_suffix = f" + {recurring} recurring" if recurring else ""
            print(f"{asp['id']}: {asp['title']} [{status}] ({completed}/{total} goals{rec_suffix})")
    elif args.archive:
        items = read_jsonl(ARCHIVE_PATH)
        print(json.dumps(items, indent=2, ensure_ascii=False))
    elif args.stepping_stones:
        # OMNI-EPIC-inspired (arXiv 2405.15568): return K most recently completed
        # aspirations as stepping-stone context for creative aspiration generation.
        # Deliberately partial — showing the full archive constrains creativity.
        archived = read_jsonl(ARCHIVE_PATH)
        # Sort by recency. Retired aspirations have completed_at=None (key exists
        # but value is None), so .get() won't use the default — use `or ""`.
        archived.sort(key=lambda a: a.get("completed_at") or "", reverse=True)
        limit = args.limit

        stones = []
        for asp in archived[:limit]:
            stone = {
                "id": asp["id"],
                "title": asp["title"],
                "motivation": asp.get("motivation", ""),
                "scope": asp.get("scope", "unknown"),
                "tags": asp.get("tags", []),
                "goals_completed": len([g for g in asp.get("goals", [])
                                        if g.get("status") == "completed"]),
                "goals_total": len(asp.get("goals", [])),
                "goal_summaries": [
                    {"title": g["title"], "status": g.get("status", "unknown"),
                     "category": g.get("category")}
                    for g in asp.get("goals", [])
                ],
            }
            stones.append(stone)
        print(json.dumps(stones, indent=2, ensure_ascii=False))
    elif args.meta:
        if not META_PATH.exists():
            print("{}")
        else:
            data = read_json(META_PATH)
            print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print("Specify one of: --active, --id, --summary, --archive, --stepping-stones, --meta", file=sys.stderr)
        sys.exit(1)


def cmd_query(args):
    """Query goals by field filters across both queues.

    Returns a flat JSON array of matching goals with identity fields
    (goal_id, asp_id, source, title, status) always included.
    Searches both world and agent queues by default.
    """
    # Require at least one filter
    has_filter = any([args.goal_status, args.goal_field, args.title_contains])
    if not has_filter:
        print("Error: specify at least one filter (--goal-status, --goal-field, --title-contains)",
              file=sys.stderr)
        sys.exit(1)

    # Always read both queues directly (ignore global --source override)
    sources = []
    world_path = WORLD_DIR / "aspirations.jsonl"
    if world_path.exists():
        sources.append(("world", read_jsonl(world_path)))
    if AGENT_DIR:
        agent_path = AGENT_DIR / "aspirations.jsonl"
        if agent_path.exists():
            sources.append(("agent", read_jsonl(agent_path)))

    results = []
    for source_name, aspirations in sources:
        for asp in aspirations:
            asp_id = asp.get("id", "")
            for goal in asp.get("goals", []):
                if _goal_matches(goal, args):
                    results.append({
                        "goal_id": goal.get("id", ""),
                        "asp_id": asp_id,
                        "source": source_name,
                        "title": goal.get("title", ""),
                        "status": goal.get("status", ""),
                    })

    print(json.dumps(results, indent=2, ensure_ascii=True))


def _goal_matches(goal, args):
    """Check if a goal matches all specified filters (AND semantics)."""
    if args.goal_status:
        if goal.get("status") != args.goal_status:
            return False
    if args.goal_field:
        field, value = args.goal_field
        actual = goal.get(field)
        if isinstance(actual, list):
            if value not in actual:
                return False
        else:
            if str(actual) != value:
                return False
    if args.title_contains:
        title = goal.get("title", "")
        if args.title_contains.lower() not in title.lower():
            return False
    return True


def recompute_progress(asp):
    """Derive progress from goals — recurring goals excluded from completion counts.

    Recurring goals run perpetually and never "complete", so they must not inflate
    the total or be counted as completed. They are tracked separately.
    """
    goals = asp.get("goals", [])
    non_recurring = [g for g in goals if not g.get("recurring")]
    recurring_count = sum(1 for g in goals if g.get("recurring"))
    asp["progress"] = {
        "completed_goals": sum(1 for g in non_recurring if g.get("status") == "completed"),
        "total_goals": len(non_recurring),
        "recurring_goals": recurring_count,
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

    # New aspirations are never archived — default it so callers don't need to include it
    asp.setdefault("archived", False)

    try:
        validate_aspiration(asp)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    # Full-cycle lock: archive-check + dup-check + write are atomic
    from _fileops import acquire_lock, release_lock
    lock_path = LIVE_PATH.with_suffix(".lock")
    try:
        acquire_lock(lock_path)
        _check_not_archived(asp["id"], action="add")
        items = read_jsonl(LIVE_PATH)
        try:
            check_no_duplicate_id(items, asp["id"])
        except ValueError as e:
            print(f"Duplicate error: {e}", file=sys.stderr)
            sys.exit(1)

        recompute_progress(asp)
        items.append(asp)
        _write_live_under_lock(items, f"add {asp['id']}")
    finally:
        release_lock(lock_path)
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

    # Full-cycle lock: read + archive cross-check + write are atomic
    from _fileops import acquire_lock, release_lock
    lock_path = LIVE_PATH.with_suffix(".lock")
    try:
        acquire_lock(lock_path)
        _check_not_archived(args.asp_id)
        items = read_jsonl(LIVE_PATH)
        result = find_aspiration_by_id(items, args.asp_id)
        if result is None:
            print(f"Aspiration {args.asp_id} not found in live file", file=sys.stderr)
            sys.exit(1)

        idx = result[0]
        recompute_progress(asp)
        items[idx] = asp
        _write_live_under_lock(items, f"update {args.asp_id}")
    finally:
        release_lock(lock_path)
    print(json.dumps(asp, indent=2, ensure_ascii=False))


def cmd_update_goal(args):
    # No re-validation — use "null" (not "") to clear date fields like deferred_until.
    goal_id = args.goal_id
    field = args.field
    value = parse_value(args.value)

    # Full-cycle lock: read + archive cross-check + write are atomic
    from _fileops import acquire_lock, release_lock
    lock_path = LIVE_PATH.with_suffix(".lock")
    try:
        acquire_lock(lock_path)
        items = read_jsonl(LIVE_PATH)
        result = find_goal_in_aspirations(items, goal_id)
        if result is None:
            print(f"Goal {goal_id} not found in any aspiration", file=sys.stderr)
            sys.exit(1)

        asp_idx, goal_idx, asp = result
        _check_not_archived(asp["id"])
        goal = asp["goals"][goal_idx]

        # Guard: recurring goals must never reach status=completed (LLM drift protection)
        if field == "status" and value == "completed" and goal.get("recurring"):
            print(f"BLOCKED: Cannot set status=completed on recurring goal {goal_id}. "
                  f"Recurring goals stay 'pending'. Use complete-by for cycle tracking, "
                  f"or set recurring=false first to permanently stop it.",
                  file=sys.stderr)
            sys.exit(1)

        goal[field] = value
        # Clear blocked_by refs when goal reaches a terminal status
        if field == "status" and value in ("completed", "skipped", "expired", "decomposed"):
            _clear_stale_blockers(items, {goal_id})
        recompute_progress(asp)
        items[asp_idx] = asp
        _write_live_under_lock(items, f"update-goal {goal_id} {field}")
    finally:
        release_lock(lock_path)
    print(json.dumps(asp, indent=2, ensure_ascii=False))


def _clear_stale_blockers(items, resolved_goal_ids):
    """Remove blocked_by references to goals that are resolved (completed/archived/terminal).

    Called from: cmd_complete/cmd_retire/cmd_archive_sweep (archival),
    cmd_complete_by (goal completion), cmd_update_goal (terminal status).
    """
    for asp in items:
        for goal in asp.get("goals", []):
            bb = goal.get("blocked_by", [])
            if isinstance(bb, str):
                bb = [bb]
            if bb:
                cleaned = [b for b in bb if b not in resolved_goal_ids]
                if len(cleaned) != len(bb):
                    goal["blocked_by"] = cleaned


def cmd_complete(args):
    # Full-cycle lock: read + guards + archive + remove are atomic
    from _fileops import acquire_lock, release_lock
    lock_path = LIVE_PATH.with_suffix(".lock")
    try:
        acquire_lock(lock_path)
        items = read_jsonl(LIVE_PATH)
        result = find_aspiration_by_id(items, args.asp_id)
        if result is None:
            print(f"Aspiration {args.asp_id} not found in live file", file=sys.stderr)
            sys.exit(1)

        idx, asp = result

        # Guard: refuse to archive aspirations containing recurring goals (LLM drift protection)
        recurring = find_recurring_goals(asp)
        if recurring and not getattr(args, 'force', False):
            rg_ids = ", ".join(g["id"] for g in recurring)
            print(f"BLOCKED: {args.asp_id} contains {len(recurring)} recurring goal(s): {rg_ids}. "
                  f"Recurring goals run perpetually and must not be archived. "
                  f"Set recurring=false on goals to stop, or use --force.",
                  file=sys.stderr)
            sys.exit(1)

        # Guard: refuse to archive if any non-recurring goals are unfinished (premature-archival protection)
        unfinished = find_unfinished_goals(asp)
        if unfinished and not getattr(args, 'force', False):
            uf_summary = "; ".join(f"{g['id']} ({g.get('status', '?')})" for g in unfinished)
            print(f"BLOCKED: {args.asp_id} has {len(unfinished)} unfinished goal(s): {uf_summary}. "
                  f"All non-recurring goals must be completed/skipped/expired/decomposed before archival. "
                  f"Use --force to override.",
                  file=sys.stderr)
            sys.exit(1)

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
        # append_jsonl acquires ARCHIVE lock (different file) — no deadlock.
        append_jsonl(ARCHIVE_PATH, asp)
        items.pop(idx)
        _clear_stale_blockers(items, archived_goal_ids)
        _write_live_under_lock(items, f"complete {args.asp_id}")
    finally:
        release_lock(lock_path)
    print(json.dumps(asp, indent=2, ensure_ascii=False))


def cmd_retire(args):
    # Full-cycle lock: read + guards + archive + remove are atomic
    from _fileops import acquire_lock, release_lock
    lock_path = LIVE_PATH.with_suffix(".lock")
    try:
        acquire_lock(lock_path)
        items = read_jsonl(LIVE_PATH)
        result = find_aspiration_by_id(items, args.asp_id)
        if result is None:
            print(f"Aspiration {args.asp_id} not found in live file", file=sys.stderr)
            sys.exit(1)

        idx, asp = result

        # Guard: refuse to retire aspirations containing recurring goals (LLM drift protection)
        recurring = find_recurring_goals(asp)
        if recurring and not getattr(args, 'force', False):
            rg_ids = ", ".join(g["id"] for g in recurring)
            print(f"BLOCKED: {args.asp_id} contains {len(recurring)} recurring goal(s): {rg_ids}. "
                  f"Recurring goals run perpetually and must not be archived. "
                  f"Set recurring=false on goals to stop, or use --force.",
                  file=sys.stderr)
            sys.exit(1)

        # Warning: retiring with unfinished goals is intentional but worth noting
        unfinished = find_unfinished_goals(asp)
        if unfinished:
            uf_summary = "; ".join(f"{g['id']} ({g.get('status', '?')})" for g in unfinished)
            print(f"RETIREMENT NOTE: {args.asp_id} has {len(unfinished)} unfinished goal(s): {uf_summary}. "
                  f"Proceeding with retirement (abandonment is intentional for retire).",
                  file=sys.stderr)

        asp["status"] = "retired"
        asp["completed_at"] = None  # Explicitly clear — retired means never completed
        asp["archived"] = True

        archived_goal_ids = {g["id"] for g in asp.get("goals", [])}

        # Archive first (crash-safety), then remove from live.
        # append_jsonl acquires ARCHIVE lock (different file) — no deadlock.
        append_jsonl(ARCHIVE_PATH, asp)
        items.pop(idx)
        _clear_stale_blockers(items, archived_goal_ids)
        _write_live_under_lock(items, f"retire {args.asp_id}")
    finally:
        release_lock(lock_path)
    print(json.dumps(asp, indent=2, ensure_ascii=False))


def cmd_archive_sweep(args):
    # Full-cycle lock: read + classify + archive + write are atomic
    from _fileops import acquire_lock, release_lock
    lock_path = LIVE_PATH.with_suffix(".lock")
    try:
        acquire_lock(lock_path)
        items = read_jsonl(LIVE_PATH)
        to_archive = []
        remaining = []
        recovered = 0

        for a in items:
            if a.get("status") in ("completed", "retired"):
                recurring = find_recurring_goals(a)
                if recurring:
                    # Safety net: aspirations with recurring goals should never be archived.
                    # Reset to active and warn — prevents LLM drift from killing recurring goals.
                    rg_ids = ", ".join(g["id"] for g in recurring)
                    print(f"WARNING: Recovering {a['id']} — has {len(recurring)} recurring goal(s): "
                          f"{rg_ids}. Resetting to active.", file=sys.stderr)
                    a["status"] = "active"
                    a["archived"] = False
                    a.pop("completed_at", None)
                    # Also reset any recurring goals that were incorrectly set to completed
                    for g in recurring:
                        if g.get("status") == "completed":
                            g["status"] = "pending"
                    recompute_progress(a)
                    remaining.append(a)
                    recovered += 1
                else:
                    # Safety net: aspirations marked completed but with unfinished goals
                    # should be recovered (same pattern as recurring-goal recovery above)
                    if a.get("status") == "completed":
                        unfinished = find_unfinished_goals(a)
                        if unfinished:
                            uf_ids = ", ".join(g["id"] for g in unfinished)
                            print(f"WARNING: Recovering {a['id']} — has {len(unfinished)} unfinished "
                                  f"goal(s): {uf_ids}. Resetting to active.", file=sys.stderr)
                            a["status"] = "active"
                            a["archived"] = False
                            a.pop("completed_at", None)
                            recompute_progress(a)
                            remaining.append(a)
                            recovered += 1
                            continue
                    to_archive.append(a)
            else:
                # Scan non-archivable aspirations for corrupted recurring goals.
                # Recurring goals must never have status=completed. If found, reset
                # to pending — same recovery as the completed/retired path above.
                recurring = find_recurring_goals(a)
                if recurring:
                    corrupted = [g for g in recurring if g.get("status") == "completed"]
                    if corrupted:
                        c_ids = ", ".join(g["id"] for g in corrupted)
                        print(f"WARNING: Recovering {len(corrupted)} corrupted recurring goal(s) "
                              f"in {a['id']}: {c_ids}. Resetting to pending.", file=sys.stderr)
                        for g in corrupted:
                            g["status"] = "pending"
                        recompute_progress(a)
                        recovered += 1
                remaining.append(a)

        if not to_archive:
            if recovered:
                _write_live_under_lock(remaining, "archive-sweep (recovery only)")
            print("0")
            return  # finally block still runs, releasing lock

        # Collect all goal IDs from aspirations being archived
        archived_goal_ids = set()
        for asp in to_archive:
            for g in asp.get("goals", []):
                archived_goal_ids.add(g["id"])

        # Append to archive — write_jsonl acquires ARCHIVE lock (different file, no deadlock)
        archive = read_jsonl(ARCHIVE_PATH)
        archive.extend(to_archive)
        write_jsonl(ARCHIVE_PATH, archive)

        # Clean stale blockers before writing
        _clear_stale_blockers(remaining, archived_goal_ids)
        _write_live_under_lock(remaining, f"archive-sweep ({len(to_archive)} archived)")
    finally:
        release_lock(lock_path)
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

    # Apply defaults for required fields (before lock — no file dependency)
    goal.setdefault("status", "pending")

    # Auto-assign category via subprocess (before lock — no file dependency, up to 5s)
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

    # Full-cycle lock: read + auto-ID + validate + dup-check + write are atomic
    from _fileops import acquire_lock, release_lock
    lock_path = LIVE_PATH.with_suffix(".lock")
    try:
        acquire_lock(lock_path)
        _check_not_archived(args.asp_id)
        items = read_jsonl(LIVE_PATH)
        result = find_aspiration_by_id(items, args.asp_id)
        if result is None:
            print(f"Aspiration {args.asp_id} not found in live file", file=sys.stderr)
            sys.exit(1)

        idx, asp = result
        goals = asp.get("goals", [])

        # Auto-assign goal ID (MUST be under lock — reads current goal list)
        if "id" not in goal or not goal["id"]:
            asp_num = args.asp_id.replace("asp-", "")
            max_seq = 0
            for g in goals:
                gid = g.get("id", "")
                match = re.match(r"^g-\d{3}-(\d{2})", gid)
                if match:
                    max_seq = max(max_seq, int(match.group(1)))
            goal["id"] = f"g-{asp_num}-{max_seq + 1:02d}"

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
        _write_live_under_lock(items, f"add-goal {goal['id']} to {args.asp_id}")
    finally:
        release_lock(lock_path)
    print(json.dumps(goal, indent=2, ensure_ascii=False))


def cmd_claim(args):
    """Atomically claim a world goal for this agent.

    Uses a single lock scope around the entire read-check-write cycle to prevent
    TOCTOU race conditions when two agents try to claim the same goal simultaneously.
    (Fix based on arXiv 2603.28990 multi-agent coordination analysis.)
    """
    from _fileops import acquire_lock, release_lock
    goal_id = args.goal_id
    agent_name = args.agent_name
    lock_path = LIVE_PATH.with_suffix(".lock")

    try:
        acquire_lock(lock_path)

        items = read_jsonl(LIVE_PATH)
        result = find_goal_in_aspirations(items, goal_id)
        if result is None:
            print(f"Goal {goal_id} not found", file=sys.stderr)
            sys.exit(1)

        asp_idx, goal_idx, asp = result
        goal = asp["goals"][goal_idx]

        existing = goal.get("claimed_by")
        if existing and existing != agent_name:
            print(f"Goal {goal_id} already claimed by {existing}", file=sys.stderr)
            sys.exit(1)

        goal["claimed_by"] = agent_name
        goal["claimed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        items[asp_idx] = asp
        _write_live_under_lock(items, f"claim {goal_id}", agent_name)
    finally:
        release_lock(lock_path)
    print(json.dumps(goal, indent=2, ensure_ascii=False))


def cmd_release(args):
    """Release a claimed goal (clear claimed_by and claimed_at).

    Uses single lock scope (same pattern as cmd_claim) to prevent TOCTOU races.
    """
    from _fileops import acquire_lock, release_lock
    goal_id = args.goal_id
    lock_path = LIVE_PATH.with_suffix(".lock")

    try:
        acquire_lock(lock_path)

        items = read_jsonl(LIVE_PATH)
        result = find_goal_in_aspirations(items, goal_id)
        if result is None:
            print(f"Goal {goal_id} not found", file=sys.stderr)
            sys.exit(1)

        asp_idx, goal_idx, asp = result
        goal = asp["goals"][goal_idx]
        goal.pop("claimed_by", None)
        goal.pop("claimed_at", None)
        items[asp_idx] = asp
        _write_live_under_lock(items, f"release {goal_id}")
    finally:
        release_lock(lock_path)
    print(json.dumps(goal, indent=2, ensure_ascii=False))


def cmd_complete_by(args):
    """Mark a goal as completed and record which agent completed it.

    Recurring goals stay 'pending' with updated lastAchievedAt/achievedCount —
    they must NOT be permanently marked 'completed'.

    Uses single lock scope (same pattern as cmd_claim) to prevent TOCTOU races.
    """
    from _fileops import acquire_lock, release_lock
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    goal_id = args.goal_id
    agent_name = args.agent_name
    lock_path = LIVE_PATH.with_suffix(".lock")

    try:
        acquire_lock(lock_path)

        items = read_jsonl(LIVE_PATH)
        result = find_goal_in_aspirations(items, goal_id)
        if result is None:
            print(f"Goal {goal_id} not found", file=sys.stderr)
            sys.exit(1)

        asp_idx, goal_idx, asp = result
        goal = asp["goals"][goal_idx]
        goal["completed_by"] = agent_name

        if goal.get("recurring"):
            # Recurring: cycle back to pending, update tracking fields.
            # Clear claim so the goal returns to the pool for any agent's next cycle.
            goal.pop("claimed_by", None)
            goal.pop("claimed_at", None)
            goal["lastAchievedAt"] = now
            goal["achievedCount"] = goal.get("achievedCount", 0) + 1
            goal["currentStreak"] = goal.get("currentStreak", 0) + 1
            goal["longestStreak"] = max(
                goal.get("longestStreak", 0), goal.get("currentStreak", 0))
            # Status stays 'pending' — the interval gate in goal-selector handles timing
        else:
            goal["status"] = "completed"
            goal["completed_date"] = now

        recompute_progress(asp)
        items[asp_idx] = asp
        _clear_stale_blockers(items, {goal_id})
        _write_live_under_lock(items, f"complete-by {goal_id}", agent_name)
    finally:
        release_lock(lock_path)
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
    parser.add_argument("--source", choices=["world", "agent"], default="world",
                        help="Which aspiration queue to operate on (default: world)")
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
    read_group.add_argument("--stepping-stones", action="store_true", dest="stepping_stones",
                            help="Return K most recent archived aspirations as stepping-stone context")
    read_group.add_argument("--meta", action="store_true", help="Show aspirations metadata")
    p_read.add_argument("--limit", type=int, default=5,
                        help="Limit results (used with --stepping-stones)")

    # query (cross-queue goal filter — lightweight alternative to full compact load)
    p_query = subparsers.add_parser("query", help="Query goals by field filters (cross-queue)")
    p_query.add_argument("--goal-status", dest="goal_status",
                         choices=sorted(VALID_GOAL_STATUSES),
                         help="Filter goals by status")
    p_query.add_argument("--goal-field", dest="goal_field", nargs=2,
                         metavar=("FIELD", "VALUE"),
                         help="Filter where goal[FIELD] == VALUE (list fields use 'contains')")
    p_query.add_argument("--title-contains", dest="title_contains",
                         help="Case-insensitive substring match on goal title")

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
    p_complete.add_argument("--force", action="store_true",
                            help="Force archival even if aspiration has recurring goals")

    # retire
    p_retire = subparsers.add_parser("retire", help="Retire (never-started) and archive an aspiration")
    p_retire.add_argument("asp_id", type=str, help="Aspiration ID to retire")
    p_retire.add_argument("--force", action="store_true",
                          help="Force retirement even if aspiration has recurring goals")

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

    # claim — atomically claim a world goal for an agent
    p_claim = subparsers.add_parser("claim", help="Claim a world goal for an agent")
    p_claim.add_argument("goal_id", type=str, help="Goal ID to claim")
    p_claim.add_argument("agent_name", type=str, help="Agent name claiming the goal")

    # release — release a claimed goal
    p_release = subparsers.add_parser("release", help="Release a claimed goal")
    p_release.add_argument("goal_id", type=str, help="Goal ID to release")

    # complete-by — mark goal completed with agent attribution
    p_cb = subparsers.add_parser("complete-by", help="Complete a goal with agent attribution")
    p_cb.add_argument("goal_id", type=str, help="Goal ID to complete")
    p_cb.add_argument("agent_name", type=str, help="Agent that completed it")

    args = parser.parse_args()

    # Override paths for agent source
    global LIVE_PATH, ARCHIVE_PATH, META_PATH
    if args.source == "agent":
        if not AGENT_DIR:
            print("Error: AYOAI_AGENT not set — cannot use --source agent", file=sys.stderr)
            sys.exit(1)
        if args.command in WORLD_ONLY_COMMANDS:
            print(f"Error: '{args.command}' is a world-only operation", file=sys.stderr)
            sys.exit(1)
        LIVE_PATH = AGENT_DIR / "aspirations.jsonl"
        ARCHIVE_PATH = AGENT_DIR / "aspirations-archive.jsonl"
        META_PATH = AGENT_DIR / "aspirations-meta.json"

    dispatch = {
        "read": cmd_read,
        "query": cmd_query,
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
        "claim": cmd_claim,
        "release": cmd_release,
        "complete-by": cmd_complete_by,
    }

    try:
        dispatch[args.command](args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
