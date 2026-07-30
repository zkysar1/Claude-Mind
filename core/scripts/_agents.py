"""Active-agent discovery — single source of truth for "who's in this deployment."

Replaces hardcoded ("alpha","bravo","zeta") tuples scattered across the
codebase. A fresh deployment (e.g. a new domain with a single agent named
"teacher") just works without grep+edit'ing 5 files.

Used by:
  - gates/capability_route.py — routing classifier (intended_agent choices)
  - aspirations.py — VALID_INTENDED_AGENTS schema vocabulary
  - capability-route-gate.py — argparse choices for --route-to
  - audit-user-to-agent.py, full-suite-recommender.py — agent fallback

Resolution order:
  1. world/team-state.yaml `agent_status:` + per-agent shard rows, merged and
     retirement-filtered through _team_state.compose_agent_status — the
     runtime source of truth, populated by team-state.py in-flight/
     clear-in-flight on every iteration. When team-state holds ANY agent this
     wins outright: an all-retired roster resolves to EMPTY and deliberately
     does NOT fall through to (2), because a retired agent keeps its dir and
     would be resurrected there.
  2. PROJECT_ROOT/*/local-paths.conf discovery — any directory with a
     local-paths.conf is an agent. Reached ONLY when team-state is absent or
     holds no agents at all: fresh installs before a first /start.
  3. MIND_AGENT env var alone — single-agent fallback when nothing else
     exists yet.
  4. Empty tuple — caller decides how to handle.

Caching: mtime-validated, NOT process-lifetime. `_CACHE` stores the last
resolved tuple alongside the team-state.yaml mtime that produced it; on
re-call, if the mtime has changed the cache is invalidated automatically.
This closes the "long-lived daemon with stale agent set after /start adds
an agent" gap (fresh-eyes review HIGH H2, 2026-05-18). A process-lifetime
cache would freeze the daemon's view of ACTIVE_AGENTS at boot — fatal on
fresh installs where the daemon imports BEFORE the first /start populates
team-state.yaml.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple

# Cache shape: {"agents": Tuple[str, ...], "mtime": float | None}
# mtime=None means cache produced by discovery/env fallback (no team-state
# file to compare against — always re-resolve).
_CACHE: dict | None = None


# --- Agent-dir resolution (Phase 2.5.C) ---
# Inlined here (not imported from _paths.py) to stay import-cycle-proof.
# MUST stay in sync with core/scripts/_paths.py, _paths.sh, and
# mind_api/src/agent_paths.py. See CLAUDE.md "Agent-dir Resolution" section.
AGENTS_PARENT_DIR = "agents"

# --- Per-session dirs (Phase 2.6) ---
# Sync invariant with core/scripts/_paths.py — not used by _agents.py itself
# (agent enumeration only), but kept here to surface drift in audits.
SESSIONS_DIRNAME = "sessions"
SESSION_DIRNAME = "session"


def _project_root() -> Path:
    # Resolve without importing _paths.py — _paths.py may import this module
    # (or a sibling that does) in the future, and we want this helper to be
    # import-cycle-proof.
    return Path(__file__).resolve().parent.parent.parent


def _agents_root(project_root: Path) -> Path:
    """Directory containing all agent subdirs. Import-cycle-proof inline."""
    return project_root / AGENTS_PARENT_DIR if AGENTS_PARENT_DIR else project_root


# Row-files subdir under WORLD_DIR ( sharding). Inlined from
# core/scripts/_team_state.py ROWS_SUBDIR to stay import-cycle-proof —
# keep in sync with that module.
_ROWS_SUBDIR = ("team-state", "agents")


def _rows_dir_for(ts_path: Path) -> Path:
    """Rows directory for a resolved team-state.yaml path (sibling layout:
    <world>/team-state.yaml + <world>/team-state/agents/)."""
    d = ts_path.parent
    for seg in _ROWS_SUBDIR:
        d = d / seg
    return d


def _load_compose():
    """Roster merge (), SSOT-imported from _team_state.

    LAZY and fail-open by design. This module is import-cycle-proof (see the
    inlined constants above), so the import happens inside the call rather
    than at module scope: _team_state does not import _agents today, and a
    function-local import keeps that safe regardless of future edits. Same
    shape _team_state itself uses for its own cycle-proof imports.

    compose_agent_status is the SSOT for merging core-file residual rows with
    per-agent shard rows AND dropping retirement tombstones. It is imported
    rather than re-derived because the merge carries two non-obvious rules a
    second copy drifts from silently: WHOLE-ROW newest-wins (row winning
    ties), and self-healing revival (a heartbeat NEWER than retired_at
    un-retires the row). A hand-rolled equivalent shipped in the first
    g-115-3735 cut and diverged from compose in BOTH stale-core directions —
    measured by fresh-eyes-code 2026-07-28, not theorized.

    Fail-open direction is deliberate: on import failure the fallback merges
    WITHOUT filtering, i.e. exactly the pre-g-115-3735 behavior. A roster that
    is too INCLUSIVE only degrades routing, whereas one that is too EXCLUSIVE
    can strand a live agent's work.
    """
    try:
        from _team_state import compose_agent_status  # lazy — cycle-proof
        return compose_agent_status
    except Exception:
        return lambda core, rows: {**core, **rows}


def _rows_token(rows_dir: Path):
    """Cache token for the shard rows: (dir mtime, newest row-file mtime).

    Dir mtime ALONE was sufficient while the roster was built from row
    FILENAMES — only create/delete/rename changes that key set, and those do
    bump the dir mtime. Since g-115-3735 the roster also depends on row
    CONTENT (the retirement tombstone), and a content write does NOT bump the
    parent dir's mtime. Measured on this repo 2026-07-28: rows dir mtime
    14:55 while the meta-tiebreaker retirement write landed 17:09 — the dir
    mtime could not see the retirement at all.

    Without the file-mtime fold a long-lived daemon would keep serving a
    roster containing an agent retired minutes ago. The daemon is the main
    consumer, so omitting this would leave the fix inert exactly where it
    matters most.
    """
    try:
        dir_m = rows_dir.stat().st_mtime
    except OSError:
        return None
    newest = 0.0
    try:
        for p in rows_dir.iterdir():
            if p.suffix != ".yaml":
                continue
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > newest:
                newest = m
    except OSError:
        pass
    return (dir_m, newest)


def _resolve_world_team_state(project_root: Path) -> Path | None:
    """Find team-state.yaml — at PROJECT_ROOT/world/ or at the configured
    external WORLD_PATH inside an agent's local-paths.conf."""
    local = project_root / "world" / "team-state.yaml"
    if local.is_file():
        return local
    for child in sorted(_agents_root(project_root).iterdir()):
        conf = child / "local-paths.conf"
        if not conf.is_file():
            continue
        try:
            for line in conf.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("WORLD_PATH="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    candidate = Path(val) / "team-state.yaml"
                    if candidate.is_file():
                        return candidate
        except OSError:
            continue
    return None


def _from_team_state(project_root: Path) -> Tuple[str, ...] | None:
    """Roster = compose_agent_status(core-file agent_status, shard rows) — the
    union of both sources (g-328-27 sharding: post-shard the rows ARE the
    runtime truth; core keys cover un-migrated deployments) MINUS any agent
    whose surviving entry carries a live retirement tombstone (g-115-3735).

    Returns None when team-state is absent or holds NO agents at all, which
    tells get_active_agents to fall through to directory discovery (a fresh
    install, before the first /start populates team-state). Returns a tuple —
    POSSIBLY EMPTY — whenever team-state WAS populated: an all-retired
    deployment genuinely has zero active agents, and saying so is the honest
    answer.

    Collapsing those two empties into one was a real defect (found by
    fresh-eyes-code 2026-07-28, hours after the original fix): `or`-chaining an
    empty roster into _from_discovery re-admitted the retired agents, because a
    retired agent keeps its dir AND its local-paths.conf. The tombstone filter
    was silently undone in precisely the case where it was doing the most work.

    Retirement is a TOMBSTONE, not a delete — the store denies the delete
    right, so a retired agent's shard SURVIVES and keeps being written. The
    pre-fix roster was built from row FILENAMES and never opened the files,
    so the tombstone was unreachable BY CONSTRUCTION rather than by omission,
    and a decommissioned agent was reported active indefinitely (measured:
    meta-tiebreaker retired 17:08:19, still returned by get_active_agents 3h
    later). Both halves needed the fix — the core-file branch read raw
    agent_status keys rather than the composed view, so it bypassed the
    tombstone too.

    Blast radius was routing: a decommissioned agent presenting as a valid
    target can absorb work nothing will execute.
    """
    ts = _resolve_world_team_state(project_root)
    if ts is None:
        return None
    core_status: dict = {}
    rows: dict = {}
    try:
        import yaml  # noqa: PLC0415
        with open(ts, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cs = data.get("agent_status")
        if isinstance(cs, dict):
            core_status = {k: v for k, v in cs.items()
                           if isinstance(k, str) and k}
    except Exception:
        pass
    try:
        import yaml  # noqa: PLC0415
        for p in sorted(_rows_dir_for(ts).iterdir()):
            if not (p.suffix == ".yaml" and p.is_file() and p.stem):
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    rows[p.stem] = yaml.safe_load(f) or {}
            except Exception:
                # Unreadable/corrupt shard reads as NOT retired — an empty
                # mapping carries no tombstone. Same fail-open direction as
                # _load_compose. A shard holding valid-but-non-mapping YAML is
                # equally safe: compose's _is_retired isinstance-guards first.
                rows[p.stem] = {}
    except OSError:
        pass
    if not core_status and not rows:
        return None
    return tuple(sorted(_load_compose()(core_status, rows)))


def _from_discovery(project_root: Path) -> Tuple[str, ...]:
    """Any directory with a local-paths.conf is an agent. Excludes core/,
    .claude/, meta/, world/, and other non-agent top-level dirs by virtue of
    none of them carrying a local-paths.conf."""
    try:
        out = []
        for child in sorted(_agents_root(project_root).iterdir()):
            if child.is_dir() and (child / "local-paths.conf").is_file():
                out.append(child.name)
        return tuple(out)
    except OSError:
        return ()


def _from_env() -> Tuple[str, ...]:
    name = os.environ.get("MIND_AGENT", "").strip()
    return (name,) if name else ()


def get_active_agents() -> Tuple[str, ...]:
    """Return tuple of active agent names. See module docstring for resolution order.

    Cache is mtime-validated against team-state.yaml — a /start that adds a
    new agent (which bumps team-state via team-state.py in-flight call) is
    picked up automatically on the next invocation, even from a long-lived
    daemon process. Discovery/env-fallback resolutions don't cache (mtime=None)
    so a freshly-created agent dir is also visible immediately.
    """
    global _CACHE
    root = _project_root()
    ts = _resolve_world_team_state(root)
    current_mtime = None
    if ts is not None:
        try:
            current_mtime = ts.stat().st_mtime
        except OSError:
            current_mtime = None
        #  sharding: roster changes also arrive as row-file
        # add/remove, which bumps the rows DIR mtime. Since  the
        # roster ALSO depends on row CONTENT (the retirement tombstone), and
        # a content write does not bump the dir mtime — so the token folds in
        # the newest row-file mtime as well. See _rows_token.
        if current_mtime is not None:
            current_mtime = (current_mtime, _rows_token(_rows_dir_for(ts)))

    if (_CACHE is not None
            and _CACHE.get("mtime") == current_mtime
            and current_mtime is not None):
        return _CACHE["agents"]

    result = _from_team_state(root)
    if result is None:
        # team-state absent, or present but holding no agents at all — a fresh
        # install. This is NOT the same as a populated-but-all-retired roster,
        # which _from_team_state returns as an empty TUPLE and which must NOT
        # reach here: discovery enumerates agent DIRS, and a retired agent
        # keeps its dir, so falling through would resurrect exactly the agents
        # the tombstone filter just excluded (fresh-eyes-code, 2026-07-28).
        result = _from_discovery(root) or _from_env() or ()
    _CACHE = {"agents": result, "mtime": current_mtime}
    return result


def clear_cache() -> None:
    """Invalidate the cache — usually not needed; get_active_agents() already
    re-reads when team-state.yaml mtime changes. Kept for explicit refresh
    in tests and for callers that bypass team-state."""
    global _CACHE
    _CACHE = None


if __name__ == "__main__":
    # Smoke test / diagnostic — `py -3 core/scripts/_agents.py`
    import json
    import sys
    agents = get_active_agents()
    print(json.dumps({"active_agents": list(agents), "count": len(agents)}))
    sys.exit(0)
