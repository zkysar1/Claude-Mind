#!/usr/bin/env python3
"""Pattern signatures engine for JSONL-based pattern signature management.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.
Follows the same patterns as pipeline.py and experience.py.
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

LIVE_PATH = MIND_DIR / "pattern-signatures.jsonl"

VALID_STATUSES = {"active", "retired", "contradicted"}
VALID_VALIDATION_STATUSES = {"unvalidated", "calibrating", "validated"}
ID_RE = re.compile(r"^sig-\d{3}$")

REQUIRED_FIELDS = {"id", "name", "description", "conditions", "expected_outcome", "created"}
DEFAULT_FIELDS = {
    "status": "active",
    "outcome_stats": {"total": 0, "confirmed": 0, "accuracy": 0.0},
    "retrieval_cues": [],
    "separation_markers": [],
    "confused_with": [],
    "validation_status": "unvalidated",
    "last_matched": None,
}


# ---------------------------------------------------------------------------
# Helpers: file I/O (same as pipeline.py / experience.py)
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
    """Validate a pattern signature record dict. Raises ValueError on invalid."""
    missing = REQUIRED_FIELDS - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not ID_RE.match(rec["id"]):
        raise ValueError(f"Invalid record ID format: {rec['id']} (expected sig-NNN)")

    if not isinstance(rec["conditions"], list):
        raise ValueError("conditions must be a list")

    status = rec.get("status", "active")
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")

    validation_status = rec.get("validation_status", "unvalidated")
    if validation_status not in VALID_VALIDATION_STATUSES:
        raise ValueError(f"Invalid validation_status: {validation_status}")


def recompute_accuracy(rec):
    """Recompute outcome_stats.accuracy from total/confirmed. Mutates and returns rec."""
    stats = rec.get("outcome_stats", {})
    total = stats.get("total", 0)
    confirmed = stats.get("confirmed", 0)
    stats["accuracy"] = round(confirmed / total, 4) if total > 0 else 0.0
    rec["outcome_stats"] = stats
    return rec


def normalize_record(rec):
    """Apply defaults for missing fields. Mutates and returns rec."""
    for field, default in DEFAULT_FIELDS.items():
        if field not in rec:
            if isinstance(default, (dict, list)):
                rec[field] = json.loads(json.dumps(default))  # deep copy
            else:
                rec[field] = default
    # Always recompute accuracy from total/confirmed (never trust input)
    rec = recompute_accuracy(rec)
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
# Helpers: nested field access
# ---------------------------------------------------------------------------

def set_nested_field(obj, field_path, value):
    """Set a nested field using dot notation (e.g., 'outcome_stats.total')."""
    parts = field_path.split(".")
    for part in parts[:-1]:
        if part not in obj or not isinstance(obj[part], dict):
            obj[part] = {}
        obj = obj[part]
    obj[parts[-1]] = value


# ---------------------------------------------------------------------------
# Subcommands: read
# ---------------------------------------------------------------------------

def cmd_read(args):
    if args.all:
        items = read_jsonl(LIVE_PATH)
        print(json.dumps(items, indent=2, ensure_ascii=False))

    elif args.active:
        items = read_jsonl(LIVE_PATH)
        filtered = [r for r in items if r.get("status") == "active"]
        print(json.dumps(filtered, indent=2, ensure_ascii=False))

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
            sig_id = rec.get("id", "?")
            name = rec.get("name", "(unnamed)")
            vs = rec.get("validation_status", "?")
            stats = rec.get("outcome_stats", {})
            accuracy = stats.get("accuracy", 0.0)
            confirmed = stats.get("confirmed", 0)
            total = stats.get("total", 0)
            print(f"{sig_id}: {name} [{vs}] accuracy={accuracy} ({confirmed}/{total})")

    else:
        print("Specify one of: --all, --active, --id, --summary", file=sys.stderr)
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

    rec = normalize_record(rec)

    try:
        validate_record(rec)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    # Stdin record ID must match the target — prevent silent ID mutation
    if rec.get("id") != args.rec_id:
        print(f"Record ID mismatch: stdin has '{rec.get('id')}' but target is '{args.rec_id}'", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(LIVE_PATH)
    result = find_record_by_id(items, args.rec_id)
    if result is None:
        print(f"Record {args.rec_id} not found", file=sys.stderr)
        sys.exit(1)

    idx = result[0]
    items[idx] = rec
    write_jsonl(LIVE_PATH, items)
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

    # Support dot-notation for nested fields (e.g., outcome_stats.total)
    if "." in field:
        set_nested_field(rec, field, value)
    else:
        rec[field] = value

    # Recompute accuracy whenever outcome_stats fields change
    if field.startswith("outcome_stats.") or field == "outcome_stats":
        rec = recompute_accuracy(rec)

    items[idx] = rec
    write_jsonl(LIVE_PATH, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_record_outcome(args):
    rec_id = args.rec_id
    outcome = args.outcome

    if outcome not in ("CONFIRMED", "CORRECTED"):
        print(f"Invalid outcome: {outcome} (must be CONFIRMED or CORRECTED)", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(LIVE_PATH)
    result = find_record_by_id(items, rec_id)

    if result is None:
        print(f"Record {rec_id} not found", file=sys.stderr)
        sys.exit(1)

    idx, rec = result

    stats = rec.get("outcome_stats", {"total": 0, "confirmed": 0, "accuracy": 0.0})
    stats["total"] = stats.get("total", 0) + 1
    if outcome == "CONFIRMED":
        stats["confirmed"] = stats.get("confirmed", 0) + 1
    stats["accuracy"] = round(stats["confirmed"] / stats["total"], 4) if stats["total"] > 0 else 0.0
    rec["outcome_stats"] = stats
    rec["last_matched"] = date.today().isoformat()

    items[idx] = rec
    write_jsonl(LIVE_PATH, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_set_status(args):
    rec_id = args.rec_id
    new_status = args.status

    if new_status not in VALID_STATUSES:
        print(f"Invalid status: {new_status} (must be one of: {', '.join(sorted(VALID_STATUSES))})", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(LIVE_PATH)
    result = find_record_by_id(items, rec_id)

    if result is None:
        print(f"Record {rec_id} not found", file=sys.stderr)
        sys.exit(1)

    idx, rec = result
    rec["status"] = new_status

    items[idx] = rec
    write_jsonl(LIVE_PATH, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Subcommands: migrate-yaml (for init-mind.sh bootstrap)
# ---------------------------------------------------------------------------

def cmd_migrate_yaml(args):
    """Convert a YAML pattern-signatures file to JSONL format."""
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
    for sig in data.get("signatures", []):
        rec = dict(sig)
        if "status" not in rec:
            rec["status"] = "active"
        if "retrieval_cues" not in rec:
            rec["retrieval_cues"] = []
        if "separation_markers" not in rec:
            rec["separation_markers"] = []
        if "confused_with" not in rec:
            rec["confused_with"] = []
        if "validation_status" not in rec:
            rec["validation_status"] = "unvalidated"
        if "last_matched" not in rec:
            rec["last_matched"] = None
        # Ensure outcome_stats and recompute accuracy
        stats = rec.get("outcome_stats", {})
        if "total" not in stats:
            stats["total"] = 0
        if "confirmed" not in stats:
            stats["confirmed"] = 0
        total = stats["total"]
        confirmed = stats["confirmed"]
        stats["accuracy"] = round(confirmed / total, 4) if total > 0 else 0.0
        rec["outcome_stats"] = stats
        records.append(rec)

    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for item in records:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"  Seeded pattern-signatures.jsonl: {len(records)} signatures")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Pattern signatures engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # read
    p_read = subparsers.add_parser("read", help="Read pattern signature records")
    read_group = p_read.add_mutually_exclusive_group(required=True)
    read_group.add_argument("--all", action="store_true", help="All records")
    read_group.add_argument("--active", action="store_true", help="Records with status=active")
    read_group.add_argument("--id", type=str, help="Find record by ID")
    read_group.add_argument("--summary", action="store_true", help="One-liner summary per record")

    # add
    subparsers.add_parser("add", help="Add record from stdin JSON")

    # update
    p_update = subparsers.add_parser("update", help="Full replace of record from stdin JSON")
    p_update.add_argument("rec_id", type=str, help="Record ID to update")

    # update-field
    p_uf = subparsers.add_parser("update-field", help="Update a single record field")
    p_uf.add_argument("rec_id", type=str, help="Record ID")
    p_uf.add_argument("field", type=str, help="Field to update (supports dot notation)")
    p_uf.add_argument("value", type=str, help="New value")

    # record-outcome
    p_ro = subparsers.add_parser("record-outcome", help="Record a pattern match outcome")
    p_ro.add_argument("rec_id", type=str, help="Record ID")
    p_ro.add_argument("outcome", type=str, help="Outcome: CONFIRMED or CORRECTED")

    # set-status
    p_ss = subparsers.add_parser("set-status", help="Update status field")
    p_ss.add_argument("rec_id", type=str, help="Record ID")
    p_ss.add_argument("status", type=str, help="New status (active, retired, contradicted)")

    # migrate-yaml (for init-mind.sh bootstrap)
    p_mig = subparsers.add_parser("migrate-yaml", help="Convert YAML pattern-signatures to JSONL")
    p_mig.add_argument("yaml_path", type=str, help="Source YAML file")
    p_mig.add_argument("jsonl_path", type=str, help="Target JSONL file")

    args = parser.parse_args()

    dispatch = {
        "read": cmd_read,
        "add": cmd_add,
        "update": cmd_update,
        "update-field": cmd_update_field,
        "record-outcome": cmd_record_outcome,
        "set-status": cmd_set_status,
        "migrate-yaml": cmd_migrate_yaml,
    }

    try:
        dispatch[args.command](args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
