"""POST /v1/team-state/{update,in-flight,clear-in-flight,clear-body-row,init,retire-agent} — team-state writes.

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
from .. import file_locks  # noqa: F401 — installs core/scripts on sys.path at module load (rb-3868; explicit, not transitive through .team_state)

from _fileops import locked_modify_yaml, locked_write_yaml  # noqa: E402
#  sharding: shared routing/composition helper (core/scripts on
# sys.path). CLI and daemon route through the SAME functions — guard-742
# parity by construction.
from _team_state import (  # noqa: E402
    body_row_shard_present,
    core_residual,
    make_clear_body_row_modifier,
    make_clear_in_flight_modifier,
    retire_agent as _retire_agent,
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
    # current_focus: lane indicator for partner Theory-of-Mind ().
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
    """POST /v1/team-state/clear-in-flight?agent=[&if_goal=]

    Removes the in_flight block from an agent; bumps last_active. No-op (no
    metadata bump) when there's nothing to clear (mirrors the CLI).

    `if_goal` is an optional COMPARE-AND-SWAP (guard-2474 clause 2, g-306-137):
    when supplied, the row is cleared ONLY if its live goal_id matches. A caller
    that verified ownership out-of-band is otherwise performing a check-then-act
    — its verdict is computed from a snapshot while this endpoint blanks
    whatever row is present at call time, so a concurrent sibling claim inside
    that window is destroyed regardless of the check. Omitting `if_goal` keeps
    the original unconditional behavior for recovery/retire/release callers.
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

    target_agent = (ctx.query.get("agent") or "").strip()
    if not target_agent:
        return Response.error(400, "missing_param", "query parameter 'agent' required")

    # RAW, deliberately (). The normalization that used to live here —
    # `(ctx.query.get("if_goal") or "").strip() or None` — collapsed
    # blank-but-supplied into absent, and absent means "clear unconditionally",
    # so `?if_goal=` or `?if_goal=%20%20` DESTROYED a live row and reported
    # ok/cleared=True. The CLI twin did not strip, so the same input PRESERVED
    # the row there: one input, two opposite outcomes. Normalization now happens
    # once inside make_clear_in_flight_modifier, below the point where the twins
    # could disagree; a blank-but-supplied value raises there and is surfaced as
    # a 400 rather than silently downgrading to an unconditional wipe.
    if_goal = ctx.query.get("if_goal")
    agent_author = _agent_name(ctx)
    status = {"cleared": False, "skipped_goal_id": None, "row_survived": False}

    #  sharding: clear operates on the agent's OWN row file. The
    # core_residual seed lets an un-migrated deployment's in_flight (still
    # in the core file) be seeded into the row and actually cleared —
    # newest-wins compose then prefers the freshly-stamped row.
    #
    # The modifier is SHARED with the CLI twin (guard-2323 / guard-547) rather
    # than hand-mirrored: both modules already import from _team_state, so the
    # copies cannot drift apart. It runs inside locked_modify_yaml's lock, which
    # is what makes the if_goal comparison atomic against a concurrent
    # POST /v1/team-state/in-flight on the same row file.
    # Factory raises on a blank-but-supplied if_goal (). It raises
    # BEFORE locked_modify_yaml takes the lock, so a caller bug costs no lock and
    # no backend round-trip — and 400 is correct rather than the 500 the
    # write_failed handler below would give, because nothing was ever written.
    try:
        _row_modifier = make_clear_in_flight_modifier(
            agent_author, if_goal=if_goal, status=status)
    except ValueError as e:
        return Response.error(400, "invalid_param", str(e))

    try:
        locked_modify_yaml(row_path(ctx.paths.world, target_agent), _row_modifier,
                           initial=core_residual(_ts_path(ctx), target_agent))
    except (OSError, ValueError) as e:
        return Response.error(500, "write_failed", str(e))

    # row_survived MUST be forwarded: the two shell/worker reporters read only
    # this response, so without it they can never distinguish "a row is still
    # standing but carried no comparable goal_id" from "nothing was there"
    # (). Both were reporting the former as "already absent".
    return Response.json({"ok": True, "agent": target_agent,
                          "cleared": status["cleared"],
                          "skipped_goal_id": status["skipped_goal_id"],
                          "row_survived": status["row_survived"]})


# ---------------------------------------------------------------------------
# POST /v1/team-state/clear-body-row  ( — the dict-key REMOVE path)
# ---------------------------------------------------------------------------

def clear_body_row(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/team-state/clear-body-row?agent=&sid=

    Removes `agent_status.<agent>.in_flight_bodies.<sid>` outright, and sweeps
    any null-valued siblings while it holds the row lock.

    This is the path `worker_close_in_flight_clear` had to fake by SETTING NULL
    through /v1/team-state/update: that dispatch's `remove` operation is
    list-only, so on a dict key it returns early and reports ok:true having done
    nothing (g-306-186). Delegates to the shared
    `_team_state.make_clear_body_row_modifier` — the SAME factory the CLI
    cmd_clear_body_row uses, so guard-742 parity holds by construction rather
    than by hand-mirroring, which is precisely why widening `_remove_nested`
    (a duplicated pair) was rejected in favour of this op.
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

    target_agent = (ctx.query.get("agent") or "").strip()
    if not target_agent:
        return Response.error(400, "missing_param", "query parameter 'agent' required")
    sid = (ctx.query.get("sid") or "").strip()
    if not sid:
        return Response.error(400, "missing_param", "query parameter 'sid' required")

    agent_author = _agent_name(ctx)
    status = {"removed": False, "nulls_swept": 0, "remaining": 0}

    # Nothing to clear from a shard that does not exist — and writing anyway
    # would CREATE it (guard-2611). Shared predicate, and it materializes the
    # shard before asking, so a partner's shard this box has never pulled does
    # not read as absent (; "shared" describes where the code lives,
    # not what it reads — it IS an .exists(), just not a bare one).
    if not body_row_shard_present(ctx.paths.world, target_agent):
        return Response.json({"ok": True, "agent": target_agent, "sid": sid,
                              "removed": False, "nulls_swept": 0,
                              "remaining": 0, "no_shard": True})

    _row_modifier = make_clear_body_row_modifier(agent_author, sid, status=status)

    # No core_residual seed, unlike clear-in-flight: in_flight_bodies is a
    # post-sharding field that has never lived in the core file, so seeding from
    # a residual could only ever re-materialize an unrelated legacy in_flight.
    try:
        locked_modify_yaml(row_path(ctx.paths.world, target_agent), _row_modifier)
    except (OSError, ValueError) as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "agent": target_agent, "sid": sid,
                          "removed": status["removed"],
                          "nulls_swept": status["nulls_swept"],
                          "remaining": status["remaining"]})


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
    except Exception as e:
        try:  # report, never raise — see note_swallowed_backend_error ()
            from storage_backend import note_swallowed_backend_error
            note_swallowed_backend_error("ensure_local", ts_path, e)
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
# POST /v1/team-state/retire-agent  ( — the sanctioned REMOVE path)
# ---------------------------------------------------------------------------

def retire_agent(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/team-state/retire-agent?agent=&source=&dry_run=

    Sanctioned removal of an agent's team-state presence (core-file
    agent_status residual + per-agent shard), archive-before-delete gated.
    g-115-1909 found this REMOVE path missing (whole-row remove refused;
    _remove_nested is list-only; no retire subcommand). Delegates to the
    shared _team_state.retire_agent — the SAME function the CLI
    cmd_retire_agent calls, so guard-742 parity holds by construction. The
    archive lands in world/team-state/.graveyard/ (a path the live system
    never reads); the op refuses to delete on an unverified archive.
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

    target = (ctx.query.get("agent") or "").strip()
    if not target:
        return Response.error(400, "missing_param", "query parameter 'agent' required")
    source = ctx.query.get("source")
    dry_run = (ctx.query.get("dry_run") or "").strip().lower() in ("1", "true", "yes")
    author = _agent_name(ctx)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    try:
        result = _retire_agent(ctx.paths.world, _ts_path(ctx), target, author,
                               now, source=source, dry_run=dry_run)
    except ValueError as e:
        return Response.error(400, "invalid_agent", str(e))
    except (RuntimeError, OSError) as e:
        return Response.error(500, "retire_failed", str(e))

    return Response.json(result)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/team-state/update")] = update
    routes[("POST", "/v1/team-state/in-flight")] = in_flight
    routes[("POST", "/v1/team-state/clear-in-flight")] = clear_in_flight
    routes[("POST", "/v1/team-state/clear-body-row")] = clear_body_row
    routes[("POST", "/v1/team-state/init")] = init
    routes[("POST", "/v1/team-state/retire-agent")] = retire_agent
