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
#  sharding: shared routing/composition helper (core/scripts on
# sys.path). CLI and daemon route through the SAME functions — guard-742
# parity by construction.
from _team_state import (  # noqa: E402
    core_residual,
    route_field,
    row_path,
    rows_dir,
    stamp_row_metadata,
)

MAX_RECENT_COMPLETIONS = 50


# ---------------------------------------------------------------------------
# Helpers (verbatim from team-state.py)
# ---------------------------------------------------------------------------

def _ts_path(ctx) -> Path:
    return ctx.paths.world / "team-state.yaml"


def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _require_agent_header(ctx):
    from ..server import Response
    agent = (ctx.headers.get("x-mind-agent") or "").strip()
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

    #  sharding: agent_status.<name>[...] writes land in that agent's
    # OWN row file (world/team-state/agents/<name>.yaml) — heartbeat/focus
    # stamps from N agents never contend on one object. Mirrors the CLI's
    # cmd_update routing exactly (shared route_field helper).
    scope, row_agent, subpath = route_field(field)
    if scope == "row":
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        if subpath == "" and (operation != "set" or not isinstance(parsed, dict)):
            return Response.error(
                400, "invalid_row_write",
                "agent_status.<name> whole-row supports only operation=set "
                "with a JSON object value")

        def _row_modifier(row):
            if not isinstance(row, dict):
                row = {}
            if subpath == "":
                row = dict(parsed)
            elif operation == "set":
                _set_nested(row, subpath, parsed)
            elif operation == "append":
                _append_nested(row, subpath, parsed)
            elif operation == "remove":
                _remove_nested(row, subpath, parsed)
            return stamp_row_metadata(row, agent, now)

        try:
            locked_modify_yaml(row_path(ctx.paths.world, row_agent), _row_modifier,
                               initial=core_residual(_ts_path(ctx), row_agent))
        except (OSError, ValueError) as e:
            return Response.error(500, "write_failed", str(e))
        return Response.json({"ok": True, "field": field, "operation": operation})

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
    # current_focus: lane indicator for partner Theory-of-Mind (5).
    # Aspiration (lane) parsed from goal_id (g-NNN-MM -> asp-NNN) + title, so
    # partners track the actual lane instead of inferring from lagging
    # completions. Persists across clear-in-flight (the last-claimed lane).
    # MUST stay byte-identical to core/scripts/team-state.py cmd_in_flight
    # (guard-742 dual-write).
    _gp = goal_id.split("-")
    _asp = ("asp-" + _gp[1]) if len(_gp) >= 3 and _gp[0] == "g" and _gp[1].isdigit() else ""
    if title and _asp:
        _focus = _asp + ": " + title
    elif title:
        _focus = title
    else:
        _focus = _asp or goal_id

    #  sharding: the claim stamp lands in the agent's OWN row file
    # (mirrors CLI cmd_in_flight).
    def _row_modifier(row):
        if not isinstance(row, dict):
            row = {}
        row["in_flight"] = {
            "goal_id": goal_id,
            "title": title,
            "claimed_at": now,
            "phase": phase,
        }
        row["last_active"] = now
        row["current_focus"] = _focus
        row["current_focus_updated_at"] = now
        return stamp_row_metadata(row, agent_author, now)

    try:
        locked_modify_yaml(row_path(ctx.paths.world, target_agent), _row_modifier,
                           initial=core_residual(_ts_path(ctx), target_agent))
    except (OSError, ValueError) as e:
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

    #  sharding: clear operates on the agent's OWN row file. The
    # core_residual seed lets an un-migrated deployment's in_flight (still
    # in the core file) be seeded into the row and actually cleared —
    # newest-wins compose then prefers the freshly-stamped row.
    def _row_modifier(row):
        if not isinstance(row, dict):
            row = {}
        if "in_flight" in row:
            now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            row.pop("in_flight")
            row["last_active"] = now
            status["cleared"] = True
            return stamp_row_metadata(row, agent_author, now)
        return row

    try:
        locked_modify_yaml(row_path(ctx.paths.world, target_agent), _row_modifier,
                           initial=core_residual(_ts_path(ctx), target_agent))
    except (OSError, ValueError) as e:
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
    # own-cloud read-path fix (2026-07-02): materialize an S3-only team-state on
    # a fresh box BEFORE the exists() gate. Without it a fresh own-cloud box sees
    # exists()==False and RE-CREATES team-state.yaml, clobbering the synced remote
    # (and the CAS fence can't recover post-restart -> the write "deadlock" zeta
    # flagged). No-op on LocalBackend / out-of-root (keystone); best-effort so
    # init never crashes on a backend hiccup.
    try:
        from storage_backend import get_backend
        get_backend().ensure_local(ts_path)
    except Exception:
        pass
    #  sharding: always ensure the per-agent rows dir exists
    # (idempotent) so aged deployments gain the layout on their next init.
    from ..agent_paths import assert_not_cruft
    try:
        rd = rows_dir(ctx.paths.world)
        assert_not_cruft(rd, "mkdir (team-state rows init)")
        rd.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # best-effort — row writes mkdir-on-demand via locked_modify_yaml
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
