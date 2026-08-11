"""POST /v1/pipeline/add, /v1/pipeline/move, /v1/pipeline/update,
/v1/pipeline/update-field, /v1/pipeline/archive-sweep

Writer endpoints for the hypothesis pipeline, following the same daemon
write infrastructure pattern as aspirations_write.py (file_locks +
history + changelog + jsonl_cache invalidation).

Pipeline records live in two files:
  - LIVE_PATH  = world/pipeline.jsonl       (active records)
  - ARCHIVE_PATH = world/pipeline-archive.jsonl (archived records)

No gates run in these endpoints — pipeline records have no gate pipeline.
Validation mirrors pipeline.py: normalize_record, validate_record,
validate_formation_quality.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..jsonl_cache import cache as _jsonl_cache
from .. import file_locks, history, changelog

from _fileops import _atomic_write_with_fallback  # noqa: E402
from _surprise import apply_derived_surprise  # noqa: E402  # : surprise is DERIVED, not caller-supplied
from storage_backend import get_backend  # noqa: E402  # s5c: own-cloud read freshness
from ..agent_paths import assert_not_cruft  # noqa: E402


# ---------------------------------------------------------------------------
# Duplicated validation constants from pipeline.py — see DECISIONS.md #3.
# Mirror upstream when these change; a parity test could enforce.
# ---------------------------------------------------------------------------

VALID_STAGES = {"discovered", "active", "measurement-pending", "resolved", "archived"}
VALID_HORIZONS = {"micro", "session", "short", "long"}
VALID_TYPES = {"high-conviction", "calibration", "exploration", "contrarian"}
VALID_OUTCOMES = {"CONFIRMED", "CORRECTED", "EXPIRED", "UNRESOLVABLE"}
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_[a-z0-9-]+$")

REQUIRED_FIELDS = {"id", "title", "stage", "horizon", "type", "confidence",
                   "position", "formed_date", "category"}
DEFAULT_FIELDS = {
    "slug": None,
    "rationale": "",
    "outcome": None,
    "reflected": False,
    "surprise": None,
    "experience_ref": None,
}


# ---------------------------------------------------------------------------
# JSONL read/write helpers (same pattern as aspirations_write.py)
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    # s5c (own-cloud): force-fresh via the backend before reading. In-lock RMW
    # callers get lost-update prevention + the If-Match fence etag; plain probe
    # reads get the latest remote state. No-op on LocalBackend.
    get_backend().refresh(path)
    items: List[Dict[str, Any]] = []
    if not path.exists():
        return items
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def _atomic_write_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    assert_not_cruft(path.parent, "mkdir (pipeline_write._atomic_write_jsonl)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=True) + "\n")

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_pipeline_write")


def _append_to_archive(path: Path, item: Dict[str, Any]) -> None:
    """Append a single record to the archive file.

    Called inside the live-path lock during move-to-archived. The live-path
    lock serializes all pipeline writes, so the archive append is safe
    without its own lock (same design as pipeline.py cmd_move's
    append_jsonl(ARCHIVE_PATH, rec) which uses a separate lock; here we
    accept the narrower guarantee because the daemon serializes on
    live_path).
    """
    assert_not_cruft(path.parent, "mkdir (pipeline_write._append_to_archive)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=True) + "\n")


# ---------------------------------------------------------------------------
# Normalization + validation (mirrors pipeline.py)
# ---------------------------------------------------------------------------

def _stringify_dates(obj):
    from datetime import date as _date, datetime as _datetime
    if isinstance(obj, dict):
        return {k: _stringify_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify_dates(v) for v in obj]
    if isinstance(obj, (_date, _datetime)):
        return obj.isoformat()
    return obj


def _normalize_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    rec = _stringify_dates(rec)
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
            # both-exist branch — surprise stayed None (). Mirrors
            # pipeline.py normalize_record (parity invariant, this module docstring).
            if rec[new_name] is None and rec[old_name] is not None:
                rec[new_name] = rec[old_name]
            del rec[old_name]
    # SURPRISE IS DERIVED, NEVER ACCEPTED FROM THE CALLER ().
    # It is a pure function of (outcome, confidence) — both already on the
    # record — so a caller-supplied value is not an input, it is a second
    # writer of a derived field, and it drifted: measured across the resolved +
    # archived union (769 records, 391 scoreable), 158 stored values (40.4%)
    # disagreed with the canonical helper and 80 (20.5%) disagreed by enough to
    # change the /review-hypotheses Step 3.5 branch. 47 of those UNDER-stated,
    # so a mandated broad re-retrieve + reconciliation never ran — silently,
    # because nothing errors and the record looks complete. Deriving here makes
    # the drift structurally impossible rather than merely forbidden by prose,
    # which is why this is a subtraction and not a validating gate: a gate
    # would keep two writers and need its own enforcement (same shape as
    # guard-1280 / guard-1604).
    #
    # All four write endpoints in this module — move, add, update, update_field
    # — call _normalize_record, but that is NOT sufficient on its own and this
    # comment used to claim it was. update_field normalizes and THEN assigns
    # `rec[field] = value`, so the assignment lands on top of anything derived
    # here; it re-applies the derivation itself (see apply_derived_surprise's
    # docstring for the two measured holes). "Every writer calls the normalizer"
    # and "every writer is covered" are different claims — the second needs the
    # ordering checked, not just the call.
    #
    # Placed AFTER the renames block on purpose, so a caller using the legacy
    # `surprise_level` name has already been folded into `surprise` and cannot
    # reintroduce a stale value behind this assignment.
    #
    # derive_surprise returns None for anything not genuinely scoreable (no
    # outcome, a non-CONFIRMED/CORRECTED outcome, or a missing/unparseable
    # confidence) and we leave the stored value untouched in that case — so an
    # unresolved record's `surprise: None` keeps meaning "not yet resolved"
    # rather than being overwritten with a 0 that reads as a real measurement.
    apply_derived_surprise(rec)

    if "slug" not in rec and "id" in rec:
        parts = rec["id"].split("_", 1)
        rec["slug"] = parts[1] if len(parts) > 1 else rec["id"]
    for field, default in DEFAULT_FIELDS.items():
        if field not in rec:
            if field == "slug":
                continue
            rec[field] = default
    for date_field in ("formed_date", "outcome_date", "reflected_date"):
        val = rec.get(date_field)
        if val is not None and not isinstance(val, str):
            rec[date_field] = str(val)
    # Auto-default resolves_by for discovered short/long hypotheses ().
    # Mirror of pipeline.py normalize_record -- keep identical. validate_
    # formation_quality soft-exempts stage=discovered from the resolves_by
    # requirement, so a discovered short/long record created without one has no
    # resolution anchor and sits un-resolvable. Default at creation: short ->
    # +7d, long -> +30d from formed_date (matches the  backfill
    # windows). Non-discovered stages keep the explicit-resolves_by contract
    # enforced by _validate_formation_quality.
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


def _validate_record(rec: Dict[str, Any],
                     changed_fields: Optional[Iterable[str]] = None) -> None:
    """Validate a pipeline record. Raises ValueError on invalid.

    `changed_fields` names the fields THIS write touches. It scopes the
    position VALUE checks only (g-115-4821) — every structural check below
    still runs on every write path, because guard-330 requires the
    full-record validator on every path and calls add-path-only validation
    insufficient. Passing None keeps the pre-g-115-4821 behaviour (validate
    everything) and is correct for create and whole-record-replace callers.

    Why position needs scoping: the checks are whole-record, so a record
    whose position was written before the g-115-3802 create-time type gate
    was refused by EVERY later mutation — including archive_sweep, which is
    the mechanism that would have retired it. 70 records (25 live / 45
    archive, measured 2026-08-04) were immutable for fields the write never
    touched. Position is the only field with such a legacy population: a
    full-corpus run of this validator returned position failures and nothing
    else on all 1,543 records.
    """
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
    surprise = rec.get("surprise")
    if surprise is not None:
        if isinstance(surprise, bool) or not isinstance(surprise, int):
            raise ValueError(
                f"Invalid surprise: {surprise!r} (must be int or null)")
    confidence = rec["confidence"]
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        raise ValueError(f"Invalid confidence: {confidence} (must be 0.0-1.0)")
    # Position VALUE checks are scoped to writes that actually touch position
    # (). The dot-prefix arm is guard-354: bare `f == "position"`
    # would let a nested `position.sub` write skip the check. Fails CLOSED —
    # an unrecognised/None changed_fields validates rather than skips.
    position = rec.get("position")
    checks_position = changed_fields is None or any(
        f == "position" or f.startswith("position.") for f in changed_fields)
    if checks_position:
        # Type gate () — the checks below are isinstance(str)-guarded, so a
        # non-string position bypassed every one of them. Mirrors core/scripts/pipeline.py.
        if position is not None and not isinstance(position, str):
            raise ValueError(
                f"Invalid position: {position!r} (must be a string; got "
                f"{type(position).__name__}). A bare number is not a prediction — "
                f"write YES/NO or a narrative stance (guard-1800).")
        if isinstance(position, str):
            pos_stripped = position.strip()
            if pos_stripped.lower() in VALID_STAGES:
                raise ValueError(
                    f"Invalid position: '{position}' (matches a pipeline stage name)")
            if pos_stripped.lower() in VALID_TYPES:
                raise ValueError(
                    f"Invalid position: '{position}' (matches a hypothesis type)")
            starts_with_verdict = pos_stripped.upper().startswith(("YES", "NO"))
            is_long_claim = len(pos_stripped) >= 20
            is_multi_word = len(pos_stripped.split()) >= 2
            if not (starts_with_verdict or is_long_claim or is_multi_word):
                raise ValueError(
                    f"Invalid position: '{position}' (must be YES/NO, >=20 chars, "
                    f"or a multi-word claim)")


def _validate_formation_quality(rec: Dict[str, Any]) -> None:
    stage = rec.get("stage", "discovered")
    horizon = rec.get("horizon", "")
    claim = rec.get("claim")
    if stage != "discovered":
        if not isinstance(claim, str) or len(claim.strip()) < 20:
            raise ValueError(
                "Invalid claim: non-discovered hypotheses must carry a "
                "claim field >=20 chars")
    elif isinstance(claim, str) and claim.strip() and len(claim.strip()) < 20:
        raise ValueError(
            f"Invalid claim: '{claim}' -- if claim field is present it "
            f"must be >=20 chars")
    if horizon in ("short", "long") and stage != "discovered":
        if not rec.get("resolves_by"):
            raise ValueError(
                f"Missing resolves_by: {horizon}-horizon hypotheses at "
                f"stage={stage} must name a resolution date.")
        resolution_method = (
            rec.get("resolution_criteria")
            or rec.get("resolution_method")
            or rec.get("rationale")
            or ""
        )
        if not isinstance(resolution_method, str) or len(resolution_method.strip()) < 10:
            raise ValueError(
                f"Missing resolution method: {horizon}-horizon hypotheses "
                f"at stage={stage} must populate resolution_criteria / "
                f"resolution_method / rationale with >=10 chars")
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
                "hypotheses must declare a measurement_channel")


# ---------------------------------------------------------------------------
# Resolution-evidence requirement ()
# ---------------------------------------------------------------------------
# Every CONFIRMED/CORRECTED resolution must carry >=1 verifiable external-
# evidence pointer so the calibration number is INDEPENDENTLY auditable. From
# the  calibration-honesty audit: ~53% of CONFIRMED/CORRECTED records
# had no outcome_detail at all, making accuracy unfalsifiable for the majority.
# The gate is GENEROUS (any one recognized pointer shape satisfies it) -- its
# job is to catch the empty/unverifiable cases, not to grade evidence quality;
# the evidence_pct metric (computed in _compute_meta) tracks real compliance
# over time. EXPIRED/UNRESOLVABLE are exempt (no prediction was validated, so
# there is nothing to back). Escape hatch: set the evidence_override field to a
# non-empty reason string (e.g. a math proof where the derivation IS the
# evidence) -- the reason persists in the record, which is MORE auditable than
# a transient CLI flag would be (the field rides the existing move-merge body,
# so no wrapper plumbing is needed). Enforced at the resolution single-writer
# (move -> resolved + add @ stage=resolved); update/update-field are tweak
# paths and stay ungated so legacy evidence-less records can still be edited.

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


def _has_resolution_evidence(rec: Dict[str, Any]) -> bool:
    """True if the record carries >=1 verifiable evidence pointer.

    Checks structured pointer fields first (experience_ref, evidence_for),
    then scans the free-text resolution fields for any recognized pointer
    shape. Generous by design -- see module note above.
    """
    # Structured pointers.
    exp_ref = rec.get("experience_ref")
    if isinstance(exp_ref, str) and exp_ref.strip():
        return True
    ev_for = rec.get("evidence_for")
    if isinstance(ev_for, str) and ev_for.strip():
        return True
    if isinstance(ev_for, (list, dict)) and ev_for:
        return True
    # Free-text pointer scan.
    text_parts = []
    for field in ("outcome_detail", "outcome_notes", "rationale",
                  "verification", "links"):
        val = rec.get(field)
        if isinstance(val, str):
            text_parts.append(val)
    text = "\n".join(text_parts)
    return any(p.search(text) for p in _EVIDENCE_PATTERNS)


def _validate_resolution_evidence(rec: Dict[str, Any]) -> None:
    """Require an external-evidence pointer on CONFIRMED/CORRECTED resolutions.

    No-op for non-accuracy outcomes (None/EXPIRED/UNRESOLVABLE) and for any
    record carrying a non-empty evidence_override reason. Raises ValueError
    otherwise (g-303-27).
    """
    if rec.get("outcome") not in _EVIDENCE_REQUIRED_OUTCOMES:
        return
    override = rec.get("evidence_override")
    if isinstance(override, str) and override.strip():
        return
    if _has_resolution_evidence(rec):
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


# ---------------------------------------------------------------------------
# Provenance stamps ()
#
# Authorship and resolver identity were only ever DERIVABLE after the fact, by
# two routes that disagree. Measured on the CONFIRMED corpus ():
# filed_by_agent 0/260, agent 0/260, author 1/260; experience_ref -> agent dir
# covers 70/260 and source_goal -> goal.filed_by_agent covers 50/260, and where
# both resolve they CONFLICT 10 times against 12 agreements. The resolver half
# is no better: of 474 resolved records, reflected_by 73 (15.4%), resolved_by 10
# (2.1%), resolver 2 (0.4%). So "who wrote this" and "who resolved this" are both
# unanswerable, which blocks every authorship-sensitive audit (self-influence,
# per-agent calibration, self-resolution bias).
#
# BOTH halves are stamped in the same change on purpose. Stamping only the author
# would leave every author-x-resolver question still blocked while LOOKING like
# provenance was solved -- the next auditor finds an author field, assumes the gap
# is closed, and rediscovers the resolver gap from scratch.
#
# `formed_at` is separate from `formed_date` rather than a widening of it: the
# date-only field is load-bearing for back-compat (_normalize_record's
# resolves_by defaulting parses it, and the archive sweep's age math reads it).
# A full timestamp is what makes intra-day ordering decidable -- the canonical
# lobbying case was "posted 06:56, filed 06:58", indistinguishable at date
# granularity, and 4 of 5 flags in the  audit were that artifact.
#
# Additive and idempotent by construction: an explicitly-supplied value always
# wins (a migration or backfill can set the true author), and re-entering a stamp
# point never overwrites an earlier one. No historical record is rewritten --
# authorship for ~73% of the corpus is genuinely unrecoverable and would have to
# be guessed, and a guessed provenance stamp reads as authority to every later
# reader (guard-1925).
#
# Neither stamp affects validation: _validate_record only tests for MISSING
# required fields, and _has_resolution_evidence reads experience_ref/evidence_for
# plus free text -- never these fields. So stamp ordering is behaviorally neutral.
# ---------------------------------------------------------------------------

def _stamp_formation_provenance(rec: Dict[str, Any], agent: str) -> None:
    """Stamp author + formed_at at hypothesis formation. Never overwrites."""
    if agent and not rec.get("author"):
        rec["author"] = agent
    if not rec.get("formed_at"):
        # Naive local-wall-clock ISO 8601, matching `date +%Y-%m-%dT%H:%M:%S`
        # (CLAUDE.md "Naming Rules" -- TZ=UTC is pinned fleet-wide, so this is
        # UTC wall time on every box and stamps are comparable across agents).
        rec["formed_at"] = datetime.now().isoformat(timespec="seconds")


def _stamp_resolution_provenance(rec: Dict[str, Any], agent: str) -> None:
    """Stamp resolved_by + resolved_at at resolution. Never overwrites.

    `resolved_by` reuses the existing sparse field name (10 live records) rather
    than minting a new one, so those records join the new ones without a
    translation layer. Deliberately NOT `reflected_by` (73 records): reflection
    is a later, separate event from resolution, and collapsing them would make
    "who decided this outcome" unanswerable again in a subtler way.

    `resolved_at` does not collide with the `resolved_date` -> `outcome_date`
    rename in _normalize_record -- that map keys on the exact string
    `resolved_date`.
    """
    if agent and not rec.get("resolved_by"):
        rec["resolved_by"] = agent
    if not rec.get("resolved_at"):
        rec["resolved_at"] = datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Record lookup
# ---------------------------------------------------------------------------

def _find_record(items: List[Dict[str, Any]], rec_id: str) -> Optional[Tuple[int, Dict]]:
    for i, rec in enumerate(items):
        if rec.get("id") == rec_id:
            return (i, rec)
    return None


def _check_no_duplicate(items: List[Dict[str, Any]], rec_id: str,
                        archive_items: List[Dict[str, Any]]) -> None:
    for item in items:
        if item.get("id") == rec_id:
            raise ValueError(f"Duplicate record ID: {rec_id}")
    for item in archive_items:
        if item.get("id") == rec_id:
            raise ValueError(f"Duplicate record ID (in archive): {rec_id}")


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_paths(ctx) -> Tuple[Path, Path, Path]:
    """Return (live_path, archive_path, base_dir)."""
    base = ctx.paths.world
    return (base / "pipeline.jsonl", base / "pipeline-archive.jsonl", base)


def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _parse_body_json(body: bytes) -> Any:
    if not body:
        raise ValueError("empty body")
    return json.loads(body.decode("utf-8"))


def _parse_value(value_str: str) -> Any:
    """Parse a string value into the appropriate Python type.
    Mirrors pipeline.py::parse_value."""
    if value_str == "true":
        return True
    if value_str == "false":
        return False
    if value_str == "null":
        return None
    if value_str == "[]":
        return []
    if value_str.startswith("{") or value_str.startswith("["):
        try:
            return json.loads(value_str)
        except json.JSONDecodeError:
            pass
    try:
        return int(value_str)
    except ValueError:
        pass
    try:
        return float(value_str)
    except ValueError:
        pass
    return value_str


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def move(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/pipeline/move?id=<rec_id>&stage=<target_stage>

    Body (optional): JSON merge data to apply before the stage transition.
    """
    from ..server import Response

    rec_id = (ctx.query.get("id") or "").strip()
    if not rec_id:
        return Response.error(400, "missing_param", "query parameter 'id' required")

    target_stage = (ctx.query.get("stage") or "").strip()
    if not target_stage:
        return Response.error(400, "missing_param", "query parameter 'stage' required")
    if target_stage not in VALID_STAGES:
        return Response.error(400, "invalid_stage",
                              f"Invalid target stage: {target_stage}")

    merge_data = {}
    if ctx.body:
        try:
            merge_data = _parse_body_json(ctx.body)
        except (ValueError, json.JSONDecodeError) as e:
            return Response.error(400, "invalid_body",
                                  f"body must be JSON merge object: {e}")
        if not isinstance(merge_data, dict):
            return Response.error(400, "invalid_body",
                                  "body must be a JSON object")

    live_path, archive_path, base_dir = _resolve_paths(ctx)
    meta_path = base_dir / "pipeline-meta.json"
    agent = _agent_name(ctx)

    written_rec = {}

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            found = _find_record(items, rec_id)
            if found is None:
                return Response.error(404, "record_not_found",
                                      f"Record {rec_id} not found in live file")
            idx, rec = found

            for key, val in merge_data.items():
                rec[key] = val
            rec["stage"] = target_stage
            rec = _normalize_record(rec)

            try:
                # A move validates position only when the merge supplies it
                # (). This is what made the pre-fix merge-repair
                # escape hatch work: supplying a valid position in the same
                # merge satisfied the whole-record check.
                _validate_record(rec, changed_fields=set(merge_data) | {"stage"})
            except ValueError as e:
                return Response.error(400, "validation_failed",
                                      f"Validation error on move: {e}")

            if target_stage in ("active", "resolved"):
                try:
                    _validate_formation_quality(rec)
                except ValueError as e:
                    return Response.error(400, "formation_quality_failed",
                                          f"Formation-quality error on move to "
                                          f"{target_stage}: {e}")

            # Resolution-evidence gate (): fires only on the move INTO
            # resolved, so re-resolved->archived transitions of a legacy
            # evidence-less record are unaffected. No-ops for EXPIRED/
            # UNRESOLVABLE and for an evidence_override reason.
            if target_stage == "resolved":
                try:
                    _validate_resolution_evidence(rec)
                except ValueError as e:
                    return Response.error(400, "resolution_evidence_required",
                                          str(e))
                # Stamp the resolution clock at the single move-INTO-resolved
                # chokepoint (): archive_sweep ages resolved records
                # on outcome_date, but nothing on this path set it — 31 live
                # records resolved date-less and never archived. Mirrors the
                # archived_date stamp below; an explicit caller value wins.
                if not rec.get("outcome_date"):
                    rec["outcome_date"] = date.today().isoformat()
                # Resolver identity, same chokepoint and same posture ().
                # outcome_date answers WHEN a hypothesis was resolved; this answers
                # WHO resolved it, which no field reliably did (resolved_by was
                # populated on 10 of 474 resolved records).
                _stamp_resolution_provenance(rec, agent)

            if target_stage == "archived":
                # Tombstone-in-live archival (): keep the record in
                # live as a stage=archived tombstone — the own-cloud merge
                # (coordination_merge.merge_pipeline) is a per-file union-by-id
                # that cannot express a cross-file removal, so popping here let
                # a pre-removal remote copy resurrect archived records at their
                # old stage. The monotonic stage rank converges the in-place
                # flip fleet-wide; archive_sweep prunes the tombstone after
                # PRUNE_GRACE_DAYS. Archive-append is deduped by id so a
                # tombstone re-move (or resurrection residue) never duplicates
                # the archive copy.
                if not rec.get("archived_date"):
                    rec["archived_date"] = date.today().isoformat()
                if not any(r.get("id") == rec_id
                           for r in _read_jsonl(archive_path)):
                    _append_to_archive(archive_path, rec)
            items[idx] = rec

            written_rec.update(rec)

            history.snapshot(live_path, base_dir, agent,
                             summary=f"pipeline-move {rec_id} -> {target_stage}")
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"pipeline-move {rec_id} -> {target_stage}",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # Meta recomputation outside the live-path lock (mirrors archive_sweep
    # and pipeline.py cmd_move which calls _update_meta_counts after the
    # lock releases).
    try:
        _update_meta(live_path, archive_path, meta_path)
    except OSError:
        pass  # best-effort; move already committed

    return Response.json({
        "ok": True,
        "record_id": rec_id,
        "stage": target_stage,
        "record": written_rec,
    })


def add(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/pipeline/add

    Body: JSON pipeline record.
    """
    from ..server import Response

    try:
        rec = _parse_body_json(ctx.body)
    except (ValueError, json.JSONDecodeError) as e:
        return Response.error(400, "invalid_body",
                              f"body must be JSON pipeline record: {e}")
    if not isinstance(rec, dict):
        return Response.error(400, "invalid_body", "body must be a JSON object")

    if "stage" not in rec:
        rec["stage"] = "discovered"
    rec = _normalize_record(rec)

    try:
        _validate_record(rec)
        _validate_formation_quality(rec)
        # Resolution-evidence gate (): a record added directly at
        # stage=resolved is a resolution event (fresh record -> no legacy-edit
        # concern). No-ops unless outcome is CONFIRMED/CORRECTED.
        if rec.get("stage") == "resolved":
            _validate_resolution_evidence(rec)
    except ValueError as e:
        return Response.error(400, "validation_failed", str(e))

    live_path, archive_path, base_dir = _resolve_paths(ctx)
    agent = _agent_name(ctx)

    # Provenance stamps (). Formation always; resolution too when the
    # record is added directly at stage=resolved -- the block above already
    # treats that as a resolution event (it runs the resolution-evidence gate),
    # so it needs the resolver identity for the same reason a move does.
    _stamp_formation_provenance(rec, agent)
    if rec.get("stage") == "resolved":
        _stamp_resolution_provenance(rec, agent)

    # Archive snapshot taken pre-lock (same pattern as pipeline.py cmd_add).
    archive_items = _read_jsonl(archive_path)

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            try:
                _check_no_duplicate(items, rec["id"], archive_items)
            except ValueError as e:
                return Response.error(409, "duplicate_id", str(e))

            items.append(rec)

            history.snapshot(live_path, base_dir, agent,
                             summary=f"pipeline-add {rec['id']}")
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"pipeline-add {rec['id']}",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({
        "ok": True,
        "record_id": rec["id"],
        "record": rec,
    })


def update(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/pipeline/update?id=<rec_id>

    Body: JSON replacement record. The record is normalized, validated
    (both record and formation-quality), and written in place of the
    existing record with the given id. Searches live first, then the
    archive file (g-115-1615): a multi-field-corrupt ARCHIVED record
    rejects every single-field update-field repair (the whole record
    re-validates on each field write), so this atomic whole-record path
    is the only tool that can repair it once archived (rb-2239).
    """
    from ..server import Response

    rec_id = (ctx.query.get("id") or "").strip()
    if not rec_id:
        return Response.error(400, "missing_param", "query parameter 'id' required")

    try:
        rec = _parse_body_json(ctx.body)
    except (ValueError, json.JSONDecodeError) as e:
        return Response.error(400, "invalid_body",
                              f"body must be JSON pipeline record: {e}")
    if not isinstance(rec, dict):
        return Response.error(400, "invalid_body", "body must be a JSON object")

    rec = _normalize_record(rec)

    try:
        _validate_record(rec)
        _validate_formation_quality(rec)
    except ValueError as e:
        return Response.error(400, "validation_failed", str(e))

    live_path, archive_path, base_dir = _resolve_paths(ctx)
    meta_path = base_dir / "pipeline-meta.json"
    agent = _agent_name(ctx)

    # Probe live + archive outside the lock to pick the target file
    # (: mirror update_field's archive-probe below so a
    # multi-field-corrupt ARCHIVED record can be repaired via this atomic
    # whole-record path; before this, update() was live-only and 404'd on
    # archived ids, leaving such records unrepairable by any tool — rb-2239).
    live_items = _read_jsonl(live_path)
    if _find_record(live_items, rec_id) is not None:
        target_path = live_path
    else:
        archive_items = _read_jsonl(archive_path)
        if _find_record(archive_items, rec_id) is not None:
            target_path = archive_path
        else:
            return Response.error(404, "record_not_found",
                                  f"Record {rec_id} not found")

    written_rec = {}

    try:
        with file_locks.locked(target_path):
            items = _read_jsonl(target_path)
            found = _find_record(items, rec_id)
            if found is None:
                return Response.error(404, "record_not_found",
                                      f"Record {rec_id} not found")
            idx = found[0]
            items[idx] = rec
            written_rec.update(rec)

            history.snapshot(target_path, base_dir, agent,
                             summary=f"pipeline-update {rec_id}")
            _atomic_write_jsonl(target_path, items)
            changelog.append(base_dir, agent, target_path, "edit",
                             summary=f"pipeline-update {rec_id}",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(target_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # Meta recomputation outside the target-path lock (mirrors archive_sweep
    # and pipeline.py cmd_update which calls _update_meta_counts after the
    # lock releases).
    try:
        _update_meta(live_path, archive_path, meta_path)
    except OSError:
        pass  # best-effort; update already committed

    return Response.json({
        "ok": True,
        "record_id": rec_id,
        "record": written_rec,
    })


def update_field(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/pipeline/update-field?id=<rec_id>&field=<field>&value=<value>"""
    from ..server import Response

    rec_id = (ctx.query.get("id") or "").strip()
    if not rec_id:
        return Response.error(400, "missing_param", "query parameter 'id' required")

    field = (ctx.query.get("field") or "").strip()
    if not field:
        return Response.error(400, "missing_param", "query parameter 'field' required")

    value_str = ctx.query.get("value")
    if value_str is None:
        return Response.error(400, "missing_param", "query parameter 'value' required")
    value = _parse_value(value_str)

    # Reject dotted field names (symmetric with pipeline.py update-field).
    if "." in field:
        return Response.error(400, "dotted_field_rejected",
                              f"Dotted field name '{field}' is not supported. "
                              f"Write flat top-level keys only.")

    live_path, archive_path, base_dir = _resolve_paths(ctx)
    meta_path = base_dir / "pipeline-meta.json"
    agent = _agent_name(ctx)

    # Probe live + archive outside the lock to pick the target file.
    live_items = _read_jsonl(live_path)
    if _find_record(live_items, rec_id) is not None:
        target_path = live_path
    else:
        archive_items = _read_jsonl(archive_path)
        if _find_record(archive_items, rec_id) is not None:
            target_path = archive_path
        else:
            return Response.error(404, "record_not_found",
                                  f"Record {rec_id} not found")

    written_rec = {}

    try:
        with file_locks.locked(target_path):
            items = _read_jsonl(target_path)
            found = _find_record(items, rec_id)
            if found is None:
                return Response.error(404, "record_not_found",
                                      f"Record {rec_id} not found")
            idx, rec = found
            rec = _normalize_record(rec)
            rec[field] = value
            # Re-derive AFTER the assignment (). _normalize_record
            # already derived surprise, but that ran one line too early to
            # matter here: `--field surprise --value 99` overwrites the derived
            # value, and `--field outcome --value CONFIRMED` changes the very
            # input the derivation reads without re-running it. Both measured.
            apply_derived_surprise(rec)

            try:
                # Single-field update: only this field is being written, so a
                # pre-existing position it does not touch must not refuse it
                # ( — this is the path that bricked 70 records).
                _validate_record(rec, changed_fields={field})
            except ValueError as e:
                return Response.error(400, "validation_failed", str(e))

            # Auto-set provenance when reflected becomes true: WHEN
            # (reflected_date) + WHO (reflected_by = the agent whose reflection
            # set the flag), so the monotonic reflected=true is AUDITABLE, not
            # opaque ( — reflection_ref provenance for the reflect log).
            # Both write-once (guarded on absence): a later re-affirm of an
            # already-reflected record never overwrites the original provenance.
            if field == "reflected" and value is True:
                if not rec.get("reflected_date"):
                    rec["reflected_date"] = date.today().isoformat()
                if agent and not rec.get("reflected_by"):
                    rec["reflected_by"] = agent

            items[idx] = rec
            written_rec.update(rec)

            history.snapshot(target_path, base_dir, agent,
                             summary=f"pipeline-update-field {rec_id} {field}")
            _atomic_write_jsonl(target_path, items)
            changelog.append(base_dir, agent, target_path, "edit",
                             summary=f"pipeline-update-field {rec_id} {field}",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(target_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # Meta recomputation outside the target-path lock (mirrors archive_sweep
    # and pipeline.py cmd_update_field which calls _update_meta_counts after
    # the lock releases).
    try:
        _update_meta(live_path, archive_path, meta_path)
    except OSError:
        pass  # best-effort; update-field already committed

    return Response.json({
        "ok": True,
        "record_id": rec_id,
        "field": field,
        "record": written_rec,
    })


# ---------------------------------------------------------------------------
# Meta recomputation (mirrors pipeline.py compute_meta + _update_meta_counts)
# ---------------------------------------------------------------------------

# Archive sweep: resolved records older than this many days get archived.
# Duplicated from pipeline.py — same Decision #3 rationale.
ARCHIVE_AGE_DAYS = 3

# Tombstone-in-live archival (): a record moved to archived STAYS in
# pipeline.jsonl as a stage=archived tombstone for this many days before
# archive_sweep physically prunes it. The grace window lets the stage flip
# converge fleet-wide through the own-cloud union merge (which cannot express
# a cross-file removal — a pre-removal remote copy resurrected 94 archived
# records at their old stage, 2026-07-11) before the live copy disappears.
PRUNE_GRACE_DAYS = 14


def _empty_meta() -> Dict[str, Any]:
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


def _compute_meta(live_items: List[Dict], archive_items: List[Dict]) -> Dict:
    """Recompute meta from all records. Mirrors pipeline.py compute_meta."""
    meta = _empty_meta()
    # Dedup by id across live+archive (; mirrors pipeline.py
    # compute_meta): a tombstoned id is present in BOTH files by design —
    # count each hypothesis once (archive copy wins) so accuracy denominators
    # never double-count.
    _by_id: Dict[str, Dict] = {}
    _no_id: List[Dict] = []
    for rec in live_items + archive_items:
        rid = rec.get("id")
        if rid is None:
            _no_id.append(rec)
        else:
            _by_id[rid] = rec  # archive iterates second -> archive copy wins
    all_items = list(_by_id.values()) + _no_id

    for rec in live_items:
        stage = rec.get("stage", "discovered")
        if stage in meta["stage_counts"]:
            meta["stage_counts"][stage] += 1
    meta["stage_counts"]["archived"] = len(archive_items)

    resolved_records = [r for r in all_items
                        if r.get("outcome") in ("CONFIRMED", "CORRECTED")]
    meta["accuracy"]["total_resolved"] = len(resolved_records)

    confirmed = sum(1 for r in resolved_records if r["outcome"] == "CONFIRMED")
    corrected = sum(1 for r in resolved_records if r["outcome"] == "CORRECTED")
    meta["accuracy"]["confirmed"] = confirmed
    meta["accuracy"]["corrected"] = corrected
    total = confirmed + corrected
    meta["accuracy"]["accuracy_pct"] = (
        round(confirmed / total * 100, 1) if total > 0 else 0.0)

    # Evidence coverage (): share of CONFIRMED/CORRECTED resolutions
    # carrying >=1 verifiable evidence pointer. Surfaced alongside accuracy_pct
    # via pipeline-read.sh --accuracy so calibration auditability is tracked
    # over time. Denominator is the same resolved_records (CONFIRMED+CORRECTED).
    with_evidence = sum(1 for r in resolved_records
                        if _has_resolution_evidence(r))
    meta["accuracy"]["with_evidence"] = with_evidence
    meta["accuracy"]["evidence_pct"] = (
        round(with_evidence / len(resolved_records) * 100, 1)
        if resolved_records else 0.0)

    by_strategy: Dict[str, Any] = {}
    for r in resolved_records:
        strategy = r.get("strategy", r.get("verification", "unknown"))
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

    by_horizon: Dict[str, Any] = {}
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

    by_depth: Dict[str, Any] = {}
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

    # by_type (high-conviction / calibration / exploration / contrarian).
    # Mirrors pipeline.py compute_meta -- consumed by precheck-eval cmd_accuracy to
    # keep designed-uncertain exploration probes out of the calibration-relevant
    # overconfidence signal (, rb-268). This block was the drift gap that
    # left the live --accuracy path without by_type while pipeline.py had it
    # (): the daemon's _compute_meta is the authoritative writer of
    # pipeline-meta.json, so a missing block here meant by_type never reached the
    # gate regardless of pipeline.py.
    by_type: Dict[str, Any] = {}
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

    # by_confidence_band -- surfaces WHERE overconfidence concentrates ().
    # Bands per rb-323: >=0.80 reliable ("high"); the 0.50-0.75 noise zone splits
    # into "medium" (0.65-0.79) and "low" (<0.65). A high band with a low pct is
    # the overconfidence-drift signal. Records without a numeric confidence are
    # skipped (legacy). Symmetric to by_type / by_strategy / by_depth.
    by_confidence_band: Dict[str, Any] = {}
    for r in resolved_records:
        c = r.get("confidence")
        if not isinstance(c, (int, float)):
            continue
        band = "high" if c >= 0.80 else "medium" if c >= 0.65 else "low"
        if band not in by_confidence_band:
            by_confidence_band[band] = {"confirmed": 0, "total": 0, "pct": 0.0}
        by_confidence_band[band]["total"] += 1
        if r["outcome"] == "CONFIRMED":
            by_confidence_band[band]["confirmed"] += 1
    for b in by_confidence_band.values():
        b["pct"] = round(b["confirmed"] / b["total"] * 100, 1) if b["total"] > 0 else 0.0
    meta["accuracy"]["by_confidence_band"] = by_confidence_band

    meta["last_updated"] = date.today().isoformat()
    return meta


def _update_meta(live_path: Path, archive_path: Path, meta_path: Path) -> None:
    """Recompute and write pipeline-meta.json. Mirrors _update_meta_counts.

    Locks: live_path for the read, meta_path for the write.
    Callers release live_path before invoking this; we re-acquire it for
    the read so a concurrent atomic-write fallback (in-place truncate-
    rewrite when OneDrive blocks os.replace) cannot land us in the brief
    empty-file window — that window is what produced the all-zero
    stage_counts symptom (g-115-740: pipeline-read.sh band-aid). archive_path
    is implicitly serialized via the live_path lock per _append_to_archive's
    contract.
    meta_path lock mirrors meta_update (line 877) — _atomic_write_with_fallback
    requires the caller hold target_path's lock for fallback-path safety.
    """
    with file_locks.locked(live_path):
        live_items = _read_jsonl(live_path)
        archive_items = _read_jsonl(archive_path)
    meta = _compute_meta(live_items, archive_items)

    with file_locks.locked(meta_path):
        # own-cloud read-path fix (2026-07-02): refresh from S3 inside the lock
        # BEFORE the read, matching meta_update/recompute_meta (L1131/1198).
        # Without it, a fresh box (or a stale-lock-break race) reads a missing/
        # stale local pipeline-meta.json and drops a peer's micro_hypothesis_stats.
        # No-op on LocalBackend; no-op for out-of-root paths (keystone).
        get_backend().refresh(meta_path)
        # Preserve micro_hypothesis_stats from existing meta. Read inside the
        # lock so a concurrent meta_update can't lose its stats between our
        # read-old and our atomic-write-new.
        if meta_path.exists():
            try:
                with meta_path.open("r", encoding="utf-8") as f:
                    old_meta = json.load(f)
                if "micro_hypothesis_stats" in old_meta:
                    meta["micro_hypothesis_stats"] = old_meta["micro_hypothesis_stats"]
            except (json.JSONDecodeError, OSError):
                pass

        assert_not_cruft(meta_path.parent, "mkdir (pipeline_write meta refresh)")
        meta_path.parent.mkdir(parents=True, exist_ok=True)

        def _write(handle):
            json.dump(meta, handle, indent=2, ensure_ascii=True)
            handle.write("\n")

        _atomic_write_with_fallback(
            meta_path, _write, fallback_counter_key="daemon_pipeline_meta_write")


# ---------------------------------------------------------------------------
# archive-sweep handler
# ---------------------------------------------------------------------------

def _is_stale_unactivated(rec: Dict[str, Any], today: date) -> bool:
    """archive_sweep auto-expiry predicate ().

    True iff a never-activated discovered / measurement-pending hypothesis is
    past its resolves_by deadline with no recorded outcome and no formation
    trace (experience_ref). Any failing clause — not-yet-due, already-outcomed,
    carries an experience_ref, absent/unparseable resolves_by, or any other
    stage — returns False so the record is left untouched (no premature expiry).
    """
    if rec.get("stage") not in ("discovered", "measurement-pending"):
        return False
    if rec.get("outcome") is not None or rec.get("experience_ref") is not None:
        return False
    resolves_by = rec.get("resolves_by")
    if not resolves_by:
        return False
    try:
        rb = date.fromisoformat(str(resolves_by)[:10])
    except (ValueError, TypeError):
        return False
    # DATE granularity + strict '<' is the FLEET-WIDE deadline semantics, not a
    # local choice (guard-2073): a date-only resolves_by denotes the END of its
    # day, so due-today has not passed. This predicate is the pinned authority --
    # core/scripts/hypothesis-discovered-overdue-sweep.py::classify_overdue was
    # reconciled TO it () after parsing date-only values as midnight and
    # disagreeing with this function on the boundary day. If you change the
    # comparison here, change it there too; its
    # test_agrees_with_pinned_sibling_predicate_across_the_boundary replicates
    # this exact expression and will fail.
    return rb < today


def archive_sweep(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/pipeline/archive-sweep

    Batch operation: sweep all resolved records whose outcome_date is older
    than ARCHIVE_AGE_DAYS, flip their stage to 'archived' IN PLACE (they stay
    in live as tombstones — g-115-1986; see PRUNE_GRACE_DAYS), validate, and
    append to pipeline-archive.jsonl (deduped by id). Also maintains existing
    live tombstones: stamps a missing archived_date, prunes tombstones whose
    archived_date is older than PRUNE_GRACE_DAYS. Recomputes
    pipeline-meta.json afterwards.
    """
    from ..server import Response

    live_path, archive_path, base_dir = _resolve_paths(ctx)
    meta_path = base_dir / "pipeline-meta.json"
    agent = _agent_name(ctx)
    today = date.today()

    archived_count = 0
    pruned_count = 0
    # Per-record validation failures are collected here rather than aborting
    # the whole batch (): one corrupt record must not wedge all
    # archival. Invalid records stay in live (un-flipped, visible) and are
    # reported as skipped_invalid in the response.
    skipped_invalid: List[Dict[str, Any]] = []

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            archive_ids = {r.get("id") for r in _read_jsonl(archive_path)}
            to_archive: List[Dict[str, Any]] = []
            remaining: List[Dict[str, Any]] = []
            stamped_count = 0

            for rec in items:
                if rec.get("stage") == "archived":
                    # Live tombstone maintenance (): stamp a missing
                    # prune clock, prune aged tombstones (the archive copy
                    # already holds the record), keep young ones so the stage
                    # flip has time to converge fleet-wide via the union merge.
                    archived_date = rec.get("archived_date")
                    if not archived_date:
                        rec["archived_date"] = today.isoformat()
                        stamped_count += 1
                        remaining.append(rec)
                        continue
                    try:
                        age = (today - date.fromisoformat(
                            str(archived_date)[:10])).days
                    except ValueError:
                        remaining.append(rec)  # unparseable clock — keep
                        continue
                    if age >= PRUNE_GRACE_DAYS:
                        pruned_count += 1
                        continue  # physically pruned; archive copy remains
                    remaining.append(rec)
                    continue
                if rec.get("stage") == "resolved":
                    # Effective resolution clock (): 33/73 live
                    # resolved records were sweep-invisible — 31 carried only
                    # resolution_date_actual/reflected_date (no writer stamped
                    # outcome_date on the move-to-resolved path), 2 carried
                    # datetime-format outcome_date that bare fromisoformat
                    # rejects. Fallbacks are >= the resolution date, so they
                    # only ever DELAY archival; [:10] matches the tombstone
                    # idiom above.
                    outcome_date = (rec.get("outcome_date")
                                    or rec.get("resolution_date_actual")
                                    or rec.get("reflected_date"))
                    if outcome_date:
                        try:
                            od = date.fromisoformat(str(outcome_date)[:10])
                            if (today - od).days >= ARCHIVE_AGE_DAYS:
                                # Validate a candidate copy BEFORE mutating the
                                # live record so a single invalid record is
                                # skipped-and-reported, not fatal to the batch
                                # ().
                                candidate = dict(rec)
                                candidate["stage"] = "archived"
                                candidate["archived_date"] = today.isoformat()
                                try:
                                    # : the sweep changes only these
                                    # two fields. Validating position here is
                                    # what made the retirement path refuse the
                                    # very records it exists to retire.
                                    _validate_record(
                                        candidate,
                                        changed_fields={"stage", "archived_date"})
                                except ValueError as e:
                                    skipped_invalid.append({
                                        "id": rec.get("id", "?"),
                                        "reason": str(e),
                                        "branch": "resolved-archive",
                                    })
                                    remaining.append(rec)  # keep in live, un-flipped
                                    continue
                                rec["stage"] = "archived"
                                rec["archived_date"] = today.isoformat()
                                to_archive.append(rec)
                                remaining.append(rec)  # keep as tombstone
                                continue
                        except ValueError:
                            # date.fromisoformat parse failure — keep in live
                            pass
                elif _is_stale_unactivated(rec, today):
                    # : expire never-activated discovered /
                    # measurement-pending records past resolves_by (predicate
                    # above). archive_sweep previously archived ONLY
                    # stage==resolved by age, so these accumulated indefinitely
                    # past resolves_by — the measurement-pending ->
                    # archived-at-resolves_by transition documented at the
                    # pipeline.py module header was never wired here. EXPIRED
                    # lands in archived (never resolved), so it is excluded from
                    # accuracy stats and never reflected. _validate_record (NOT
                    # _validate_formation_quality) runs here, so claim-less
                    # discovered records validate cleanly.
                    # Validate a candidate copy BEFORE mutating the live record
                    # so a single invalid record is skipped-and-reported, not
                    # fatal to the batch ().
                    candidate = dict(rec)
                    candidate["outcome"] = "EXPIRED"
                    candidate["outcome_date"] = today.isoformat()
                    candidate["stage"] = "archived"
                    candidate["archived_date"] = today.isoformat()
                    try:
                        #  — same as the resolved-archive branch above.
                        _validate_record(
                            candidate,
                            changed_fields={"outcome", "outcome_date",
                                            "stage", "archived_date"})
                    except ValueError as e:
                        skipped_invalid.append({
                            "id": rec.get("id", "?"),
                            "reason": str(e),
                            "branch": "expiry",
                        })
                        remaining.append(rec)  # keep in live, un-flipped
                        continue
                    rec["outcome"] = "EXPIRED"
                    rec["outcome_date"] = today.isoformat()
                    rec["stage"] = "archived"
                    rec["archived_date"] = today.isoformat()
                    to_archive.append(rec)
                    remaining.append(rec)  # keep as tombstone
                    continue
                remaining.append(rec)

            archived_count = len(to_archive)

            if not to_archive and pruned_count == 0 and stamped_count == 0:
                return Response.json({
                    "ok": True,
                    "archived_count": 0,
                    "pruned_count": 0,
                    "skipped_invalid": skipped_invalid,
                })

            # Append each newly-flipped record to the archive file — once.
            # Ids already archived (tombstone re-sweeps, resurrection residue)
            # are skipped so the archive never grows duplicate groups.
            for rec in to_archive:
                if rec.get("id") not in archive_ids:
                    _append_to_archive(archive_path, rec)

            history.snapshot(live_path, base_dir, agent,
                             summary=f"pipeline-archive-sweep ({archived_count} records)")
            _atomic_write_jsonl(live_path, remaining)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"pipeline-archive-sweep ({archived_count} records)",
                             lines_changed=len(remaining))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # Meta recomputation outside the live-path lock (same pattern as
    # pipeline.py cmd_archive_sweep which calls _update_meta_counts after
    # locked_modify_jsonl returns).
    try:
        _update_meta(live_path, archive_path, meta_path)
    except OSError:
        pass  # best-effort; sweep already committed

    return Response.json({
        "ok": True,
        "archived_count": archived_count,
        "pruned_count": pruned_count,
        "skipped_invalid": skipped_invalid,
    })


def meta_update(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/pipeline/meta-update?field=<field>&value=<value>

    Mirrors pipeline.py cmd_meta_update: locked read of pipeline-meta.json,
    set field=value, stamp last_updated, atomic write back.  Dotted field
    names are rejected (flat top-level keys only).

    pipeline-meta.json is plain JSON (not JSONL), so we use json.load /
    json.dump with the same indent/ensure_ascii as _fileops.locked_write_json.
    """
    from ..server import Response

    field = (ctx.query.get("field") or "").strip()
    if not field:
        return Response.error(400, "missing_param",
                              "query parameter 'field' required")

    value_str = ctx.query.get("value")
    if value_str is None:
        return Response.error(400, "missing_param",
                              "query parameter 'value' required")
    value = _parse_value(value_str)

    # Reject dotted field names — symmetric with cmd_meta_update's Option A
    # reject ().
    if "." in field:
        return Response.error(400, "dotted_field_rejected",
                              f"Dotted field name '{field}' is not supported. "
                              f"This endpoint writes flat top-level keys only.")

    live_path, archive_path, base_dir = _resolve_paths(ctx)
    meta_path = base_dir / "pipeline-meta.json"
    agent = _agent_name(ctx)

    _EMPTY_META = {
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

    try:
        # WRITE-CLASS RESOLVED, no locked_rmw cure needed (, measured):
        # coordination_merge.merge_handler_for("pipeline-meta.json") returns
        # merge_pipeline_meta, so this is class (a) MERGE-PROTECTED — a
        # reconciler runs below the write and a conflict is merged rather than
        # refused, which is what locked_rmw's retry would otherwise be for. The
        # other half of the cure is already present below: the read is force-
        # freshed INSIDE the lock, so this site never had the unlocked-read
        # window that the class-(b) sites in mind_api/src/meta did.
        with file_locks.locked(meta_path):
            # #38 own-cloud: force-fresh the local cache BEFORE the raw read so
            # (a) the read sees a peer's committed meta and (b) the backend
            # records the If-Match fence etag. Without it the _atomic_write
            # below issues an UNCONDITIONAL PUT (fence=None) that silently
            # clobbers a concurrent peer's meta update on a stale-lock-break
            # race. No-op on LocalBackend; also materializes a remote-only meta.
            get_backend().refresh(meta_path)
            # Read existing or create default.
            if meta_path.exists():
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            else:
                data = dict(_EMPTY_META)

            data[field] = value
            data["last_updated"] = date.today().isoformat()

            # Atomic write via _fileops helper (same retry policy as
            # locked_write_json but we already hold the lock).
            assert_not_cruft(meta_path.parent, "mkdir (pipeline_write meta_update)")
            meta_path.parent.mkdir(parents=True, exist_ok=True)

            def _write(handle):
                json.dump(data, handle, indent=2, ensure_ascii=True)
                handle.write("\n")

            _atomic_write_with_fallback(
                meta_path, _write,
                fallback_counter_key="daemon_pipeline_meta_update")

            history.snapshot(meta_path, base_dir, agent,
                             summary="pipeline-meta-update")
            changelog.append(base_dir, agent, meta_path, "edit",
                             summary="pipeline-meta-update",
                             lines_changed=1)

    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "data": data})


def recompute_meta(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/pipeline/recompute-meta

    Mirrors pipeline.py cmd_recompute_meta: full recount of pipeline-meta.json
    from live + archive records, preserving micro_hypothesis_stats from the
    existing meta. Unlike the post-sweep _update_meta() refresh, this mirrors
    the CLI's write_json -> locked_write_json path, so it ALSO writes a history
    snapshot + changelog entry (cmd_recompute_meta is an explicit maintenance
    recount, not an incidental refresh). The serialization
    (`json.dump(meta, h, indent=2, ensure_ascii=True)` + trailing "\\n") is
    byte-identical to _fileops.locked_write_json (_fileops.py:1651-1653).
    """
    from ..server import Response

    live_path, archive_path, base_dir = _resolve_paths(ctx)
    meta_path = base_dir / "pipeline-meta.json"
    agent = _agent_name(ctx)

    try:
        with file_locks.locked(live_path):
            live_items = _read_jsonl(live_path)
            archive_items = _read_jsonl(archive_path)
        meta = _compute_meta(live_items, archive_items)

        with file_locks.locked(meta_path):
            # Preserve micro_hypothesis_stats from existing meta. Read inside
            # the lock so a concurrent meta_update can't lose its stats between
            # our read-old and our atomic-write-new (same as _update_meta).
            # #38 own-cloud: refresh BEFORE the raw read so the read sees a
            # peer's committed meta and the backend records the If-Match fence
            # etag — without it the _atomic_write issues an unconditional PUT
            # that silently clobbers a concurrent meta_update. No-op locally.
            get_backend().refresh(meta_path)
            if meta_path.exists():
                try:
                    old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if "micro_hypothesis_stats" in old_meta:
                        meta["micro_hypothesis_stats"] = old_meta["micro_hypothesis_stats"]
                except (json.JSONDecodeError, OSError):
                    pass

            assert_not_cruft(meta_path.parent, "mkdir (pipeline_write recompute-meta)")
            meta_path.parent.mkdir(parents=True, exist_ok=True)

            def _write(handle):
                json.dump(meta, handle, indent=2, ensure_ascii=True)
                handle.write("\n")

            _atomic_write_with_fallback(
                meta_path, _write,
                fallback_counter_key="daemon_pipeline_recompute_meta")

            history.snapshot(meta_path, base_dir, agent,
                             summary="pipeline-recompute-meta")
            changelog.append(base_dir, agent, meta_path, "edit",
                             summary="pipeline-recompute-meta", lines_changed=1)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "meta": meta})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/pipeline/move")] = move
    routes[("POST", "/v1/pipeline/add")] = add
    routes[("POST", "/v1/pipeline/update")] = update
    routes[("POST", "/v1/pipeline/update-field")] = update_field
    routes[("POST", "/v1/pipeline/archive-sweep")] = archive_sweep
    routes[("POST", "/v1/pipeline/meta-update")] = meta_update
    routes[("POST", "/v1/pipeline/recompute-meta")] = recompute_meta
