#!/usr/bin/env python3
"""Reasoning bank and guardrails engine for JSONL-based record management.

Manages two stores:
  - mind/reasoning-bank.jsonl  (reasoning bank entries, rb-NNN)
  - mind/guardrails.jsonl      (guardrail rules, guard-NNN)

All shell scripts are thin wrappers around this. Top-level subcommands `rb` and
`guard` select the store; nested subcommands manage records.

Follows the same patterns as experience.py and pipeline.py.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import MIND_DIR

RB_PATH = MIND_DIR / "reasoning-bank.jsonl"
GUARD_PATH = MIND_DIR / "guardrails.jsonl"

RB_ID_RE = re.compile(r"^rb-\d{3}$")
GUARD_ID_RE = re.compile(r"^guard-\d{3}$")

RB_VALID_TYPES = {"success", "failure", "user_provided"}  # user_provided: from /respond Step 7.5 interaction learning
RB_VALID_STATUSES = {"active", "retired"}
GUARD_VALID_STATUSES = {"active", "retired"}

RB_REQUIRED_FIELDS = {"id", "title", "type", "category", "content", "created"}
RB_DEFAULT_FIELDS = {
    "status": "active",
    "description": "",
    "source_hypothesis": None,
    "outcome": None,
    "failure_lesson": None,
    "preventive_guardrail": None,
    "tags": [],
    "when_to_use": {"conditions": [], "category": ""},
    "utilization": {
        "retrieval_count": 0,
        "last_retrieved": None,
        "times_helpful": 0,
        "times_noise": 0,
        "times_active": 0,
        "times_skipped": 0,
        "utilization_score": 0.0,
    },
}

GUARD_REQUIRED_FIELDS = {"id", "rule", "category", "trigger_condition", "source", "created"}
GUARD_DEFAULT_FIELDS = {
    "status": "active",
    "when_to_use": {"conditions": [], "category": ""},
    "utilization": {
        "retrieval_count": 0,
        "last_retrieved": None,
        "times_helpful": 0,
        "times_noise": 0,
        "times_active": 0,
        "times_skipped": 0,
        "utilization_score": 0.0,
    },
}

UTILIZATION_COUNTERS = {
    "retrieval_count", "times_helpful", "times_noise", "times_active", "times_skipped",
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
# Helpers: search
# ---------------------------------------------------------------------------

def find_record_by_id(items, rec_id):
    """Find a record by ID. Returns (index, record) or None."""
    for i, rec in enumerate(items):
        if rec.get("id") == rec_id:
            return (i, rec)
    return None


def check_no_duplicate_id(items, rec_id):
    """Raise ValueError if rec_id already exists in items."""
    for item in items:
        if item.get("id") == rec_id:
            raise ValueError(f"Duplicate record ID: {rec_id}")


# ---------------------------------------------------------------------------
# Helpers: nested field access
# ---------------------------------------------------------------------------

def set_nested_field(obj, field_path, value):
    """Set a nested field using dot notation (e.g., 'utilization.retrieval_count')."""
    parts = field_path.split(".")
    for part in parts[:-1]:
        if part not in obj or not isinstance(obj[part], dict):
            obj[part] = {}
        obj = obj[part]
    obj[parts[-1]] = value


# ---------------------------------------------------------------------------
# Helpers: utilization score
# ---------------------------------------------------------------------------

def recompute_utilization_score(rec):
    """Recompute utilization_score = times_helpful / max(retrieval_count, 1)."""
    util = rec.get("utilization")
    if util and isinstance(util, dict):
        rc = util.get("retrieval_count", 0)
        th = util.get("times_helpful", 0)
        util["utilization_score"] = round(th / max(rc, 1), 4)


# ---------------------------------------------------------------------------
# Helpers: validation
# ---------------------------------------------------------------------------

def validate_utilization(util):
    """Validate the utilization object shape (all 6 counter fields present)."""
    if not isinstance(util, dict):
        raise ValueError("utilization must be a dict")
    required_keys = {"retrieval_count", "last_retrieved", "times_helpful",
                     "times_noise", "times_active", "times_skipped", "utilization_score"}
    missing = required_keys - set(util.keys())
    if missing:
        raise ValueError(f"utilization missing fields: {missing}")


def validate_rb_record(rec):
    """Validate a reasoning bank record dict. Raises ValueError on invalid."""
    missing = RB_REQUIRED_FIELDS - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not RB_ID_RE.match(rec["id"]):
        raise ValueError(f"Invalid record ID format: {rec['id']} (expected rb-NNN)")

    if rec.get("type") not in RB_VALID_TYPES:
        raise ValueError(f"Invalid type: {rec.get('type')} (expected: {RB_VALID_TYPES})")

    status = rec.get("status", "active")
    if status not in RB_VALID_STATUSES:
        raise ValueError(f"Invalid status: {status} (expected: {RB_VALID_STATUSES})")

    util = rec.get("utilization")
    if util is not None:
        validate_utilization(util)


def validate_guard_record(rec):
    """Validate a guardrail record dict. Raises ValueError on invalid."""
    missing = GUARD_REQUIRED_FIELDS - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not GUARD_ID_RE.match(rec["id"]):
        raise ValueError(f"Invalid record ID format: {rec['id']} (expected guard-NNN)")

    status = rec.get("status", "active")
    if status not in GUARD_VALID_STATUSES:
        raise ValueError(f"Invalid status: {status} (expected: {GUARD_VALID_STATUSES})")

    util = rec.get("utilization")
    if util is not None:
        validate_utilization(util)


def normalize_record(rec, defaults):
    """Apply defaults for missing fields. Mutates and returns rec."""
    for field, default in defaults.items():
        if field not in rec:
            if isinstance(default, (dict, list)):
                rec[field] = json.loads(json.dumps(default))  # deep copy
            else:
                rec[field] = default
    return rec


# ---------------------------------------------------------------------------
# Reasoning Bank subcommands
# ---------------------------------------------------------------------------

def rb_read(args):
    if args.active:
        items = read_jsonl(RB_PATH)
        filtered = [r for r in items if r.get("status") == "active"]
        print(json.dumps(filtered, indent=2, ensure_ascii=False))

    elif args.id:
        items = read_jsonl(RB_PATH)
        result = find_record_by_id(items, args.id)
        if result is None:
            print(f"Record {args.id} not found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result[1], indent=2, ensure_ascii=False))

    elif args.category:
        items = read_jsonl(RB_PATH)
        filtered = [r for r in items if r.get("category") == args.category]
        print(json.dumps(filtered, indent=2, ensure_ascii=False))

    elif args.summary:
        items = read_jsonl(RB_PATH)
        for rec in items:
            typ = rec.get("type", "?")
            cat = rec.get("category", "?")
            title = rec.get("title", "(untitled)")
            print(f"{rec.get('id', '?')}: [{typ}] {cat} — {title}")

    else:
        print("Specify one of: --active, --id, --category, --summary", file=sys.stderr)
        sys.exit(1)


def rb_add(args):
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

    rec = normalize_record(rec, RB_DEFAULT_FIELDS)

    try:
        validate_rb_record(rec)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(RB_PATH)
    try:
        check_no_duplicate_id(items, rec["id"])
    except ValueError as e:
        print(f"Duplicate error: {e}", file=sys.stderr)
        sys.exit(1)

    append_jsonl(RB_PATH, rec)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def rb_update_field(args):
    rec_id = args.rec_id
    field = args.field
    value = parse_value(args.value)

    items = read_jsonl(RB_PATH)
    result = find_record_by_id(items, rec_id)
    if result is None:
        print(f"Record {rec_id} not found", file=sys.stderr)
        sys.exit(1)

    idx, rec = result

    if "." in field:
        set_nested_field(rec, field, value)
    else:
        rec[field] = value

    # Recompute utilization_score when utilization fields change
    if field.startswith("utilization."):
        recompute_utilization_score(rec)

    items[idx] = rec
    write_jsonl(RB_PATH, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def rb_increment(args):
    rec_id = args.rec_id
    field = args.field

    # Field must be a utilization sub-field
    if not field.startswith("utilization."):
        print(f"Increment only supports utilization sub-fields, got: {field}", file=sys.stderr)
        sys.exit(1)

    counter_name = field.split(".", 1)[1]
    if counter_name not in UTILIZATION_COUNTERS:
        print(f"Invalid utilization counter: {counter_name} (expected one of: {UTILIZATION_COUNTERS})", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(RB_PATH)
    result = find_record_by_id(items, rec_id)
    if result is None:
        print(f"Record {rec_id} not found", file=sys.stderr)
        sys.exit(1)

    idx, rec = result
    util = rec.get("utilization", {})
    current = util.get(counter_name, 0)
    util[counter_name] = current + 1
    rec["utilization"] = util

    recompute_utilization_score(rec)

    items[idx] = rec
    write_jsonl(RB_PATH, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Guardrail subcommands
# ---------------------------------------------------------------------------

def guard_read(args):
    if args.active:
        items = read_jsonl(GUARD_PATH)
        filtered = [r for r in items if r.get("status") == "active"]
        print(json.dumps(filtered, indent=2, ensure_ascii=False))

    elif args.id:
        items = read_jsonl(GUARD_PATH)
        result = find_record_by_id(items, args.id)
        if result is None:
            print(f"Record {args.id} not found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result[1], indent=2, ensure_ascii=False))

    elif args.category:
        items = read_jsonl(GUARD_PATH)
        filtered = [r for r in items if r.get("category") == args.category]
        print(json.dumps(filtered, indent=2, ensure_ascii=False))

    elif args.summary:
        items = read_jsonl(GUARD_PATH)
        for rec in items:
            cat = rec.get("category", "?")
            rule = rec.get("rule", "(no rule)")[:80]
            print(f"{rec.get('id', '?')}: [{cat}] {rule}")

    else:
        print("Specify one of: --active, --id, --category, --summary", file=sys.stderr)
        sys.exit(1)


def guard_add(args):
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

    rec = normalize_record(rec, GUARD_DEFAULT_FIELDS)

    try:
        validate_guard_record(rec)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(GUARD_PATH)
    try:
        check_no_duplicate_id(items, rec["id"])
    except ValueError as e:
        print(f"Duplicate error: {e}", file=sys.stderr)
        sys.exit(1)

    append_jsonl(GUARD_PATH, rec)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def guard_update_field(args):
    rec_id = args.rec_id
    field = args.field
    value = parse_value(args.value)

    items = read_jsonl(GUARD_PATH)
    result = find_record_by_id(items, rec_id)
    if result is None:
        print(f"Record {rec_id} not found", file=sys.stderr)
        sys.exit(1)

    idx, rec = result

    if "." in field:
        set_nested_field(rec, field, value)
    else:
        rec[field] = value

    if field.startswith("utilization."):
        recompute_utilization_score(rec)

    items[idx] = rec
    write_jsonl(GUARD_PATH, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def guard_increment(args):
    rec_id = args.rec_id
    field = args.field

    if not field.startswith("utilization."):
        print(f"Increment only supports utilization sub-fields, got: {field}", file=sys.stderr)
        sys.exit(1)

    counter_name = field.split(".", 1)[1]
    if counter_name not in UTILIZATION_COUNTERS:
        print(f"Invalid utilization counter: {counter_name} (expected one of: {UTILIZATION_COUNTERS})", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(GUARD_PATH)
    result = find_record_by_id(items, rec_id)
    if result is None:
        print(f"Record {rec_id} not found", file=sys.stderr)
        sys.exit(1)

    idx, rec = result
    util = rec.get("utilization", {})
    current = util.get(counter_name, 0)
    util[counter_name] = current + 1
    rec["utilization"] = util

    recompute_utilization_score(rec)

    items[idx] = rec
    write_jsonl(GUARD_PATH, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_rb_parser(subparsers):
    """Build the 'rb' subcommand parser."""
    rb_parser = subparsers.add_parser("rb", help="Reasoning bank operations")
    rb_sub = rb_parser.add_subparsers(dest="rb_command", required=True)

    # rb read
    p_read = rb_sub.add_parser("read", help="Read reasoning bank records")
    read_group = p_read.add_mutually_exclusive_group(required=True)
    read_group.add_argument("--active", action="store_true", help="All active records")
    read_group.add_argument("--id", type=str, help="Find record by ID")
    read_group.add_argument("--category", type=str, help="Filter by category")
    read_group.add_argument("--summary", action="store_true", help="One-liner summary per record")

    # rb add
    rb_sub.add_parser("add", help="Add record from stdin JSON")

    # rb update-field
    p_uf = rb_sub.add_parser("update-field", help="Update a single record field")
    p_uf.add_argument("rec_id", type=str, help="Record ID")
    p_uf.add_argument("field", type=str, help="Field to update (supports dot notation)")
    p_uf.add_argument("value", type=str, help="New value")

    # rb increment
    p_inc = rb_sub.add_parser("increment", help="Atomic counter increment + recompute score")
    p_inc.add_argument("rec_id", type=str, help="Record ID")
    p_inc.add_argument("field", type=str, help="Utilization sub-field (e.g., utilization.retrieval_count)")


def build_guard_parser(subparsers):
    """Build the 'guard' subcommand parser."""
    guard_parser = subparsers.add_parser("guard", help="Guardrail operations")
    guard_sub = guard_parser.add_subparsers(dest="guard_command", required=True)

    # guard read
    p_read = guard_sub.add_parser("read", help="Read guardrail records")
    read_group = p_read.add_mutually_exclusive_group(required=True)
    read_group.add_argument("--active", action="store_true", help="All active guardrails")
    read_group.add_argument("--id", type=str, help="Find record by ID")
    read_group.add_argument("--category", type=str, help="Filter by category")
    read_group.add_argument("--summary", action="store_true", help="One-liner summary per guardrail")

    # guard add
    guard_sub.add_parser("add", help="Add record from stdin JSON")

    # guard update-field
    p_uf = guard_sub.add_parser("update-field", help="Update a single record field")
    p_uf.add_argument("rec_id", type=str, help="Record ID")
    p_uf.add_argument("field", type=str, help="Field to update (supports dot notation)")
    p_uf.add_argument("value", type=str, help="New value")

    # guard increment
    p_inc = guard_sub.add_parser("increment", help="Atomic counter increment + recompute score")
    p_inc.add_argument("rec_id", type=str, help="Record ID")
    p_inc.add_argument("field", type=str, help="Utilization sub-field (e.g., utilization.retrieval_count)")


def main():
    parser = argparse.ArgumentParser(description="Reasoning bank and guardrails engine")
    subparsers = parser.add_subparsers(dest="store", required=True)

    build_rb_parser(subparsers)
    build_guard_parser(subparsers)

    args = parser.parse_args()

    rb_dispatch = {
        "read": rb_read,
        "add": rb_add,
        "update-field": rb_update_field,
        "increment": rb_increment,
    }

    guard_dispatch = {
        "read": guard_read,
        "add": guard_add,
        "update-field": guard_update_field,
        "increment": guard_increment,
    }

    try:
        if args.store == "rb":
            rb_dispatch[args.rb_command](args)
        elif args.store == "guard":
            guard_dispatch[args.guard_command](args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
