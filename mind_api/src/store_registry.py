"""Declarative registry of pure-CRUD JSONL stores for the generic store
endpoint (endpoints/store.py).

ONE StoreSpec per store. The generic endpoint is the SINGLE implementation
of locked append/replace/merge over the daemon write infra; per-store
variation lives here as data + small callables lifted VERBATIM from each
family CLI's validate_record / merge rules, so on-disk semantics are
byte-identical to the deleted CLI subcommands.

Adding a store = adding a row (data, not code). Design + rationale:
zeta/reports/phase3-h2-wave-plan.md and the HARDENING anchor sec17.4.

Wave 1 ships the `journal` row (canonical reference). Wave 2 adds
`reasoning-bank` and `guardrails`. StoreSpec fields are additive — Wave 2
added created_field, prepare, recompute, immutable_fields, increment_prefix,
increment_counters; journal needs none of them so they default to no-ops.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional


@dataclass(frozen=True)
class StoreSpec:
    # path(ctx) -> absolute Path of the live JSONL store (via ctx.paths.*,
    # already _absolutize'd by agent_paths.py — never a module constant).
    path: Callable[[Any], Path]
    # base(ctx) -> base dir for history/changelog (ctx.paths.world|.meta|
    # .agent). resolve_base_dir's G1 patch means agent stores DO get
    # history+changelog, matching the family CLI's _fileops.locked_* path.
    base: Callable[[Any], Path]
    # Record key used for lookup on replace/merge. journal keys by the
    # integer `session` field, not `id` — the generic contract is always
    # ?id=<value>; id_field says which record field that value matches.
    id_field: str = "id"
    # Coerce the ?id= query string into the typed key (journal: int, and
    # accept the "session-N" wrapper form).
    id_coerce: Callable[[str], Any] = str
    required_fields: frozenset = frozenset()
    # Static defaults applied when a field is absent (deep-copied).
    default_fields: Dict[str, Any] = field(default_factory=dict)
    # Computed defaults applied when a field is absent (e.g. date->today).
    # Only-if-absent (caller may override) — distinct from a script-owned
    # stamp that overwrites unconditionally (see created_field below).
    defaults_dynamic: Dict[str, Callable[[], Any]] = field(default_factory=dict)
    # allocate(items) -> new key value, used on append when id_field absent.
    # None => caller MUST supply the key (dup-checked instead).
    allocate: Optional[Callable[[List[dict]], Any]] = None
    # validate(ctx, rec, *, skip_id=False) -> None; raise ValueError on
    # invalid. Lifted verbatim from the family CLI's validate_record.
    validate: Optional[Callable[..., None]] = None
    # field -> "union" | "append" for merge; absent field => scalar
    # overwrite. Lifted verbatim from the family CLI's cmd_merge.
    merge_lists: Dict[str, str] = field(default_factory=dict)
    # --- Wave 2 fields (rb/guard) — no-ops for journal -----------------------
    # Script-owned timestamp field: append UNCONDITIONALLY overwrites
    # rec[created_field] = created_stamp() regardless of stdin value.
    created_field: Optional[str] = None
    created_stamp: Optional[Callable[[], str]] = None
    # prepare(ctx, rec) mutates rec in place BEFORE validate (e.g. rb
    # source_goal injection from team-state.yaml).
    prepare: Optional[Callable[[Any, dict], None]] = None
    # Post-mutation derived-field refresh (recompute_utilization_score).
    recompute: Optional[Callable[[dict], None]] = None
    # set-field field names that trigger recompute ({"utilization"} for rb/guard).
    recompute_on_fields: FrozenSet[str] = frozenset()
    # set-field rejects field if field or field.split('.')[0] in this set.
    immutable_fields: FrozenSet[str] = frozenset()
    # increment requires field.startswith(this) ("utilization." for rb/guard).
    increment_prefix: str = ""
    # Valid increment counters (UTILIZATION_COUNTERS for rb/guard).
    increment_counters: FrozenSet[str] = frozenset()


def apply_defaults(rec: dict, defaults: Dict[str, Any]) -> dict:
    """Apply static defaults for absent fields (deep-copy mutable values).
    Mirrors the family CLI normalize_record bodies, including deep-backfill
    of dict sub-keys (e.g. utilization counter additions auto-heal legacy
    records on their next read/write path — g-001-109)."""
    for f, d in defaults.items():
        if f not in rec:
            rec[f] = json.loads(json.dumps(d)) if isinstance(d, (dict, list)) else d
        elif isinstance(d, dict) and isinstance(rec[f], dict):
            for sub_key, sub_default in d.items():
                if sub_key not in rec[f]:
                    rec[f][sub_key] = sub_default
    return rec


# ---------------------------------------------------------------------------
# journal (Wave 1 — canonical reference). Lifted verbatim from journal.py.
# ---------------------------------------------------------------------------

_JOURNAL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _journal_coerce_id(s: Any) -> int:
    """journal.py cmd_update/cmd_merge accept 'session-N' or bare 'N'."""
    s = str(s)
    return int(s.split("-", 1)[1]) if s.startswith("session-") else int(s)


def _journal_file_re(agent_name: str):
    """Canonical journal_file regex: <agent>/journal/YYYY/MM/YYYY-MM-DD.md.

    Single source of truth shared by _journal_prepare (derive) and
    _journal_validate (check) so the two can never drift."""
    return re.compile(
        rf"^{re.escape(agent_name)}/journal/\d{{4}}/\d{{2}}/\d{{4}}-\d{{2}}-\d{{2}}\.md$"
    )


def _journal_prepare(ctx, rec) -> None:
    """Derive the canonical journal_file from date + agent (SSOT) — B7.

    journal_file is fully determined by the record's `date` and the bound
    agent, and has NO consumer beyond this store's own validation +
    persistence. Yet callers historically hand-built it and got it wrong:
    charlie passed the INDEX path `agents/charlie/journal.jsonl` (the
    `agents/` prefix + `.jsonl` extension) instead of the dated ENTRY path,
    producing 4 `validation_failed` rejections (B7). Requiring the caller to
    supply a value that must match a regex derived from data the record
    ALREADY carries is the single-source-of-truth violation at the root of
    the bug class; derive it here instead.

    A caller value that already matches the canonical shape is preserved
    (backward-compatible). Anything else (absent / index path / wrong agent
    prefix / wrong extension) is replaced with the value derived from `date`
    (or today, when `date` is absent or unparseable — _journal_validate
    still independently rejects a malformed `date`, so that error is never
    masked). prepare runs before defaults_dynamic, so an omitted date is None
    here; the today() fallback keeps journal_file canonical and the
    subsequently-applied date default stays consistent with it."""
    agent_name = ctx.paths.agent.name
    jf = rec.get("journal_file")
    if isinstance(jf, str) and _journal_file_re(agent_name).match(jf):
        return  # canonical caller value — respect SSOT, change nothing
    raw_date = rec.get("date")
    try:
        d = datetime.strptime(str(raw_date), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        d = date.today()
    rec["journal_file"] = (
        f"{agent_name}/journal/{d.year:04d}/{d.month:02d}/{d.isoformat()}.md"
    )


def _journal_validate(ctx, rec, *, skip_id: bool = False) -> None:
    """Verbatim port of journal.py validate_record. The journal_file regex
    is agent-name-scoped; the name is resolved from ctx (never a module
    constant) per .claude/rules/path-resolution.md.

    journal_file is normally derived upstream by _journal_prepare (B7); this
    regex check stays as the structural backstop for the rare path that
    bypasses prepare (e.g. a direct validate call in a future caller)."""
    agent_name = ctx.paths.agent.name
    jf_re = _journal_file_re(agent_name)
    required = {"session", "date", "journal_file"} - ({"session"} if skip_id else set())
    missing = required - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not skip_id:
        if not isinstance(rec["session"], int) or rec["session"] < 1:
            raise ValueError(
                f"Invalid session: {rec['session']} (must be a positive integer)")

    if not _JOURNAL_DATE_RE.match(rec["date"]):
        raise ValueError(f"Invalid date format: {rec['date']} (expected YYYY-MM-DD)")
    try:
        date.fromisoformat(rec["date"])
    except ValueError:
        raise ValueError(f"Invalid date: {rec['date']}")

    if not jf_re.match(rec["journal_file"]):
        raise ValueError(
            f"Invalid journal_file: {rec['journal_file']} "
            f"(expected {agent_name}/journal/YYYY/MM/YYYY-MM-DD.md)")

    for lf in ("goals_completed", "key_events", "tags"):
        v = rec.get(lf, [])
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            raise ValueError(f"{lf} must be an array of strings")


def _journal_next_session(items: List[dict]) -> int:
    """journal.py get_max_session(items) + 1."""
    return (max((r.get("session", 0) for r in items), default=0)) + 1


# ---------------------------------------------------------------------------
# reasoning-bank + guardrails (Wave 2). Lifted verbatim from
# core/scripts/reasoning-bank.py (pipeline_write.py precedent —
# daemon-import-unsafe to import; mirror upstream on change).
# ---------------------------------------------------------------------------

RB_ID_RE = re.compile(r"^rb-\d+$")
GUARD_ID_RE = re.compile(r"^guard-\d+$")

RB_VALID_TYPES = {"success", "failure", "user_provided"}
RB_VALID_STATUSES = {"active", "retired"}
RB_VALID_APPLIES_TO = {"any", "framework", "domain", "specific"}
GUARD_VALID_STATUSES = {"active", "retired"}

RB_REQUIRED_FIELDS = {"id", "title", "type", "category", "content", "applies_to"}
RB_DEFAULT_FIELDS = {
    "status": "active",
    "description": "",
    "source_hypothesis": None,
    "source_goal": None,
    "outcome": None,
    "failure_lesson": None,
    "preventive_guardrail": None,
    "experience_ref": None,
    "tags": [],
    "when_to_use": {"conditions": [], "category": ""},
    "utilization": {
        "retrieval_count": 0,
        "last_retrieved": None,
        "times_helpful": 0,
        "times_noise": 0,
        "times_active": 0,
        "times_skipped": 0,
        "times_inferred_helpful": 0,
        "times_inferred_unknown": 0,
        "times_cited": 0,
        "utilization_score": 0.0,
    },
}

GUARD_REQUIRED_FIELDS = {"id", "rule", "category", "trigger_condition", "source"}
GUARD_DEFAULT_FIELDS = {
    "status": "active",
    "experience_ref": None,
    "trigger_pattern": None,
    "when_to_use": {"conditions": [], "category": ""},
    "utilization": {
        "retrieval_count": 0,
        "last_retrieved": None,
        "times_helpful": 0,
        "times_noise": 0,
        "times_active": 0,
        "times_skipped": 0,
        "times_inferred_helpful": 0,
        "times_inferred_unknown": 0,
        "times_cited": 0,
        "utilization_score": 0.0,
    },
}

GUARD_KNOWN_FIELDS = (
    GUARD_REQUIRED_FIELDS
    | set(GUARD_DEFAULT_FIELDS.keys())
    | {
        "created",
        "tags",
        "action_hint",
        "severity",
        "context_triggers",
        "phases",
        "source_reflection_id",
        "title",
        "next_review_eligible_at",
        "auto_flagged_for_review",
        "retirement_date",
        "retirement_reason",
    }
)

UTILIZATION_COUNTERS = {
    "retrieval_count", "times_helpful", "times_noise", "times_active",
    "times_skipped", "times_inferred_helpful", "times_inferred_unknown",
    "times_cited",
}

# Experience-ref regex — lifted from experience.py (^exp-[a-z0-9._-]+$).
# Do NOT `from experience import` — daemon-import-unsafe.
EXPERIENCE_REF_RE = re.compile(r"^exp-[a-z0-9._-]+$")

_TAG_SEP_RE = re.compile(r"[-_ ]+")


def _normalize_tags(rec):
    """Canonicalize tags list in-place: lowercase + kebab-case + dedup."""
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


def _validate_utilization(util):
    """Validate the utilization object shape AND value invariants."""
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
        raise ValueError(
            f"utilization.utilization_score must be a non-negative number, got: {score!r}")


def _recompute_utilization_score(rec):
    """Recompute utilization_score (and v2) from counter state.
    Verbatim from reasoning-bank.py recompute_utilization_score."""
    util = rec["utilization"]
    rc = util["retrieval_count"]
    th = util["times_helpful"]
    tih = util["times_inferred_helpful"]
    util["utilization_score"] = round((th + 0.5 * tih) / max(rc, 1), 4)
    ta = util.get("times_active", 0)
    tc = util.get("times_cited", 0)
    util["utilization_score_v2"] = round(
        (th + 0.5 * tih + 0.25 * ta + 1.0 * tc) / max(rc + 1, 1), 4
    )


def _stamp_now() -> str:
    """Return current local time as ISO 8601 without microseconds.
    Verbatim from reasoning-bank.py _stamp_now."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def validate_rb_record(ctx, rec, *, skip_id: bool = False) -> None:
    """Verbatim port of reasoning-bank.py validate_rb_record.
    Leading ctx param for uniform StoreSpec.validate signature (unused)."""
    required = RB_REQUIRED_FIELDS - ({"id"} if skip_id else set())
    missing = required - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")
    if not skip_id:
        if not RB_ID_RE.match(rec["id"]):
            raise ValueError(f"Invalid record ID format: {rec['id']} (expected rb-NNN)")
    # isinstance guard precedes membership: `["x"] not in <set>` raises
    # TypeError: unhashable type: 'list' (B10 — a list-valued field 500'd the
    # request and dropped the rb lesson). Short-circuit to a clean ValueError.
    if not isinstance(rec["type"], str) or rec["type"] not in RB_VALID_TYPES:
        raise ValueError(f"Invalid type: {rec['type']!r} (expected one of {RB_VALID_TYPES})")
    if not isinstance(rec["status"], str) or rec["status"] not in RB_VALID_STATUSES:
        raise ValueError(f"Invalid status: {rec['status']!r} (expected: {RB_VALID_STATUSES})")
    util = rec.get("utilization")
    if util is not None:
        _validate_utilization(util)
    applies = rec.get("applies_to")
    if not isinstance(applies, str) or applies not in RB_VALID_APPLIES_TO:
        raise ValueError(
            f"Invalid applies_to: {applies!r} (expected one of {RB_VALID_APPLIES_TO})")
    exp_ref = rec.get("experience_ref")
    if exp_ref is not None and not EXPERIENCE_REF_RE.match(exp_ref):
        raise ValueError(f"Invalid experience_ref format: {exp_ref!r} (expected exp-SLUG)")
    _normalize_tags(rec)


def validate_guard_record(ctx, rec, *, skip_id: bool = False) -> None:
    """Verbatim port of reasoning-bank.py validate_guard_record.
    Leading ctx param for uniform StoreSpec.validate signature (unused)."""
    required = GUARD_REQUIRED_FIELDS - ({"id"} if skip_id else set())
    missing = required - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")
    unknown = set(rec.keys()) - GUARD_KNOWN_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown field(s): {sorted(unknown)}. "
            f"Silent drops were masking caller bugs (guard-332/337 trigger_pattern "
            f"loss). Either fix the caller to use a known field, or add the field "
            f"to GUARD_KNOWN_FIELDS in store_registry.py with a source-writer citation. "
            f"Allowed fields: {sorted(GUARD_KNOWN_FIELDS)}.")
    if not skip_id:
        if not GUARD_ID_RE.match(rec["id"]):
            raise ValueError(f"Invalid record ID format: {rec['id']} (expected guard-NNN)")
    if not isinstance(rec["status"], str) or rec["status"] not in GUARD_VALID_STATUSES:
        raise ValueError(f"Invalid status: {rec['status']!r} (expected: {GUARD_VALID_STATUSES})")
    util = rec.get("utilization")
    if util is not None:
        _validate_utilization(util)
    exp_ref = rec.get("experience_ref")
    if exp_ref is not None and not EXPERIENCE_REF_RE.match(exp_ref):
        raise ValueError(f"Invalid experience_ref format: {exp_ref!r} (expected exp-SLUG)")
    _normalize_tags(rec)


def _rb_inject_source_goal(ctx, rec):
    """Auto-populate source_goal from team-state.yaml in_flight.goal_id.
    Verbatim logic from reasoning-bank.py _read_in_flight_goal_id + rb_add
    lines ~600-603. Reads from ctx.paths (not os.environ)."""
    if "source_goal" in rec:
        return  # caller wins (including explicit null)
    try:
        import yaml  # lazy import — only needed in the auto-populate path
    except ImportError:
        return
    agent_name = ctx.paths.agent_name
    if not agent_name:
        return
    path = ctx.paths.world / "team-state.yaml"
    if not path.exists():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        goal_id = (data.get("agent_status", {})
                       .get(agent_name, {})
                       .get("in_flight", {})
                       .get("goal_id"))
        if goal_id:
            rec["source_goal"] = goal_id
    except Exception:
        return  # fail-open


def _rb_allocate(items: List[dict]) -> str:
    from _fileops import next_id_for_prefix
    return next_id_for_prefix(items, "rb")


def _guard_allocate(items: List[dict]) -> str:
    from _fileops import next_id_for_prefix
    return next_id_for_prefix(items, "guard")


# ---------------------------------------------------------------------------
# pattern-signatures (Wave 3). Lifted verbatim from
# core/scripts/pattern-signatures.py (daemon-import-unsafe to import;
# mirror upstream on change).
# ---------------------------------------------------------------------------

PATSIG_ID_RE = re.compile(r"^sig-\d+$")

PATSIG_VALID_STATUSES = {"active", "retired", "contradicted"}
PATSIG_VALID_VALIDATION_STATUSES = {"unvalidated", "calibrating", "validated"}

PATSIG_REQUIRED_FIELDS = {"id", "name", "description", "conditions", "expected_outcome"}
PATSIG_DEFAULT_FIELDS = {
    "status": "active",
    "outcome_stats": {"total": 0, "confirmed": 0, "accuracy": 0.0},
    "retrieval_cues": [],
    "separation_markers": [],
    "confused_with": [],
    "validation_status": "unvalidated",
    "last_matched": None,
}


def _recompute_patsig_accuracy(rec):
    """Recompute outcome_stats.accuracy from total/confirmed. Mutates rec.
    Lifted verbatim from pattern-signatures.py recompute_accuracy (lines 143-150)."""
    stats = rec.get("outcome_stats", {})
    total = stats.get("total", 0)
    confirmed = stats.get("confirmed", 0)
    stats["accuracy"] = round(confirmed / total, 4) if total > 0 else 0.0
    rec["outcome_stats"] = stats


def validate_patsig_record(ctx, rec, *, skip_id: bool = False) -> None:
    """Lifted verbatim from pattern-signatures.py validate_record (lines 117-141).
    Leading ctx param for uniform StoreSpec.validate signature (unused).
    Renamed kw skip_id_check -> skip_id."""
    required = PATSIG_REQUIRED_FIELDS - ({"id"} if skip_id else set())
    missing = required - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not skip_id:
        if not PATSIG_ID_RE.match(rec["id"]):
            raise ValueError(f"Invalid record ID format: {rec['id']} (expected sig-NNN)")

    if not isinstance(rec["conditions"], list):
        raise ValueError("conditions must be a list")

    status = rec.get("status", "active")
    if status not in PATSIG_VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")

    validation_status = rec.get("validation_status", "unvalidated")
    if validation_status not in PATSIG_VALID_VALIDATION_STATUSES:
        raise ValueError(f"Invalid validation_status: {validation_status}")


def _patsig_allocate(items: List[dict]) -> str:
    from _fileops import next_id_for_prefix
    return next_id_for_prefix(items, "sig")


# ---------------------------------------------------------------------------
# spark-questions (Wave 3 -- meta store). Lifted verbatim from
# core/scripts/spark-questions.py (daemon-import-unsafe to import;
# mirror upstream on change).
#
# DUAL-TYPE WRINKLE: spark-questions has two record types (question sq-N,
# candidate sq-cN) with separate id-regex, required fields, and defaults,
# dispatched by rec["type"]. The type-dispatched defaults and id-allocation
# are handled via prepare hook + thread-local state so that the generic
# store endpoint (store.py) needs no modification.
# ---------------------------------------------------------------------------

SPARK_VALID_TYPES = {"question", "candidate"}
SPARK_VALID_STATUSES = {"active", "retired"}

SPARK_QUESTION_ID_RE = re.compile(r"^sq-\d+$")
SPARK_CANDIDATE_ID_RE = re.compile(r"^sq-c\d+$")

SPARK_QUESTION_REQUIRED_FIELDS = {"id", "text", "category", "type"}
SPARK_CANDIDATE_REQUIRED_FIELDS = {"id", "text", "category", "type"}

SPARK_QUESTION_DEFAULTS = {
    "times_asked": 0,
    "sparks_generated": 0,
    "yield_rate": 0.0,
    "status": "active",
}

SPARK_CANDIDATE_DEFAULTS = {
    "proposed_session": 0,
}

# Thread-local storage for passing the record type from _spark_prepare to
# _spark_allocate. The generic store.py append handler calls prepare(ctx, rec)
# BEFORE allocate(items), both in the same request thread. This avoids
# modifying store.py's allocate(items) signature.
_spark_tls = threading.local()


def _spark_prepare(ctx, rec):
    """Type-dispatched defaults for spark-questions. Lifted verbatim from
    spark-questions.py normalize_record (lines 134-149).
    Also stashes rec["type"] in thread-local for _spark_allocate."""
    rec_type = rec.get("type")
    # Stash type for the allocator (same thread, same request).
    _spark_tls.record_type = rec_type
    if rec_type == "question":
        for f, default in SPARK_QUESTION_DEFAULTS.items():
            if f not in rec:
                rec[f] = default
        # Always recompute yield_rate (verbatim from normalize_record lines 142-144)
        rec["yield_rate"] = round(
            rec.get("sparks_generated", 0) / max(rec.get("times_asked", 0), 1), 4
        )
    elif rec_type == "candidate":
        for f, default in SPARK_CANDIDATE_DEFAULTS.items():
            if f not in rec:
                rec[f] = default


def _recompute_spark_yield_rate(rec):
    """Recompute yield_rate for question-type records. Lifted verbatim from
    spark-questions.py cmd_update_field (lines 253-256) and cmd_increment
    (lines 291-293)."""
    if rec.get("type") == "question":
        rec["yield_rate"] = round(
            rec.get("sparks_generated", 0) / max(rec.get("times_asked", 0), 1), 4
        )


def validate_spark_record(ctx, rec, *, skip_id: bool = False) -> None:
    """Lifted verbatim from spark-questions.py validate_record (lines 105-132).
    Leading ctx param for uniform StoreSpec.validate signature (unused).
    Renamed kw skip_id_check -> skip_id."""
    rec_type = rec.get("type")
    if rec_type not in SPARK_VALID_TYPES:
        raise ValueError(f"Invalid type: {rec_type} (expected 'question' or 'candidate')")

    if rec_type == "question":
        required = SPARK_QUESTION_REQUIRED_FIELDS - ({"id"} if skip_id else set())
        missing = required - set(rec.keys())
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        if not skip_id:
            if not SPARK_QUESTION_ID_RE.match(rec["id"]):
                raise ValueError(f"Invalid question ID format: {rec['id']} (expected sq-NNN)")
        if rec.get("status") and rec["status"] not in SPARK_VALID_STATUSES:
            raise ValueError(f"Invalid status: {rec['status']}")
    else:
        required = SPARK_CANDIDATE_REQUIRED_FIELDS - ({"id"} if skip_id else set())
        missing = required - set(rec.keys())
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        if not skip_id:
            if not SPARK_CANDIDATE_ID_RE.match(rec["id"]):
                raise ValueError(f"Invalid candidate ID format: {rec['id']} (expected sq-cNN)")


def _spark_allocate(items: List[dict]) -> str:
    """Type-dispatched id allocation for spark-questions. Reads the record
    type from thread-local state stashed by _spark_prepare (same request
    thread). Lifted verbatim from spark-questions.py cmd_add (lines 201-207)."""
    from _fileops import next_id_for_prefix
    rec_type = getattr(_spark_tls, "record_type", "question")
    if rec_type == "question":
        return next_id_for_prefix(items, "sq", pad_width=3)
    else:
        return next_id_for_prefix(items, "sq-c", pad_width=2, separator="")


STORE_REGISTRY: Dict[str, StoreSpec] = {
    "journal": StoreSpec(
        path=lambda ctx: ctx.paths.agent / "journal.jsonl",
        base=lambda ctx: ctx.paths.agent,
        id_field="session",
        id_coerce=_journal_coerce_id,
        required_fields=frozenset({"session", "date", "journal_file"}),
        default_fields={
            "goals_completed": [],
            "hypotheses_resolved": 0,
            "hypotheses_created": 0,
            "key_events": [],
            "tags": [],
        },
        defaults_dynamic={"date": lambda: date.today().isoformat()},
        allocate=_journal_next_session,
        prepare=_journal_prepare,  # B7: derive canonical journal_file (SSOT)
        validate=_journal_validate,
        merge_lists={
            "goals_completed": "union",
            "tags": "union",
            "key_events": "append",
        },
    ),
    "reasoning-bank": StoreSpec(
        path=lambda ctx: ctx.paths.world / "reasoning-bank.jsonl",
        base=lambda ctx: ctx.paths.world,
        id_field="id",
        id_coerce=str,
        required_fields=frozenset(RB_REQUIRED_FIELDS),
        default_fields=RB_DEFAULT_FIELDS,
        allocate=_rb_allocate,
        validate=validate_rb_record,
        prepare=_rb_inject_source_goal,
        created_field="created",
        created_stamp=_stamp_now,
        recompute=_recompute_utilization_score,
        recompute_on_fields=frozenset({"utilization"}),
        immutable_fields=frozenset({"created"}),
        increment_prefix="utilization.",
        increment_counters=UTILIZATION_COUNTERS,
    ),
    "guardrails": StoreSpec(
        path=lambda ctx: ctx.paths.world / "guardrails.jsonl",
        base=lambda ctx: ctx.paths.world,
        id_field="id",
        id_coerce=str,
        required_fields=frozenset(GUARD_REQUIRED_FIELDS),
        default_fields=GUARD_DEFAULT_FIELDS,
        allocate=_guard_allocate,
        validate=validate_guard_record,
        created_field="created",
        created_stamp=_stamp_now,
        recompute=_recompute_utilization_score,
        recompute_on_fields=frozenset({"utilization"}),
        immutable_fields=frozenset({"created"}),
        increment_prefix="utilization.",
        increment_counters=UTILIZATION_COUNTERS,
    ),
    "pattern-signatures": StoreSpec(
        path=lambda ctx: ctx.paths.world / "pattern-signatures.jsonl",
        base=lambda ctx: ctx.paths.world,
        id_field="id",
        id_coerce=str,
        required_fields=frozenset(PATSIG_REQUIRED_FIELDS),
        default_fields=PATSIG_DEFAULT_FIELDS,
        allocate=_patsig_allocate,
        validate=validate_patsig_record,
        created_field="created",
        created_stamp=_stamp_now,
        recompute=_recompute_patsig_accuracy,
        recompute_on_fields=frozenset({"outcome_stats"}),
        immutable_fields=frozenset({"created"}),
    ),
    "spark-questions": StoreSpec(
        path=lambda ctx: ctx.paths.meta / "spark-questions.jsonl",
        base=lambda ctx: ctx.paths.meta,
        id_field="id",
        id_coerce=str,
        required_fields=frozenset(SPARK_QUESTION_REQUIRED_FIELDS),
        default_fields={},  # type-dispatched via prepare hook
        allocate=_spark_allocate,
        validate=validate_spark_record,
        prepare=_spark_prepare,
        recompute=_recompute_spark_yield_rate,
        recompute_on_fields=frozenset({"times_asked", "sparks_generated"}),
    ),
}
