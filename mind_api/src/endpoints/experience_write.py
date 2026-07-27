"""POST /v1/experience/{add,update-field,archive-goal,meta-update,
archive-sweep,recompute-index} — experience writes.

Daemonises core/scripts/experience.py cmd_add / cmd_update_field /
cmd_archive_goal / cmd_meta_update / cmd_archive_sweep / cmd_recompute_index.
Experience records are PER-AGENT (ctx.paths.agent / experience.jsonl), so every
write attributes to the X-Mind-Agent header. (recompute-index READS the
collective pipeline from ctx.paths.world but WRITES the per-agent
experiential-index.yaml, so it is still agent-header-gated.)

The maintenance trio added 2026-05-29:
  - meta-update: set one flat field on experience-meta.json (raw write_json,
    no recompute, no history/changelog).
  - archive-sweep: 2-phase stale-record move (live→archive) under per-file
    locks + meta recompute; both writes carry history+changelog.
  - recompute-index: aggregate world pipeline CONFIRMED/CORRECTED outcomes into
    the hand-formatted experiential-index.yaml (byte-exact string layout, raw
    write, no history/changelog).

WHY A DEDICATED MODULE, NOT A StoreSpec ROW: experience is NOT a pure-CRUD
JSONL store. Two impurities make store.py's generic writer insufficient:

  1. Every add/update recomputes the experience-meta.json sidecar
     (_update_meta: total_live/total_archived/by_type/by_category/last_updated).
     StoreSpec has no post-commit side-effect hook.
  2. The duplicate check spans BOTH experience.jsonl AND
     experience-archive.jsonl (check_no_duplicate_id), and update-field
     probes live-then-archive to pick its target. StoreSpec._find scans a
     single configured path only.

BYTE-COMPATIBILITY: experience.jsonl lines are `json.dumps(rec,
ensure_ascii=True) + "\n"` (== _fileops). experience-meta.json mirrors
experience.py write_json EXACTLY (json.dump indent=2 ensure_ascii=True +
trailing "\n", atomic tmp+os.replace, NO history/changelog — the CLI
_update_meta writes it raw). The record dict is assembled in the SAME key
order as the CLI so the line is byte-identical except for `created` (a
datetime.now() stamp that differs by construction between any two writes).
history.snapshot + changelog.append use base = ctx.paths.agent (post-G1
resolve_base_dir returns AGENT_DIR for agent-dir files — see
core/scripts/_fileops.py:594).

Constants + validate/normalize are lifted VERBATIM from experience.py
(daemon-import-unsafe: experience.py's module-top `from _paths import
PROJECT_ROOT, WORLD_DIR, AGENT_DIR` raises when those are unset; the daemon
resolves per-request via ctx.paths). content_path existence resolves against
ctx.paths.project_root (the CLI uses the PROJECT_ROOT module constant).
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from .. import file_locks, history, changelog
from ..jsonl_cache import cache as _jsonl_cache
from ..agent_paths import assert_not_cruft

from _fileops import _atomic_write_with_fallback, _validate_no_surrogates  # noqa: E402
from storage_backend import get_backend  # noqa: E402  # s5c: own-cloud read freshness

# --- Constants lifted verbatim from experience.py -------------------------

VALID_TYPES = {"goal_execution", "hypothesis_formation", "research",
               "reflection", "user_correction", "user_interaction",
               "execution_reflection", "chat_session"}
ID_RE = re.compile(r"^exp-[a-z0-9._-]+$")

REQUIRED_FIELDS = {"id", "type", "category", "summary", "content_path"}
DEFAULT_FIELDS = {
    "goal_id": None,
    "hypothesis_id": None,
    "tree_nodes_related": [],
    "verbatim_anchors": [],
    "reasoning_chain": [],
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

MIN_TRACE_BYTES = 200
MIN_SUMMARY_CHARS = 20

# Archive-sweep staleness thresholds — verbatim from experience.py:91-94 (must
# match config/memory-pipeline.yaml experience_staleness section).
ARCHIVE_UNUSED_AFTER_DAYS = 30
ARCHIVE_LOW_UTILITY_AFTER_DAYS = 90
PROTECT_MIN_RETRIEVAL_COUNT = 5
PROTECT_MIN_UTILITY_RATIO = 0.5


# --- Paths ----------------------------------------------------------------

def _live_path(ctx) -> Path:
    return ctx.paths.agent / "experience.jsonl"


def _archive_path(ctx) -> Path:
    return ctx.paths.agent / "experience-archive.jsonl"


def _meta_path(ctx) -> Path:
    return ctx.paths.agent / "experience-meta.json"


def _index_path(ctx) -> Path:
    """experiential-index.yaml — AGENT-scoped output of recompute-index."""
    return ctx.paths.agent / "experiential-index.yaml"


def _pipeline_live_path(ctx) -> Path:
    """recompute-index reads the COLLECTIVE pipeline (world), not agent data."""
    return ctx.paths.world / "pipeline.jsonl"


def _pipeline_archive_path(ctx) -> Path:
    return ctx.paths.world / "pipeline-archive.jsonl"


def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _require_agent_header(ctx):
    from ..server import Response
    agent = (ctx.headers.get("x-mind-agent") or "").strip()
    if not agent:
        return Response.error(
            400, "missing_agent_header",
            "X-Mind-Agent header required for experience writes (g-115-957).")
    return None


# --- JSONL helpers --------------------------------------------------------

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
            if line:
                items.append(json.loads(line))
    return items


def _atomic_write_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    assert_not_cruft(path.parent, "mkdir (experience write)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=True) + "\n")

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_experience_write")


# --- Validate / normalize (verbatim from experience.py) -------------------

# Goal-id embedded in an experience id: exp-{goal-id}-{slug}. Verbatim from
# experience.py derive_goal_id_from_id () — the daemon add path builds
# ids as exp-{goal_id}-{skill_slug}, so a caller-formed cmd_add record can leave
# the goal_id FIELD null while the id still names the goal. Underscore-prefixed
# per the daemon's private-helper convention (cf. _normalize_record).
GOAL_ID_IN_EXP_ID_RE = re.compile(r"^exp-(g-\d{3}-\d{2,4})-")


def _derive_goal_id_from_id(rec_id):
    """Return the g-NNN-NN goal-id embedded in an experience id, or None."""
    if not rec_id:
        return None
    m = GOAL_ID_IN_EXP_ID_RE.match(rec_id)
    return m.group(1) if m else None


def _normalize_record(rec: dict) -> dict:
    """experience.py normalize_record — defaults + deep sub-key backfill."""
    for f, default in DEFAULT_FIELDS.items():
        if f not in rec:
            if isinstance(default, (dict, list)):
                rec[f] = json.loads(json.dumps(default))  # deep copy
            else:
                rec[f] = default
        elif isinstance(default, dict) and isinstance(rec[f], dict):
            for sub_key, sub_default in default.items():
                if sub_key not in rec[f]:
                    rec[f][sub_key] = sub_default
    # Backfill a null goal_id from the id when the id embeds a canonical goal-id
    # (exp-g-NNN-NN-*). Symmetric with experience.py normalize_record ()
    # — without this the LIVE daemon write path leaves goal_id null on caller-formed
    # cmd_add records, blinding the daemon --goal read filter (rec.get("goal_id")
    # == goal). NEVER overwrites a present value; slug-only ids stay null.
    if not rec.get("goal_id"):
        derived = _derive_goal_id_from_id(rec.get("id"))
        if derived:
            rec["goal_id"] = derived
    return rec


def _validate_record(ctx, rec: dict) -> None:
    """experience.py validate_record. content_path resolves against
    ctx.paths.project_root (the CLI uses the PROJECT_ROOT module constant)."""
    missing = REQUIRED_FIELDS - set(rec.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")
    if not ID_RE.match(rec["id"]):
        raise ValueError(f"Invalid record ID format: {rec['id']} (expected exp-{{slug}})")
    if rec["type"] not in VALID_TYPES:
        raise ValueError(f"Invalid type: {rec['type']}")
    anchors = rec.get("verbatim_anchors", [])
    if not isinstance(anchors, list):
        raise ValueError("verbatim_anchors must be a list")
    for anchor in anchors:
        if not isinstance(anchor, dict) or "key" not in anchor or "content" not in anchor:
            raise ValueError("Each verbatim_anchor must have 'key' and 'content' fields")
    stats = rec.get("retrieval_stats")
    if stats is not None and not isinstance(stats, dict):
        raise ValueError("retrieval_stats must be a dict")
    # guard-1373 /  (Layer-B write-time enforcement): reject a
    # content_path under a temp/ directory segment. temp/ is periodically
    # drained, so a body registered there is orphan-prone (foxtrot accumulated
    # 23 such orphaned entries — ). guard-1373 is the LLM-facing rule
    # (Layer A) and the verify-learning audit is the detective (Layer C); this
    # closes the programmatic write path that neither prevented. Segment match
    # (PurePosixPath.parts), not substring, so "template/" etc. are unaffected.
    cp_parts = PurePosixPath(str(rec["content_path"]).replace("\\", "/")).parts
    if "temp" in cp_parts:
        raise ValueError(
            f"content_path must not be under a temp/ directory (orphan-prone — "
            f"temp/ is drained): {rec['content_path']}; use a durable location "
            f"such as agents/<agent>/experience/ (guard-1373)")
    content_path = Path(rec["content_path"])
    if not content_path.is_absolute():
        content_path = ctx.paths.project_root / content_path
    if not content_path.exists():
        raise ValueError(f"content_path file does not exist: {rec['content_path']}")


def _stamp_now() -> str:
    """experience.py _stamp_now — full ISO datetime, seconds precision."""
    return datetime.now().isoformat(timespec="seconds")


def _find_by_id(items: List[dict], rec_id: str) -> Optional[Tuple[int, dict]]:
    for i, rec in enumerate(items):
        if rec.get("id") == rec_id:
            return (i, rec)
    return None


def _check_no_duplicate_id(items, rec_id, archive_items=None) -> None:
    for item in items:
        if item.get("id") == rec_id:
            raise ValueError(f"Duplicate record ID: {rec_id}")
    if archive_items:
        for item in archive_items:
            if item.get("id") == rec_id:
                raise ValueError(f"Duplicate record ID (in archive): {rec_id}")


def _uniquify_id(base_id: str, items, archive_items=None) -> str:
    """: auto-suffix on id collision. A recurring re-run of the same
    goal+skill is a NEW execution, not an idempotent retry — the pre-fix 409
    refusal silently lost the fresh trace (orphaned .md). Try base, then
    -YYYYMMDD, then -YYYYMMDD-2..-99; same-day exhaustion raises ValueError."""
    existing = {r.get("id") for r in items}
    existing.update(r.get("id") for r in (archive_items or []))
    if base_id not in existing:
        return base_id
    dated = f"{base_id}-{date.today().strftime('%Y%m%d')}"
    if dated not in existing:
        return dated
    for n in range(2, 100):
        cand = f"{dated}-{n}"
        if cand not in existing:
            return cand
    raise ValueError(
        f"could not uniquify {base_id} after 99 same-day attempts")


def _parse_value(value_str: str):
    """experience.py parse_value."""
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


# --- experience-meta.json recompute (verbatim from experience.py) ---------

def _empty_meta() -> dict:
    """experience.py empty_meta (verbatim key order). Used by meta-update when
    experience-meta.json is absent — meta-update does NOT recompute counts (it
    sets one flat field on the existing/empty skeleton), unlike _compute_meta."""
    return {
        "last_updated": None,
        "total_live": 0,
        "total_archived": 0,
        "by_type": {},
        "by_category": {},
    }


def _compute_meta(live_items, archive_items) -> dict:
    """experience.py empty_meta + compute_meta. Key order: last_updated,
    total_live, total_archived, by_type, by_category."""
    meta = {
        "last_updated": None,
        "total_live": 0,
        "total_archived": 0,
        "by_type": {},
        "by_category": {},
    }
    meta["total_live"] = len(live_items)
    meta["total_archived"] = len(archive_items)
    by_type: Dict[str, int] = {}
    by_category: Dict[str, int] = {}
    for rec in live_items + archive_items:
        t = rec.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
        c = rec.get("category", "unknown")
        by_category[c] = by_category.get(c, 0) + 1
    meta["by_type"] = by_type
    meta["by_category"] = by_category
    meta["last_updated"] = date.today().isoformat()
    return meta


def _update_meta(ctx) -> None:
    """Recompute experience-meta.json. Mirrors experience.py write_json
    byte-for-byte (indent=2, ensure_ascii=True, trailing newline, atomic
    tmp+os.replace, NO history/changelog)."""
    live = _read_jsonl(_live_path(ctx))
    archive = _read_jsonl(_archive_path(ctx))
    meta = _compute_meta(live, archive)
    p = _meta_path(ctx)
    assert_not_cruft(p.parent, "mkdir (experience meta)")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=True)
        f.write("\n")
    os.replace(str(tmp), str(p))


def _append_record(ctx, rec: dict, summary: str) -> None:
    """Append one record to experience.jsonl under the file lock, with
    history + changelog (base = agent dir, per resolve_base_dir post-G1)."""
    live = _live_path(ctx)
    base = ctx.paths.agent
    agent = _agent_name(ctx)
    _validate_no_surrogates(rec, live)
    assert_not_cruft(live.parent, "mkdir (experience append)")
    live.parent.mkdir(parents=True, exist_ok=True)
    history.snapshot(live, base, agent, summary=summary)
    # s5c (own-cloud): route the append through the backend so the experience
    # record reaches S3 (primary domain state, not a deferrable audit log).
    # LocalBackend does the IDENTICAL raw open('a') append — unchanged locally.
    get_backend().append_jsonl_record(live, rec)
    changelog.append(base, agent, live, "edit", summary=summary, lines_changed=1)
    _jsonl_cache().invalidate(live)


def _parse_body(ctx) -> Tuple[Optional[dict], Optional["Response"]]:  # type: ignore[name-defined]
    from ..server import Response
    if not ctx.body:
        return None, Response.error(400, "invalid_body", "empty body")
    try:
        obj = json.loads(ctx.body.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as e:
        return None, Response.error(400, "invalid_body", f"body must be JSON: {e}")
    if not isinstance(obj, dict):
        return None, Response.error(400, "invalid_body", "body must be a JSON object")
    return obj, None


# ---------------------------------------------------------------------------
# POST /v1/experience/add
# ---------------------------------------------------------------------------

def add(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/experience/add  body: JSON experience record.

    Mirrors experience.py cmd_add: normalize -> stamp created -> validate
    (content_path on disk) -> locked dup-check(live+archive) -> append ->
    _update_meta.
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
    rec, err = _parse_body(ctx)
    if err:
        return err

    # Order MUST match cmd_add: normalize THEN created (key lands after defaults).
    rec = _normalize_record(rec)
    rec["created"] = _stamp_now()

    # : derive goal_id from the id when the payload omits it — a
    # null goal_id makes the record invisible to the exact-match --goal read
    # filter even though the goal id is embedded in the record id
    # (fleet measured 28-34% null on 2026-07-10). Conservative prefix regex:
    # only the canonical exp-{goal-id}-{suffix} shape derives.
    if not rec.get("goal_id"):
        m = re.match(r"^exp-(g-\d+-\d+(?:-\d+)?)-", str(rec.get("id") or ""))
        if m:
            rec["goal_id"] = m.group(1)

    try:
        _validate_record(ctx, rec)
    except ValueError as e:
        return Response.error(400, "validation_failed", str(e))

    live = _live_path(ctx)
    try:
        with file_locks.locked(live):
            items = _read_jsonl(live)
            archive = _read_jsonl(_archive_path(ctx))
            try:
                _check_no_duplicate_id(items, rec["id"], archive)
            except ValueError as e:
                return Response.error(409, "duplicate_id", str(e))
            _append_record(ctx, rec, summary=f"experience-add {rec['id']}")
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    _update_meta(ctx)
    return Response.json({"ok": True, "record": rec})


# ---------------------------------------------------------------------------
# POST /v1/experience/update-field
# ---------------------------------------------------------------------------

def update_field(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/experience/update-field?id=&field=&value=

    Mirrors experience.py cmd_update_field: reject created, probe live then
    archive for target, locked full-rewrite (normalize -> reject dotted ->
    set -> validate -> recompute utility_ratio), _update_meta.
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

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

    if field == "created" or field.startswith("created."):
        return Response.error(
            400, "immutable_field",
            "`created` is script-stamped at add time and cannot be updated.")
    if "." in field:
        return Response.error(
            400, "dotted_field_rejected",
            f"dotted field name '{field}' is not supported; write flat "
            f"top-level keys only.")

    # Probe live then archive to pick the target file.
    if _find_by_id(_read_jsonl(_live_path(ctx)), rec_id) is not None:
        target = _live_path(ctx)
    elif _find_by_id(_read_jsonl(_archive_path(ctx)), rec_id) is not None:
        target = _archive_path(ctx)
    else:
        return Response.error(404, "not_found", f"Record {rec_id} not found")

    base = ctx.paths.agent
    agent = _agent_name(ctx)
    written: dict = {}
    try:
        with file_locks.locked(target):
            items = _read_jsonl(target)
            found = _find_by_id(items, rec_id)
            if found is None:
                return Response.error(404, "not_found", f"Record {rec_id} not found")
            idx, rec = found
            rec = _normalize_record(rec)
            rec[field] = value
            try:
                _validate_record(ctx, rec)
            except ValueError as e:
                return Response.error(400, "validation_failed", str(e))
            stats = rec.get("retrieval_stats")
            if (stats and isinstance(stats, dict)
                    and field.startswith("retrieval_stats.")):
                rc = stats["retrieval_count"]
                tu = stats["times_useful"]
                stats["utility_ratio"] = round(tu / max(rc, 1), 4)
            items[idx] = rec
            for item in items:
                _validate_no_surrogates(item, target)
            history.snapshot(target, base, agent,
                             summary=f"experience-update-field {rec_id} {field}")
            _atomic_write_jsonl(target, items)
            changelog.append(base, agent, target, "edit",
                             summary=f"experience-update-field {rec_id} {field}",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(target)
            written = rec
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    _update_meta(ctx)
    return Response.json({"ok": True, "record": written})


# ---------------------------------------------------------------------------
# POST /v1/experience/archive-goal
# ---------------------------------------------------------------------------

def archive_goal(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/experience/archive-goal  body: JSON
        {goal, skill_slug, category, summary, trace_file, ...optional extra}

    Mirrors experience.py cmd_archive_goal: validate trace file, dup-check,
    move trace to canonical content_path, assemble record, append, _update_meta.
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
    body, err = _parse_body(ctx)
    if err:
        return err

    goal_id = (body.get("goal") or "").strip()
    skill_slug = (body.get("skill_slug") or "").strip()
    category = body.get("category") or ""
    summary = body.get("summary") or ""
    trace_file = body.get("trace_file") or ""
    if not (goal_id and skill_slug and trace_file):
        return Response.error(
            400, "missing_param",
            "body requires 'goal', 'skill_slug', and 'trace_file'")

    extra = {k: v for k, v in body.items()
             if k not in ("goal", "skill_slug", "category", "summary", "trace_file")}

    warnings: List[str] = []

    trace_src = Path(trace_file)
    if not trace_src.is_absolute():
        trace_src = ctx.paths.project_root / trace_src
    if not trace_src.exists():
        return Response.error(400, "trace_missing",
                              f"trace-file does not exist: {trace_file}")
    trace_bytes = trace_src.stat().st_size
    if trace_bytes == 0:
        return Response.error(400, "trace_empty",
                              f"trace-file is empty: {trace_file}")
    if trace_bytes < MIN_TRACE_BYTES:
        warnings.append(f"trace-file is only {trace_bytes} bytes (<{MIN_TRACE_BYTES})")
    if len(summary.strip()) < MIN_SUMMARY_CHARS:
        warnings.append(f"summary is only {len(summary.strip())} chars (<{MIN_SUMMARY_CHARS})")

    base_experience_id = f"exp-{goal_id}-{skill_slug}"
    if not ID_RE.match(base_experience_id):
        return Response.error(400, "invalid_id",
                              f"computed experience id fails validation: {base_experience_id}")

    # : uniquify BEFORE deriving the canonical .md path so a
    # recurring re-run gets a fresh id (dated suffix) instead of a 409 that
    # orphans the trace. Suffix chars ([0-9-]) preserve ID_RE validity.
    items = _read_jsonl(_live_path(ctx))
    archive = _read_jsonl(_archive_path(ctx))
    try:
        experience_id = _uniquify_id(base_experience_id, items, archive)
    except ValueError as e:
        return Response.error(409, "duplicate_id", str(e))

    content_dir = ctx.paths.agent / "experience"
    assert_not_cruft(content_dir, "mkdir (experience content_dir)")
    content_dir.mkdir(parents=True, exist_ok=True)
    canonical = content_dir / f"{experience_id}.md"
    try:
        content_path_rel = str(canonical.relative_to(ctx.paths.project_root)).replace("\\", "/")
    except ValueError:
        content_path_rel = str(canonical).replace("\\", "/")

    # COPY trace to canonical (must precede _validate_record's content_path
    # check — the validator requires the file to exist at content_path, so the
    # ordering itself cannot change without splitting the validator).
    # : copy-not-move. The old os.replace consumed the caller's
    # trace on EVERY post-move failure (validation_failed, in-lock 409s,
    # write_failed), stranding an orphan .md and making retries
    # non-idempotent (observed 5-attempt sequence,  close). The
    # source is deleted only after the record lands; _abort removes the copy
    # on every post-copy failure path.
    copied_from: Optional[Path] = None
    if trace_src.resolve() != canonical.resolve():
        if canonical.exists():
            return Response.error(
                409, "content_path_exists",
                f"canonical content_path already exists: {canonical}")
        shutil.copy2(str(trace_src), str(canonical))
        copied_from = trace_src

    def _abort(resp):
        # Failed attempt must leave trace_src intact and no orphan .md.
        # `canonical` is read at call time — after the in-lock race rename
        # it is rebound to new_canonical, so the right copy is removed.
        if copied_from is not None:
            try:
                canonical.unlink()
            except OSError:
                pass
        return resp

    # Assemble record — key order MUST match cmd_archive_goal (experience.py:533-545).
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
    for k in ("retrieval_audit", "enabled_by", "temporal_credit"):
        if k in extra:
            rec[k] = extra[k]

    rec = _normalize_record(rec)
    try:
        _validate_record(ctx, rec)
    except ValueError as e:
        return _abort(Response.error(400, "validation_failed", str(e)))

    try:
        with file_locks.locked(_live_path(ctx)):
            cur = _read_jsonl(_live_path(ctx))
            cur_archive = _read_jsonl(_archive_path(ctx))
            cur_ids = {r.get("id") for r in cur}
            cur_ids.update(r.get("id") for r in cur_archive)
            if rec["id"] in cur_ids:
                # Race: another writer took the id between the unlocked
                # uniquify and this lock. Re-uniquify against the fresh sets
                # and rename the already-moved canonical .md to match.
                try:
                    new_id = _uniquify_id(base_experience_id, cur, cur_archive)
                except ValueError as e:
                    return _abort(Response.error(409, "duplicate_id", str(e)))
                new_canonical = content_dir / f"{new_id}.md"
                if new_canonical.exists():
                    return _abort(Response.error(
                        409, "content_path_exists",
                        f"canonical content_path already exists: {new_canonical}"))
                os.replace(str(canonical), str(new_canonical))
                canonical = new_canonical
                try:
                    rec["content_path"] = str(
                        new_canonical.relative_to(ctx.paths.project_root)).replace("\\", "/")
                except ValueError:
                    rec["content_path"] = str(new_canonical).replace("\\", "/")
                rec["id"] = new_id
            _append_record(ctx, rec, summary=f"experience-archive-goal {rec['id']}")
    except OSError as e:
        return _abort(Response.error(500, "write_failed", str(e)))

    # Record landed — consume the source now (completes the move semantics).
    if copied_from is not None:
        try:
            copied_from.unlink()
        except OSError:
            warnings.append(f"trace source cleanup failed: {copied_from}")

    _update_meta(ctx)
    resp = {"ok": True, "record": rec}
    if warnings:
        resp["warnings"] = warnings
    return Response.json(resp)


# ---------------------------------------------------------------------------
# POST /v1/experience/meta-update
# ---------------------------------------------------------------------------

def meta_update(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/experience/meta-update?field=&value=

    Mirrors experience.py cmd_meta_update: set ONE flat top-level field on
    experience-meta.json (NOT a recompute — preserves existing counts), stamp
    last_updated, write via the raw write_json path (no history/changelog,
    indent=2, ensure_ascii=True, trailing newline). Dotted fields rejected
    (the CLI's symmetric Option-A reject, g-115-566). The read-modify-write is
    locked (daemon is multi-threaded) — does not change bytes."""
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

    field = (ctx.query.get("field") or "").strip()
    if not field:
        return Response.error(400, "missing_param", "query parameter 'field' required")
    value_str = ctx.query.get("value")
    if value_str is None:
        return Response.error(400, "missing_param", "query parameter 'value' required")
    if "." in field:
        return Response.error(
            400, "dotted_field_rejected",
            f"dotted field name '{field}' is not supported by experience "
            f"meta-update; flat top-level keys only.")
    value = _parse_value(value_str)

    p = _meta_path(ctx)
    get_backend().ensure_local(p)  # own-cloud read-path fix: materialize S3-only file before RMW lock; no-op on LocalBackend and out-of-root paths
    data: dict = {}
    try:
        with file_locks.locked(p):
            if p.exists():
                with p.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = _empty_meta()
            data[field] = value
            data["last_updated"] = date.today().isoformat()
            assert_not_cruft(p.parent, "mkdir (experience meta-update)")
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(p) + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=True)
                f.write("\n")
            os.replace(str(tmp), str(p))
    except OSError as e:
        return Response.error(500, "write_failed", str(e))
    return Response.json({"ok": True, "meta": data})


# ---------------------------------------------------------------------------
# POST /v1/experience/archive-sweep
# ---------------------------------------------------------------------------

def archive_sweep(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/experience/archive-sweep

    Mirrors experience.py cmd_archive_sweep: snapshot LIVE (unlocked) and pick
    stale candidates (already-archived; OR unused-after-30d w/ retrieval_count
    0; OR low-utility-after-90d w/ utility<0.2; protected if retrieval>=5 AND
    utility>=0.5). Phase 1 appends candidates to ARCHIVE under its lock (in-lock
    dedup vs existing ids); phase 2 rewrites LIVE filtering archived_ids against
    a FRESH in-lock read (concurrent appends preserved); then _update_meta.
    Both file writes carry history+changelog (base=agent, post-G1
    resolve_base_dir → AGENT_DIR), matching the CLI's locked_modify_jsonl."""
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

    items = _read_jsonl(_live_path(ctx))
    today = date.today()
    to_archive: List[dict] = []
    for rec in items:
        if rec.get("archived", False):
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
        if (retrieval_count >= PROTECT_MIN_RETRIEVAL_COUNT
                and utility_ratio >= PROTECT_MIN_UTILITY_RATIO):
            continue
        if age_days >= ARCHIVE_UNUSED_AFTER_DAYS and retrieval_count == 0:
            rec["archived"] = True
            rec["archived_date"] = today.isoformat()
            to_archive.append(rec)
            continue
        if age_days >= ARCHIVE_LOW_UTILITY_AFTER_DAYS and utility_ratio < 0.2:
            rec["archived"] = True
            rec["archived_date"] = today.isoformat()
            to_archive.append(rec)
            continue

    if not to_archive:
        return Response.json({"ok": True, "archived": 0})

    archived_ids = {r["id"] for r in to_archive}
    base = ctx.paths.agent
    agent = _agent_name(ctx)
    archive_p = _archive_path(ctx)
    live_p = _live_path(ctx)
    try:
        # Phase 1: append candidates to ARCHIVE under archive's lock.
        with file_locks.locked(archive_p):
            arch_items = _read_jsonl(archive_p)
            existing = {r.get("id") for r in arch_items}
            for r in to_archive:
                if r["id"] not in existing:
                    arch_items.append(r)
            for it in arch_items:
                _validate_no_surrogates(it, archive_p)
            history.snapshot(archive_p, base, agent,
                             summary="experience-archive-sweep")
            _atomic_write_jsonl(archive_p, arch_items)
            changelog.append(base, agent, archive_p, "edit",
                             summary="experience-archive-sweep",
                             lines_changed=len(arch_items))
            _jsonl_cache().invalidate(archive_p)
        # Phase 2: rewrite LIVE filtering archived_ids, fresh in-lock read.
        with file_locks.locked(live_p):
            live_items = _read_jsonl(live_p)
            remaining = [r for r in live_items
                         if r.get("id") not in archived_ids]
            for it in remaining:
                _validate_no_surrogates(it, live_p)
            history.snapshot(live_p, base, agent,
                             summary="experience-archive-sweep")
            _atomic_write_jsonl(live_p, remaining)
            changelog.append(base, agent, live_p, "edit",
                             summary="experience-archive-sweep",
                             lines_changed=len(remaining))
            _jsonl_cache().invalidate(live_p)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    _update_meta(ctx)
    return Response.json({"ok": True, "archived": len(to_archive)})


# ---------------------------------------------------------------------------
# POST /v1/experience/recompute-index
# ---------------------------------------------------------------------------

def recompute_index(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/experience/recompute-index

    Mirrors experience.py cmd_recompute_index: read the COLLECTIVE pipeline
    (world live+archive), aggregate CONFIRMED/CORRECTED resolved hypotheses into
    experiential-index.yaml (AGENT-scoped). The YAML is HAND-FORMATTED with the
    EXACT string layout of the CLI (no PyYAML) so the bytes match; written raw
    (tmp+os.replace, NO history/changelog), like the CLI."""
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

    all_pipeline = (_read_jsonl(_pipeline_live_path(ctx))
                    + _read_jsonl(_pipeline_archive_path(ctx)))
    resolved = [r for r in all_pipeline
                if r.get("outcome") in ("CONFIRMED", "CORRECTED")]

    total_resolved = len(resolved)
    total_correct = sum(1 for r in resolved if r.get("outcome") == "CONFIRMED")
    total_incorrect = sum(1 for r in resolved if r.get("outcome") == "CORRECTED")
    accuracy_pct = round(total_correct / max(total_resolved, 1) * 100, 1)

    by_category: Dict[str, Dict[str, Any]] = {}
    for r in resolved:
        cat = r.get("category", "unknown")
        if cat not in by_category:
            by_category[cat] = {"total": 0, "confirmed": 0, "corrected": 0}
        by_category[cat]["total"] += 1
        if r.get("outcome") == "CONFIRMED":
            by_category[cat]["confirmed"] += 1
        else:
            by_category[cat]["corrected"] += 1
    for cat, stats in by_category.items():
        stats["accuracy"] = round(stats["confirmed"] / max(stats["total"], 1) * 100, 1)

    by_violation_cause: Dict[str, int] = {}
    for r in resolved:
        if r.get("outcome") == "CORRECTED":
            cat = r.get("category", "unknown")
            by_violation_cause[cat] = by_violation_cause.get(cat, 0) + 1

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

    # EXACT string layout of cmd_recompute_index (experience.py:864-887).
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

    p = _index_path(ctx)
    try:
        assert_not_cruft(p.parent, "mkdir (experiential-index)")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(p) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(yaml_content)
        os.replace(str(tmp), str(p))
    except OSError as e:
        return Response.error(500, "write_failed", str(e))
    return Response.json({"ok": True, "index": index_data})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/experience/add")] = add
    routes[("POST", "/v1/experience/update-field")] = update_field
    routes[("POST", "/v1/experience/archive-goal")] = archive_goal
    routes[("POST", "/v1/experience/meta-update")] = meta_update
    routes[("POST", "/v1/experience/archive-sweep")] = archive_sweep
    routes[("POST", "/v1/experience/recompute-index")] = recompute_index
