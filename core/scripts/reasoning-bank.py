#!/usr/bin/env python3
# domain-leak-exempt: argparse help text for --applies-to flag cites domain
# examples (Roblox, AyoAI services, NPC systems) so the user knows what
# "domain" means at the CLI. The terms are help-text examples, not engine logic.
"""Reasoning bank and guardrails library — constants, validators, and utilities.

Manages two stores:
  - world/reasoning-bank.jsonl  (reasoning bank entries, rb-NNN)
  - world/guardrails.jsonl      (guardrail rules, guard-NNN)

CLI subcommands were removed in H2 Wave 2 (2026-05-15). All add / update-field /
increment operations now route through the daemon generic store endpoint
(mind_api/src/endpoints/store.py + mind_api/src/store_registry.py). This module
is retained as a library for importers that use its constants, validators, and
utility functions.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# g-276-05: force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import WORLD_DIR
from _rb_helpers import is_universal_rb, sort_universal_rbs
from _jsonl_helpers import set_nested_field

def _read_in_flight_goal_id():
    """Read agent_status.<MIND_AGENT>.in_flight.goal_id from team-state.yaml.

    g-240-61 auto-population: rb entries attribute to their originating goal.
    Callers that forget to pass `source_goal` left orphan entries (rb-416, rb-418)
    which tripped aspiration-trajectory.py zero_learning_velocity detection.

    Returns None if: team-state.yaml missing, agent unset, no in_flight block,
    or any parse error. FAIL-OPEN — never block an rb add on attribution lookup.
    """
    try:
        import yaml  # lazy import — only needed in the auto-populate path
    except ImportError:
        return None
    agent = os.environ.get("MIND_AGENT")
    if not agent:
        return None
    try:
        # g-328-27 sharding: row-first read (world/team-state/agents/<agent>.yaml)
        # with core-file residual fallback for un-migrated deployments.
        from _team_state import read_agent_row
        status = read_agent_row(WORLD_DIR, agent,
                                core_path=WORLD_DIR / "team-state.yaml") or {}
        return (status.get("in_flight") or {}).get("goal_id")
    except Exception:
        return None
# EXPERIENCE_REF_RE is the experience-store ID regex — single source of truth
# lives in experience.py. Do not redefine it here; importing keeps the two
# stores' ID format locked together by construction.
from experience import ID_RE as EXPERIENCE_REF_RE

RB_PATH = WORLD_DIR / "reasoning-bank.jsonl"
GUARD_PATH = WORLD_DIR / "guardrails.jsonl"

RB_ID_RE = re.compile(r"^rb-\d+$")  # allow 1+ digits — auto-id allocator
                                     # produces rb-{max+1}; the prior \d{3}
                                     # constraint would silently break on the
                                     # 1000th record.
GUARD_ID_RE = re.compile(r"^guard-\d+$")  # same rationale as RB_ID_RE.

RB_VALID_TYPES = {"success", "failure", "user_provided"}  # user_provided: from /respond Step 7.5 interaction learning
RB_VALID_STATUSES = {"active", "retired"}
# applies_to controls cross-domain surfacing in retrieve.py — see
# _rb_helpers.is_universal_rb. Required field as of 2026-05-10 (P3 #14): the
# legacy None→specific default silently misclassified 294/299 entries that
# were actually framework/domain/any. Forcing explicit choice prevents the
# leaker class from re-forming. None is rejected; the missing-field check in
# validate_rb_record catches absent keys.
RB_VALID_APPLIES_TO = {"any", "framework", "domain", "specific"}
# entry_type (g-306-11, BRD Gap 7a-Mind): optional reasoning-bank taxonomy tag.
# null (the default) = an ordinary reasoning lesson; "procedure" = a reusable
# multi-step how-to that `retrieve.sh --entry-type procedure` can target.
# Additive + null-safe (the rb validator has no unknown-field gate); NO
# embeddings, NO new store. Extend this SET (not the validator body) to add
# future entry types. Kept verbatim-in-sync with mind_api/src/store_registry.py.
RB_VALID_ENTRY_TYPES = {"procedure"}
GUARD_VALID_STATUSES = {"active", "retired"}

# `created` is SCRIPT-OWNED: stamped at write time by rb_add / guard_add from
# the system clock, never read from stdin. See _stamp_now() + CLAUDE.md
# "Timestamps: ALWAYS local system time". Callers must NOT supply `created`;
# any stdin value is overwritten. update-field rejects `field == "created"`.
# This eliminates the whole class of LLM-narrated timestamp drift.
RB_REQUIRED_FIELDS = {"id", "title", "type", "category", "content", "applies_to"}
RB_DEFAULT_FIELDS = {
    "status": "active",
    "description": "",
    "source_hypothesis": None,
    "source_goal": None,  # g-240-61: attribution pointer. Auto-populated by
                          # rb_add from team-state.yaml in_flight.goal_id when
                          # caller omits. Orphan rb entries produced false
                          # zero_learning_velocity signal in aspiration-trajectory.py.
    "outcome": None,
    "failure_lesson": None,
    "preventive_guardrail": None,
    "experience_ref": None,
    "tags": [],
    # entry_type (g-306-11): optional reasoning-bank taxonomy. null = ordinary
    # lesson; "procedure" = reusable multi-step how-to, retrievable via
    # retrieve.sh --entry-type procedure. Additive + null-safe (validator allows
    # null or a value in RB_VALID_ENTRY_TYPES). Mirror of the daemon default in
    # mind_api/src/store_registry.py.
    "entry_type": None,
    # valid_from / valid_to (g-306-35, BRD Gap 5): optional bi-temporal
    # record-level validity interval. null/null (the default) = a record with
    # no explicit validity window (treated as currently-valid by the reader
    # path). On falsification the OLD record gets valid_to=now and a NEW record
    # is inserted with valid_from=now (close-old-insert-new, NOT in-place
    # mutation). Additive + null-safe (no unknown-field gate on RB); validated
    # by _validate_bitemporal. Mirror of the daemon default in
    # mind_api/src/store_registry.py.
    "valid_from": None,
    "valid_to": None,
    # applies_to is REQUIRED (in RB_REQUIRED_FIELDS). Made required 2026-05-10
    # after audit found 299/620 active entries (48%) silently defaulted to
    # None and the absent-→-specific convention misclassified 294 of them
    # (mostly framework/domain/any). Forcing explicit choice prevents the
    # leaker class from re-forming. Valid values: any | framework | domain |
    # specific. Author must pick one.
    "when_to_use": {"conditions": [], "category": ""},
    "utilization": {
        "retrieval_count": 0,
        "last_retrieved": None,
        "times_helpful": 0,
        "times_noise": 0,
        "times_active": 0,
        "times_skipped": 0,
        "times_inferred_helpful": 0,
        "times_inferred_unknown": 0,  # C.3: how many --infer passes left this
                                       # item in the unknown bucket. After
                                       # unknown_threshold consecutive
                                       # unknowns, auto_flagged_for_review fires.
        "times_cited": 0,
        "utilization_score": 0.0,
    },
}

GUARD_REQUIRED_FIELDS = {"id", "rule", "category", "trigger_condition", "source"}  # `created` is script-stamped (see RB_REQUIRED_FIELDS note).
GUARD_DEFAULT_FIELDS = {
    "status": "active",
    "experience_ref": None,
    "trigger_pattern": None,  # g-247-01: optional regex/string pattern for
                              # grep-detectable guardrails. Was silently dropped
                              # for guard-332 and guard-337 until this field and
                              # the GUARD_KNOWN_FIELDS gate landed.
    # valid_from / valid_to (g-306-35, BRD Gap 5): optional bi-temporal validity
    # interval (see RB_DEFAULT_FIELDS for semantics). Added to GUARD_DEFAULT_FIELDS
    # so they flow into GUARD_KNOWN_FIELDS automatically (the unknown-field gate
    # is `set(GUARD_DEFAULT_FIELDS.keys())` | ...), keeping the additive field
    # accepted by the strict guardrail allowlist. Mirror of the daemon default in
    # mind_api/src/store_registry.py.
    "valid_from": None,
    "valid_to": None,
    "when_to_use": {"conditions": [], "category": ""},
    "utilization": {
        "retrieval_count": 0,
        "last_retrieved": None,
        "times_helpful": 0,
        "times_noise": 0,
        "times_active": 0,
        "times_skipped": 0,
        "times_inferred_helpful": 0,
        "times_inferred_unknown": 0,  # C.3: how many --infer passes left this
                                       # item in the unknown bucket. After
                                       # unknown_threshold consecutive
                                       # unknowns, auto_flagged_for_review fires.
        "times_cited": 0,
        "utilization_score": 0.0,
    },
}

# g-247-01: strict allowlist for guardrail fields. Fields outside this set are
# rejected at add/update with a loud error naming the field — silent drops were
# masking caller bugs (guard-332 + guard-337 trigger_pattern lost; reflect-on-
# outcome pseudocode ships `description` which currently vanishes). Rule of
# thumb for extending: a field earns a spot here ONLY if (a) an active SKILL.md
# writer uses it OR (b) it is a core schema element. Fields that drifted into
# legacy records (type, context, times_triggered at top level, phase, triggers,
# action, applies_to, content, confidence, related_patterns, evidence) are
# INTENTIONALLY excluded — their writers either don't exist anymore or write
# junk that health metrics downstream can't interpret.
GUARD_KNOWN_FIELDS = (
    GUARD_REQUIRED_FIELDS
    | set(GUARD_DEFAULT_FIELDS.keys())
    | {
        "created",              # script-stamped at add time (see _stamp_now).
                                # Must be in the allowlist so update-field paths
                                # don't reject existing records that carry it.
        "tags",                 # 119/175 records — widely used label list
        "action_hint",          # 49/175 — remediation hint for the agent
        "severity",             # 30/175 — HIGH/MEDIUM/LOW
        "context_triggers",     # 14/175 — phase/context filter list
        "phases",               # 14/175 — loop phase filter list
        "source_reflection_id", # aspirations-spark writes this (2 records); ref pointer
        "title",                # reflect-on-outcome writes this; short human label
        # B.1 / C.3 curation fields (top-level, optional):
        "next_review_eligible_at",   # ISO date — exemption window written by
                                     # aspirations-curate-memory after KEEP.
                                     # Absent → eligible immediately.
        "auto_flagged_for_review",   # bool — set by utilization-feedback when
                                     # times_inferred_unknown crosses threshold.
                                     # Forces inclusion in B.1 candidate list.
        "retirement_date",      # ISO date — set when status flips to retired
        "retirement_reason",    # short string — why this entry was retired
        "encoded_by",           # agent name — g-306-109. SOURCE WRITER is the
                                # daemon: endpoints/store.py::append via
                                # StoreSpec.author_field. Mirrored here because
                                # this allowlist and the daemon's are a
                                # deliberately-kept-in-sync pair (see the
                                # GUARD_DEFAULT_FIELDS "Mirror of the CLI
                                # default" note in store_registry.py). Every
                                # guardrail the daemon writes now carries this
                                # field, so leaving it out would make the two
                                # validators disagree about what a valid
                                # guardrail IS — the displaced_from failure
                                # shape, where an unallowlisted stamp refuses
                                # every later write to the records carrying it.
                                # No production caller reaches this validator
                                # today (daemon-only architecture); the mirror
                                # is what keeps that still true if one returns.
    }
)

# CRITICAL (rb-245): all counters for guardrails + reasoning-bank live UNDER the
# `utilization` sub-object, never at top level. Do NOT add a top-level
# `times_triggered` here — that field name belongs to pattern-signatures.jsonl,
# a different store with a different schema. Confusing the two caused the
# session-47 iter 51/52 audit retraction (2026-04-17). Extend this set only if
# the new counter is also written by guardrail-check.py / related increment
# paths AND the `utilization` schema in GUARD_DEFAULT_FIELDS /
# REASONING_BANK_DEFAULT_FIELDS above gains the same key.
UTILIZATION_COUNTERS = {
    "retrieval_count", "times_helpful", "times_noise", "times_active", "times_skipped",
    "times_inferred_helpful", "times_inferred_unknown", "times_cited",
}
# times_cited is intentionally excluded from utilization_score — citation during
# encoding is a weaker signal than explicit times_helpful attestation. Track in
# parallel for diagnostics per g-001-109 design.

# RB_ADD_SCHEMA_TEXT and GUARD_ADD_SCHEMA_TEXT removed in H2 Wave 2
# (2026-05-15): CLI subcommands (rb add, guard add, etc.) migrated to the
# generic daemon store endpoint. Record contracts now live in
# mind_api/src/store_registry.py. The remaining symbols below are imported
# by guardrail-check.py, utilization-feedback.py, learning-ratio.py, and
# test files — do NOT delete this module.

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
    """Atomically write a list of dicts as JSONL with locking and history."""
    from _fileops import locked_write_jsonl
    locked_write_jsonl(path, items)

def append_jsonl(path, item):
    """Append one JSON line to a JSONL file with locking and history."""
    from _fileops import locked_append_jsonl
    locked_append_jsonl(path, item)

def _stamp_now():
    """Return current local time as ISO 8601 without microseconds.

    Single source of truth for record creation timestamps. Matches CLAUDE.md
    "Timestamps: ALWAYS local system time" + `$(date +%Y-%m-%dT%H:%M:%S)`.
    Callers MUST use this — never trust an LLM-supplied `created` field.
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

# set_nested_field now lives in _jsonl_helpers.py (g-240-30) — strict version
# refuses to create phantom parents on typo'd paths. Imported at top of file.

# ---------------------------------------------------------------------------
# Helpers: utilization score
# ---------------------------------------------------------------------------

def recompute_utilization_score(rec):
    """Recompute utilization_score = (times_helpful + 0.5*times_inferred_helpful) / (max(retrieval_count, times_helpful + times_inferred_helpful) + 1).

    times_inferred_helpful is half-weighted because it is produced by token-overlap
    inference (utilization-feedback.py --infer) rather than explicit attestation
    or reflect-on-outcome citation. Half-weight matches the tree-node formula at
    utilization-feedback.py:84.

    Also writes utilization_score_v2 (C.1 parallel field). v2 reads times_active
    and times_cited in addition to helpful/inferred, with weights matching the
    composite evidence formula in utilization-stats.py:

        v2 = (helpful + 0.5*inferred + 0.25*active + 1.0*cited)
             / (max(retrieval, helpful + inferred + active + cited) + 1)

    Why parallel: reflect-maintain Step 1b currently filters on `utilization_score`;
    flipping it to v2 would shift the candidate-list ranking overnight. Run both
    fields in parallel for the 30-day soak window, then migrate consumers.

    Callers MUST have run normalize_record(rec, {RB|GUARD}_DEFAULT_FIELDS) first —
    the update/increment paths do this at entry, so every counter key is guaranteed
    present. Missing keys here signal a schema drift and should fail loudly, not
    silently coerce to 0.
    """
    util = rec["utilization"]
    rc = util["retrieval_count"]
    th = util["times_helpful"]
    tih = util["times_inferred_helpful"]
    ta = util.get("times_active", 0)
    tc = util.get("times_cited", 0)
    # Denominator = max(retrievals, credited usages) + 1 (g-115-1959). rb and
    # guardrail entries take DIRECT helpful bumps for context-carried citations
    # (spark strengthen-existing, code-review consultation credit) that never
    # pass a tracked retrieve.sh scan, so th can legitimately exceed rc — under
    # the old max(rc, 1) denominator an untested h=5/rc=0 entry scored 5.0 and
    # permanently outranked every scan-tested entry in sort_universal_rbs
    # (221 rb + 1 guardrail entries live on 2026-07-11). Counting each credited
    # usage as an implied opportunity caps context-only entries at <1.0 with an
    # n-confidence gradient (h=1 -> 0.5, h=9 -> 0.9); the +1 prior shrinks
    # low-n scores. Order-preserving where rc dominates (uniform +1 shift).
    # This DIVERGES from the tree-node utility_ratio formula (tree.py /
    # utilization-feedback.py / tree_write.py keep max(rc, 1)) BY DESIGN: the
    # retrieval engine stamps tree rc on every return, so th<=rc holds there
    # (0 violations across 1175 nodes probed 2026-07-11) and the shared-formula
    # doctrine's precondition is intact for trees only.
    usage_v1 = th + tih
    util["utilization_score"] = round((th + 0.5 * tih) / (max(rc, usage_v1) + 1), 4)
    usage_v2 = th + tih + ta + tc
    util["utilization_score_v2"] = round(
        (th + 0.5 * tih + 0.25 * ta + 1.0 * tc) / (max(rc, usage_v2) + 1), 4
    )

# ---------------------------------------------------------------------------
# Helpers: validation
# ---------------------------------------------------------------------------

_TAG_SEP_RE = re.compile(r"[-_ ]+")

def _normalize_tags(rec):
    """Canonicalize the tags list in-place: lowercase + kebab-case + dedup.

    Preserves order. Skips non-string entries (let strict validation surface
    them downstream). Idempotent — already-canonical tags pass through
    unchanged. Called from both validate_rb_record and validate_guard_record
    so every write path normalizes; legacy records auto-heal on next update.

    Single source of truth for the casing/separator convention. Without this
    the LLM authoring an entry can introduce variants (IAUS vs iaus, SSOT vs
    ssot, work_class vs work-class) that fragment retrieval — same lesson,
    two tags, half the surfacing pressure.
    """
    tags = rec.get("tags")
    if not isinstance(tags, list):
        return
    seen = set()
    out = []
    for t in tags:
        if not isinstance(t, str):
            out.append(t)
            continue
        canonical = _TAG_SEP_RE.sub("-", t.lower().strip())
        if canonical and canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    rec["tags"] = out

def validate_utilization(util):
    """Validate the utilization object shape AND value invariants.

    Required: the counters in UTILIZATION_COUNTERS, the last_retrieved timestamp,
    and the derived utilization_score. Keep in sync with UTILIZATION_COUNTERS and
    the {RB|GUARD}_DEFAULT_FIELDS['utilization'] blocks above.

    Value invariants:
      - Each counter must be a non-negative int (bools rejected — True/False as
        counters is a type bug that would silently masquerade as 1/0).
      - utilization_score must be a non-negative number. recompute_utilization_score
        always produces a value in [0, ∞), so rejecting negatives here catches
        hand-rolled negative scores in full-dict utilization writes.
    """
    if not isinstance(util, dict):
        raise ValueError("utilization must be a dict")
    required_keys = UTILIZATION_COUNTERS | {"last_retrieved", "utilization_score"}
    missing = required_keys - set(util.keys())
    if missing:
        raise ValueError(f"utilization missing fields: {missing}")

    for k in UTILIZATION_COUNTERS:
        v = util[k]
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError(f"utilization.{k} must be a non-negative int, got: {v!r}")

    score = util["utilization_score"]
    if not isinstance(score, (int, float)) or isinstance(score, bool) or score < 0:
        raise ValueError(f"utilization.utilization_score must be a non-negative number, got: {score!r}")

def _validate_bitemporal(rec) -> None:
    """Validate optional bi-temporal validity-interval fields (g-306-35, BRD
    Gap 5): valid_from / valid_to. Each is null/absent (the default) or an
    ISO-8601 local-datetime string in the _stamp_now format
    "%Y-%m-%dT%H:%M:%S". When BOTH are present, valid_from must be <= valid_to
    (a record cannot stop being valid before it started). isinstance guard
    precedes datetime.fromisoformat (B10: a non-str field would raise TypeError
    on parse and 500 the write -- short-circuit to a clean ValueError).
    guard-420: datetime comparison only on parsed datetimes, never on the raw
    strings. Additive + null-safe: existing records without the fields pass
    untouched. Kept verbatim-in-sync across mind_api/src/store_registry.py +
    core/scripts/reasoning-bank.py."""
    parsed = {}
    for _bt_field in ("valid_from", "valid_to"):
        val = rec.get(_bt_field)
        if val is None:
            continue
        if not isinstance(val, str):
            raise ValueError(
                f"Invalid {_bt_field}: {val!r} "
                f"(expected null or an ISO-8601 datetime string)")
        try:
            parsed[_bt_field] = datetime.fromisoformat(val)
        except ValueError:
            raise ValueError(
                f"Invalid {_bt_field}: {val!r} "
                f"(expected ISO-8601 datetime, e.g. 2026-06-19T01:00:00)")
    if "valid_from" in parsed and "valid_to" in parsed:
        if parsed["valid_from"] > parsed["valid_to"]:
            raise ValueError(
                f"valid_from ({rec['valid_from']!r}) must be <= "
                f"valid_to ({rec['valid_to']!r})")

def validate_rb_record(rec, *, skip_id_check=False):
    """Validate a reasoning bank record dict. Raises ValueError on invalid.

    skip_id_check=True is used by the auto-id path in rb_add: pre-lock
    validation runs without `id` (which is allocated inside the lock by
    locked_append_jsonl_with_allocator). Post-allocation, validate_rb_record
    runs again with skip_id_check=False so the regex check still fires
    on the allocated id. Default False — every other caller (rb_update_field
    re-validation, etc.) demands a present, well-formed id.
    """
    required = RB_REQUIRED_FIELDS - ({"id"} if skip_id_check else set())
    missing = required - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not skip_id_check:
        if not RB_ID_RE.match(rec["id"]):
            raise ValueError(f"Invalid record ID format: {rec['id']} (expected rb-NNN)")

    # Assumes normalize_record has already run — status is guaranteed present.
    # If this invariant is violated, KeyError fails loud rather than silently
    # assuming "active" and masking the caller's bug.
    # isinstance guard precedes membership: `["x"] not in <set>` raises
    # TypeError: unhashable type: 'list', escaping the caller's ValueError
    # handler as a 500 that drops the write (B10). Short-circuit to ValueError.
    # Kept verbatim-in-sync with mind_api/src/store_registry.py validate_rb_record.
    if not isinstance(rec["type"], str) or rec["type"] not in RB_VALID_TYPES:
        raise ValueError(f"Invalid type: {rec['type']!r} (expected one of {RB_VALID_TYPES})")

    if not isinstance(rec["status"], str) or rec["status"] not in RB_VALID_STATUSES:
        raise ValueError(f"Invalid status: {rec['status']!r} (expected: {RB_VALID_STATUSES})")

    util = rec.get("utilization")
    if util is not None:
        validate_utilization(util)

    applies = rec.get("applies_to")
    if not isinstance(applies, str) or applies not in RB_VALID_APPLIES_TO:
        raise ValueError(
            f"Invalid applies_to: {applies!r} (expected one of {RB_VALID_APPLIES_TO})"
        )

    # entry_type (g-306-11): optional. null/absent = ordinary lesson; otherwise
    # must be a string in RB_VALID_ENTRY_TYPES. isinstance guard precedes
    # membership (B10): a list-valued entry_type would raise TypeError on
    # `list not in set` and 500 the write — short-circuit to a clean ValueError.
    # Kept verbatim-in-sync with mind_api/src/store_registry.py.
    entry_type = rec.get("entry_type")
    if entry_type is not None and (
            not isinstance(entry_type, str)
            or entry_type not in RB_VALID_ENTRY_TYPES):
        raise ValueError(
            f"Invalid entry_type: {entry_type!r} "
            f"(expected null or one of {RB_VALID_ENTRY_TYPES})"
        )

    exp_ref = rec.get("experience_ref")
    if exp_ref is not None and not EXPERIENCE_REF_RE.match(exp_ref):
        raise ValueError(f"Invalid experience_ref format: {exp_ref!r} (expected exp-SLUG)")
    _validate_bitemporal(rec)

    # Tag canonicalization (lowercase + kebab-case + dedup). Idempotent on
    # already-canonical tags. Mutates rec["tags"] in place. Runs after the
    # other validations so failures surface their own errors first.
    _normalize_tags(rec)

def validate_guard_record(rec, *, skip_id_check=False):
    """Validate a guardrail record dict. Raises ValueError on invalid.

    skip_id_check=True is used by the auto-id path in guard_add — see
    validate_rb_record's docstring for the same rationale.
    """
    required = GUARD_REQUIRED_FIELDS - ({"id"} if skip_id_check else set())
    missing = required - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    # g-247-01: reject unknown fields loudly instead of silently dropping them.
    # Caller's JSON must match GUARD_KNOWN_FIELDS — any extra field raises here.
    # See the GUARD_KNOWN_FIELDS definition for the policy on extending the set.
    unknown = set(rec.keys()) - GUARD_KNOWN_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown field(s): {sorted(unknown)}. "
            f"Silent drops were masking caller bugs (guard-332/337 trigger_pattern "
            f"loss). Either fix the caller to use a known field, or add the field "
            f"to GUARD_KNOWN_FIELDS in reasoning-bank.py with a source-writer citation. "
            f"Allowed fields: {sorted(GUARD_KNOWN_FIELDS)}."
        )

    if not skip_id_check:
        if not GUARD_ID_RE.match(rec["id"]):
            raise ValueError(f"Invalid record ID format: {rec['id']} (expected guard-NNN)")

    # Assumes normalize_record has already run — see validate_rb_record comment.
    if rec["status"] not in GUARD_VALID_STATUSES:
        raise ValueError(f"Invalid status: {rec['status']} (expected: {GUARD_VALID_STATUSES})")

    util = rec.get("utilization")
    if util is not None:
        validate_utilization(util)

    exp_ref = rec.get("experience_ref")
    if exp_ref is not None and not EXPERIENCE_REF_RE.match(exp_ref):
        raise ValueError(f"Invalid experience_ref format: {exp_ref!r} (expected exp-SLUG)")
    _validate_bitemporal(rec)

    # Tag canonicalization — same rule as validate_rb_record. Mutates in place.
    _normalize_tags(rec)

def normalize_record(rec, defaults):
    """Apply defaults for missing fields. Mutates and returns rec.

    Also fills in missing sub-keys inside dict-valued defaults (e.g., utilization).
    This means adding a new counter to UTILIZATION_COUNTERS + the defaults block
    auto-backfills existing records on their next read/write path — no separate
    migration pass needed (g-001-109).
    """
    for field, default in defaults.items():
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
# CLI subcommands removed in H2 Wave 2 (2026-05-15).
# All add / update-field / increment operations now route through the daemon
# generic store endpoint: mind_api/src/endpoints/store.py +
# mind_api/src/store_registry.py.  Shell wrappers (reasoning-bank-add.sh,
# guardrails-add.sh, etc.) call the daemon directly — no Python CLI fallback.
#
# This module is retained as a library for importers that use its constants,
# validators, and utility functions (guardrail-check.py, learning-ratio.py,
# utilization-feedback.py, board.py, _rb_helpers.py, tests).
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # NOT a CLI fallback (no-python-cli-fallback.md): a refusal that names the
    # writers. Measured 2026-08-29 on a downstream deployment: a Body ran
    # `python3 core/scripts/reasoning-bank.py add --entry ...`, got rc=0 and no
    # output, and read the silence as "added". Nothing was written.
    import sys

    sys.stderr.write(
        "reasoning-bank.py is a daemon-side library, not a command -- running it "
        "writes nothing. Use the wrappers: `bash core/scripts/reasoning-bank-add.sh` "
        "(JSON record on stdin), `reasoning-bank-read.sh`, "
        "`reasoning-bank-update-field.sh <id> <field> <value>`, "
        "`reasoning-bank-increment.sh <id> <field>`.\n"
    )
    sys.exit(2)
