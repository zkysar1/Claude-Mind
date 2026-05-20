#!/usr/bin/env python3
"""Canonical SID -> agent-name resolver for Claude Code hooks.

Phase 2.6: This module is now a thin wrapper around `_session_binding.py`,
which knows both the new binding.yaml layout AND the legacy
`.active-agent-<SID>` file. The public surface (`resolve(sid, root) -> str`,
CLI `python3 _resolve_agent_from_sid.py <sid>`) is unchanged so all existing
callers keep working.

For richer intel (mode, started_at, etc.), import `_session_binding` directly
and use `resolve_binding(sid, root) -> SessionBinding | None`.

Failure behavior: resolution failures return "" (Python) or produce no
stdout (shell). Exit code is always 0 -- hooks must fail open.
"""

import sys
from pathlib import Path

# Re-export for callers that imported these from this module historically.
from _session_binding import (  # noqa: F401, E402
    _RESERVED_AGENT_NAMES as RESERVED_AGENT_NAMES,
    _AGENTS_PARENT_DIR,
    resolve_agent_name,
)


def _agent_dir(project_root: Path, name: str) -> Path:
    parent = project_root / _AGENTS_PARENT_DIR if _AGENTS_PARENT_DIR else project_root
    return parent / name


def resolve(sid: str, project_root: Path) -> str:
    """Return the validated agent name bound to this SID, or '' if unresolvable.

    Phase 2.6: delegates to `_session_binding.resolve_agent_name`, which
    first tries the new binding.yaml layout, then falls back to the legacy
    `.active-agent-<SID>` file at PROJECT_ROOT.
    """
    return resolve_agent_name(sid, project_root)


def main():
    if len(sys.argv) < 2:
        return
    # core/scripts/_resolve_agent_from_sid.py -> core/scripts -> core -> project root
    project_root = Path(__file__).resolve().parent.parent.parent
    name = resolve(sys.argv[1], project_root)
    if name:
        print(name)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
