#!/usr/bin/env python3
"""Dead end registry for meta-strategy approaches.

Tracks meta-strategy approaches proven to fail. Prevents the agent from
retrying known-bad parameter configurations. Inspired by AutoContext's
dead-end tracking.

Subcommands:
  add        — register a new dead end (JSON from stdin)
  check      — check if a proposed value hits a dead end
  read       — list dead ends (with optional filters)
  increment  — bump times_matched counter
  review     — mark as reviewed (agent confirmed it's still a dead end)
"""

import argparse
import json
import sys
from datetime import datetime

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import META_DIR


DE_PATH = META_DIR / "dead-ends.jsonl"
VALID_CATEGORIES = {"meta_weight", "meta_heuristic", "meta_experiment", "encoding_rule", "domain_approach"}


def read_all():
    """Read all dead end records."""
    if not DE_PATH.exists():
        return []
    records = []
    with open(DE_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def write_all(records):
    """Rewrite all records with locking and history."""
    from _fileops import locked_write_jsonl
    locked_write_jsonl(DE_PATH, records)


def next_id(records):
    """Generate next de-NNN ID."""
    max_num = 0
    for rec in records:
        rid = rec.get("id", "")
        if rid.startswith("de-"):
            try:
                num = int(rid[3:])
                max_num = max(max_num, num)
            except ValueError:
                pass
    return f"de-{max_num + 1:03d}"


def cmd_add(args):
    """Register a new dead end from JSON on stdin.

    Atomic via locked_modify_jsonl — the read-modify-write cycle (find
    overlapping existing record → either merge or append) runs entirely
    inside the lock, so two concurrent adds cannot:
      - both allocate the same de-NNN id (next_id race)
      - both fail to see each other's overlapping record (merge race
        producing two records that should have been one)
      - tear the file via the bare open("a") append previously used as
        a fallback when no merge target was found
    """
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin", file=sys.stderr)
        sys.exit(1)

    raw = sys.stdin.read().strip()
    item = json.loads(raw)

    # Set defaults
    item.setdefault("registered", datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    item.setdefault("times_matched", 0)
    item.setdefault("status", "active")
    item.setdefault("category", "meta_weight")

    # Validate category (pre-lock — cheap, fail-fast)
    if item.get("category") not in VALID_CATEGORIES:
        print(f"ERROR: Invalid category '{item.get('category')}'. Valid: {VALID_CATEGORIES}", file=sys.stderr)
        sys.exit(1)

    # Required fields (pre-lock)
    for field in ["strategy_file", "field", "failure_pattern"]:
        if field not in item:
            print(f"ERROR: Missing required field '{field}'", file=sys.stderr)
            sys.exit(1)

    from _fileops import locked_modify_jsonl

    # Closure carry-out: status reported in stdout reflects what actually
    # happened inside the lock (merged vs added, with the resolved id).
    outcome = {"status": None, "id": None}

    def _modifier(records):
        # Allocate id only if caller didn't supply one. Inside the lock
        # so two concurrent adds get distinct ids.
        if "id" not in item:
            item["id"] = next_id(records)

        # Check for overlapping existing record — same file + field +
        # active/reviewed + overlapping value_range.
        for existing in records:
            if (existing.get("strategy_file") == item.get("strategy_file") and
                    existing.get("field") == item.get("field") and
                    existing.get("status") in ("active", "reviewed")):
                existing_range = existing.get("value_range")
                new_range = item.get("value_range")
                if existing_range and new_range:
                    if (new_range[0] <= existing_range[1] and
                            new_range[1] >= existing_range[0]):
                        existing["value_range"] = [
                            min(existing_range[0], new_range[0]),
                            max(existing_range[1], new_range[1]),
                        ]
                        existing["evidence"] = list(set(
                            existing.get("evidence", []) +
                            item.get("evidence", [])))
                        existing["failure_pattern"] = item.get(
                            "failure_pattern", existing["failure_pattern"])
                        outcome["status"] = "merged"
                        outcome["id"] = existing["id"]
                        return records

        # No merge target — append the new record
        records.append(item)
        outcome["status"] = "added"
        outcome["id"] = item["id"]
        return records

    locked_modify_jsonl(DE_PATH, _modifier, initial=[])
    print(json.dumps(outcome))


def cmd_check(args):
    """Check if a proposed value hits an active dead end."""
    records = read_all()
    matches = []

    for rec in records:
        # "reviewed" = agent confirmed still a dead end — must still block
        if rec.get("status") not in ("active", "reviewed"):
            continue
        if rec.get("strategy_file") != args.file or rec.get("field") != args.field:
            continue

        value_range = rec.get("value_range")
        value_pattern = rec.get("value_pattern")

        if value_range and args.value is not None:
            try:
                val = float(args.value)
                if value_range[0] <= val <= value_range[1]:
                    matches.append({
                        "id": rec["id"],
                        "failure_pattern": rec["failure_pattern"],
                        "value_range": value_range,
                        "times_matched": rec.get("times_matched", 0),
                    })
            except (ValueError, TypeError):
                pass

        if value_pattern and args.value is not None:
            if value_pattern.lower() in str(args.value).lower():
                matches.append({
                    "id": rec["id"],
                    "failure_pattern": rec["failure_pattern"],
                    "value_pattern": value_pattern,
                    "times_matched": rec.get("times_matched", 0),
                })

    result = {
        "blocked": len(matches) > 0,
        "matches": matches,
    }
    print(json.dumps(result, ensure_ascii=False, default=str))


def cmd_read(args):
    """List dead ends with optional filters."""
    records = read_all()

    if args.active:
        records = [r for r in records if r.get("status") == "active"]
    if args.category:
        records = [r for r in records if r.get("category") == args.category]

    print(json.dumps(records, ensure_ascii=False, default=str))


def cmd_increment(args):
    """Bump times_matched counter for a dead end."""
    records = read_all()
    found = False
    for rec in records:
        if rec["id"] == args.id:
            rec["times_matched"] = rec.get("times_matched", 0) + 1
            found = True
            break

    if not found:
        print(json.dumps({"error": f"Dead end {args.id} not found"}))
        return

    write_all(records)
    print(json.dumps({"status": "incremented", "id": args.id}))


def cmd_review(args):
    """Mark a dead end as reviewed (agent confirmed it's still valid)."""
    records = read_all()
    found = False
    for rec in records:
        if rec["id"] == args.id:
            rec["status"] = "reviewed"
            rec["reviewed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            found = True
            break

    if not found:
        print(json.dumps({"error": f"Dead end {args.id} not found"}))
        return

    write_all(records)
    print(json.dumps({"status": "reviewed", "id": args.id}))


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Dead end registry for meta-strategy approaches")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("add", help="Register a new dead end (JSON from stdin)")

    p_check = sub.add_parser("check", help="Check if a value hits a dead end")
    p_check.add_argument("--file", required=True, help="Strategy file (relative to meta/)")
    p_check.add_argument("--field", required=True, help="Dot-notation field path")
    p_check.add_argument("--value", required=True, help="Proposed value to check")

    p_read = sub.add_parser("read", help="List dead ends")
    p_read.add_argument("--active", action="store_true", help="Only active dead ends")
    p_read.add_argument("--category", help="Filter by category")

    p_inc = sub.add_parser("increment", help="Bump times_matched counter")
    p_inc.add_argument("id", help="Dead end ID (de-NNN)")

    p_rev = sub.add_parser("review", help="Mark as reviewed")
    p_rev.add_argument("id", help="Dead end ID (de-NNN)")

    return parser


DISPATCH = {
    "add": cmd_add,
    "check": cmd_check,
    "read": cmd_read,
    "increment": cmd_increment,
    "review": cmd_review,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
