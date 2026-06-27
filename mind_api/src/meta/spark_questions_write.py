"""POST /v1/spark-questions/{increment,promote}.

Daemonises the two BESPOKE spark-question write commands from
core/scripts/spark-questions.py:

  - cmd_increment: atomic counter bump (times_asked / sparks_generated) with
    yield_rate recompute. The generic store/increment handler only supports
    nested-prefix counters; spark-questions needs the flat-field + derived
    yield_rate recompute, so it stays bespoke (spark-questions.py:183-218).
  - cmd_promote: candidate -> active-question reformat + id change
    (spark-questions.py:224-274).

The CRUD subcommands (add / update-field / retire) were already migrated to
the generic store endpoint (POST /v1/store/{append,set-field}) in H2 Wave 3,
so only these two bespoke operations remained on the Python CLI.

BYTE-COMPATIBILITY with the CLI write path is a hard requirement so a
spark-questions.jsonl alternately written by the CLI and the daemon does not
churn .history / git. Both CLI commands route through
_fileops.locked_modify_jsonl, which:
  - reads the records list under the file lock,
  - applies the modifier,
  - validates no surrogates,
  - save_history(path, base_dir, agent),
  - rewrites the whole file via _atomic_write_with_fallback with
    `json.dumps(item, ensure_ascii=True) + "\n"`,
  - append_changelog(base_dir, agent, path, "edit", lines_changed=len).

`_locked_modify` below replicates that cycle exactly EXCEPT agent attribution
comes from the X-Mind-Agent header (the daemon serves all agents from one
process; _fileops._agent_name's os.environ read would mis-attribute every
write). The rewritten records are byte-identical because the serialization,
key order, and modify logic are lifted verbatim. spark-questions.jsonl lives
under META_DIR, so base_dir = ctx.paths.meta (resolve_base_dir would return
META_DIR for the same path).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import file_locks, history, changelog
from ..jsonl_cache import cache as _jsonl_cache
from ..agent_paths import assert_not_cruft

from _fileops import _atomic_write_with_fallback, _validate_no_surrogates  # noqa: E402
from storage_backend import get_backend  # noqa: E402  # s5c: own-cloud read freshness

# Lifted verbatim from core/scripts/spark-questions.py.
QUESTION_ID_RE = re.compile(r"^sq-\d+$")
_INCREMENTABLE_FIELDS = ("times_asked", "sparks_generated")


# ---------------------------------------------------------------------------
# Small daemon-local helpers (mirror aspirations_write / board_write)
# ---------------------------------------------------------------------------

def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _path(ctx) -> Path:
    return ctx.paths.meta / "spark-questions.jsonl"


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
    """Full JSONL rewrite — byte-identical to _fileops.locked_modify_jsonl's
    inner write (`json.dumps(item, ensure_ascii=True) + "\\n"`)."""
    assert_not_cruft(path.parent, "mkdir (spark_questions_write)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=True) + "\n")

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_spark_questions_write")


def _find_record_by_id(items: List[Dict[str, Any]], rec_id: str):
    for i, rec in enumerate(items):
        if rec.get("id") == rec_id:
            return (i, rec)
    return None


def _locked_modify(ctx, modifier: Callable[[List[Dict[str, Any]]], None],
                   summary: str) -> "Response":  # type: ignore[name-defined]
    """Replicate _fileops.locked_modify_jsonl with header-agent attribution.

    `modifier(items)` mutates the records list in place. It may raise
    ValueError to abort with a 400 (record not found / wrong type / etc.).
    Returns a Response: 400 on modifier ValueError, 500 on write failure,
    else the caller builds the success Response from the mutated record.
    """
    from ..server import Response

    live_path = _path(ctx)
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)

    try:
        assert_not_cruft(live_path.parent, "mkdir (spark_questions_write)")
        live_path.parent.mkdir(parents=True, exist_ok=True)
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            try:
                modifier(items)
            except ValueError as e:
                return Response.error(400, "modify_failed", str(e))
            for item in items:
                _validate_no_surrogates(item, live_path)
            history.snapshot(live_path, base_dir, agent, summary=summary)
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=summary, lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))
    return None  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# POST /v1/spark-questions/increment
# ---------------------------------------------------------------------------

def increment(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/spark-questions/increment?rec_id=<id>&field=<times_asked|sparks_generated>

    Mirrors spark-questions.py cmd_increment: bump the counter on a question
    record and recompute yield_rate = round(sparks / max(asked, 1), 4).
    """
    from ..server import Response

    rec_id = (ctx.query.get("rec_id") or "").strip()
    if not rec_id:
        return Response.error(400, "missing_param", "query parameter 'rec_id' required")
    field = (ctx.query.get("field") or "").strip()
    if field not in _INCREMENTABLE_FIELDS:
        return Response.error(
            400, "invalid_field",
            f"Can only increment 'times_asked' or 'sparks_generated', got: {field}")

    written: Dict[str, Any] = {}

    def _modifier(items: List[Dict[str, Any]]) -> None:
        result = _find_record_by_id(items, rec_id)
        if result is None:
            raise ValueError(f"Record {rec_id} not found")
        idx, rec = result
        if rec.get("type") != "question":
            raise ValueError(
                f"Record {rec_id} is not a question (type={rec.get('type')})")
        rec[field] = rec.get(field, 0) + 1
        rec["yield_rate"] = round(
            rec.get("sparks_generated", 0) / max(rec.get("times_asked", 0), 1), 4)
        items[idx] = rec
        written["rec"] = rec

    err = _locked_modify(ctx, _modifier, f"spark-increment {rec_id} {field}")
    if err is not None:
        return err
    return Response.json({"ok": True, "record": written["rec"]})


# ---------------------------------------------------------------------------
# POST /v1/spark-questions/promote
# ---------------------------------------------------------------------------

def promote(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/spark-questions/promote?candidate_id=<id>&new_id=<sq-NNN>

    Mirrors spark-questions.py cmd_promote: convert a candidate to an active
    question with a new id and reset counters. The framework-YAML sync is a
    read-only check returned as a `note` (the CLI prints the same to stderr).
    """
    from ..server import Response

    candidate_id = (ctx.query.get("candidate_id") or "").strip()
    if not candidate_id:
        return Response.error(400, "missing_param",
                              "query parameter 'candidate_id' required")
    new_id = (ctx.query.get("new_id") or "").strip()
    if not QUESTION_ID_RE.match(new_id):
        return Response.error(400, "invalid_new_id",
                              f"Invalid new ID format: {new_id} (expected sq-NNN)")

    written: Dict[str, Any] = {}

    def _modifier(items: List[Dict[str, Any]]) -> None:
        result = _find_record_by_id(items, candidate_id)
        if result is None:
            raise ValueError(f"Candidate {candidate_id} not found")
        idx, rec = result
        if rec.get("type") != "candidate":
            raise ValueError(
                f"Record {candidate_id} is not a candidate (type={rec.get('type')})")
        # Duplicate-id check inside the lock (was racy outside).
        for item in items:
            if item.get("id") == new_id:
                raise ValueError(f"Duplicate record ID: {new_id}")
        rec["id"] = new_id
        rec["type"] = "question"
        rec["status"] = "active"
        rec["times_asked"] = 0
        rec["sparks_generated"] = 0
        rec["yield_rate"] = 0.0
        rec.pop("proposed_session", None)
        items[idx] = rec
        written["rec"] = rec

    err = _locked_modify(ctx, _modifier,
                         f"spark-promote {candidate_id} -> {new_id}")
    if err is not None:
        return err

    note = _framework_sync_note(ctx, candidate_id, new_id)
    body: Dict[str, Any] = {"ok": True, "record": written["rec"]}
    if note:
        body["note"] = note
    return Response.json(body)


def _framework_sync_note(ctx, candidate_id: str, new_id: str) -> Optional[str]:
    """Read-only check mirroring spark-questions.py _sync_framework_promotion.

    Returns a human-readable note when core/config/spark-questions.yaml still
    references the candidate id (meaning the framework seed needs updating);
    otherwise None. NEVER modifies the YAML — runtime state is authoritative.
    """
    yaml_path = ctx.paths.project_root / "core" / "config" / "spark-questions.yaml"
    try:
        if not yaml_path.exists():
            return None
        content = yaml_path.read_text(encoding="utf-8")
        if f"id: {candidate_id}" not in content:
            return None
        return (f"Framework YAML sync needed for {candidate_id} -> {new_id}. "
                f"Update core/config/spark-questions.yaml: move {candidate_id} "
                f"to seed_questions as {new_id} and update initial_state.")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/spark-questions/increment")] = increment
    routes[("POST", "/v1/spark-questions/promote")] = promote
