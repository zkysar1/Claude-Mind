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

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import META_DIR, CONFIG_DIR

LIVE_PATH = META_DIR / "spark-questions.jsonl"

VALID_TYPES = {"question", "candidate"}
VALID_STATUSES = {"active", "retired"}
# Allow any digit count — auto-id allocator produces zero-padded ids inside
# the file lock (sq-001..sq-999, then sq-1000+; sq-c01..sq-c99, then sq-c100+).
# Strict-padded regex would silently fail on overflow. Same fix applied to
# rb/guard/sig regexes when those allocators moved inside the lock.
QUESTION_ID_RE = re.compile(r"^sq-\d+$")
CANDIDATE_ID_RE = re.compile(r"^sq-c\d+$")

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

def validate_record(rec, *, skip_id_check=False):
    """Validate a spark question record dict. Raises ValueError on invalid.

    skip_id_check=True is used by the auto-id path in cmd_add — see
    reasoning-bank.validate_rb_record for the same rationale.
    """
    rec_type = rec.get("type")
    if rec_type not in VALID_TYPES:
        raise ValueError(f"Invalid type: {rec_type} (expected 'question' or 'candidate')")

    if rec_type == "question":
        required = QUESTION_REQUIRED_FIELDS - ({"id"} if skip_id_check else set())
        missing = required - set(rec.keys())
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        if not skip_id_check:
            if not QUESTION_ID_RE.match(rec["id"]):
                raise ValueError(f"Invalid question ID format: {rec['id']} (expected sq-NNN)")
        if rec.get("status") and rec["status"] not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {rec['status']}")
    else:
        required = CANDIDATE_REQUIRED_FIELDS - ({"id"} if skip_id_check else set())
        missing = required - set(rec.keys())
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        if not skip_id_check:
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


# --- cmd_add, cmd_update_field, cmd_retire GUTTED --------------------------
# Migrated to daemon store endpoint (H2 Wave 3, 2026-05-15). These
# subcommands now route through POST /v1/store/{append,set-field}
# via thin .sh wrappers (spark-questions-{add,update-field,retire}.sh).
# Validators and helpers above are kept as they are referenced by
# store_registry.py (lifted verbatim there, mirrored here for the
# surviving bespoke cmd_increment and cmd_promote).
# See: mind_api/src/store_registry.py, mind_api/src/endpoints/store.py
# -----------------------------------------------------------------------

def cmd_increment(args):
    rec_id = args.rec_id
    field = args.field

    if field not in ("times_asked", "sparks_generated"):
        print(f"Can only increment 'times_asked' or 'sparks_generated', got: {field}",
              file=sys.stderr)
        sys.exit(1)

    from _fileops import locked_modify_jsonl

    written = {"rec": None}

    def _modifier(items):
        result = find_record_by_id(items, rec_id)
        if result is None:
            raise ValueError(f"Record {rec_id} not found")
        idx, rec = result
        if rec.get("type") != "question":
            raise ValueError(
                f"Record {rec_id} is not a question (type={rec.get('type')})")
        rec[field] = rec.get(field, 0) + 1
        # Recompute yield_rate
        rec["yield_rate"] = round(
            rec.get("sparks_generated", 0) / max(rec.get("times_asked", 0), 1), 4
        )
        items[idx] = rec
        written["rec"] = rec
        return items

    try:
        locked_modify_jsonl(LIVE_PATH, _modifier)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(written["rec"], indent=2, ensure_ascii=False))


# cmd_retire GUTTED — migrated to daemon store endpoint (H2 Wave 3).
# See spark-questions-retire.sh.

def cmd_promote(args):
    candidate_id = args.candidate_id
    new_id = args.new_id

    if not QUESTION_ID_RE.match(new_id):
        print(f"Invalid new ID format: {new_id} (expected sq-NNN)", file=sys.stderr)
        sys.exit(1)

    from _fileops import locked_modify_jsonl

    written = {"rec": None}

    def _modifier(items):
        # Find candidate
        result = find_record_by_id(items, candidate_id)
        if result is None:
            raise ValueError(f"Candidate {candidate_id} not found")
        idx, rec = result
        if rec.get("type") != "candidate":
            raise ValueError(
                f"Record {candidate_id} is not a candidate (type={rec.get('type')})")
        # A RETIRED candidate must never reach the live spark set. Retiring is
        # the only safe way to neutralise a row on this union-by-id store
        # (guard-1072: DELETE does not survive the cross-box merge, MARK does),
        # so without this check the mark is cosmetic — the row stays one
        # `promote` call from firing on real goals. Measured : 2 of 7
        # candidates were leaked test fixtures and both passed every filter here.
        if rec.get("status") == "retired":
            raise ValueError(
                f"Candidate {candidate_id} is retired and cannot be promoted")
        # Check new ID doesn't already exist (inside lock — was racy outside)
        check_no_duplicate_id(items, new_id)
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
        written["rec"] = rec
        return items

    try:
        locked_modify_jsonl(LIVE_PATH, _modifier)
    except ValueError as e:
        msg = str(e)
        if "Duplicate record ID" in msg:
            print(f"Duplicate error: {msg}", file=sys.stderr)
        else:
            print(msg, file=sys.stderr)
        sys.exit(1)

    # Sync framework config so new agents get the promoted question as active.
    # This keeps core/config/spark-questions.yaml and runtime in agreement.
    _sync_framework_promotion(candidate_id, new_id, written["rec"])

    print(json.dumps(written["rec"], indent=2, ensure_ascii=False))

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

    # add, update-field, retire: GUTTED — migrated to daemon store endpoint
    # (H2 Wave 3). Wrappers now call POST /v1/store/*.

    # increment (bespoke — flat counters + type check + recompute; the generic
    # store/increment handler supports nested-prefix counters only)
    p_inc = subparsers.add_parser("increment", help="Atomically increment a counter and recompute yield_rate")
    p_inc.add_argument("rec_id", type=str, help="Record ID")
    p_inc.add_argument("field", type=str, help="Field to increment (times_asked or sparks_generated)")

    # promote (bespoke — candidate->active reformat + id change)
    p_promo = subparsers.add_parser("promote", help="Promote a candidate to active question")
    p_promo.add_argument("candidate_id", type=str, help="Candidate ID to promote")
    p_promo.add_argument("new_id", type=str, help="New question ID (sq-NNN format)")

    # migrate-yaml (for init-mind.sh bootstrap)
    p_mig = subparsers.add_parser("migrate-yaml", help="Convert YAML spark-questions to JSONL")
    p_mig.add_argument("yaml_path", type=str, help="Source YAML file")
    p_mig.add_argument("jsonl_path", type=str, help="Target JSONL file")

    args = parser.parse_args()

    dispatch = {
        "increment": cmd_increment,
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
