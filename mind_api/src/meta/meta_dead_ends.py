"""Dead-end registry endpoints — parity with core/scripts/meta-dead-ends.py.

Daemonises the meta-strategy dead-end registry (Batch 6). The registry tracks
meta-strategy approaches proven to fail so the agent does not retry known-bad
parameter configurations. Five subcommands map to five routes:

  POST /v1/meta/dead-ends/add        (cmd_add)        body: JSON record
  GET  /v1/meta/dead-ends/check      (cmd_check)      ?file=&field=&value=
  GET  /v1/meta/dead-ends/read       (cmd_read)       ?active=&category=
  POST /v1/meta/dead-ends/increment  (cmd_increment)  ?id=
  POST /v1/meta/dead-ends/review     (cmd_review)     ?id=

SCOPE: META. The CLI's DE_PATH = META_DIR/dead-ends.jsonl (meta-dead-ends.py:29),
so base_dir = ctx.paths.meta. resolve_base_dir(DE_PATH) returns META_DIR (non-None)
for the CLI, so save_history + append_changelog DO fire on the write path — the
daemon mirrors that with history.snapshot + changelog.append on ctx.paths.meta.
No X-Mind-Agent header is REQUIRED (meta files are agent-agnostic); attribution
still uses the header when present, defaulting to "system" exactly as the CLI's
_fileops._agent_name does (os.environ.get("MIND_AGENT", "system")).

BYTE-COMPATIBILITY — both write paths route through the GENERIC _fileops locked
family, which pass NO summary:
  - cmd_add        -> locked_modify_jsonl  (read-modify-write the whole file)
  - cmd_increment  -> read_all (unlocked) + write_all = locked_write_jsonl
  - cmd_review     -> read_all (unlocked) + write_all = locked_write_jsonl
Both locked_write_jsonl (_fileops.py:1340/1349) and locked_modify_jsonl
(_fileops.py:1495/1504) call:
    save_history(path, base_dir, agent)                          # summary=""
    _atomic_write_with_fallback(... json.dumps(item, ensure_ascii=True)+"\n")
    append_changelog(base_dir, agent, path, "edit", lines_changed=len(items))  # summary=""
So this module passes NO summary to history.snapshot / changelog.append — unlike
bespoke endpoints (aspirations_write etc.) whose specific CLI commands DO pass a
summary. The changelog entry serialises "summary": summary into its bytes
(_fileops.append_changelog:569), so a non-empty summary here would churn
changelog.jsonl. Verified empirically by test_runtime_meta_dead_ends, which
diffs dead-ends.jsonl + changelog.jsonl + .history against the real CLI.

STDOUT byte-compat (the wrapper cut prints the response body verbatim):
  - check : json.dumps(result, ensure_ascii=False, default=str) + "\n"   (line 189)
  - read  : json.dumps(records, ensure_ascii=False, default=str) + "\n"  (line 201)
  - add   : json.dumps(outcome) + "\n"               (ensure_ascii=True default, no indent; line 145)
  - increment/review : json.dumps({...}) + "\n"      (ensure_ascii=True default; lines 215/219/233/238)

sys.exit MAPPING (meta-dead-ends.py): only cmd_add has reachable command-level
sys.exit(1) — invalid category (line 94) and missing required field (line 100);
both are bad-input -> HTTP 400. The stdin-tty exit (line 80) has no daemon
analogue (empty body -> 400 via _parse_body). cmd_increment / cmd_review use
print()+return for not-found (exit 0, stdout = {"error": ...}); the daemon
mirrors that with HTTP 200 + the same error body (NOT 404), so the wrapper exits
0 like the CLI.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import file_locks, history, changelog
from ..jsonl_cache import cache as _jsonl_cache
from ..agent_paths import assert_not_cruft

from _fileops import _atomic_write_with_fallback, _validate_no_surrogates  # noqa: E402

# Lifted verbatim from core/scripts/meta-dead-ends.py:30.
VALID_CATEGORIES = {
    "meta_weight", "meta_heuristic", "meta_experiment",
    "encoding_rule", "domain_approach",
}


# ---------------------------------------------------------------------------
# Daemon-local helpers (mirror spark_questions_write)
# ---------------------------------------------------------------------------

def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-ayoai-agent") or "").strip() or "system"


def _path(ctx) -> Path:
    return ctx.paths.meta / "dead-ends.jsonl"


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Mirror meta-dead-ends.py:read_all (line 33) — strip + json.loads each
    non-empty line, no lock."""
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
    """Full JSONL rewrite — byte-identical to the _fileops locked family's inner
    write (`json.dumps(item, ensure_ascii=True) + "\\n"`)."""
    assert_not_cruft(path.parent, "mkdir (meta_dead_ends)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=True) + "\n")

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_meta_dead_ends_write")


def _next_id(records: List[Dict[str, Any]]) -> str:
    """Mirror meta-dead-ends.py:next_id (line 52)."""
    max_num = 0
    for rec in records:
        rid = rec.get("id", "")
        if rid.startswith("de-"):
            try:
                max_num = max(max_num, int(rid[3:]))
            except ValueError:
                pass
    return f"de-{max_num + 1:03d}"


def _persist(ctx, items: List[Dict[str, Any]]) -> Optional["Response"]:  # type: ignore[name-defined]
    """Locked snapshot -> atomic JSONL rewrite -> changelog -> cache invalidate.

    Replicates _fileops.locked_write_jsonl (the inner of write_all / the tail of
    locked_modify_jsonl) with header-agent attribution and NO summary. Returns a
    Response on OSError, else None.
    """
    from ..server import Response

    live_path = _path(ctx)
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)
    try:
        assert_not_cruft(live_path.parent, "mkdir (meta_dead_ends)")
        live_path.parent.mkdir(parents=True, exist_ok=True)
        with file_locks.locked(live_path):
            for item in items:
                _validate_no_surrogates(item, live_path)
            history.snapshot(live_path, base_dir, agent)
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))
    return None


# ---------------------------------------------------------------------------
# POST /v1/meta/dead-ends/add
# ---------------------------------------------------------------------------

def add(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/meta/dead-ends/add  body: JSON dead-end record.

    Mirrors meta-dead-ends.py cmd_add: defaults -> validate -> locked
    read-modify-write (allocate id, merge overlapping range or append).
    """
    from ..server import Response

    if not ctx.body:
        return Response.error(400, "invalid_body", "expected JSON on body")
    try:
        item = json.loads(ctx.body.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as e:
        return Response.error(400, "invalid_body", f"body must be JSON: {e}")
    if not isinstance(item, dict):
        return Response.error(400, "invalid_body", "body must be a JSON object")

    # Defaults (meta-dead-ends.py:86-89).
    item.setdefault("registered", datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    item.setdefault("times_matched", 0)
    item.setdefault("status", "active")
    item.setdefault("category", "meta_weight")

    # Validate category (pre-lock, fail-fast — meta-dead-ends.py:92).
    if item.get("category") not in VALID_CATEGORIES:
        return Response.error(
            400, "invalid_category",
            f"Invalid category '{item.get('category')}'. Valid: {VALID_CATEGORIES}")

    # Required fields (meta-dead-ends.py:97).
    for field in ("strategy_file", "field", "failure_pattern"):
        if field not in item:
            return Response.error(
                400, "missing_field", f"Missing required field '{field}'")

    outcome: Dict[str, Any] = {"status": None, "id": None}
    live_path = _path(ctx)
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)

    try:
        assert_not_cruft(live_path.parent, "mkdir (meta_dead_ends)")
        live_path.parent.mkdir(parents=True, exist_ok=True)
        with file_locks.locked(live_path):
            records = _read_jsonl(live_path)

            # Allocate id inside the lock (meta-dead-ends.py:111).
            if "id" not in item:
                item["id"] = _next_id(records)

            # Overlapping-range merge (meta-dead-ends.py:116-136).
            merged = False
            for existing in records:
                if (existing.get("strategy_file") == item.get("strategy_file") and
                        existing.get("field") == item.get("field") and
                        existing.get("status") in ("active", "reviewed")):
                    existing_range = existing.get("value_range")
                    new_range = item.get("value_range")
                    if existing_range and new_range:
                        if (new_range[0] <= existing_range[1] and
                                new_range[1] >= existing_range[0]):
                            existing["value_range"] = [
                                min(existing_range[0], new_range[0]),
                                max(existing_range[1], new_range[1]),
                            ]
                            existing["evidence"] = list(set(
                                existing.get("evidence", []) +
                                item.get("evidence", [])))
                            existing["failure_pattern"] = item.get(
                                "failure_pattern", existing["failure_pattern"])
                            outcome["status"] = "merged"
                            outcome["id"] = existing["id"]
                            merged = True
                            break

            if not merged:
                records.append(item)
                outcome["status"] = "added"
                outcome["id"] = item["id"]

            for rec in records:
                _validate_no_surrogates(rec, live_path)
            history.snapshot(live_path, base_dir, agent)
            _atomic_write_jsonl(live_path, records)
            changelog.append(base_dir, agent, live_path, "edit",
                             lines_changed=len(records))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.text(json.dumps(outcome) + "\n",
                         content_type="application/json")


# ---------------------------------------------------------------------------
# GET /v1/meta/dead-ends/check
# ---------------------------------------------------------------------------

def check(ctx) -> "Response":  # type: ignore[name-defined]
    """GET /v1/meta/dead-ends/check?file=&field=&value=

    Mirrors meta-dead-ends.py cmd_check (line 148): scan active/reviewed records
    matching file+field; report range and pattern matches.
    """
    from ..server import Response

    file_arg = ctx.query.get("file")
    field_arg = ctx.query.get("field")
    value_arg = ctx.query.get("value")
    # argparse marks all three required (meta-dead-ends.py:252-254).
    for name, val in (("file", file_arg), ("field", field_arg), ("value", value_arg)):
        if val is None:
            return Response.error(400, "missing_param",
                                  f"query parameter '{name}' required")

    records = _read_jsonl(_path(ctx))
    matches: List[Dict[str, Any]] = []

    for rec in records:
        if rec.get("status") not in ("active", "reviewed"):
            continue
        if rec.get("strategy_file") != file_arg or rec.get("field") != field_arg:
            continue

        value_range = rec.get("value_range")
        value_pattern = rec.get("value_pattern")

        if value_range and value_arg is not None:
            try:
                val = float(value_arg)
                if value_range[0] <= val <= value_range[1]:
                    matches.append({
                        "id": rec["id"],
                        "failure_pattern": rec["failure_pattern"],
                        "value_range": value_range,
                        "times_matched": rec.get("times_matched", 0),
                    })
            except (ValueError, TypeError):
                pass

        if value_pattern and value_arg is not None:
            if value_pattern.lower() in str(value_arg).lower():
                matches.append({
                    "id": rec["id"],
                    "failure_pattern": rec["failure_pattern"],
                    "value_pattern": value_pattern,
                    "times_matched": rec.get("times_matched", 0),
                })

    result = {"blocked": len(matches) > 0, "matches": matches}
    return Response.text(
        json.dumps(result, ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# GET /v1/meta/dead-ends/read
# ---------------------------------------------------------------------------

def read(ctx) -> "Response":  # type: ignore[name-defined]
    """GET /v1/meta/dead-ends/read?active=&category=

    Mirrors meta-dead-ends.py cmd_read (line 192): optional active/category
    filters, emit the full record list.
    """
    from ..server import Response

    records = _read_jsonl(_path(ctx))

    # --active store_true (meta-dead-ends.py:257). Inlined truthiness gate
    # (avoid a meta->endpoints import for the 3-line endpoints._jsonl_common.flag).
    active = ctx.query.get("active")
    if active is not None and active.lower() not in ("", "0", "false", "no"):
        records = [r for r in records if r.get("status") == "active"]
    category = ctx.query.get("category")
    if category:
        records = [r for r in records if r.get("category") == category]

    return Response.text(
        json.dumps(records, ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/dead-ends/increment
# ---------------------------------------------------------------------------

def increment(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/meta/dead-ends/increment?id=<de-NNN>

    Mirrors meta-dead-ends.py cmd_increment (line 204): bump times_matched.
    Not-found -> HTTP 200 + {"error": ...} (CLI prints + returns, exit 0).
    """
    from ..server import Response

    rec_id = (ctx.query.get("id") or "").strip()
    if not rec_id:
        return Response.error(400, "missing_param", "query parameter 'id' required")

    records = _read_jsonl(_path(ctx))
    found = False
    for rec in records:
        if rec["id"] == rec_id:
            rec["times_matched"] = rec.get("times_matched", 0) + 1
            found = True
            break

    if not found:
        return Response.text(
            json.dumps({"error": f"Dead end {rec_id} not found"}) + "\n",
            content_type="application/json")

    err = _persist(ctx, records)
    if err is not None:
        return err
    return Response.text(
        json.dumps({"status": "incremented", "id": rec_id}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/dead-ends/review
# ---------------------------------------------------------------------------

def review(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/meta/dead-ends/review?id=<de-NNN>

    Mirrors meta-dead-ends.py cmd_review (line 222): mark status=reviewed +
    stamp reviewed_at. Not-found -> HTTP 200 + {"error": ...} (exit 0).
    """
    from ..server import Response

    rec_id = (ctx.query.get("id") or "").strip()
    if not rec_id:
        return Response.error(400, "missing_param", "query parameter 'id' required")

    records = _read_jsonl(_path(ctx))
    found = False
    for rec in records:
        if rec["id"] == rec_id:
            rec["status"] = "reviewed"
            rec["reviewed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            found = True
            break

    if not found:
        return Response.text(
            json.dumps({"error": f"Dead end {rec_id} not found"}) + "\n",
            content_type="application/json")

    err = _persist(ctx, records)
    if err is not None:
        return err
    return Response.text(
        json.dumps({"status": "reviewed", "id": rec_id}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/meta/dead-ends/add")] = add
    routes[("GET", "/v1/meta/dead-ends/check")] = check
    routes[("GET", "/v1/meta/dead-ends/read")] = read
    routes[("POST", "/v1/meta/dead-ends/increment")] = increment
    routes[("POST", "/v1/meta/dead-ends/review")] = review
