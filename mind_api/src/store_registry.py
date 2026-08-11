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
    # set-field stamps this field on every successful field write, giving
    # cross-box merge an explicit recency key for content fields ( /
    # guard-1703). The stamp is a PER-FIELD MAP — {<written-field>: <iso-ts>} —
    # not a record-level scalar (): the guardrail merge handler resolves
    # FIELD BY FIELD, so a record-level key would mean "the newer write wins every
    # content field" and would discard a concurrent amendment to a different field
    # of the same record. guard-1153: LWW must key on a timestamp written by the
    # SAME MUTATION that writes the field. None => no stamp (the default; the
    # other stores keep their existing behavior unchanged).
    #
    # Scoped to set-field ONLY, deliberately. `increment` is excluded because a
    # utilization bump is not a content amendment and the merge already resolves
    # counters by per-counter MAX; stamping there would make an unrelated
    # increment win a content tiebreak. `append` is excluded because a new
    # record's `created` already orders it.
    #
    # MUST be present in the store's KNOWN_FIELDS allowlist — set_field stamps
    # before it validates, so an unallowlisted stamp self-rejects every write.
    amend_stamp_field: Optional[str] = None
    # Writing-agent provenance field, stamped by append ONLY ().
    # NEVER-OVERWRITE: applied with a PRESENCE test, so an explicit caller value
    # — including an explicit null — wins, matching the caller-wins contract
    # _rb_inject_source_goal already documents. Contrast created_field directly
    # above, which overwrites unconditionally: `created` is a clock reading the
    # caller has no standing to assert, whereas authorship is something a caller
    # (a backfill tool, a cross-agent relay) can legitimately know better than
    # the request header does.
    #
    # Declarative rather than a `prepare` hook on purpose. rb ALREADY owns its
    # prepare slot (_rb_inject_source_goal), so a hook-based stamp would have to
    # be chained into that one store and re-implemented for the other two — the
    # per-store duplication rb-4074 warns against. One emitter in store.py
    # append() + one declaration per store keeps the mutation surface separable:
    # deleting any single declaration reddens exactly that store's test.
    #
    # Scoped to APPEND only. `set_field`/`increment` mutate an EXISTING record,
    # where the writer is an amender, not the author — stamping there would
    # silently rewrite history to name whoever last bumped a counter.
    #
    # MUST be present in the store's KNOWN_FIELDS allowlist where one exists
    # (guardrails does; rb and pattern-signatures have no unknown-field gate) —
    # append stamps BEFORE it validates, so an unallowlisted stamp would
    # self-reject every write to that store.
    author_field: Optional[str] = None


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
# entry_type (, BRD Gap 7a-Mind): optional reasoning-bank taxonomy tag.
# null (the default) = an ordinary reasoning lesson; "procedure" = a reusable
# multi-step how-to that `retrieve.sh --entry-type procedure` can target.
# Additive + null-safe (the rb validator has no unknown-field gate); NO
# embeddings, NO new store. Extend this SET (not the validator body) to add
# future entry types. Kept verbatim-in-sync with core/scripts/reasoning-bank.py.
RB_VALID_ENTRY_TYPES = {"procedure"}
GUARD_VALID_STATUSES = {"active", "retired"}

RB_REQUIRED_FIELDS = {"id", "title", "type", "category", "content", "applies_to"}
RB_DEFAULT_FIELDS = {
    "status": "active",
    "description": "",
    "source_hypothesis": None,
    "source_goal": None,
    # origin_goal_id (): the EXECUTING goal id at write time, for the
    # Gate D spillover analysis (distinct from source_goal, the semantic source a
    # caller may override). Auto-injected from team-state in_flight by the prepare
    # hook; defaults null when no goal is executing. Additive — pre-
    # readers ignore it (the rb validator has no unknown-field gate).
    "origin_goal_id": None,
    # poignancy (, BRD Gap 1a; Generative Agents 2304.03442): optional
    # 1-10 importance rating set by the LLM author at write (one-shot
    # self-rating). Additive + null-safe — the rb validator has no unknown-field
    # gate, and retrieve scoring treats null as neutral (no backfill). Folded
    # into ranking only when tree.yaml retrieval.poignancy_blend_enabled is true
    # (default false), so this default is inert until the blend is enabled.
    "poignancy": None,
    "outcome": None,
    "failure_lesson": None,
    "preventive_guardrail": None,
    "experience_ref": None,
    "tags": [],
    # entry_type (): optional reasoning-bank taxonomy. null = ordinary
    # lesson; "procedure" = reusable multi-step how-to, retrievable via
    # retrieve.sh --entry-type procedure. Additive + null-safe (validator allows
    # null or a value in RB_VALID_ENTRY_TYPES). Mirror of the CLI default in
    # core/scripts/reasoning-bank.py.
    "entry_type": None,
    # valid_from / valid_to (, BRD Gap 5): optional bi-temporal
    # record-level validity interval. null/null (the default) = a record with
    # no explicit validity window (treated as currently-valid by the reader
    # path). On falsification the OLD record gets valid_to=now and a NEW record
    # is inserted with valid_from=now (close-old-insert-new, NOT in-place
    # mutation). Additive + null-safe (no unknown-field gate on RB); validated
    # by _validate_bitemporal. Mirror of the CLI default in
    # core/scripts/reasoning-bank.py.
    "valid_from": None,
    "valid_to": None,
    # amended_fields (): PER-FIELD in-place amendment stamps,
    # {<field-name>: <iso-timestamp>} — the ordering key
    # coordination_merge._merge_rb_record needs to keep the NEWER text when two
    # boxes hold divergent copies of one record. Same mechanism GUARD_DEFAULT_FIELDS
    # carries (); see that comment for the byte-order derivation and for
    # why the stamp is per-field rather than a record-level scalar.
    #
    # The reasoning bank's EXPOSURE IS WORSE than the guardrail case: _guard_identity
    # keys on (created, FULL rule) so a guardrail's primary payload is immune (a
    # divergent rule splits into two records), whereas _rb_identity keys on
    # (created, TITLE), leaving `content` — the PRIMARY payload — a mutable
    # non-identity field that reached the byte-order tiebreak directly.
    #
    # Declared HERE and not in an explicit RB_KNOWN_FIELDS set on purpose: default
    # fields flow into the allowlist automatically, and the allowlist entry is
    # load-bearing because set_field stamps BEFORE it validates — an unallowlisted
    # stamp field would self-reject every write to this store.
    "amended_fields": {},
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
    # valid_from / valid_to (, BRD Gap 5): optional bi-temporal validity
    # interval (see RB_DEFAULT_FIELDS for semantics). Added to GUARD_DEFAULT_FIELDS
    # so they flow into GUARD_KNOWN_FIELDS automatically (the unknown-field gate
    # is `set(GUARD_DEFAULT_FIELDS.keys())` | ...), keeping the additive field
    # accepted by the strict guardrail allowlist. Mirror of the CLI default in
    # core/scripts/reasoning-bank.py.
    "valid_from": None,
    "valid_to": None,
    # amended_fields (): PER-FIELD in-place amendment stamps,
    # {<field-name>: <iso-timestamp>} — the ordering key
    # coordination_merge._merge_guard_record needs to keep the NEWER text when two
    # boxes hold divergent versions of the same guardrail. Its generic tiebreak
    # decides by byte order, which is unrelated to recency (an appended clause
    # starting with a space loses to the text it extends; one starting with a
    # comma wins) — so the merge needs an explicit stamp.
    #
    # WHY PER-FIELD, not the record-level scalar this replaces ():
    # _merge_guard_record merges FIELD BY FIELD, so a record-level key silently
    # means "the newer WRITE wins every content field" — discarding a concurrent
    # amendment to a DIFFERENT field of the same guardrail. guard-1153 names the
    # correct shape directly: LWW on a timestamp written BY THE SAME MUTATION that
    # writes the field. A record-level stamp is that guard's "UNCORRELATED
    # timestamp" failure mode.
    #
    # Declared HERE rather than in the explicit set below so it flows into
    # GUARD_KNOWN_FIELDS automatically, exactly as valid_from/valid_to do. That
    # placement is load-bearing, not stylistic: set_field applies the stamp and
    # THEN validates, so an unallowlisted stamp would make every guardrail field
    # write self-reject. Written by the set-field writer via
    # StoreSpec.amend_stamp_field.
    "amended_fields": {},
    # amended_at: the RETIRED record-level scalar (live only on records written
    # between d30d21bd and ). Kept in the allowlist — NOT as a writer
    # target — so those records still validate on their next write, and read as a
    # per-field FLOOR by _merge_guard_record's migration path. Removing it would
    # make every already-stamped record self-reject (rb-2148: add the sibling
    # field, never break the locked one).
    "amended_at": None,
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
        # displaced_from (): SOURCE WRITER is
        # coordination_merge.py::_merge_id_keyed_jsonl (~L321-323), the
        # collision-reid path — when two boxes independently mint the same
        # guard-N for different records, one is re-id'd and stamped with the id
        # it was displaced from. The field was never added here, so the strict
        # unknown-field gate below REFUSED every update to any re-id'd record:
        # 5 live guardrails (guard-1262, guard-1468, guard-1546, guard-1570,
        # guard-1697) could not be retired, amended, or corrected at all, and
        # the refusal names `displaced_from` rather than the field the caller
        # actually passed — so it reads as a caller bug. Measured while
        # reconciling forked guardrails; the reid stamp is also the merge's own
        # fingerprint of the fork (guard-1697 displaced_from guard-1475 is
        # exactly one of the reconciled pairs).
        "displaced_from",
        # encoded_by (): SOURCE WRITER is endpoints/store.py::append,
        # via StoreSpec.author_field — the writing agent stamped at the append
        # chokepoint, never-overwrite. Allowlisted here rather than added to
        # GUARD_DEFAULT_FIELDS (which would flow in automatically, as
        # valid_from/valid_to do) precisely BECAUSE defaults would backfill a
        # null onto every historical record rewritten by any later path; this
        # goal's contract is that pre-existing rows stay byte-identical, so the
        # field must be ALLOWED without being DEFAULTED.
        "encoded_by",
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
    ta = util.get("times_active", 0)
    tc = util.get("times_cited", 0)
    # Denominator = max(retrievals, credited usages) + 1 () —
    # verbatim twin of reasoning-bank.py recompute_utilization_score; see the
    # full WHY comment there (context-carried helpful bumps make th>rc
    # legitimate for rb/guardrails; old max(rc,1) let untested entries outrank
    # scan-tested ones in sort_universal_rbs). Tree-node utility_ratio
    # deliberately keeps max(rc,1) — its th<=rc precondition holds.
    usage_v1 = th + tih
    util["utilization_score"] = round((th + 0.5 * tih) / (max(rc, usage_v1) + 1), 4)
    usage_v2 = th + tih + ta + tc
    util["utilization_score_v2"] = round(
        (th + 0.5 * tih + 0.25 * ta + 1.0 * tc) / (max(rc, usage_v2) + 1), 4
    )


def _stamp_now() -> str:
    """Return current local time as ISO 8601 without microseconds.
    Verbatim from reasoning-bank.py _stamp_now."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _validate_bitemporal(rec) -> None:
    """Validate optional bi-temporal validity-interval fields (, BRD
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
    # entry_type (): optional. null/absent = ordinary lesson; otherwise
    # must be a string in RB_VALID_ENTRY_TYPES. isinstance guard precedes
    # membership (B10): a list-valued entry_type would raise TypeError on
    # `list not in set` and 500 the write — short-circuit to a clean ValueError.
    entry_type = rec.get("entry_type")
    if entry_type is not None and (
            not isinstance(entry_type, str)
            or entry_type not in RB_VALID_ENTRY_TYPES):
        raise ValueError(
            f"Invalid entry_type: {entry_type!r} "
            f"(expected null or one of {RB_VALID_ENTRY_TYPES})")
    exp_ref = rec.get("experience_ref")
    if exp_ref is not None and not EXPERIENCE_REF_RE.match(exp_ref):
        raise ValueError(f"Invalid experience_ref format: {exp_ref!r} (expected exp-SLUG)")
    _validate_bitemporal(rec)
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
    _validate_bitemporal(rec)
    _normalize_tags(rec)


def _rb_inject_source_goal(ctx, rec):
    """Auto-populate source_goal AND origin_goal_id from team-state.yaml
    in_flight.goal_id (one read, sets both). source_goal is the SEMANTIC source
    (a caller may override it to a different goal); origin_goal_id (g-325-06) is
    the EXECUTING goal at write time, for the Gate D spillover analysis (SPILL-1:
    which experiment-arm goal produced this knowledge). Each is caller-wins — an
    explicit value (including null) is preserved independently. Verbatim base
    logic from reasoning-bank.py _read_in_flight_goal_id + rb_add lines ~600-603.
    Reads from ctx.paths (not os.environ)."""
    need_source = "source_goal" not in rec
    need_origin = "origin_goal_id" not in rec
    if not need_source and not need_origin:
        return  # caller set both (including explicit null) — caller wins
    try:
        import yaml  # lazy import — only needed in the auto-populate path
    except ImportError:
        return
    agent_name = ctx.paths.agent_name
    if not agent_name:
        return
    path = ctx.paths.world / "team-state.yaml"
    # own-cloud read-path fix (2026-07-02): materialize an S3-only team-state on
    # a fresh box before the exists() gate, else source_goal injection silently
    # no-ops. Best-effort (this whole helper is caller-wins enrichment); no-op on
    # LocalBackend and for out-of-root paths (keystone).  sharding: the
    # agent's live status is its ROW file — materialize + read that first, with
    # core-file residual fallback.
    try:
        from _team_state import read_agent_row, row_path as _ts_row_path
    except ImportError:
        return
    try:
        from storage_backend import get_backend
        get_backend().ensure_local(path)
        get_backend().ensure_local(_ts_row_path(ctx.paths.world, agent_name))
    except Exception as e:
        try:  # report, never raise — see note_swallowed_backend_error ()
            from storage_backend import note_swallowed_backend_error
            # Two calls share this guard, so which one failed is not recoverable
            # here — name what was attempted rather than pin the wrong path.
            note_swallowed_backend_error(
                "ensure_local", f"{path} (or its team-state row)", e)
        except Exception:
            pass
    try:
        status = read_agent_row(ctx.paths.world, agent_name, core_path=path) or {}
        goal_id = (status.get("in_flight") or {}).get("goal_id")
        if goal_id:
            if need_source:
                rec["source_goal"] = goal_id
            if need_origin:
                rec["origin_goal_id"] = goal_id
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
    # amended_fields (): PER-FIELD in-place amendment stamps,
    # {<field-name>: <iso-timestamp>}. Same mechanism GUARD_DEFAULT_FIELDS
    # () and RB_DEFAULT_FIELDS carry; see the GUARD comment for the
    # byte-order derivation and for why the stamp is per-field, not record-level.
    #
    # EXPOSURE MEASURED, not assumed (probe, 2026-07-28): _sig_identity keys on
    # (created, name), so `name` is immune — a divergent name SPLITS into two
    # records. `description` and `expected_outcome` are the amendable NON-identity
    # free-text fields (both live on real records, both PATSIG_REQUIRED_FIELDS),
    # and merging description='base text' against 'base text and more' returned
    # 'base text' — the amendment was reverted.
    "amended_fields": {},
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
        amend_stamp_field="amended_fields",
        # . NAME CHOSEN BY KEY CENSUS ON SEMANTICS, not on which
        # existing name is best populated (rb-6166). Measured 2026-08-01 across
        # all three sibling stores: the most-populated author-ish key by far is
        # `source` (guardrails 1939/1939 = 100%, rb 70, patsig 15) — and it is
        # NOT authorship. Its live values are provenance narrative and derivation
        # method ("session-1: Built ... User caught the gap.", "user correction
        # session ...", "execution-reflection", "encoding-build-encoding-cycle"),
        # so stamping an agent name there would both mean the wrong thing and
        # clobber a load-bearing field on a store where it is REQUIRED. Likewise
        # `source_goal` (rb 5363 = 89.9%) names the goal, not the writer.
        # `encoded_by` carried exactly ONE row in 5966 — value "bravo", an agent
        # name — so it is the only key in the corpus already meaning what this
        # stamps, and adopting it makes that orphan row consistent instead of
        # minting a second name for one concept. It also matches the framework's
        # own verb for writing a distilled lesson into a durable store (encode);
        # `author`, the sibling pipeline_write.py name, fits an authored
        # hypothesis better than an encoded lesson. Zero readers repo-wide at
        # adoption, so no consumer assumes its rarity.
        author_field="encoded_by",
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
        amend_stamp_field="amended_fields",
        #  — see the census rationale on the reasoning-bank spec above.
        # This is the one store with a strict unknown-field gate, so the name is
        # ALSO allowlisted in GUARD_KNOWN_FIELDS; dropping either half breaks
        # every guardrail write, not just the stamp.
        author_field="encoded_by",
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
        amend_stamp_field="amended_fields",
        #  — see the census rationale on the reasoning-bank spec above.
        # This store had NO authorship key on any of its 79 rows.
        author_field="encoded_by",
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
