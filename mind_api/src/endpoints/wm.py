"""GET /v1/wm/read — pre-migration `wm.py read` parity.

Query parameters:
    slot=<name>        slot to read (e.g. 'active_context',
                       'encoding_queue', 'active_context.retrieval_manifest').
                       Omit for full dump.
    json=1             JSON output (default: YAML, matching CLI default)

Response content-type:
    application/json   when json=1 (or for the "null" miss reply, which is
                       always JSON-valid)
    text/x-yaml        for YAML dumps
    text/plain         for scalar slot values without json=1

Equivalence target: stdout of
    python3 core/scripts/wm.py read [<slot>] [--json]

Side-effect skipped on daemon path (Phase B scope-cut, see DECISIONS.md):
    wm.py's cmd_read updates `slot_meta[<slot>].accessed_at` after a
    non-top-level slot read. The daemon does NOT — that's a daemon-safe
    writer (PR 2 territory). Until then the fallback path still records.
"""
from __future__ import annotations

import json

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise RuntimeError("PyYAML required for wm endpoint") from e

from ..yaml_cache import cache


# Mirrors wm.py's TOP_LEVEL_KEYS at the time of writing. When wm.py's set
# grows/shrinks, mirror here. Cross-checked by mind_api/tests/test_runtime_wm.py.
_TOP_LEVEL_KEYS = {
    "encoding_queue", "session_id", "session_start",
    "goals_completed_this_session", "aspiration_touched_last",
    "last_goal_category",
}


def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    slot = q.get("slot") or None
    as_json = _flag(q, "json")  # default False — matches CLI's YAML default

    # Per-Body WM routing (Phase 1A, ) — route by request SID; falls back
    # to the agent-wide WM when there is no body-manifest (today's behavior). The
    # path cache is keyed by path, so per-Body files get distinct cache entries.
    sid = (ctx.headers.get("x-mind-sid") or "").strip()
    wm_path = ctx.paths.wm_path(sid or None)
    data = cache().get(wm_path)

    if data is None:
        # CLI prints "{}" for missing wm under --json, nothing otherwise.
        if as_json:
            return Response.text("{}", content_type="application/json")
        return Response.text("", content_type="text/plain")

    if slot is None:
        # Full dump
        if as_json:
            return Response.text(
                json.dumps(data, ensure_ascii=False, default=str),
                content_type="application/json",
            )
        body = yaml.dump(data, default_flow_style=False,
                         allow_unicode=True, sort_keys=False)
        return Response.text(body, content_type="text/x-yaml")

    parent, key, _is_top = _resolve_slot(data, slot)
    if parent is None or key not in parent:
        # CLI prints "null" via print() for both --json and YAML modes.
        return Response.text("null\n", content_type="application/json"
                             if as_json else "text/plain")

    value = parent[key]

    if as_json:
        return Response.text(
            json.dumps(value, ensure_ascii=False, default=str) + "\n",
            content_type="application/json",
        )
    if isinstance(value, (dict, list)):
        body = yaml.dump(value, default_flow_style=False,
                         allow_unicode=True, sort_keys=False)
        return Response.text(body, content_type="text/x-yaml")
    # Scalar non-JSON: CLI calls print() so we mirror its newline.
    body = (str(value) if value is not None else "null") + "\n"
    return Response.text(body, content_type="text/plain")


def _resolve_slot(data, slot_path):
    """Mirrors wm.py:resolve_slot — top-level keys vs slots-mapped keys."""
    parts = slot_path.split(".")
    root_key = parts[0]
    if root_key in _TOP_LEVEL_KEYS:
        current = data
        for part in parts[:-1]:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None, None, True
        return current, parts[-1], True
    slots = data.get("slots", {})
    if len(parts) == 1:
        return slots, root_key, False
    current = slots
    for part in parts[:-1]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None, None, False
    return current, parts[-1], False


def _flag(q, name: str) -> bool:
    v = q.get(name)
    if v is None:
        return False
    return v.lower() not in ("", "0", "false", "no")


def register(routes) -> None:
    routes[("GET", "/v1/wm/read")] = read
