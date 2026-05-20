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
from datetime import date, datetime
from pathlib import Path

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import PROJECT_ROOT, WORLD_DIR, AGENT_DIR
from _jsonl_helpers import set_nested_field

# Per-agent experience stores (agent directory)
LIVE_PATH = AGENT_DIR / "experience.jsonl" if AGENT_DIR else None
ARCHIVE_PATH = AGENT_DIR / "experience-archive.jsonl" if AGENT_DIR else None
META_PATH = AGENT_DIR / "experience-meta.json" if AGENT_DIR else None
INDEX_PATH = AGENT_DIR / "experiential-index.yaml" if AGENT_DIR else None

# Collective domain stores (world/) — used by recompute-index
PIPELINE_LIVE_PATH = WORLD_DIR / "pipeline.jsonl"
PIPELINE_ARCHIVE_PATH = WORLD_DIR / "pipeline-archive.jsonl"

VALID_TYPES = {"goal_execution", "hypothesis_formation", "research", "reflection", "user_correction", "user_interaction", "execution_reflection", "chat_session"}
ID_RE = re.compile(r"^exp-[a-z0-9._-]+$")

# `created` is SCRIPT-OWNED: stamped at add time by cmd_add / cmd_archive_goal
# from the system clock via _stamp_now(), never read from stdin. Matches
# reasoning-bank.py / guardrails pattern. Callers must NOT supply `created`;
# any stdin value is overwritten. update-field rejects `field == "created"`.
# Format: full ISO datetime (`YYYY-MM-DDTHH:MM:SS`) seconds precision local
# time. Was date-only until  — that caused same-day false-positives
# in experience-staleness-check.sh (parses date-only as midnight).
# Single source of truth for the `add` schema — used as both the argparse
# epilog (visible via --help) AND the schema dump on validation error /
# explicit --schema flag. Eliminates the discover-by-failure round-trips
# the LLM hits when authoring experience records (rb-512 pattern).
EXPERIENCE_ADD_SCHEMA_TEXT = (
    "Stdin JSON fields:\n"
    "  REQUIRED:\n"
    "    id            — must match exp-{slug} (lowercase a-z0-9._-)\n"
    "    type          — one of: goal_execution, hypothesis_formation,\n"
    "                    research, reflection, user_correction,\n"
    "                    user_interaction, execution_reflection,\n"
    "                    chat_session\n"
    "    category      — domain category string\n"
    "    summary       — one-line description\n"
    "    content_path  — path to the .md trace file (must exist on disk)\n"
    "  OPTIONAL: goal_id, hypothesis_id, tree_nodes_related (list of strs),\n"
    "    verbatim_anchors (list of {key, content} objects),\n"
    "    reasoning_chain (list of strs), retrieval_stats (object),\n"
    "    archived (bool), archived_date\n"
    "  SCRIPT-OWNED: created — stamped by the script, do NOT supply on stdin\n"
    "\n"
    "Example:\n"
    "  echo '{\"id\":\"exp-foo\",\"type\":\"research\",\"category\":\"npc\",\n"
    "    \"summary\":\"...\",\"content_path\":\"bravo/experience/foo.md\"}' \\\n"
    "    | experience-add.sh"
)

REQUIRED_FIELDS = {"id", "type", "category", "summary", "content_path"}
DEFAULT_FIELDS = {
    "goal_id": None,
    "hypothesis_id": None,
    "tree_nodes_related": [],
    "verbatim_anchors": [],
    "reasoning_chain": [],             # Decision trajectory from execution diary
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
    """Append one JSON line to a JSONL file, creating it if needed.

    Delegates to _fileops.locked_append_jsonl so concurrent same-agent
    writers (sq-018 racing Phase 4.25, parallel /encode-session firings,
    subprocess+main interleaving) cannot interleave partial lines.
    AGENT_DIR-rooted files (LIVE_PATH = <agent>/experience.jsonl) skip
    history+changelog inside locked_append_jsonl via base_dir=None — net
    behavior is open/append/close gated by a sibling .lock file.
    g-115-447 / msg-20260508-150112-bravo-827 (fresh-eyes-code finding).
    """
    from _fileops import locked_append_jsonl
    locked_append_jsonl(path, item)

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
    """Apply defaults for missing fields. Mutates and returns rec.

    Also fills in missing sub-keys inside dict-valued defaults (e.g.,
    retrieval_stats.times_noise when a legacy record pre-dates that counter).
    This lets new counters be added to DEFAULT_FIELDS without a migration
    pass — existing records auto-backfill on their next read/write path.
    Ported from reasoning-bank.py normalize_record (g-240-31, aligning
    experience normalization with the reasoning-bank style).
    """
    for field, default in DEFAULT_FIELDS.items():
        if field not in rec:
            if isinstance(default, (dict, list)):
                rec[field] = json.loads(json.dumps(default))  # deep copy
            else:
                rec[field] = default
        elif isinstance(default, dict) and isinstance(rec[field], dict):
            for sub_key, sub_default in default.items():
                if sub_key not in rec[field]:
                    rec[field][sub_key] = sub_default
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

# set_nested_field now lives in _jsonl_helpers.py () — imported at top.

# ---------------------------------------------------------------------------
# Subcommands: read
# ---------------------------------------------------------------------------

def _stamp_now():
    """Return now as ISO 8601 datetime (seconds precision, local time).

    Single source of truth for experience `created` timestamps. Full
    datetime required by experience-staleness-check.sh — date-only caused
    same-day false-positives (g-248-37, rb-428 family): staleness check
    parses date-only as midnight, so any entry older than the threshold
    hours fired the force_experience_archival sentinel on the same day
    it was written. Existing date-only entries in experience.jsonl remain
    parseable by datetime.fromisoformat and their age-from-midnight is
    correctly >threshold for older entries.
    Callers MUST use this — never trust an LLM-supplied `created`.
    """
    return datetime.now().isoformat(timespec="seconds")

def cmd_add(args):
    if getattr(args, "schema", False):
        print(EXPERIENCE_ADD_SCHEMA_TEXT)
        return
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

    # Script owns `created`: overwrite whatever stdin supplied. See _stamp_now
    # and the REQUIRED_FIELDS comment. Matches rb_add / guard_add semantics.
    rec["created"] = _stamp_now()

    try:
        validate_record(rec)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        print("", file=sys.stderr)
        print(EXPERIENCE_ADD_SCHEMA_TEXT, file=sys.stderr)
        sys.exit(1)

    # Experience IDs are exp-{slug} strings supplied by the caller (slugs
    # are derived from goal id / source). No auto-id path — only the dup
    # check needs to move inside the lock to close the race window.
    archive = read_jsonl(ARCHIVE_PATH)

    from _fileops import locked_append_jsonl_with_allocator

    def _build(items):
        check_no_duplicate_id(items, rec["id"], archive)
        return rec

    try:
        written = locked_append_jsonl_with_allocator(LIVE_PATH, _build)
    except ValueError as e:
        msg = str(e)
        if "Duplicate" in msg:
            print(f"Duplicate error: {msg}", file=sys.stderr)
        else:
            print(f"Validation error: {msg}", file=sys.stderr)
        sys.exit(1)

    _update_meta()

    print(json.dumps(written, indent=2, ensure_ascii=False))

def cmd_archive_goal(args):
    """Phase 4.25 bash-enforced goal-execution experience archive.

    Replaces the LLM-assembled write path with one subcommand call. The caller
    pre-writes a reasoning-trace .md (to any path). This subcommand validates
    the trace, moves it to the canonical content_path, assembles the record
    from CLI args + optional stdin JSON, and appends to experience.jsonl via
    the same add() path.

    Stderr warnings fire when trace body or summary looks too short to be a
    real archive (drift indicator — caller abbreviated under context pressure).
    """
    if AGENT_DIR is None:
        print("Error: no agent bound (MIND_AGENT unset)", file=sys.stderr)
        sys.exit(1)

    # Optional stdin JSON for verbatim_anchors / tree_nodes_related / retrieval_audit / etc.
    extra = {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                extra = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"Invalid stdin JSON: {e}", file=sys.stderr)
                sys.exit(1)
            if not isinstance(extra, dict):
                print("Stdin JSON must be an object", file=sys.stderr)
                sys.exit(1)

    goal_id = args.goal
    skill_slug = args.skill_slug
    category = args.category
    summary = args.summary
    trace_src = Path(args.trace_file)

    # Validate trace file exists and has content
    if not trace_src.is_absolute():
        trace_src = PROJECT_ROOT / trace_src
    if not trace_src.exists():
        print(f"Error: trace-file does not exist: {trace_src}", file=sys.stderr)
        sys.exit(1)
    trace_bytes = trace_src.stat().st_size
    if trace_bytes == 0:
        print(f"Error: trace-file is empty: {trace_src}", file=sys.stderr)
        sys.exit(1)

    # Drift warnings — do NOT block, just signal.
    MIN_TRACE_BYTES = 200
    MIN_SUMMARY_CHARS = 20
    if trace_bytes < MIN_TRACE_BYTES:
        print(f"WARN: trace-file is only {trace_bytes} bytes (<{MIN_TRACE_BYTES}). "
              f"Phase 4.25 may have abbreviated under context pressure.",
              file=sys.stderr)
    if len(summary.strip()) < MIN_SUMMARY_CHARS:
        print(f"WARN: summary is only {len(summary.strip())} chars (<{MIN_SUMMARY_CHARS}). "
              f"Phase 4.25 summary looks truncated.",
              file=sys.stderr)

    experience_id = f"exp-{goal_id}-{skill_slug}"
    if not ID_RE.match(experience_id):
        print(f"Error: computed experience id fails validation: {experience_id}", file=sys.stderr)
        sys.exit(1)

    # Canonical content path, relative to PROJECT_ROOT for portability.
    content_dir = AGENT_DIR / "experience"
    content_dir.mkdir(parents=True, exist_ok=True)
    canonical = content_dir / f"{experience_id}.md"
    try:
        content_path_rel = str(canonical.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        content_path_rel = str(canonical).replace("\\", "/")

    # Early duplicate-check BEFORE filesystem move — read JSONL and fail fast
    # if the ID is already present. Catches the most common late-stage
    # failure (re-archival of the same goal.skill_slug pair) without
    # creating an orphan .md.
    items = read_jsonl(LIVE_PATH)
    archive = read_jsonl(ARCHIVE_PATH)
    try:
        check_no_duplicate_id(items, experience_id, archive)
    except ValueError as e:
        print(f"Duplicate error: {e}", file=sys.stderr)
        sys.exit(1)

    # Move trace file into canonical position — must happen BEFORE
    # validate_record, because validate_record enforces that content_path
    # resolves to an existing file. Any schema-validation failure after this
    # point leaves an orphan .md at canonical; the user must manually rm
    # and re-run. This is an accepted tradeoff with validate_record's
    # contract — do NOT move os.replace past validate_record without also
    # splitting the validator into schema-only and filesystem-only phases.
    if trace_src.resolve() != canonical.resolve():
        if canonical.exists():
            print(f"Error: canonical content_path already exists: {canonical}. "
                  f"Refusing to overwrite.", file=sys.stderr)
            sys.exit(1)
        os.replace(str(trace_src), str(canonical))

    # Assemble record — defaults from DEFAULT_FIELDS fill in via normalize_record.
    # Uses _stamp_now() for single-source-of-truth stamping (shared with cmd_add).
    rec = {
        "id": experience_id,
        "type": extra.get("type", "goal_execution"),
        "created": _stamp_now(),
        "category": category,
        "summary": summary,
        "goal_id": goal_id,
        "hypothesis_id": extra.get("hypothesis_id"),
        "tree_nodes_related": extra.get("tree_nodes_related", []),
        "verbatim_anchors": extra.get("verbatim_anchors", []),
        "reasoning_chain": extra.get("reasoning_chain", []),
        "content_path": content_path_rel,
    }
    # Optional pass-throughs — only set when caller provided them; otherwise
    # let normalize_record apply DEFAULT_FIELDS.
    for k in ("retrieval_audit", "enabled_by", "temporal_credit"):
        if k in extra:
            rec[k] = extra[k]

    rec = normalize_record(rec)
    try:
        validate_record(rec)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    append_jsonl(LIVE_PATH, rec)
    _update_meta()

    print(json.dumps(rec, indent=2, ensure_ascii=False))

def cmd_update_field(args):
    rec_id = args.rec_id
    field = args.field
    value = parse_value(args.value)

    # `created` is immutable post-write — see _stamp_now. Back-door edits
    # through update-field are rejected. Matches rb_update_field / guard_update_field.
    if field == "created" or field.startswith("created."):
        print("Error: `created` is script-stamped at add time and cannot be updated.", file=sys.stderr)
        sys.exit(1)

    # Probe live + archive to determine target. See pipeline.cmd_update_field
    # for the same probe-then-locked-modify pattern and stale-probe rationale.
    if find_record_by_id(read_jsonl(LIVE_PATH), rec_id) is not None:
        target_path = LIVE_PATH
    elif find_record_by_id(read_jsonl(ARCHIVE_PATH), rec_id) is not None:
        target_path = ARCHIVE_PATH
    else:
        print(f"Record {rec_id} not found", file=sys.stderr)
        sys.exit(1)

    from _fileops import locked_modify_jsonl

    written = {"rec": None}

    def _modifier(items):
        result = find_record_by_id(items, rec_id)
        if result is None:
            raise ValueError(f"Record {rec_id} not found")
        idx, rec = result
        # Self-heal legacy records missing default fields added after creation.
        rec = normalize_record(rec)
        # Symmetric Option A reject ( / zeta/reports/-decision).
        # Pre-2026-05-10 this site silently honored dotted paths via
        # set_nested_field; per the decision, dotted paths now fail loud.
        if "." in field:
            print(
                f"BLOCKED: dotted field name '{field}' is not supported by "
                f"experience.py update-field. This script writes flat top-level "
                f"keys only. To write a nested value, pass the parent field "
                f"with a full nested JSON. For dotted-path navigation, use "
                f"team-state.py.",
                file=sys.stderr,
            )
            sys.exit(1)
        rec[field] = value
        # DO NOT REMOVE: full-record validation after any field update — see
        # guard-330 / rb-364. Update-field must not be a back-door around
        # add-time validation. ORDER MATTERS: validate BEFORE recompute, so
        # direct writes to derived fields (e.g., utility_ratio) are caught
        # instead of silently clobbered by the recompute step.
        validate_record(rec)
        # Recalculate utility_ratio when retrieval stats change (after validate
        # — recompute output is deterministic). Strict lookups: normalize_record
        # deep-backfills missing sub-keys, so retrieval_count/times_useful are
        # guaranteed present here. Fail loud if not ().
        stats = rec.get("retrieval_stats")
        if stats and isinstance(stats, dict) and field.startswith("retrieval_stats."):
            rc = stats["retrieval_count"]
            tu = stats["times_useful"]
            stats["utility_ratio"] = round(tu / max(rc, 1), 4)
        items[idx] = rec
        written["rec"] = rec
        return items

    try:
        locked_modify_jsonl(target_path, _modifier)
    except ValueError as e:
        msg = str(e)
        if "not found" in msg:
            print(msg, file=sys.stderr)
        else:
            print(f"Validation error: {msg}", file=sys.stderr)
        sys.exit(1)
    _update_meta()
    print(json.dumps(written["rec"], indent=2, ensure_ascii=False))

def cmd_archive_sweep(args):
    """Sweep old/low-utility experience records to archive.

    Pre-work: snapshot LIVE (unlocked read) and pick archive candidates.
    Phase 1: append candidates to ARCHIVE under archive's lock (in-lock dedup
    against existing archive ids).
    Phase 2: rewrite LIVE under live's lock, filtering by archived_ids
    against a FRESH in-lock read — so concurrent appends or non-archived-
    record mutations during the sweep are preserved.

    Two locks total (one per file), no nesting. Concurrent writers to LIVE
    during phases 1-2 see the live records still present until phase 2
    completes. Worst case: a record is BOTH selected for archival AND
    concurrently mutated between snapshot and phase 2 — the archive copy
    is the snapshot version (mutation lost in archive), and the record is
    removed from live regardless of mutation (mutation lost in live).
    Mitigation: archive-sweep targets records that are by definition stale
    (age >= 30 days, retrieval_count == 0), so concurrent mutation of
    candidate records is exceptionally unlikely.
    """
    items = read_jsonl(LIVE_PATH)
    today = date.today()

    # Only to_archive is computed here; the live-file rewrite in phase 2 below
    # filters by archived_ids against a fresh in-lock read, so a separate
    # `remaining` list would be both redundant AND stale (it would miss
    # concurrent appends that landed between the snapshot above and the lock
    # in phase 2).
    to_archive = []

    for rec in items:
        if rec.get("archived", False):
            # Already marked archived but still in live file — move it
            to_archive.append(rec)
            continue

        created_str = rec.get("created", "")
        try:
            created_date = date.fromisoformat(created_str[:10])
        except (ValueError, TypeError):
            continue

        age_days = (today - created_date).days
        stats = rec.get("retrieval_stats", {})
        retrieval_count = stats.get("retrieval_count", 0)
        utility_ratio = stats.get("utility_ratio", 0.0)

        # Protection: never archive high-value experiences
        if retrieval_count >= PROTECT_MIN_RETRIEVAL_COUNT and utility_ratio >= PROTECT_MIN_UTILITY_RATIO:
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

    if not to_archive:
        print("0")
        return

    from _fileops import locked_modify_jsonl

    # Phase 1: append to archive under archive's lock. Read-modify-write
    # so concurrent archive operations don't clobber each other.
    archived_ids = {r["id"] for r in to_archive}

    def _archive_modifier(arch_items):
        existing = {r.get("id") for r in arch_items}
        for r in to_archive:
            if r["id"] not in existing:
                arch_items.append(r)
        return arch_items

    locked_modify_jsonl(ARCHIVE_PATH, _archive_modifier)

    # Phase 2: rewrite live with archived records filtered out, under
    # live's lock. The remaining set is computed afresh from live's current
    # state — concurrent appends or updates to live between the snapshot
    # above and now are preserved. Only records in archived_ids are dropped.
    def _live_modifier(live_items):
        return [r for r in live_items if r.get("id") not in archived_ids]

    locked_modify_jsonl(LIVE_PATH, _live_modifier)

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

    # Symmetric Option A reject (). See update-field comment above.
    if "." in field:
        print(
            f"BLOCKED: dotted field name '{field}' is not supported by "
            f"experience.py meta-update. This script writes flat top-level "
            f"keys only. For dotted-path navigation, use team-state.py.",
            file=sys.stderr,
        )
        sys.exit(1)
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

    # add
    p_add = subparsers.add_parser(
        "add",
        help="Add record from stdin JSON",
        epilog=EXPERIENCE_ADD_SCHEMA_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_add.add_argument(
        "--schema",
        action="store_true",
        help="Print stdin JSON schema and exit (no record written)",
    )

    # archive-goal (Phase 4.25 bash-enforced write path)
    p_ag = subparsers.add_parser(
        "archive-goal",
        help="Phase 4.25: archive a goal_execution experience record from a pre-written trace file + CLI args + optional stdin JSON",
    )
    p_ag.add_argument("--goal", type=str, required=True, help="Goal ID (e.g., g-001-12)")
    p_ag.add_argument("--skill-slug", type=str, required=True,
                      help="Skill name slug (no slash, no spaces — e.g., aspirations-execute)")
    p_ag.add_argument("--category", type=str, required=True, help="Experience category")
    p_ag.add_argument("--summary", type=str, required=True,
                      help="One-line summary of what was learned (min ~20 chars)")
    p_ag.add_argument("--trace-file", type=str, required=True,
                      help="Path to pre-written markdown trace file (will be moved to canonical content_path)")

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
        "add": cmd_add,
        "archive-goal": cmd_archive_goal,
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
