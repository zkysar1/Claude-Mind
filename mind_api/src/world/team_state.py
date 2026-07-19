"""GET /v1/team-state/read — parity with `team-state.py read`.

Query params (all optional):
    field=<dot.path>     e.g. strategic_focus.primary
    json=1               JSON output (default: YAML)

Without `field`, dumps the entire state. Missing file returns the EMPTY_STATE
template (mirrors read_state()'s behavior).

Schema migration: any key in EMPTY_STATE that's missing in the on-disk file
gets backfilled with the empty default — matches read_state() in
team-state.py so callers see a consistent shape across fresh and aged files.
"""
from __future__ import annotations

import json

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise RuntimeError("PyYAML required for team-state endpoint") from e

from .. import file_locks  # noqa: F401 — installs core/scripts on sys.path at module load (rb-3868)
from ..yaml_cache import cache

# Shared sharding helper (core/scripts lands on sys.path via the file_locks
# import above — the ordering is load-bearing; isolated import breaks without
# it (rb-3868 / 7). Single source of truth for row composition;
# guard-742 parity by construction.
from _team_state import compose_state  # noqa: E402


# Mirrored from team-state.py EMPTY_STATE. When that schema gains a key,
# mirror here. Field-set parity cross-checked by
# core/scripts/tests/test_daemon_cli_mirror_parity.py (4).
_EMPTY_STATE_DEFAULTS = {
    "last_updated": None,
    "last_updated_by": None,
    "strategic_focus": {
        "primary": None,
        "rationale": None,
        "set_by": None,
        "set_at": None,
        "acknowledged_by": [],
    },
    "active_blockers": [],
    "recent_completions": [],
    "agent_status": {},
    "critical_blockers": [],
    # Mirror of team-state.py inbox_alert_backlog (); null when zero
    # matching goals. 9: this daemon mirror lagged the CLI schema add,
    # so daemon writes dropped the trailing key vs the CLI (byte-compat drift).
    "inbox_alert_backlog": None,
}


def _path(ctx):
    return ctx.paths.world / "team-state.yaml"


def _read_state(ctx):
    """Mirror of team-state.py:read_state — returns EMPTY_STATE on miss,
    backfills any missing top-level key with its EMPTY_STATE default, then
    composes per-agent row files over the core document (g-328-27 sharding;
    row files win newest-wins over core residuals)."""
    data = cache().get(_path(ctx))
    if not data:
        out = dict(_EMPTY_STATE_DEFAULTS)
    else:
        # Don't mutate the cached dict — copy first.
        out = dict(data)
    for k, default in _EMPTY_STATE_DEFAULTS.items():
        if k not in out:
            out[k] = default if not isinstance(default, (list, dict)) else type(default)()
    return compose_state(out, ctx.paths.world)


def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    state = _read_state(ctx)

    field = q.get("field")
    as_json = (q.get("json") or "").lower() not in ("", "0", "false", "no")

    if field:
        parts = field.split(".")
        val = state
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                val = None
                break
        if as_json:
            return Response.text(
                json.dumps(val, ensure_ascii=False, default=str),
                content_type="application/json",
            )
        # CLI: yaml.dump for dict/list, str() for scalars, empty for None.
        if isinstance(val, (dict, list)):
            body = yaml.dump(val, default_flow_style=False, allow_unicode=True).rstrip()
            return Response.text(body, content_type="text/x-yaml")
        if val is not None:
            return Response.text(str(val), content_type="text/plain")
        return Response.text("", content_type="text/plain")

    # Full dump
    if as_json:
        return Response.text(
            json.dumps(state, ensure_ascii=False, default=str),
            content_type="application/json",
        )
    body = yaml.dump(state, default_flow_style=False, allow_unicode=True).rstrip()
    return Response.text(body, content_type="text/x-yaml")


def register(routes) -> None:
    routes[("GET", "/v1/team-state/read")] = read
