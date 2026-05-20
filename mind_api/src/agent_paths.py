"""Per-agent path resolution for the runtime daemon.

The daemon receives the agent name on every request via the `X-Mind-Agent`
header. Each agent has its own `<agent>/local-paths.conf` describing where
WORLD_DIR and META_DIR live (typically a shared external drive). This module
parses those confs lazily and caches the result keyed by agent name.

Why not import `_paths.py` from core/scripts/? Because that module bakes the
agent context into module-level constants at import time, and the daemon
serves many agents from one process. We need per-request resolution.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Dict, NamedTuple, Optional

# Single source of truth for path absolutization lives in
# core/scripts/_path_helpers.py. Daemon and CLI tiers share it. Add
# core/scripts/ to sys.path once at module load so the import is direct.
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "core" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from _path_helpers import absolutize  # noqa: E402


# --- Agent-dir resolution (Phase 2.5.C/D) ---
# Empty string = agent dirs at PROJECT_ROOT (legacy layout, pre-2026-05-19).
# Currently "agents" — agent dirs live at PROJECT_ROOT/agents/<name>.
# AGENTS_PARENT_DIR MUST stay in sync across all 3 resolver layers:
#   core/scripts/_paths.py
#   core/scripts/_paths.sh
#   mind_api/src/agent_paths.py  (this file)
# Plus 2 import-cycle-proof inlined copies:
#   core/scripts/_agents.py
#   core/scripts/path-resolution-hook.py
# See CLAUDE.md "Agent-dir Resolution" section.
AGENTS_PARENT_DIR = "agents"

# --- Per-session dirs (Phase 2.6) ---
# Each agent has agents/<name>/sessions/<SID>/ for per-session snapshot
# state (binding, runner-token, scratch, iteration-checkpoint, etc.).
# Cross-session state stays in agents/<name>/session/ (singular).
SESSIONS_DIRNAME = "sessions"
SESSION_DIRNAME = "session"


# --- Resolution ------------------------------------------------------------

class Tenant(NamedTuple):
    """H9-light: named pair of (world_path, meta_path) — the Phase-5 abstraction seam.

    A tenant is the unit that selects which (WORLD, META) the daemon serves
    for a given request. Today there is exactly one tenant per agent (the
    default tenant, derived from `<agent>/local-paths.conf`). The
    X-Mind-Tenant header captured by `server.py` into `ctx.tenant` is
    propagated but not yet used to select a different (world, meta) pair.

    H5 (multi-tenant runtime, post-stop) will extend AgentPathResolver to
    map a tenant identifier from the header to a non-default Tenant. The
    type defined here is the seam that makes that extension possible
    without re-touching the world/ or meta/ packages.

    Construct via `AgentPaths.tenant` (the default path) or directly when
    an alternate (world, meta) pair is needed for testing.
    """
    world_path: Path
    meta_path: Path


class AgentPaths:
    """Resolved paths for a specific agent.

    `world` and `meta` come from the agent's local-paths.conf; `agent` is
    always PROJECT_ROOT/<agent-name>. None of the directories are required to
    exist (the caller is responsible for handling missing files).
    """

    __slots__ = ("agent_name", "world", "meta", "agent", "project_root")

    def __init__(self, agent_name: str, world: Path, meta: Path, agent: Path,
                 project_root: Path):
        self.agent_name = agent_name
        self.world = world
        self.meta = meta
        self.agent = agent
        # project_root is the framework repo root (parent of core/). Carried
        # explicitly so daemon endpoints can invoke gates that need the repo
        # path (uncommitted-work git probe, goal-duplication git log + target-
        # state scan) without re-deriving via Path manipulation.
        self.project_root = project_root

    def __repr__(self) -> str:  # pragma: no cover — diagnostic only
        return (f"AgentPaths(agent={self.agent_name!r}, world={self.world}, "
                f"meta={self.meta}, agent={self.agent}, "
                f"project_root={self.project_root})")

    @property
    def tenant(self) -> "Tenant":
        """The default Tenant for this agent — pair of (world, meta) paths.

        H9-light seam: callers needing only the (world, meta) pair (e.g.,
        the world-service and meta-service modules under mind_api/src/world
        and mind_api/src/meta) can request `paths.tenant` and stay agnostic
        to the rest of AgentPaths. See `Tenant` for the H5 evolution path.
        """
        return Tenant(world_path=self.world, meta_path=self.meta)

    @property
    def sessions_root(self) -> Path:
        """Parent dir for per-session dirs: agents/<name>/sessions/."""
        return self.agent / SESSIONS_DIRNAME

    def session_dir(self, sid: str) -> Path:
        """Per-session dir for a given sid: agents/<name>/sessions/<sid>/."""
        return self.sessions_root / sid

    @property
    def state_dir(self) -> Path:
        """Cross-session state dir (the singular 'session/'): agents/<name>/session/."""
        return self.agent / SESSION_DIRNAME


def _parse_conf(conf_path: Path) -> Dict[str, str]:
    """Parse a local-paths.conf file into a dict. Mirrors _paths.py:_parse_conf."""
    out: Dict[str, str] = {}
    try:
        text = conf_path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("\"", "'"):
            val = val[1:-1]
        out[key.strip()] = val
    return out


class AgentPathResolver:
    """Caches `AgentPaths` keyed by agent name. Thread-safe.

    The cache is invalidated only by `clear_cache()`. In practice, an agent's
    local-paths.conf is written once at /start time and never changes for the
    life of the daemon. If it does change, restart the daemon — the cost is
    one cold call.
    """

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self._cache: Dict[str, AgentPaths] = {}
        self._lock = threading.Lock()

    def resolve(self, agent_name: Optional[str]) -> AgentPaths:
        """Resolve paths for `agent_name`.

        When `agent_name` is None or empty, falls back to the first agent that
        has a local-paths.conf. This matches the behaviour of _paths.py for
        hook contexts where MIND_AGENT is not set.
        """
        key = (agent_name or "").strip()
        if not key:
            key = self._first_available_agent()

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

        resolved = self._resolve_uncached(key)
        with self._lock:
            self._cache[key] = resolved
        return resolved

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # --- internals ---

    def _agents_root(self) -> Path:
        """Directory containing all agent subdirs."""
        if AGENTS_PARENT_DIR:
            return self.project_root / AGENTS_PARENT_DIR
        return self.project_root

    def _agent_dir(self, name: str) -> Path:
        """Filesystem path for an individual agent's dir."""
        return self._agents_root() / name

    def _first_available_agent(self) -> str:
        """Find the first agent dir with a local-paths.conf. Empty string if none."""
        for conf in sorted(self._agents_root().glob("*/local-paths.conf")):
            return conf.parent.name
        return ""

    def _resolve_uncached(self, agent_name: str) -> AgentPaths:
        """Compute paths for `agent_name` without touching the cache.

        Resolution order matches _paths.py (plan v1 step 0.1 hard-cut):
          - env override MIND_WORLD wins for world; MIND_META wins for meta
          - else local-paths.conf entries WORLD_PATH / META_PATH
          - else raise RuntimeError (NO PROJECT_ROOT/world|meta fallback)

        Single source of truth: world/meta MUST come from env or conf. A
        PROJECT_ROOT mirror is the cruft failure mode this resolver refuses.
        """
        # local-paths.conf — may not exist (e.g. bare daemon test with no agents)
        conf = {}
        if agent_name:
            conf_path = self._agent_dir(agent_name) / "local-paths.conf"
            conf = _parse_conf(conf_path) if conf_path.exists() else {}
        else:
            # No agent_name: try first available conf (hook contexts without
            # an X-Mind-Agent header). If none exists, world/meta resolution
            # below raises — matches CLI behavior.
            for cand in sorted(self._agents_root().glob("*/local-paths.conf")):
                conf = _parse_conf(cand)
                if conf:
                    break

        world_src = os.environ.get("MIND_WORLD") or conf.get("WORLD_PATH")
        if not world_src:
            raise RuntimeError(
                f"agent_paths: WORLD_PATH unresolved for agent={agent_name!r} "
                f"(no MIND_WORLD env, no WORLD_PATH in local-paths.conf). "
                f"Plan v1 step 0.1: no PROJECT_ROOT/world fallback."
            )
        meta_src = os.environ.get("MIND_META") or conf.get("META_PATH")
        if not meta_src:
            raise RuntimeError(
                f"agent_paths: META_PATH unresolved for agent={agent_name!r} "
                f"(no MIND_META env, no META_PATH in local-paths.conf). "
                f"Plan v1 step 0.1: no PROJECT_ROOT/meta fallback."
            )
        if not agent_name:
            raise RuntimeError(
                "agent_paths: agent_name empty — daemon must receive a valid "
                "X-Mind-Agent header. No PROJECT_ROOT fallback for agent dir."
            )

        world = absolutize(world_src, self.project_root)
        meta = absolutize(meta_src, self.project_root)
        agent_dir = self._agent_dir(agent_name)

        return AgentPaths(
            agent_name=agent_name,
            world=world,
            meta=meta,
            agent=agent_dir,
            project_root=self.project_root,
        )
