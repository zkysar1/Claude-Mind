#!/usr/bin/env python3
"""Experience archive engine for JSONL-based full-fidelity experience management.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.
Follows the same patterns as pipeline.py and aspirations.py.
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

from _paths import PROJECT_ROOT, WORLD_DIR, AGENT_DIR

# Per-agent experience stores (agent directory)
LIVE_PATH = AGENT_DIR / "experience.jsonl" if AGENT_DIR else None
ARCHIVE_PATH = AGENT_DIR / "experience-archive.jsonl" if AGENT_DIR else None
META_PATH = AGENT_DIR / "experience-meta.json" if AGENT_DIR else None
INDEX_PATH = AGENT_DIR / "experiential-index.yaml" if AGENT_DIR else None

# Collective domain stores (world/) — used by recompute-index
PIPELINE_LIVE_PATH = WORLD_DIR / "pipeline.jsonl"
PIPELINE_ARCHIVE_PATH = WORLD_DIR / "pipeline-archive.jsonl"

VALID_TYPES = {"goal_execution", "hypothesis_formation", "research", "reflection", "user_correction", "user_interaction", "execution_reflection"}
ID_RE = re.compile(r"^exp-[a-z0-9._-]+$")

REQUIRED_FIELDS = {"id", "type", "created", "category", "summary", "content_path"}
DEFAULT_FIELDS = {
    "goal_id": None,
    "hypothesis_id": None,
    "tree_nodes_related": [],
    "verbatim_anchors": [],
    "retrieval_stats": {
        "retrieval_count": 0,
        "times_useful": 0,
        "times_noise": 0,
        "utility_ratio": 0.0,
        "last_retrieved": None,
    },
    "archived": False,
    "archived_date": None,
}

# Staleness thresholds — must match config/memory-pipeline.yaml experience_staleness section.
# If you change these, update the YAML too (and vice versa).
ARCHIVE_UNUSED_AFTER_DAYS = 30
ARCHIVE_LOW_UTILITY_AFTER_DAYS = 90
PROTECT_MIN_RETRIEVAL_COUNT = 5
PROTECT_MIN_UTILITY_RATIO = 0.5


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
    """Validate an experience record dict. Raises ValueError on invalid."""
    missing = REQUIRED_FIELDS - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not ID_RE.match(rec["id"]):
        raise ValueError(f"Invalid record ID format: {rec['id']} (expected exp-{{slug}})")

    if rec["type"] not in VALID_TYPES:
        raise ValueError(f"Invalid type: {rec['type']}")

    # Validate verbatim_anchors structure
    anchors = rec.get("verbatim_anchors", [])
    if not isinstance(anchors, list):
        raise ValueError("verbatim_anchors must be a list")
    for anchor in anchors:
        if not isinstance(anchor, dict) or "key" not in anchor or "content" not in anchor:
            raise ValueError("Each verbatim_anchor must have 'key' and 'content' fields")

    # Validate retrieval_stats structure
    stats = rec.get("retrieval_stats")
    if stats is not None and not isinstance(stats, dict):
        raise ValueError("retrieval_stats must be a dict")

    # Validate content_path file exists
    content_path = Path(rec["content_path"])
    if not content_path.is_absolute():
        content_path = PROJECT_ROOT / content_path
    if not content_path.exists():
        raise ValueError(f"content_path file does not exist: {rec['content_path']}")


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
        "total_live": 0,
        "total_archived": 0,
        "by_type": {},
        "by_category": {},
    }


def compute_meta(live_items, archive_items):
    """Recompute meta from all records."""
    meta = empty_meta()
    meta["total_live"] = len(live_items)
    meta["total_archived"] = len(archive_items)

    by_type = {}
    by_category = {}
    for rec in live_items + archive_items:
        t = rec.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
        c = rec.get("category", "unknown")
        by_category[c] = by_category.get(c, 0) + 1

    meta["by_type"] = by_type
    meta["by_category"] = by_category
    meta["last_updated"] = date.today().isoformat()
    return meta


def _update_meta():
    """Recompute full meta from current data."""
    items = read_jsonl(LIVE_PATH)
    archive = read_jsonl(ARCHIVE_PATH)
    meta = compute_meta(items, archive)
    write_json(META_PATH, meta)


# ---------------------------------------------------------------------------
# Helpers: nested field access
# ---------------------------------------------------------------------------

def set_nested_field(obj, field_path, value):
    """Set a nested field using dot notation (e.g., 'retrieval_stats.retrieval_count')."""
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
    if args.id:
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

    elif args.category:
        items = read_jsonl(LIVE_PATH)
        filtered = [r for r in items if r.get("category") == args.category]
        print(json.dumps(filtered, indent=2, ensure_ascii=False))

    elif args.goal:
        items = read_jsonl(LIVE_PATH)
        filtered = [r for r in items if r.get("goal_id") == args.goal]
        print(json.dumps(filtered, indent=2, ensure_ascii=False))

    elif args.hypothesis:
        items = read_jsonl(LIVE_PATH)
        filtered = [r for r in items if r.get("hypothesis_id") == args.hypothesis]
        print(json.dumps(filtered, indent=2, ensure_ascii=False))

    elif args.summary:
        items = read_jsonl(LIVE_PATH)
        for rec in items:
            typ = rec.get("type", "?")
            cat = rec.get("category", "?")
            summary = rec.get("summary", "(no summary)")
            print(f"{rec.get('id', '?')}: [{typ}] {cat} — {summary}")

    elif args.type:
        items = read_jsonl(LIVE_PATH)
        filtered = [r for r in items if r.get("type") == args.type]
        print(json.dumps(filtered, indent=2, ensure_ascii=False))

    elif args.most_retrieved is not None:
        n = args.most_retrieved if args.most_retrieved > 0 else 10
        items = read_jsonl(LIVE_PATH)
        items.sort(key=lambda r: r.get("retrieval_stats", {}).get("retrieval_count", 0), reverse=True)
        print(json.dumps(items[:n], indent=2, ensure_ascii=False))

    elif args.least_retrieved is not None:
        n = args.least_retrieved if args.least_retrieved > 0 else 10
        items = read_jsonl(LIVE_PATH)
        items.sort(key=lambda r: r.get("retrieval_stats", {}).get("retrieval_count", 0))
        print(json.dumps(items[:n], indent=2, ensure_ascii=False))

    elif args.archive:
        items = read_jsonl(ARCHIVE_PATH)
        print(json.dumps(items, indent=2, ensure_ascii=False))

    elif args.meta:
        if not META_PATH.exists():
            print("{}")
        else:
            data = read_json(META_PATH)
            print(json.dumps(data, indent=2, ensure_ascii=False))

    elif args.validate_integrity:
        # Inline validate — same as validate subcommand but accessible via read --validate
        cmd_validate(args)

    else:
        print("Specify one of: --id, --category, --goal, --hypothesis, --summary, --type, "
              "--most-retrieved, --least-retrieved, --archive, --meta, --validate", file=sys.stderr)
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
    archive = read_jsonl(ARCHIVE_PATH)
    try:
        check_no_duplicate_id(items, rec["id"], archive)
    except ValueError as e:
        print(f"Duplicate error: {e}", file=sys.stderr)
        sys.exit(1)

    append_jsonl(LIVE_PATH, rec)
    _update_meta()

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

    # Support dot-notation for nested fields (e.g., retrieval_stats.retrieval_count)
    if "." in field:
        set_nested_field(rec, field, value)
    else:
        rec[field] = value

    # Recalculate utility_ratio when retrieval stats change
    stats = rec.get("retrieval_stats")
    if stats and isinstance(stats, dict) and field.startswith("retrieval_stats."):
        rc = stats.get("retrieval_count", 0)
        tu = stats.get("times_useful", 0)
        stats["utility_ratio"] = round(tu / max(rc, 1), 4)

    items[idx] = rec
    target_path = ARCHIVE_PATH if in_archive else LIVE_PATH
    write_jsonl(target_path, items)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_archive_sweep(args):
    """Sweep old/low-utility experience records to archive."""
    items = read_jsonl(LIVE_PATH)
    today = date.today()

    to_archive = []
    remaining = []

    for rec in items:
        if rec.get("archived", False):
            # Already marked archived but still in live file — move it
            to_archive.append(rec)
            continue

        created_str = rec.get("created", "")
        try:
            created_date = date.fromisoformat(created_str[:10])
        except (ValueError, TypeError):
            remaining.append(rec)
            continue

        age_days = (today - created_date).days
        stats = rec.get("retrieval_stats", {})
        retrieval_count = stats.get("retrieval_count", 0)
        utility_ratio = stats.get("utility_ratio", 0.0)

        # Protection: never archive high-value experiences
        if retrieval_count >= PROTECT_MIN_RETRIEVAL_COUNT and utility_ratio >= PROTECT_MIN_UTILITY_RATIO:
            remaining.append(rec)
            continue

        # Archive: never retrieved after 30 days
        if age_days >= ARCHIVE_UNUSED_AFTER_DAYS and retrieval_count == 0:
            rec["archived"] = True
            rec["archived_date"] = today.isoformat()
            to_archive.append(rec)
            continue

        # Archive: low utility after 90 days
        if age_days >= ARCHIVE_LOW_UTILITY_AFTER_DAYS and utility_ratio < 0.2:
            rec["archived"] = True
            rec["archived_date"] = today.isoformat()
            to_archive.append(rec)
            continue

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

    _update_meta()

    print(str(len(to_archive)))


def cmd_validate(args):
    """Check for orphaned JSONL records and .md files."""
    experience_dir = AGENT_DIR / "experience" if AGENT_DIR else None
    items = read_jsonl(LIVE_PATH) + read_jsonl(ARCHIVE_PATH)

    # Collect all content_paths from JSONL
    jsonl_paths = {}
    for rec in items:
        cp = rec.get("content_path", "")
        if cp:
            abs_cp = Path(cp) if Path(cp).is_absolute() else PROJECT_ROOT / cp
            jsonl_paths[str(abs_cp)] = rec.get("id", "?")

    # Collect all .md files in experience dir
    md_files = {}
    if experience_dir.exists():
        for f in experience_dir.iterdir():
            if f.suffix == ".md":
                md_files[str(f)] = f.name

    # JSONL records without .md files
    missing_md = []
    for abs_path, rec_id in jsonl_paths.items():
        if not Path(abs_path).exists():
            missing_md.append({"id": rec_id, "expected_path": abs_path})

    # .md files without JSONL records
    orphan_md = []
    jsonl_abs_set = set(jsonl_paths.keys())
    for abs_path, name in md_files.items():
        if abs_path not in jsonl_abs_set:
            orphan_md.append({"file": name, "path": abs_path})

    result = {
        "valid": len(missing_md) == 0 and len(orphan_md) == 0,
        "jsonl_without_md": missing_md,
        "md_without_jsonl": orphan_md,
        "total_jsonl": len(items),
        "total_md": len(md_files),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["valid"] else 1)


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


# ---------------------------------------------------------------------------
# Subcommands: recompute-index
# ---------------------------------------------------------------------------

def cmd_recompute_index(args):
    """Recompute experiential-index.yaml from pipeline resolved hypotheses."""
    # Read all resolved hypotheses from both live and archive pipeline files
    pipeline_live = read_jsonl(PIPELINE_LIVE_PATH)
    pipeline_archive = read_jsonl(PIPELINE_ARCHIVE_PATH)
    all_pipeline = pipeline_live + pipeline_archive

    # Filter to resolved records with CONFIRMED or CORRECTED outcomes
    resolved = [
        r for r in all_pipeline
        if r.get("outcome") in ("CONFIRMED", "CORRECTED")
    ]

    total_resolved = len(resolved)
    total_correct = sum(1 for r in resolved if r.get("outcome") == "CONFIRMED")
    total_incorrect = sum(1 for r in resolved if r.get("outcome") == "CORRECTED")
    accuracy_pct = round(total_correct / max(total_resolved, 1) * 100, 1)

    # Group by category
    by_category = {}
    for r in resolved:
        cat = r.get("category", "unknown")
        if cat not in by_category:
            by_category[cat] = {"total": 0, "confirmed": 0, "corrected": 0}
        by_category[cat]["total"] += 1
        if r.get("outcome") == "CONFIRMED":
            by_category[cat]["confirmed"] += 1
        else:
            by_category[cat]["corrected"] += 1

    # Compute per-category accuracy
    for cat, stats in by_category.items():
        stats["accuracy"] = round(stats["confirmed"] / max(stats["total"], 1) * 100, 1)

    # by_violation_cause: count of CORRECTED outcomes per category
    by_violation_cause = {}
    for r in resolved:
        if r.get("outcome") == "CORRECTED":
            cat = r.get("category", "unknown")
            by_violation_cause[cat] = by_violation_cause.get(cat, 0) + 1

    # Build the index data structure
    today = date.today().isoformat()
    index_data = {
        "last_updated": today,
        "summary": {
            "total_resolved": total_resolved,
            "total_correct": total_correct,
            "total_incorrect": total_incorrect,
            "accuracy_pct": accuracy_pct,
        },
        "by_category": by_category,
        "by_violation_cause": by_violation_cause,
    }

    # Write YAML using string formatting (no PyYAML dependency)
    lines = []
    lines.append(f'last_updated: "{today}"')
    lines.append("summary:")
    lines.append(f"  total_resolved: {total_resolved}")
    lines.append(f"  total_correct: {total_correct}")
    lines.append(f"  total_incorrect: {total_incorrect}")
    lines.append(f"  accuracy_pct: {accuracy_pct}")
    lines.append("by_category:")
    for cat in sorted(by_category.keys()):
        stats = by_category[cat]
        lines.append(f"  {cat}:")
        lines.append(f"    total: {stats['total']}")
        lines.append(f"    confirmed: {stats['confirmed']}")
        lines.append(f"    corrected: {stats['corrected']}")
        lines.append(f"    accuracy: {stats['accuracy']}")
    lines.append("by_violation_cause:")
    if by_violation_cause:
        for cat in sorted(by_violation_cause.keys()):
            lines.append(f"  {cat}: {by_violation_cause[cat]}")
    else:
        lines.append("  {}")
    lines.append("")

    yaml_content = "\n".join(lines)

    # Write to file
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(INDEX_PATH) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(yaml_content)
    os.replace(str(tmp), str(INDEX_PATH))

    # Print result as JSON to stdout
    print(json.dumps(index_data, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Experience archive engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # read
    p_read = subparsers.add_parser("read", help="Read experience records")
    read_group = p_read.add_mutually_exclusive_group(required=True)
    read_group.add_argument("--id", type=str, help="Find record by ID (searches live then archive)")
    read_group.add_argument("--category", type=str, help="Filter by category (live only)")
    read_group.add_argument("--goal", type=str, help="Filter by goal_id")
    read_group.add_argument("--hypothesis", type=str, help="Filter by hypothesis_id")
    read_group.add_argument("--summary", action="store_true", help="One-liner summary per record")
    read_group.add_argument("--type", type=str, help="Filter by type")
    read_group.add_argument("--most-retrieved", type=int, nargs="?", const=10, help="Top N by retrieval_count")
    read_group.add_argument("--least-retrieved", type=int, nargs="?", const=10, help="Bottom N by retrieval_count")
    read_group.add_argument("--archive", action="store_true", help="Archived records")
    read_group.add_argument("--meta", action="store_true", help="Full metadata")
    read_group.add_argument("--validate", dest="validate_integrity", action="store_true",
                            help="Check for orphan JSONL records and .md files")

    # add
    subparsers.add_parser("add", help="Add record from stdin JSON")

    # update-field
    p_uf = subparsers.add_parser("update-field", help="Update a single record field")
    p_uf.add_argument("rec_id", type=str, help="Record ID")
    p_uf.add_argument("field", type=str, help="Field to update (supports dot notation)")
    p_uf.add_argument("value", type=str, help="New value")

    # archive-sweep
    subparsers.add_parser("archive-sweep", help="Sweep old/low-utility records to archive")

    # validate
    subparsers.add_parser("validate", help="Check for orphan JSONL records and .md files")

    # meta-update
    p_meta = subparsers.add_parser("meta-update", help="Update a metadata field")
    p_meta.add_argument("field", type=str, help="Field to update")
    p_meta.add_argument("value", type=str, help="New value")

    # recompute-index
    subparsers.add_parser("recompute-index", help="Recompute experiential-index.yaml from pipeline data")

    args = parser.parse_args()

    dispatch = {
        "read": cmd_read,
        "add": cmd_add,
        "update-field": cmd_update_field,
        "archive-sweep": cmd_archive_sweep,
        "validate": cmd_validate,
        "meta-update": cmd_meta_update,
        "recompute-index": cmd_recompute_index,
    }

    try:
        dispatch[args.command](args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
