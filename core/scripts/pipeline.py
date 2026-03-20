#!/usr/bin/env python3
"""Hypothesis pipeline engine for JSONL-based pipeline management.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import MIND_DIR

LIVE_PATH = MIND_DIR / "pipeline.jsonl"
ARCHIVE_PATH = MIND_DIR / "pipeline-archive.jsonl"
META_PATH = MIND_DIR / "pipeline-meta.json"

VALID_STAGES = {"discovered", "evaluating", "active", "resolved", "archived"}
VALID_HORIZONS = {"micro", "session", "short", "long"}
VALID_TYPES = {"high-conviction", "calibration", "exploration", "contrarian"}
VALID_OUTCOMES = {"CONFIRMED", "CORRECTED", "EXPIRED", "UNRESOLVABLE"}
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_[a-z0-9-]+$")

REQUIRED_FIELDS = {"id", "title", "stage", "horizon", "type", "confidence", "position", "formed_date", "category"}
DEFAULT_FIELDS = {
    "slug": None,  # derived from id
    "rationale": "",
    "outcome": None,
    "reflected": False,
    "surprise": None,
    "experience_ref": None,  # optional pointer to experience archive record
}

# Archive sweep: resolved records older than this many days get archived
ARCHIVE_AGE_DAYS = 3


# ---------------------------------------------------------------------------
# Helpers: nested field access
# ---------------------------------------------------------------------------

def set_nested_field(obj, field_path, value):
    """Set a nested field using dot notation (e.g., 'process_score.dual_classification')."""
    parts = field_path.split(".")
    for part in parts[:-1]:
        if part not in obj or not isinstance(obj[part], dict):
            obj[part] = {}
        obj = obj[part]
    obj[parts[-1]] = value


# ---------------------------------------------------------------------------
# Helpers: file I/O (same as aspirations.py)
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
    """Validate a pipeline record dict. Raises ValueError on invalid."""
    missing = REQUIRED_FIELDS - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not ID_RE.match(rec["id"]):
        raise ValueError(f"Invalid record ID format: {rec['id']} (expected YYYY-MM-DD_slug)")

    if rec["stage"] not in VALID_STAGES:
        raise ValueError(f"Invalid stage: {rec['stage']}")

    if rec["horizon"] not in VALID_HORIZONS:
        raise ValueError(f"Invalid horizon: {rec['horizon']}")

    if rec["type"] not in VALID_TYPES:
        raise ValueError(f"Invalid type: {rec['type']}")

    if rec.get("outcome") is not None and rec["outcome"] not in VALID_OUTCOMES:
        raise ValueError(f"Invalid outcome: {rec['outcome']}")

    confidence = rec["confidence"]
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        raise ValueError(f"Invalid confidence: {confidence} (must be 0.0-1.0)")


def stringify_dates(obj):
    """Recursively convert date/datetime values to ISO strings in a dict."""
    if isinstance(obj, dict):
        return {k: stringify_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [stringify_dates(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


def normalize_record(rec):
    """Normalize field names from legacy formats. Mutates and returns rec."""
    # Convert any date objects to strings first
    rec = stringify_dates(rec)

    # Field renames
    renames = {
        "outcome_notes": "outcome_detail",
        "surprise_level": "surprise",
        "resolved_date": "outcome_date",
        "created": "formed_date",
    }
    for old_name, new_name in renames.items():
        if old_name in rec and new_name not in rec:
            rec[new_name] = rec[old_name]
            del rec[old_name]
        elif old_name in rec and new_name in rec:
            # Both exist — prefer new name, drop old
            del rec[old_name]

    # Derive slug from id if missing
    if "slug" not in rec and "id" in rec:
        # id format: YYYY-MM-DD_slug
        parts = rec["id"].split("_", 1)
        rec["slug"] = parts[1] if len(parts) > 1 else rec["id"]

    # Apply defaults for missing fields
    for field, default in DEFAULT_FIELDS.items():
        if field not in rec:
            if field == "slug":
                continue  # already handled above
            rec[field] = default

    # Normalize date fields to strings
    for date_field in ("formed_date", "outcome_date", "reflected_date"):
        val = rec.get(date_field)
        if val is not None and not isinstance(val, str):
            rec[date_field] = str(val)

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


def check_no_duplicate_id(items, rec_id, archive_items=None):
    """Raise ValueError if rec_id already exists in items or archive."""
    for item in items:
        if item.get("id") == rec_id:
            raise ValueError(f"Duplicate record ID: {rec_id}")
    if archive_items:
        for item in archive_items:
            if item.get("id") == rec_id:
                raise ValueError(f"Duplicate record ID (in archive): {rec_id}")


# ---------------------------------------------------------------------------
# Helpers: meta
# ---------------------------------------------------------------------------

def empty_meta():
    """Return a fresh empty meta dict."""
    return {
        "last_updated": None,
        "stage_counts": {
            "discovered": 0,
            "evaluating": 0,
            "active": 0,
            "resolved": 0,
            "archived": 0,
        },
        "accuracy": {
            "total_resolved": 0,
            "confirmed": 0,
            "corrected": 0,
            "accuracy_pct": 0.0,
            "by_strategy": {},
            "by_time_horizon": {},
            "by_depth": {},
        },
        "micro_hypothesis_stats": {},
    }


def compute_meta(live_items, archive_items):
    """Recompute meta from all records."""
    meta = empty_meta()
    all_items = live_items + archive_items

    # Stage counts
    for rec in live_items:
        stage = rec.get("stage", "discovered")
        if stage in meta["stage_counts"]:
            meta["stage_counts"][stage] += 1
    meta["stage_counts"]["archived"] = len(archive_items)

    # Accuracy from resolved + archived with outcomes
    resolved_records = [r for r in all_items if r.get("outcome") in ("CONFIRMED", "CORRECTED")]
    meta["accuracy"]["total_resolved"] = len(resolved_records)

    confirmed = sum(1 for r in resolved_records if r["outcome"] == "CONFIRMED")
    corrected = sum(1 for r in resolved_records if r["outcome"] == "CORRECTED")
    meta["accuracy"]["confirmed"] = confirmed
    meta["accuracy"]["corrected"] = corrected
    total = confirmed + corrected
    meta["accuracy"]["accuracy_pct"] = round(confirmed / total * 100, 1) if total > 0 else 0.0

    # by_strategy
    by_strategy = {}
    for r in resolved_records:
        strategy = r.get("strategy", r.get("verification", "unknown"))
        # Skip non-string strategy values (legacy dicts)
        if not isinstance(strategy, str):
            continue
        if strategy and strategy != "unknown":
            if strategy not in by_strategy:
                by_strategy[strategy] = {"confirmed": 0, "total": 0, "pct": 0.0}
            by_strategy[strategy]["total"] += 1
            if r["outcome"] == "CONFIRMED":
                by_strategy[strategy]["confirmed"] += 1
    for s in by_strategy.values():
        s["pct"] = round(s["confirmed"] / s["total"] * 100, 1) if s["total"] > 0 else 0.0
    meta["accuracy"]["by_strategy"] = by_strategy

    # by_time_horizon
    by_horizon = {}
    for r in resolved_records:
        h = r.get("horizon", "session")
        if h not in by_horizon:
            by_horizon[h] = {"confirmed": 0, "total": 0, "pct": 0.0}
        by_horizon[h]["total"] += 1
        if r["outcome"] == "CONFIRMED":
            by_horizon[h]["confirmed"] += 1
    for h in by_horizon.values():
        h["pct"] = round(h["confirmed"] / h["total"] * 100, 1) if h["total"] > 0 else 0.0
    meta["accuracy"]["by_time_horizon"] = by_horizon

    # by_depth
    by_depth = {}
    for r in resolved_records:
        d = r.get("depth")
        if d:
            if d not in by_depth:
                by_depth[d] = {"confirmed": 0, "total": 0, "pct": 0.0}
            by_depth[d]["total"] += 1
            if r["outcome"] == "CONFIRMED":
                by_depth[d]["confirmed"] += 1
    for d in by_depth.values():
        d["pct"] = round(d["confirmed"] / d["total"] * 100, 1) if d["total"] > 0 else 0.0
    meta["accuracy"]["by_depth"] = by_depth

    meta["last_updated"] = date.today().isoformat()
    return meta


# ---------------------------------------------------------------------------
# Subcommands: read
# ---------------------------------------------------------------------------

def cmd_read(args):
    if args.stage:
        if args.stage not in VALID_STAGES:
            print(f"Invalid stage: {args.stage}", file=sys.stderr)
            sys.exit(1)
        if args.stage == "archived":
            items = read_jsonl(ARCHIVE_PATH)
        else:
            items = read_jsonl(LIVE_PATH)
            items = [r for r in items if r.get("stage") == args.stage]
        print(json.dumps(items, indent=2, ensure_ascii=False))

    elif args.id:
        # Search live first, then archive
        items = read_jsonl(LIVE_PATH)
        result = find_record_by_id(items, args.id)
        if result is None:
            items = read_jsonl(ARCHIVE_PATH)
            result = find_record_by_id(items, args.id)
        if result is None:
            print(f"Record {args.id} not found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result[1], indent=2, ensure_ascii=False))

    elif args.summary:
        items = read_jsonl(LIVE_PATH)
        for rec in items:
            stage = rec.get("stage", "?").upper()
            outcome = rec.get("outcome", "")
            outcome_str = f" → {outcome}" if outcome else ""
            title = rec.get("title", "(untitled)")
            print(f"{rec.get('id', '?')}: {title} [{stage}]{outcome_str}")

    elif args.counts:
        if META_PATH.exists():
            meta = read_json(META_PATH)
            print(json.dumps(meta.get("stage_counts", {}), indent=2, ensure_ascii=False))
        else:
            # Compute from data
            items = read_jsonl(LIVE_PATH)
            archive = read_jsonl(ARCHIVE_PATH)
            counts = {"discovered": 0, "evaluating": 0, "active": 0, "resolved": 0, "archived": len(archive)}
            for r in items:
                stage = r.get("stage", "discovered")
                if stage in counts:
                    counts[stage] += 1
            print(json.dumps(counts, indent=2, ensure_ascii=False))

    elif args.accuracy:
        if META_PATH.exists():
            meta = read_json(META_PATH)
            print(json.dumps(meta.get("accuracy", {}), indent=2, ensure_ascii=False))
        else:
            print("{}")

    elif args.unreflected:
        items = read_jsonl(LIVE_PATH)
        unreflected = [r for r in items if r.get("stage") == "resolved" and not r.get("reflected", False)]
        print(json.dumps(unreflected, indent=2, ensure_ascii=False))

    elif args.replay_candidates:
        items = read_jsonl(LIVE_PATH)
        archive = read_jsonl(ARCHIVE_PATH)
        all_resolved = [r for r in items + archive if r.get("stage") in ("resolved", "archived")]
        # Filter: reflected=true, and spaced repetition scheduling
        candidates = []
        today = date.today()
        for r in all_resolved:
            if not r.get("reflected", False):
                continue
            replay = r.get("replay_metadata") or {}
            next_review = replay.get("next_review_date")
            if next_review:
                try:
                    review_date = date.fromisoformat(next_review)
                    if review_date > today:
                        continue
                except ValueError:
                    pass
            candidates.append(r)
        print(json.dumps(candidates, indent=2, ensure_ascii=False))

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
        print("Specify one of: --stage, --id, --summary, --counts, --accuracy, --unreflected, --replay-candidates, --archive, --meta", file=sys.stderr)
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

    # Apply defaults
    if "stage" not in rec:
        rec["stage"] = "discovered"
    rec = normalize_record(rec)

    try:
        validate_record(rec)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    items = read_jsonl(LIVE_PATH)
    archive = read_jsonl(ARCHIVE_PATH)
    try:
        check_no_duplicate_id(items, rec["id"], archive)
    except ValueError as e:
        print(f"Duplicate error: {e}", file=sys.stderr)
        sys.exit(1)

    append_jsonl(LIVE_PATH, rec)

    # Update meta counts
    _update_meta_counts()

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

    items = read_jsonl(LIVE_PATH)
    result = find_record_by_id(items, args.rec_id)
    if result is None:
        print(f"Record {args.rec_id} not found in live file", file=sys.stderr)
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

    # Also check archive for field updates
    in_archive = False
    if result is None:
        items = read_jsonl(ARCHIVE_PATH)
        result = find_record_by_id(items, rec_id)
        in_archive = True

    if result is None:
        print(f"Record {rec_id} not found", file=sys.stderr)
        sys.exit(1)

    idx, rec = result

    # Support dot-notation for nested fields (e.g., process_score.dual_classification)
    if "." in field:
        set_nested_field(rec, field, value)
    else:
        rec[field] = value

    # Auto-set reflected_date when reflected becomes true
    if field == "reflected" and value is True and not rec.get("reflected_date"):
        rec["reflected_date"] = date.today().isoformat()

    items[idx] = rec
    target_path = ARCHIVE_PATH if in_archive else LIVE_PATH
    write_jsonl(target_path, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_move(args):
    rec_id = args.rec_id
    target_stage = args.stage

    if target_stage not in VALID_STAGES:
        print(f"Invalid target stage: {target_stage}", file=sys.stderr)
        sys.exit(1)

    # Read optional merge data from stdin
    merge_data = {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                merge_data = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"Invalid merge JSON: {e}", file=sys.stderr)
                sys.exit(1)

    items = read_jsonl(LIVE_PATH)
    result = find_record_by_id(items, rec_id)
    if result is None:
        print(f"Record {rec_id} not found in live file", file=sys.stderr)
        sys.exit(1)

    idx, rec = result

    # Merge additional data
    for key, val in merge_data.items():
        rec[key] = val

    # Set the new stage
    rec["stage"] = target_stage

    if target_stage == "archived":
        # Append to archive BEFORE removing from live (crash-safe: duplicate > data loss)
        rec = normalize_record(rec)
        append_jsonl(ARCHIVE_PATH, rec)
        items.pop(idx)
        write_jsonl(LIVE_PATH, items)
    else:
        # Stay in live file with new stage
        rec = normalize_record(rec)
        items[idx] = rec
        write_jsonl(LIVE_PATH, items)

    # Update meta
    _update_meta_counts()

    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_archive_sweep(args):
    items = read_jsonl(LIVE_PATH)
    today = date.today()

    to_archive = []
    remaining = []
    for rec in items:
        if rec.get("stage") == "resolved":
            outcome_date = rec.get("outcome_date")
            if outcome_date:
                try:
                    od = date.fromisoformat(outcome_date)
                    if (today - od).days >= ARCHIVE_AGE_DAYS:
                        rec["stage"] = "archived"
                        to_archive.append(rec)
                        continue
                except ValueError:
                    pass
        remaining.append(rec)

    if not to_archive:
        print("0")
        return

    # Append to archive first (crash-safe ordering)
    archive = read_jsonl(ARCHIVE_PATH)
    archive.extend(to_archive)
    write_jsonl(ARCHIVE_PATH, archive)

    # Rewrite live
    write_jsonl(LIVE_PATH, remaining)

    _update_meta_counts()

    print(str(len(to_archive)))


def cmd_recompute_meta(args):
    items = read_jsonl(LIVE_PATH)
    archive = read_jsonl(ARCHIVE_PATH)
    meta = compute_meta(items, archive)

    # Preserve micro_hypothesis_stats if they exist in current meta
    if META_PATH.exists():
        old_meta = read_json(META_PATH)
        if "micro_hypothesis_stats" in old_meta:
            meta["micro_hypothesis_stats"] = old_meta["micro_hypothesis_stats"]

    write_json(META_PATH, meta)
    print(json.dumps(meta, indent=2, ensure_ascii=False))


def cmd_meta_update(args):
    field = args.field
    value = parse_value(args.value)

    if META_PATH.exists():
        data = read_json(META_PATH)
    else:
        data = empty_meta()

    data[field] = value
    data["last_updated"] = date.today().isoformat()
    write_json(META_PATH, data)
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _update_meta_counts():
    """Recompute full meta (counts + accuracy) from current data."""
    items = read_jsonl(LIVE_PATH)
    archive = read_jsonl(ARCHIVE_PATH)
    meta = compute_meta(items, archive)

    # Preserve micro_hypothesis_stats from existing meta
    if META_PATH.exists():
        old_meta = read_json(META_PATH)
        if "micro_hypothesis_stats" in old_meta:
            meta["micro_hypothesis_stats"] = old_meta["micro_hypothesis_stats"]

    write_json(META_PATH, meta)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Hypothesis pipeline engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # read
    p_read = subparsers.add_parser("read", help="Read pipeline records")
    read_group = p_read.add_mutually_exclusive_group(required=True)
    read_group.add_argument("--stage", type=str, help="List records in stage")
    read_group.add_argument("--id", type=str, help="Find record by ID")
    read_group.add_argument("--summary", action="store_true", help="One-liner summary per record")
    read_group.add_argument("--counts", action="store_true", help="Stage counts")
    read_group.add_argument("--accuracy", action="store_true", help="Accuracy report")
    read_group.add_argument("--unreflected", action="store_true", help="Resolved + reflected=false")
    read_group.add_argument("--replay-candidates", action="store_true", help="Spaced repetition filter")
    read_group.add_argument("--archive", action="store_true", help="Archived records")
    read_group.add_argument("--meta", action="store_true", help="Full metadata")

    # add
    subparsers.add_parser("add", help="Add record from stdin JSON")

    # update
    p_update = subparsers.add_parser("update", help="Update record from stdin JSON")
    p_update.add_argument("rec_id", type=str, help="Record ID to update")

    # update-field
    p_uf = subparsers.add_parser("update-field", help="Update a single record field")
    p_uf.add_argument("rec_id", type=str, help="Record ID")
    p_uf.add_argument("field", type=str, help="Field to update")
    p_uf.add_argument("value", type=str, help="New value")

    # move
    p_move = subparsers.add_parser("move", help="Move record to a different stage (optional stdin JSON merge)")
    p_move.add_argument("rec_id", type=str, help="Record ID to move")
    p_move.add_argument("stage", type=str, help="Target stage")

    # archive-sweep
    subparsers.add_parser("archive-sweep", help="Sweep old resolved records to archive")

    # recompute-meta
    subparsers.add_parser("recompute-meta", help="Full recount from records")

    # meta-update
    p_meta = subparsers.add_parser("meta-update", help="Update a metadata field")
    p_meta.add_argument("field", type=str, help="Field to update")
    p_meta.add_argument("value", type=str, help="New value")

    args = parser.parse_args()

    dispatch = {
        "read": cmd_read,
        "add": cmd_add,
        "update": cmd_update,
        "update-field": cmd_update_field,
        "move": cmd_move,
        "archive-sweep": cmd_archive_sweep,
        "recompute-meta": cmd_recompute_meta,
        "meta-update": cmd_meta_update,
    }

    try:
        dispatch[args.command](args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
