#!/usr/bin/env python3
"""Hypothesis pipeline engine for JSONL-based pipeline management.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import WORLD_DIR
from _jsonl_helpers import set_nested_field

LIVE_PATH = WORLD_DIR / "pipeline.jsonl"
ARCHIVE_PATH = WORLD_DIR / "pipeline-archive.jsonl"
META_PATH = WORLD_DIR / "pipeline-meta.json"

# Pipeline stages — single source of truth for the enum (drives validate_record,
# cmd_read --stage, cmd_move, compute_meta stage_counts, and position-typo reject).
# "evaluating" was retired 2026-04-19 (rb-363) — zero writers ever existed; do not
# re-add without identifying a real writer call site first.
# "measurement-pending" added 2026-05-08 ( / rb-754) — distinguishes
# hypotheses past resolves_no_earlier_than whose measurement_channel returned
# no data from genuinely-active hypotheses. Transitions: active→measurement-pending
# (set by /review-hypotheses --resolve when channel produces no value),
# measurement-pending→resolved (when channel finally produces data),
# measurement-pending→archived (at resolves_by absolute deadline).
VALID_STAGES = {"discovered", "active", "measurement-pending", "resolved", "archived"}
VALID_HORIZONS = {"micro", "session", "short", "long"}
VALID_TYPES = {"high-conviction", "calibration", "exploration", "contrarian"}
VALID_OUTCOMES = {"CONFIRMED", "CORRECTED", "EXPIRED", "UNRESOLVABLE"}
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_[a-z0-9-]+$")

# Single source of truth for the `add` schema — used as both the argparse
# epilog (visible via --help) AND the schema dump on validation error /
# explicit --schema flag. Eliminates the discover-by-failure round-trips
# the LLM hits when authoring pipeline records (rb-512 pattern).
PIPELINE_ADD_SCHEMA_TEXT = (
    "Stdin JSON fields:\n"
    "  REQUIRED:\n"
    "    id           — must match YYYY-MM-DD_{slug} (slug = lowercase a-z0-9-)\n"
    "    title        — short hypothesis statement\n"
    "    stage        — one of: discovered, active, measurement-pending,\n"
    "                   resolved, archived\n"
    "    horizon      — one of: micro, session, short, long\n"
    "    type         — one of: high-conviction, calibration, exploration,\n"
    "                   contrarian\n"
    "    confidence   — float [0.0, 1.0]\n"
    "    position     — narrative claim (string); not the stage enum value\n"
    "    formed_date  — ISO date YYYY-MM-DD\n"
    "    category     — domain category string\n"
    "  AUTO-DEFAULTED if absent: stage='discovered', slug, rationale,\n"
    "    outcome, evidence_for, evidence_against, mitigations, links,\n"
    "    notes, last_reviewed\n"
    "\n"
    "Example:\n"
    "  echo '{\"id\":\"2026-04-25_foo\",\"title\":\"...\",\"stage\":\"discovered\",\n"
    "    \"horizon\":\"session\",\"type\":\"calibration\",\"confidence\":0.5,\n"
    "    \"position\":\"...\",\"formed_date\":\"2026-04-25\",\n"
    "    \"category\":\"npc\"}' | pipeline-add.sh"
)

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

# set_nested_field now lives in _jsonl_helpers.py () — imported at top.

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
    """Atomically write a list of dicts as JSONL with locking and history."""
    from _fileops import locked_write_jsonl
    locked_write_jsonl(path, items)

def append_jsonl(path, item):
    """Append one JSON line to a JSONL file with locking and history."""
    from _fileops import locked_append_jsonl
    locked_append_jsonl(path, item)

def read_json(path):
    """Read a JSON file and return a dict."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json(path, data):
    """Atomically write a dict as pretty-printed JSON with locking and history."""
    from _fileops import locked_write_json
    locked_write_json(path, data)

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

    # Surprise field must be int or None. Qualifier strings ("none"/"mild"/
    # "low"/"moderate"/"high") were drift from /review-hypotheses --resolve;
    # 18 records accumulated over 3 weeks before  remediated via
    # QUALIFIER_MAP backfill (rb-493). This gate closes the write-time hole.
    # isinstance(True, int) == True, so exclude booleans explicitly.
    surprise = rec.get("surprise")
    if surprise is not None:
        if isinstance(surprise, bool) or not isinstance(surprise, int):
            raise ValueError(
                f"Invalid surprise: {surprise!r} (must be int or null; "
                f"qualifier strings like 'low'/'moderate' were the g-002-13 "
                f"drift class — see rb-493 for backfill pattern)"
            )

    confidence = rec["confidence"]
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        raise ValueError(f"Invalid confidence: {confidence} (must be 0.0-1.0)")

    # Position content validation — rejects prompt-template artifacts that look like
    # valid strings but are actually the wrong field value. Encoded from the 2026-04-10
    # to 2026-04-13 malformed-cohort incident (): 6 hypotheses had position
    # fields containing "for", stage names, or category names. Accepts: (a) YES/NO
    # prefixes (any case), (b) length >=20 (genuine multi-word claim), or
    # (c) multi-word short answers — rejects everything else.
    position = rec.get("position")
    if isinstance(position, str):
        pos_stripped = position.strip()
        if pos_stripped.lower() in VALID_STAGES:
            raise ValueError(
                f"Invalid position: '{position}' (matches a pipeline stage name — "
                f"the stage field was likely written to position by mistake)"
            )
        if pos_stripped.lower() in VALID_TYPES:
            raise ValueError(
                f"Invalid position: '{position}' (matches a hypothesis type — "
                f"the type field was likely written to position by mistake)"
            )
        starts_with_verdict = pos_stripped.upper().startswith(("YES", "NO"))
        is_long_claim = len(pos_stripped) >= 20
        is_multi_word = len(pos_stripped.split()) >= 2
        if not (starts_with_verdict or is_long_claim or is_multi_word):
            raise ValueError(
                f"Invalid position: '{position}' (must be YES/NO, >=20 chars, or "
                f"a multi-word claim — likely a prompt-template artifact)"
            )

def validate_formation_quality(rec):
    """Formation-quality gate (). Rejects hypotheses with formation
    problems that distort downstream health metrics: empty claim, missing
    resolution anchor, short-horizon without measurement channel.

    Source: bravo session-53 iter-17 expired 6/11 active hypotheses with
    formation-quality problems (not prediction failures) — 3 had empty
    claim, 3 had no resolution method. These filled the pipeline with
    philosophical assertions that made flowing-count drop while
    active-count stayed high.

    Gate is soft-exempt for stage='discovered' so draft records can still
    be seeded before fleshing out. Strict for active/resolved stages.

    Raises ValueError on invalid.
    """
    stage = rec.get("stage", "discovered")
    horizon = rec.get("horizon", "")

    # (a) claim field: if present, must be non-empty and >=20 chars. If
    # absent, require the title to carry the claim (title is already a
    # REQUIRED_FIELDS check). Skeletal discovered records with missing
    # claim fall back to title; active/resolved records must have claim.
    claim = rec.get("claim")
    if stage != "discovered":
        if not isinstance(claim, str) or len(claim.strip()) < 20:
            raise ValueError(
                "Invalid claim: non-discovered hypotheses must carry a "
                "claim field >=20 chars (got "
                f"{'missing' if claim is None else repr(claim)[:40]}). "
                "Populate 'claim' with the testable assertion before "
                "moving to active/resolved."
            )
    elif isinstance(claim, str) and claim.strip() and len(claim.strip()) < 20:
        # discovered-stage claim field present but too short — reject
        # (allow missing, but not malformed).
        raise ValueError(
            f"Invalid claim: '{claim}' — if claim field is present it "
            f"must be >=20 chars; leave it unset to seed a skeletal "
            f"discovered record, or write the full testable assertion."
        )

    # (b) resolution anchor: short/long horizons must name a resolution
    # date AND a resolution method (criteria / rationale). Micro/session
    # horizons have implicit self-check verification so are exempt.
    if horizon in ("short", "long"):
        if stage != "discovered":
            if not rec.get("resolves_by"):
                raise ValueError(
                    f"Missing resolves_by: {horizon}-horizon hypotheses at "
                    f"stage={stage} must name a resolution date."
                )
            resolution_method = (
                rec.get("resolution_criteria")
                or rec.get("resolution_method")
                or rec.get("rationale")
                or ""
            )
            if not isinstance(resolution_method, str) or len(resolution_method.strip()) < 10:
                raise ValueError(
                    f"Missing resolution method: {horizon}-horizon "
                    f"hypotheses at stage={stage} must populate one of "
                    f"resolution_criteria / resolution_method / rationale "
                    f"with >=10 chars explaining HOW the outcome gets "
                    f"decided. Empty or <10-char rationale treated as "
                    f"missing."
                )

    # (c) short-horizon measurement channel — short horizon (hours to days)
    # hypotheses at active stage must declare a measurement channel so the
    # review loop has something to read. Long-horizon exempt (external
    # events). Discovered-stage exempt (still drafting).
    if horizon == "short" and stage == "active":
        channel = (
            rec.get("measurement_channel")
            or rec.get("verification_channel")
            or rec.get("resolution_source")
            or ""
        )
        if not isinstance(channel, str) or len(channel.strip()) < 5:
            raise ValueError(
                "Missing measurement_channel: short-horizon active "
                "hypotheses must declare a measurement_channel (or "
                "verification_channel / resolution_source) naming the "
                "artifact, log, script, or metric that will settle the "
                "prediction. Empty or <5-char field treated as missing."
            )

# ---------------------------------------------------------------------------
# Resolution-evidence requirement ()
# ---------------------------------------------------------------------------
# Mirror of mind_api/src/world/pipeline_write.py (the live single-writer).
# Every CONFIRMED/CORRECTED resolution must carry >=1 verifiable external-
# evidence pointer so the calibration number is independently auditable
# ( audit: ~53% of CONFIRMED/CORRECTED records had no outcome_detail).
# Generous detector (any one shape satisfies it); EXPIRED/UNRESOLVABLE exempt;
# evidence_override field is the escape hatch. Kept in sync per DECISIONS.md #3.

# Recognized evidence-pointer shapes in free-text resolution fields.
_EVIDENCE_PATTERNS = (
    re.compile(r"\bg-(?:\d{3}-\d{2,4}|xw-\d{8}T\d{6}-\d{2})\b"),  # goal-id (incl xw, )
    re.compile(r"\b(?:rb|guard|sig|sa|bel)-\d+\b"),             # rb/guardrail/sig/...
    re.compile(r"\bexp-[a-z0-9][\w-]+", re.I),                  # experience-ref
    re.compile(r"\bmsg-\d{8}-"),                                # board message id
    re.compile(r"[\w./-]+\.(?:py|sh|md|lua|js|ts|yaml|jsonl?|txt):\d+"),  # file:line
    re.compile(r"\b[\w-]+\.(?:sh|py)\b"),                       # canonical-script name
    re.compile(r"\d+(?:\.\d+)?\s*(?:%|pct|percent)", re.I),     # percentage w/ measurement
    re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"),        # commit SHA (hex, >=1 letter)
    re.compile(r"\bsession[\s_-]?\d+\b", re.I),                 # session-id (named)
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-", re.I),  # session-id (UUID)
)

# Outcomes that assert a validated prediction and therefore require evidence.
_EVIDENCE_REQUIRED_OUTCOMES = ("CONFIRMED", "CORRECTED")

def has_resolution_evidence(rec):
    """True if the record carries >=1 verifiable evidence pointer.

    Checks structured pointer fields first (experience_ref, evidence_for),
    then scans the free-text resolution fields for any recognized pointer
    shape. Generous by design. Mirror of pipeline_write._has_resolution_evidence.
    """
    exp_ref = rec.get("experience_ref")
    if isinstance(exp_ref, str) and exp_ref.strip():
        return True
    ev_for = rec.get("evidence_for")
    if isinstance(ev_for, str) and ev_for.strip():
        return True
    if isinstance(ev_for, (list, dict)) and ev_for:
        return True
    text_parts = []
    for field in ("outcome_detail", "outcome_notes", "rationale",
                  "verification", "links"):
        val = rec.get(field)
        if isinstance(val, str):
            text_parts.append(val)
    text = "\n".join(text_parts)
    return any(p.search(text) for p in _EVIDENCE_PATTERNS)

def validate_resolution_evidence(rec):
    """Require an external-evidence pointer on CONFIRMED/CORRECTED resolutions.

    No-op for non-accuracy outcomes (None/EXPIRED/UNRESOLVABLE) and for any
    record carrying a non-empty evidence_override reason. Raises ValueError
    otherwise (g-303-27). Mirror of pipeline_write._validate_resolution_evidence.
    """
    if rec.get("outcome") not in _EVIDENCE_REQUIRED_OUTCOMES:
        return
    override = rec.get("evidence_override")
    if isinstance(override, str) and override.strip():
        return
    if has_resolution_evidence(rec):
        return
    raise ValueError(
        "Missing resolution evidence: a CONFIRMED/CORRECTED resolution must "
        "record at least one verifiable external-evidence pointer so the "
        "calibration number is independently auditable (g-303-27). Add one to "
        "outcome_detail (or set experience_ref / evidence_for): a goal-id "
        "(g-NNN-NN), commit SHA, file:line, session-id, an rb-/guard-/exp-/msg- "
        "id, a canonical-script name (foo.sh/foo.py), or a percentage with "
        "measurement context. For a genuinely pointer-free resolution (e.g. a "
        "math proof where the derivation IS the evidence), set evidence_override "
        "to a short reason string."
    )

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
            # Both exist — prefer new name, but copy the old value when the new
            # name is still at its None default and the old carries a real value.
            # DEFAULT_FIELDS pre-seeds surprise=None on every record, so without
            # this copy a {surprise_level: N} update always lost its value to the
            # both-exist branch — surprise stayed None ( /
            # idea:normalize-record-rename-valuedrop). Applies to all renames.
            if rec[new_name] is None and rec[old_name] is not None:
                rec[new_name] = rec[old_name]
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

    # Auto-default resolves_by for discovered short/long hypotheses ().
    # validate_formation_quality soft-exempts stage=discovered from the
    # resolves_by requirement, so a discovered short/long record created
    # without one has no resolution anchor and sits un-resolvable until it is
    # promoted (which re-validates) or manually backfilled -- the orphan class
    #  had to clean up. Default it at creation: short -> +7d,
    # long -> +30d from formed_date (matches the  backfill windows).
    # Non-discovered stages keep the explicit-resolves_by contract enforced
    # by validate_formation_quality.
    if (
        rec.get("stage", "discovered") == "discovered"
        and rec.get("horizon", "") in ("short", "long")
        and not rec.get("resolves_by")
        and rec.get("formed_date")
    ):
        try:
            _formed = datetime.strptime(str(rec["formed_date"])[:10], "%Y-%m-%d").date()
            _window = 7 if rec["horizon"] == "short" else 30
            _anchor = (_formed + timedelta(days=_window)).isoformat()
            rec["resolves_by"] = _anchor
            # Match the  backfill precedent: also set
            # resolves_no_earlier_than to the same anchor so the draft is gated
            # from selection (goal-selector hypothesis-gate) and from premature
            # resolution (review-hypotheses not-due gate) until the window
            # elapses. Preserve any pre-existing resolves_no_earlier_than.
            if not rec.get("resolves_no_earlier_than"):
                rec["resolves_no_earlier_than"] = _anchor
        except (ValueError, TypeError):
            pass  # malformed formed_date -- leave unset; discovered stays exempt

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
            "active": 0,
            "measurement-pending": 0,
            "resolved": 0,
            "archived": 0,
        },
        "accuracy": {
            "total_resolved": 0,
            "confirmed": 0,
            "corrected": 0,
            "accuracy_pct": 0.0,
            "with_evidence": 0,
            "evidence_pct": 0.0,
            "by_strategy": {},
            "by_time_horizon": {},
            "by_depth": {},
            "by_type": {},
            "by_confidence_band": {},
        },
        "micro_hypothesis_stats": {},
    }

def compute_meta(live_items, archive_items):
    """Recompute meta from all records."""
    meta = empty_meta()
    # Dedup by id across live+archive (; mirrors pipeline_write.py
    # _compute_meta): a tombstoned id is present in BOTH files by design —
    # count each hypothesis once (archive copy wins) so accuracy denominators
    # never double-count.
    _by_id = {}
    _no_id = []
    for rec in live_items + archive_items:
        rid = rec.get("id")
        if rid is None:
            _no_id.append(rec)
        else:
            _by_id[rid] = rec  # archive iterates second -> archive copy wins
    all_items = list(_by_id.values()) + _no_id

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

    # Evidence coverage (): share of CONFIRMED/CORRECTED resolutions
    # carrying >=1 verifiable evidence pointer. Mirror of pipeline_write._compute_meta.
    with_evidence = sum(1 for r in resolved_records if has_resolution_evidence(r))
    meta["accuracy"]["with_evidence"] = with_evidence
    meta["accuracy"]["evidence_pct"] = (
        round(with_evidence / len(resolved_records) * 100, 1) if resolved_records else 0.0)

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

    # by_type ( / rb-268): segment resolved records by hypothesis type
    # so the accuracy gate can exclude designed-uncertain `exploration` probes
    # from the calibration-relevant overconfidence signal (canonical incident
    # : aggregate 37.5% fired while high-conviction was 100% and the
    # misses were all in exploration). Symmetric to by_depth above.
    by_type = {}
    for r in resolved_records:
        t = r.get("type")
        if t:
            if t not in by_type:
                by_type[t] = {"confirmed": 0, "total": 0, "pct": 0.0}
            by_type[t]["total"] += 1
            if r["outcome"] == "CONFIRMED":
                by_type[t]["confirmed"] += 1
    for t in by_type.values():
        t["pct"] = round(t["confirmed"] / t["total"] * 100, 1) if t["total"] > 0 else 0.0
    meta["accuracy"]["by_type"] = by_type

    # by_confidence_band ( / rb-323): bucket resolved records by the
    # confidence assigned at hypothesis time so cmd_accuracy can surface WHERE
    # overconfidence concentrates. Bands: high>=0.80, medium 0.65-0.79, low<0.65.
    # Records lacking a numeric confidence (or a bool, which is not a sensible
    # confidence) are skipped — not bucketed.
    by_band = {}
    for r in resolved_records:
        c = r.get("confidence")
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            continue
        band = "high" if c >= 0.80 else "medium" if c >= 0.65 else "low"
        if band not in by_band:
            by_band[band] = {"confirmed": 0, "total": 0, "pct": 0.0}
        by_band[band]["total"] += 1
        if r["outcome"] == "CONFIRMED":
            by_band[band]["confirmed"] += 1
    for b in by_band.values():
        b["pct"] = round(b["confirmed"] / b["total"] * 100, 1) if b["total"] > 0 else 0.0
    meta["accuracy"]["by_confidence_band"] = by_band

    meta["last_updated"] = date.today().isoformat()
    return meta

# ---------------------------------------------------------------------------
# Subcommands: read
# ---------------------------------------------------------------------------

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

    # recompute-meta
    subparsers.add_parser("recompute-meta", help="Full recount from records")

    args = parser.parse_args()

    dispatch = {
        "recompute-meta": cmd_recompute_meta,
    }

    try:
        dispatch[args.command](args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
