#!/usr/bin/env python3
"""Spark questions engine for JSONL-based spark question management.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.
Follows the same patterns as pipeline.py and experience.py.
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

from _paths import META_DIR, CONFIG_DIR

LIVE_PATH = META_DIR / "spark-questions.jsonl"

VALID_TYPES = {"question", "candidate"}
VALID_STATUSES = {"active", "retired"}
QUESTION_ID_RE = re.compile(r"^sq-\d{3}$")
CANDIDATE_ID_RE = re.compile(r"^sq-c\d{2}$")

QUESTION_REQUIRED_FIELDS = {"id", "text", "category", "type"}
QUESTION_DEFAULTS = {
    "times_asked": 0,
    "sparks_generated": 0,
    "yield_rate": 0.0,
    "status": "active",
}

CANDIDATE_REQUIRED_FIELDS = {"id", "text", "category", "type"}
CANDIDATE_DEFAULTS = {
    "proposed_session": 0,
}


# ---------------------------------------------------------------------------
# Helpers: file I/O (same as pipeline.py)
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
    """Atomically write JSONL with locking and history."""
    from _fileops import locked_write_jsonl
    locked_write_jsonl(path, items)


def append_jsonl(path, item):
    """Append one JSON line with locking and history."""
    from _fileops import locked_append_jsonl
    locked_append_jsonl(path, item)


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
    """Validate a spark question record dict. Raises ValueError on invalid."""
    rec_type = rec.get("type")
    if rec_type not in VALID_TYPES:
        raise ValueError(f"Invalid type: {rec_type} (expected 'question' or 'candidate')")

    if rec_type == "question":
        missing = QUESTION_REQUIRED_FIELDS - set(rec.keys())
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        if not QUESTION_ID_RE.match(rec["id"]):
            raise ValueError(f"Invalid question ID format: {rec['id']} (expected sq-NNN)")
        if rec.get("status") and rec["status"] not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {rec['status']}")
    else:
        missing = CANDIDATE_REQUIRED_FIELDS - set(rec.keys())
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        if not CANDIDATE_ID_RE.match(rec["id"]):
            raise ValueError(f"Invalid candidate ID format: {rec['id']} (expected sq-cNN)")


def normalize_record(rec):
    """Apply defaults for missing fields. Mutates and returns rec."""
    rec_type = rec.get("type")
    if rec_type == "question":
        for field, default in QUESTION_DEFAULTS.items():
            if field not in rec:
                rec[field] = default
        # Always recompute yield_rate
        rec["yield_rate"] = round(
            rec.get("sparks_generated", 0) / max(rec.get("times_asked", 0), 1), 4
        )
    elif rec_type == "candidate":
        for field, default in CANDIDATE_DEFAULTS.items():
            if field not in rec:
                rec[field] = default
    return rec


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
# Subcommands: read
# ---------------------------------------------------------------------------

def cmd_read(args):
    if args.active:
        items = read_jsonl(LIVE_PATH)
        filtered = [r for r in items if r.get("type") == "question" and r.get("status") == "active"]
        print(json.dumps(filtered, indent=2, ensure_ascii=False))

    elif args.candidates:
        items = read_jsonl(LIVE_PATH)
        filtered = [r for r in items if r.get("type") == "candidate"]
        print(json.dumps(filtered, indent=2, ensure_ascii=False))

    elif args.all:
        items = read_jsonl(LIVE_PATH)
        print(json.dumps(items, indent=2, ensure_ascii=False))

    elif args.id:
        items = read_jsonl(LIVE_PATH)
        result = find_record_by_id(items, args.id)
        if result is None:
            print(f"Record {args.id} not found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result[1], indent=2, ensure_ascii=False))

    elif args.summary:
        items = read_jsonl(LIVE_PATH)
        for rec in items:
            text = rec.get("text", "")
            truncated = text[:60] + "..." if len(text) > 60 else text
            rec_id = rec.get("id", "?")
            if rec.get("type") == "question":
                yr = rec.get("yield_rate", 0.0)
                ta = rec.get("times_asked", 0)
                print(f"{rec_id}: {truncated} [yield={yr:.2f}, asked={ta}]")
            else:
                print(f"{rec_id}: {truncated} [CANDIDATE]")

    else:
        print("Specify one of: --active, --candidates, --all, --id, --summary",
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

    rec = normalize_record(rec)

    try:
        validate_record(rec)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(LIVE_PATH)
    try:
        check_no_duplicate_id(items, rec["id"])
    except ValueError as e:
        print(f"Duplicate error: {e}", file=sys.stderr)
        sys.exit(1)

    append_jsonl(LIVE_PATH, rec)

    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_update_field(args):
    rec_id = args.rec_id
    field = args.field
    value = parse_value(args.value)

    items = read_jsonl(LIVE_PATH)
    result = find_record_by_id(items, rec_id)

    if result is None:
        print(f"Record {rec_id} not found", file=sys.stderr)
        sys.exit(1)

    idx, rec = result
    rec[field] = value

    # Recompute yield_rate if a counter field changed on a question
    if rec.get("type") == "question" and field in ("times_asked", "sparks_generated"):
        rec["yield_rate"] = round(
            rec.get("sparks_generated", 0) / max(rec.get("times_asked", 0), 1), 4
        )

    items[idx] = rec
    write_jsonl(LIVE_PATH, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_increment(args):
    rec_id = args.rec_id
    field = args.field

    if field not in ("times_asked", "sparks_generated"):
        print(f"Can only increment 'times_asked' or 'sparks_generated', got: {field}",
              file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(LIVE_PATH)
    result = find_record_by_id(items, rec_id)

    if result is None:
        print(f"Record {rec_id} not found", file=sys.stderr)
        sys.exit(1)

    idx, rec = result

    if rec.get("type") != "question":
        print(f"Record {rec_id} is not a question (type={rec.get('type')})", file=sys.stderr)
        sys.exit(1)

    rec[field] = rec.get(field, 0) + 1

    # Recompute yield_rate
    rec["yield_rate"] = round(
        rec.get("sparks_generated", 0) / max(rec.get("times_asked", 0), 1), 4
    )

    items[idx] = rec
    write_jsonl(LIVE_PATH, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_retire(args):
    rec_id = args.rec_id

    items = read_jsonl(LIVE_PATH)
    result = find_record_by_id(items, rec_id)

    if result is None:
        print(f"Record {rec_id} not found", file=sys.stderr)
        sys.exit(1)

    idx, rec = result

    if rec.get("type") != "question":
        print(f"Record {rec_id} is not a question (type={rec.get('type')})", file=sys.stderr)
        sys.exit(1)

    rec["status"] = "retired"

    items[idx] = rec
    write_jsonl(LIVE_PATH, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_promote(args):
    candidate_id = args.candidate_id
    new_id = args.new_id

    if not QUESTION_ID_RE.match(new_id):
        print(f"Invalid new ID format: {new_id} (expected sq-NNN)", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(LIVE_PATH)

    # Find candidate
    result = find_record_by_id(items, candidate_id)
    if result is None:
        print(f"Candidate {candidate_id} not found", file=sys.stderr)
        sys.exit(1)

    idx, rec = result

    if rec.get("type") != "candidate":
        print(f"Record {candidate_id} is not a candidate (type={rec.get('type')})",
              file=sys.stderr)
        sys.exit(1)

    # Check new ID doesn't already exist
    try:
        check_no_duplicate_id(items, new_id)
    except ValueError as e:
        print(f"Duplicate error: {e}", file=sys.stderr)
        sys.exit(1)

    # Promote: change type, set new ID, apply question defaults
    rec["id"] = new_id
    rec["type"] = "question"
    rec["status"] = "active"
    rec["times_asked"] = 0
    rec["sparks_generated"] = 0
    rec["yield_rate"] = 0.0

    # Remove candidate-only fields
    rec.pop("proposed_session", None)

    items[idx] = rec
    write_jsonl(LIVE_PATH, items)

    # Sync framework config so new agents get the promoted question as active.
    # This keeps core/config/spark-questions.yaml and runtime in agreement.
    _sync_framework_promotion(candidate_id, new_id, rec)

    print(json.dumps(rec, indent=2, ensure_ascii=False))


def _sync_framework_promotion(candidate_id, new_id, rec):
    """Warn when framework YAML needs updating after a runtime promotion.

    Checks if the candidate ID still exists in core/config/spark-questions.yaml.
    If so, prints a stderr warning with the exact update needed. Does not modify
    the YAML (runtime state is authoritative; YAML is for new-agent seeding).
    """
    yaml_path = CONFIG_DIR / "spark-questions.yaml"
    if not yaml_path.exists():
        return
    try:
        content = yaml_path.read_text(encoding="utf-8")
        if f"id: {candidate_id}" not in content:
            return  # Already promoted or never in framework
        print(f"Note: Framework YAML sync needed for {candidate_id} -> {new_id}. "
              f"Update core/config/spark-questions.yaml: move {candidate_id} to "
              f"seed_questions as {new_id} and update initial_state.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: could not check framework YAML: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcommands: migrate-yaml (for init-mind.sh bootstrap)
# ---------------------------------------------------------------------------

def cmd_migrate_yaml(args):
    """Convert a YAML spark-questions file to JSONL format."""
    try:
        import yaml
    except ImportError:
        print("ERROR: PyYAML required for migrate-yaml. Install: pip install pyyaml", file=sys.stderr)
        sys.exit(1)

    yaml_path = Path(args.yaml_path)
    jsonl_path = Path(args.jsonl_path)

    if not yaml_path.exists():
        print(f"Source file not found: {yaml_path}", file=sys.stderr)
        sys.exit(1)

    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if data is None:
        data = {}

    records = []

    # Active questions
    for q in data.get("questions", []):
        rec = dict(q)
        rec["type"] = "question"
        if "status" not in rec:
            rec["status"] = "active"
        if "times_asked" not in rec:
            rec["times_asked"] = 0
        if "sparks_generated" not in rec:
            rec["sparks_generated"] = 0
        ta = rec["times_asked"]
        sg = rec["sparks_generated"]
        rec["yield_rate"] = round(sg / max(ta, 1), 4)
        records.append(rec)

    # Candidates
    for c in data.get("candidates", []):
        rec = dict(c)
        rec["type"] = "candidate"
        if "proposed_session" not in rec:
            rec["proposed_session"] = 0
        records.append(rec)

    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for item in records:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    q_count = sum(1 for r in records if r["type"] == "question")
    c_count = sum(1 for r in records if r["type"] == "candidate")
    print(f"  Seeded spark-questions.jsonl: {q_count} questions + {c_count} candidates")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Spark questions engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # read
    p_read = subparsers.add_parser("read", help="Read spark question records")
    read_group = p_read.add_mutually_exclusive_group(required=True)
    read_group.add_argument("--active", action="store_true", help="Active questions only")
    read_group.add_argument("--candidates", action="store_true", help="Candidates only")
    read_group.add_argument("--all", action="store_true", help="All records")
    read_group.add_argument("--id", type=str, help="Find record by ID")
    read_group.add_argument("--summary", action="store_true", help="One-liner summary per record")

    # add
    subparsers.add_parser("add", help="Add record from stdin JSON")

    # update-field
    p_uf = subparsers.add_parser("update-field", help="Update a single record field")
    p_uf.add_argument("rec_id", type=str, help="Record ID")
    p_uf.add_argument("field", type=str, help="Field to update")
    p_uf.add_argument("value", type=str, help="New value")

    # increment
    p_inc = subparsers.add_parser("increment", help="Atomically increment a counter and recompute yield_rate")
    p_inc.add_argument("rec_id", type=str, help="Record ID")
    p_inc.add_argument("field", type=str, help="Field to increment (times_asked or sparks_generated)")

    # retire
    p_ret = subparsers.add_parser("retire", help="Retire a question (set status=retired)")
    p_ret.add_argument("rec_id", type=str, help="Question ID to retire")

    # promote
    p_promo = subparsers.add_parser("promote", help="Promote a candidate to active question")
    p_promo.add_argument("candidate_id", type=str, help="Candidate ID to promote")
    p_promo.add_argument("new_id", type=str, help="New question ID (sq-NNN format)")

    # migrate-yaml (for init-mind.sh bootstrap)
    p_mig = subparsers.add_parser("migrate-yaml", help="Convert YAML spark-questions to JSONL")
    p_mig.add_argument("yaml_path", type=str, help="Source YAML file")
    p_mig.add_argument("jsonl_path", type=str, help="Target JSONL file")

    args = parser.parse_args()

    dispatch = {
        "read": cmd_read,
        "add": cmd_add,
        "update-field": cmd_update_field,
        "increment": cmd_increment,
        "retire": cmd_retire,
        "promote": cmd_promote,
        "migrate-yaml": cmd_migrate_yaml,
    }

    try:
        dispatch[args.command](args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
