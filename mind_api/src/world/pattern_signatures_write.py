"""POST /v1/pattern-signatures/record-outcome.

Daemonises the one BESPOKE pattern-signature write command from
core/scripts/pattern-signatures.py: cmd_record_outcome
(pattern-signatures.py:202-235). It bumps outcome_stats.total (and .confirmed
on CONFIRMED), recomputes accuracy, and stamps last_matched with a DATE-ONLY
value (date.today().isoformat(), e.g. "2026-05-29" — NOT a full timestamp).

The CRUD subcommands (add / update / update-field / set-status) were already
migrated to the generic store endpoint (POST /v1/store/{append,replace,
set-field}) in H2 Wave 3, so only this counter+recompute operation remained
on the Python CLI.

BYTE-COMPATIBILITY: cmd_record_outcome routes through
_fileops.locked_modify_jsonl (read under lock -> modify -> validate -> history
-> atomic rewrite with `json.dumps(item, ensure_ascii=True) + "\\n"` ->
changelog). `_locked_modify` below replicates that cycle exactly EXCEPT agent
attribution comes from the X-Mind-Agent header. pattern-signatures.jsonl
lives under WORLD_DIR, so base_dir = ctx.paths.world (resolve_base_dir returns
WORLD_DIR for the same path).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List

from .. import file_locks, history, changelog
from ..jsonl_cache import cache as _jsonl_cache
from ..agent_paths import assert_not_cruft

from _fileops import _atomic_write_with_fallback, _validate_no_surrogates  # noqa: E402
from storage_backend import get_backend  # noqa: E402  # s5c: own-cloud read freshness

_VALID_OUTCOMES = ("CONFIRMED", "CORRECTED")


# ---------------------------------------------------------------------------
# Small daemon-local helpers (mirror spark_questions_write / board_write)
# ---------------------------------------------------------------------------

def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-ayoai-agent") or "").strip() or "system"


def _path(ctx) -> Path:
    return ctx.paths.world / "pattern-signatures.jsonl"


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
    assert_not_cruft(path.parent, "mkdir (pattern_signatures_write)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=True) + "\n")

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_pattern_signatures_write")


def _find_record_by_id(items: List[Dict[str, Any]], rec_id: str):
    for i, rec in enumerate(items):
        if rec.get("id") == rec_id:
            return (i, rec)
    return None


def _locked_modify(ctx, modifier: Callable[[List[Dict[str, Any]]], None],
                   summary: str) -> "Response":  # type: ignore[name-defined]
    """Replicate _fileops.locked_modify_jsonl with header-agent attribution.

    Returns a 400/500 Response on failure, or None on success (caller builds
    the success Response).
    """
    from ..server import Response

    live_path = _path(ctx)
    base_dir = ctx.paths.world
    agent = _agent_name(ctx)

    try:
        assert_not_cruft(live_path.parent, "mkdir (pattern_signatures_write)")
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
# POST /v1/pattern-signatures/record-outcome
# ---------------------------------------------------------------------------

def record_outcome(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/pattern-signatures/record-outcome?rec_id=<sig-id>&outcome=<CONFIRMED|CORRECTED>

    Mirrors pattern-signatures.py cmd_record_outcome.
    """
    from ..server import Response

    rec_id = (ctx.query.get("rec_id") or "").strip()
    if not rec_id:
        return Response.error(400, "missing_param", "query parameter 'rec_id' required")
    outcome = (ctx.query.get("outcome") or "").strip()
    if outcome not in _VALID_OUTCOMES:
        return Response.error(
            400, "invalid_outcome",
            f"Invalid outcome: {outcome} (must be CONFIRMED or CORRECTED)")

    written: Dict[str, Any] = {}

    def _modifier(items: List[Dict[str, Any]]) -> None:
        result = _find_record_by_id(items, rec_id)
        if result is None:
            raise ValueError(f"Record {rec_id} not found")
        idx, rec = result
        stats = rec.get("outcome_stats",
                        {"total": 0, "confirmed": 0, "accuracy": 0.0})
        stats["total"] = stats.get("total", 0) + 1
        if outcome == "CONFIRMED":
            stats["confirmed"] = stats.get("confirmed", 0) + 1
        stats["accuracy"] = (round(stats["confirmed"] / stats["total"], 4)
                             if stats["total"] > 0 else 0.0)
        rec["outcome_stats"] = stats
        rec["last_matched"] = date.today().isoformat()
        items[idx] = rec
        written["rec"] = rec

    err = _locked_modify(ctx, _modifier, f"patsig-record-outcome {rec_id} {outcome}")
    if err is not None:
        return err
    return Response.json({"ok": True, "record": written["rec"]})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/pattern-signatures/record-outcome")] = record_outcome
