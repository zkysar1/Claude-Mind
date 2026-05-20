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
from datetime import date, datetime
from pathlib import Path

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import WORLD_DIR
from _jsonl_helpers import set_nested_field

LIVE_PATH = WORLD_DIR / "pattern-signatures.jsonl"

VALID_STATUSES = {"active", "retired", "contradicted"}
VALID_VALIDATION_STATUSES = {"unvalidated", "calibrating", "validated"}
ID_RE = re.compile(r"^sig-\d+$")  # allow 1+ digits — auto-id allocator
                                   # produces sig-{max+1}; the prior \d{3}
                                   # constraint would silently break on the
                                   # 1000th signature.

# `created` is SCRIPT-OWNED: stamped at add time by cmd_add from the system
# clock, never read from stdin. Matches reasoning-bank.py / guardrails _stamp_now
# pattern. Callers must NOT supply `created`; any stdin value is overwritten.
# cmd_update preserves the original record's `created`; update-field rejects
# `field == "created"`. This eliminates LLM-narrated timestamp drift.
REQUIRED_FIELDS = {"id", "name", "description", "conditions", "expected_outcome"}
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
    """Atomically write a list of dicts as JSONL with locking and history."""
    from _fileops import locked_write_jsonl
    locked_write_jsonl(path, items)

def append_jsonl(path, item):
    """Append one JSON line to a JSONL file with locking and history."""
    from _fileops import locked_append_jsonl
    locked_append_jsonl(path, item)

def _stamp_now():
    """Return current local time as ISO 8601 without microseconds.

    Matches reasoning-bank.py _stamp_now — CLAUDE.md "Timestamps: ALWAYS local
    system time" + `$(date +%Y-%m-%dT%H:%M:%S)`. Callers MUST use this —
    never trust an LLM-supplied `created` field.
    """
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

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

def validate_record(rec, *, skip_id_check=False):
    """Validate a pattern signature record dict. Raises ValueError on invalid.

    skip_id_check=True is used by the auto-id path in cmd_add — see
    reasoning-bank.validate_rb_record for the same rationale.
    """
    required = REQUIRED_FIELDS - ({"id"} if skip_id_check else set())
    missing = required - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not skip_id_check:
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

# set_nested_field now lives in _jsonl_helpers.py () — imported at top.

# ---------------------------------------------------------------------------
# Subcommands: read
# ---------------------------------------------------------------------------


# --- cmd_add, cmd_update, cmd_update_field, cmd_set_status GUTTED ----------
# Migrated to daemon store endpoint (H2 Wave 3, 2026-05-15). These
# subcommands now route through POST /v1/store/{append,replace,set-field}
# via thin .sh wrappers (pattern-signatures-{add,update,update-field,
# set-status}.sh). Validators and helpers above are kept as they are
# referenced by store_registry.py (lifted verbatim there, mirrored here
# for the surviving bespoke cmd_record_outcome).
# See: mind_api/src/store_registry.py, mind_api/src/endpoints/store.py
# -----------------------------------------------------------------------

def cmd_record_outcome(args):
    rec_id = args.rec_id
    outcome = args.outcome

    if outcome not in ("CONFIRMED", "CORRECTED"):
        print(f"Invalid outcome: {outcome} (must be CONFIRMED or CORRECTED)", file=sys.stderr)
        sys.exit(1)

    from _fileops import locked_modify_jsonl

    written = {"rec": None}

    def _modifier(items):
        result = find_record_by_id(items, rec_id)
        if result is None:
            raise ValueError(f"Record {rec_id} not found")
        idx, rec = result
        stats = rec.get("outcome_stats", {"total": 0, "confirmed": 0, "accuracy": 0.0})
        stats["total"] = stats.get("total", 0) + 1
        if outcome == "CONFIRMED":
            stats["confirmed"] = stats.get("confirmed", 0) + 1
        stats["accuracy"] = round(stats["confirmed"] / stats["total"], 4) if stats["total"] > 0 else 0.0
        rec["outcome_stats"] = stats
        rec["last_matched"] = date.today().isoformat()
        items[idx] = rec
        written["rec"] = rec
        return items

    try:
        locked_modify_jsonl(LIVE_PATH, _modifier)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(written["rec"], indent=2, ensure_ascii=False))


# cmd_set_status GUTTED — migrated to daemon store endpoint (H2 Wave 3).
# See pattern-signatures-set-status.sh.

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
        # Stamp `created` if the YAML didn't provide one — migration should
        # never produce records that can't pass post-fix validation.
        if "created" not in rec:
            rec["created"] = _stamp_now()
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

    # add, update, update-field, set-status: GUTTED — migrated to daemon
    # store endpoint (H2 Wave 3). Wrappers now call POST /v1/store/*.

    # record-outcome (bespoke — counter+recompute, not pure CRUD)
    p_ro = subparsers.add_parser("record-outcome", help="Record a pattern match outcome")
    p_ro.add_argument("rec_id", type=str, help="Record ID")
    p_ro.add_argument("outcome", type=str, help="Outcome: CONFIRMED or CORRECTED")

    # migrate-yaml (for init-mind.sh bootstrap)
    p_mig = subparsers.add_parser("migrate-yaml", help="Convert YAML pattern-signatures to JSONL")
    p_mig.add_argument("yaml_path", type=str, help="Source YAML file")
    p_mig.add_argument("jsonl_path", type=str, help="Target JSONL file")

    args = parser.parse_args()

    dispatch = {
        "record-outcome": cmd_record_outcome,
        "migrate-yaml": cmd_migrate_yaml,
    }

    try:
        dispatch[args.command](args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
