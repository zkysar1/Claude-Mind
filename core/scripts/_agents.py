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
  1. world/team-state.yaml `agent_status:` keys — the runtime source of truth,
     populated by team-state.py in-flight/clear-in-flight on every iteration.
  2. PROJECT_ROOT/*/local-paths.conf discovery — any directory with a
     local-paths.conf is an agent. Covers fresh installs before team-state
     has been populated by a first /start.
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


def _from_team_state(project_root: Path) -> Tuple[str, ...]:
    """Roster = union of core-file agent_status keys and per-agent row-file
    stems (g-328-27 sharding — post-shard the rows ARE the runtime truth;
    core keys cover un-migrated deployments)."""
    ts = _resolve_world_team_state(project_root)
    if ts is None:
        return ()
    names: set = set()
    try:
        import yaml  # noqa: PLC0415
        with open(ts, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        agent_status = data.get("agent_status") or {}
        if isinstance(agent_status, dict):
            names.update(k for k in agent_status.keys() if isinstance(k, str) and k)
    except Exception:
        pass
    try:
        for p in _rows_dir_for(ts).iterdir():
            if p.suffix == ".yaml" and p.is_file() and p.stem:
                names.add(p.stem)
    except OSError:
        pass
    return tuple(sorted(names))


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
        # add/remove, which bumps the rows DIR mtime (content-only row
        # updates don't change the key set, so dir mtime suffices).
        # Fold it into the cache token so a /start that creates a new
        # agent's row invalidates a long-lived daemon's cached roster.
        if current_mtime is not None:
            try:
                current_mtime = (current_mtime,
                                 _rows_dir_for(ts).stat().st_mtime)
            except OSError:
                current_mtime = (current_mtime, None)

    if (_CACHE is not None
            and _CACHE.get("mtime") == current_mtime
            and current_mtime is not None):
        return _CACHE["agents"]

    result = _from_team_state(root) or _from_discovery(root) or _from_env() or ()
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
