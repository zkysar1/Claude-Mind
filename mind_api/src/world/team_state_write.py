"""POST /v1/team-state/{update,in-flight,clear-in-flight,init} — team-state writes.

Daemonises core/scripts/team-state.py cmd_update / cmd_in_flight /
cmd_clear_in_flight / cmd_init. The read path (GET /v1/team-state/read) lives
in world/team_state.py.

BYTE-COMPATIBILITY is GUARANTEED BY CONSTRUCTION: every write goes through
`_fileops.locked_modify_yaml` (and `locked_write_yaml` for init) — the EXACT
same function team-state.py calls. There is no second YAML serializer to drift
from. This mirrors the established daemon precedent
aspirations_write._team_state_append_completion (saves ~400ms/call vs shelling
out to team-state.py). The command logic (set/append/remove dotted-path,
in_flight stamping, ring-buffer) is replicated verbatim as modifier closures.

ATTRIBUTION: the team-state DATA's last_updated_by is set to the requesting
agent (X-Mind-Agent header) inside the modifier. The changelog.jsonl audit
line's `agent` field is env-derived (locked_modify_yaml calls _fileops._agent_name
internally) — a known, accepted limitation shared with
_team_state_append_completion. The user-visible team-state.yaml is correct +
correctly attributed; only the changelog audit metadata is env-scoped.

sys.exit() in the CLI's _validate_field_path / _validate_agent_name would kill a
daemon thread — those are reimplemented to return HTTP 400.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# EMPTY_STATE single source of truth: the read module mirrors team-state.py.
from .team_state import _EMPTY_STATE_DEFAULTS as EMPTY_STATE

from _fileops import locked_modify_yaml, locked_write_yaml  # noqa: E402

MAX_RECENT_COMPLETIONS = 50


# ---------------------------------------------------------------------------
# Helpers (verbatim from team-state.py)
# ---------------------------------------------------------------------------

def _ts_path(ctx) -> Path:
    return ctx.paths.world / "team-state.yaml"


def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-ayoai-agent") or "").strip() or "system"


def _require_agent_header(ctx):
    from ..server import Response
    agent = (ctx.headers.get("x-ayoai-agent") or "").strip()
    if not agent:
        return Response.error(
            400, "missing_agent_header",
            "X-Mind-Agent header required for team-state writes (g-115-957).")
    return None


def _empty_state() -> dict:
    """Deep-ish copy of EMPTY_STATE (mutable containers fresh per call)."""
    return json.loads(json.dumps(EMPTY_STATE))


def _backfill(state: dict) -> None:
    for key, default in EMPTY_STATE.items():
        if key not in state:
            state[key] = default if not isinstance(default, (list, dict)) else type(default)()


def _stamp_metadata(state: dict, agent: str) -> dict:
    state["last_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    state["last_updated_by"] = agent
    return state


def _set_nested(data, field, value):
    parts = field.split(".")
    target = data
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value


def _append_nested(data, field, value):
    parts = field.split(".")
    target = data
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    key = parts[-1]
    if key not in target or not isinstance(target[key], list):
        target[key] = []
    target[key].append(value)


def _remove_nested(data, field, value):
    parts = field.split(".")
    target = data
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            return
        target = target[part]
    key = parts[-1]
    if key not in target or not isinstance(target[key], list):
        return
    lst = target[key]
    if isinstance(value, str):
        target[key] = [item for item in lst
                       if not (isinstance(item, dict) and item.get("id") == value)
                       and item != value]
    else:
        target[key] = [item for item in lst if item != value]


def _field_path_error(field):
    """Mirror team-state.py _validate_field_path → return error string or None."""
    if not field:
        return "empty field"
    if any(p == "" for p in field.split(".")):
        return (f"malformed field {field!r} — empty segment "
                f"(likely unset env var)")
    return None


# ---------------------------------------------------------------------------
# POST /v1/team-state/update
# ---------------------------------------------------------------------------

def update(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/team-state/update?field=&value=&operation=set|append|remove

    `value` is JSON-parsed if possible, else kept as the raw string (== CLI).
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

    field = (ctx.query.get("field") or "").strip()
    fp_err = _field_path_error(field)
    if fp_err:
        return Response.error(400, "invalid_field", fp_err)

    value_str = ctx.query.get("value")
    if value_str is None:
        # Allow value via body for large/structured payloads.
        value_str = (ctx.body or b"").decode("utf-8") if ctx.body else None
    if value_str is None:
        return Response.error(400, "missing_param", "query parameter 'value' required")

    operation = (ctx.query.get("operation") or "set").strip()
    if operation not in ("set", "append", "remove"):
        return Response.error(400, "invalid_operation",
                              f"operation must be set|append|remove, got {operation!r}")

    try:
        parsed = json.loads(value_str)
    except (json.JSONDecodeError, TypeError):
        parsed = value_str

    agent = _agent_name(ctx)

    def _modifier(state):
        _backfill(state)
        if operation == "set":
            _set_nested(state, field, parsed)
        elif operation == "append":
            _append_nested(state, field, parsed)
        elif operation == "remove":
            _remove_nested(state, field, parsed)
        if "recent_completions" in state:
            state["recent_completions"] = state["recent_completions"][-MAX_RECENT_COMPLETIONS:]
        return _stamp_metadata(state, agent)

    try:
        locked_modify_yaml(_ts_path(ctx), _modifier, initial=_empty_state())
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "field": field, "operation": operation})


# ---------------------------------------------------------------------------
# POST /v1/team-state/in-flight
# ---------------------------------------------------------------------------

def in_flight(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/team-state/in-flight?agent=&goal_id=&title=&phase=

    Marks an agent in-flight on a goal; stamps claimed_at + last_active.
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

    target_agent = (ctx.query.get("agent") or "").strip()
    if not target_agent:
        return Response.error(400, "missing_param", "query parameter 'agent' required")
    goal_id = (ctx.query.get("goal_id") or "").strip()
    title = ctx.query.get("title") or ""
    phase = ctx.query.get("phase") or ""
    if not goal_id:
        return Response.error(400, "missing_param", "query parameter 'goal_id' required")

    agent_author = _agent_name(ctx)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    def _modifier(state):
        _backfill(state)
        if "agent_status" not in state or not isinstance(state["agent_status"], dict):
            state["agent_status"] = {}
        entry = state["agent_status"].get(target_agent)
        if not isinstance(entry, dict):
            entry = {}
        entry["in_flight"] = {
            "goal_id": goal_id,
            "title": title,
            "claimed_at": now,
            "phase": phase,
        }
        entry["last_active"] = now
        state["agent_status"][target_agent] = entry
        return _stamp_metadata(state, agent_author)

    try:
        locked_modify_yaml(_ts_path(ctx), _modifier, initial=_empty_state())
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "agent": target_agent,
                          "goal_id": goal_id, "phase": phase})


# ---------------------------------------------------------------------------
# POST /v1/team-state/clear-in-flight
# ---------------------------------------------------------------------------

def clear_in_flight(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/team-state/clear-in-flight?agent=

    Removes the in_flight block from an agent; bumps last_active. No-op (no
    metadata bump) when there's nothing to clear (mirrors the CLI).
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

    target_agent = (ctx.query.get("agent") or "").strip()
    if not target_agent:
        return Response.error(400, "missing_param", "query parameter 'agent' required")

    agent_author = _agent_name(ctx)
    status = {"cleared": False}

    def _modifier(state):
        _backfill(state)
        agent_status = state.get("agent_status") or {}
        entry = agent_status.get(target_agent) or {}
        if "in_flight" in entry:
            entry.pop("in_flight")
            entry["last_active"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            agent_status[target_agent] = entry
            state["agent_status"] = agent_status
            status["cleared"] = True
            return _stamp_metadata(state, agent_author)
        return state

    try:
        locked_modify_yaml(_ts_path(ctx), _modifier, initial=_empty_state())
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "agent": target_agent,
                          "cleared": status["cleared"]})


# ---------------------------------------------------------------------------
# POST /v1/team-state/init
# ---------------------------------------------------------------------------

def init(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/team-state/init — create team-state.yaml if missing."""
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

    ts_path = _ts_path(ctx)
    if ts_path.exists():
        return Response.json({"ok": True, "created": False,
                              "detail": "team-state.yaml already exists"})

    from ..agent_paths import assert_not_cruft
    assert_not_cruft(ts_path.parent, "mkdir (team-state init)")
    ts_path.parent.mkdir(parents=True, exist_ok=True)
    state = _stamp_metadata(_empty_state(), _agent_name(ctx))
    try:
        locked_write_yaml(ts_path, state)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "created": True})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/team-state/update")] = update
    routes[("POST", "/v1/team-state/in-flight")] = in_flight
    routes[("POST", "/v1/team-state/clear-in-flight")] = clear_in_flight
    routes[("POST", "/v1/team-state/init")] = init
