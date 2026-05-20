#!/usr/bin/env python3
"""Write a per-session summary into agents/<name>/sessions/<SID>/session-summary.yaml.

Phase 2.6.D: makes per-session dirs self-describing for queryable history.
The dir name IS the SID; binding.yaml says what mode it ran in; this summary
says what actually happened during the session.

Schema:
  session_id: <SID>
  agent: <name>
  ended_at: <iso-timestamp>
  ended_reason: graceful-stop|consolidate|crash-recovered|unknown
  iterations_completed: <int>          # goals finished this session
  goals_filed: <int>                   # new goals created this session
  tree_writes: <int>                   # knowledge-tree edits
  uncommitted_paths_at_end: <int>      # files left uncommitted

Counters are best-effort — failure to read any source produces a 0 for that
field rather than a write failure. Use of this summary is observational
(queryable history); critical recovery paths must not depend on it.

CLI:
  py -3 core/scripts/session-summary-write.py \\
      --sid <SID> --agent <NAME> --reason graceful-stop \\
      [--iterations N] [--goals-filed N] [--tree-writes N]

Returns 0 on success, prints binding path on stdout. Best-effort: if the
session dir doesn't exist, exits 0 silently (the binding was never created
or already retired).
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _session_binding import (  # noqa: E402
    _AGENTS_PARENT_DIR,
    _SESSIONS_DIRNAME,
    _valid_agent_name,
    _valid_sid_shape,
)

VALID_REASONS = ("graceful-stop", "consolidate", "crash-recovered", "unknown")


def _project_root() -> Path:
    return SCRIPT_DIR.parent.parent


def _now_iso_local() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _write_summary_atomic(target: Path, body: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, target)


def write_summary(sid: str, agent: str, reason: str,
                  iterations_completed: int = 0,
                  goals_filed: int = 0,
                  tree_writes: int = 0,
                  uncommitted_paths_at_end: int = 0,
                  project_root: Path | None = None) -> Path | None:
    """Write the summary. Returns path on success, None if session dir absent."""
    if not _valid_sid_shape(sid) or not _valid_agent_name(agent):
        return None
    if reason not in VALID_REASONS:
        reason = "unknown"

    pr = project_root or _project_root()
    session_dir = (pr / _AGENTS_PARENT_DIR / agent / _SESSIONS_DIRNAME / sid
                   if _AGENTS_PARENT_DIR else pr / agent / _SESSIONS_DIRNAME / sid)
    if not session_dir.is_dir():
        # Session dir absent — binding was never written or already retired.
        # Best-effort: no summary to attach to. Return None silently.
        return None

    target = session_dir / "session-summary.yaml"
    body = (
        f"session_id: {sid}\n"
        f"agent: {agent}\n"
        f"ended_at: '{_now_iso_local()}'\n"
        f"ended_reason: {reason}\n"
        f"iterations_completed: {int(iterations_completed)}\n"
        f"goals_filed: {int(goals_filed)}\n"
        f"tree_writes: {int(tree_writes)}\n"
        f"uncommitted_paths_at_end: {int(uncommitted_paths_at_end)}\n"
    )
    _write_summary_atomic(target, body)
    return target


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sid", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--reason", default="unknown", choices=VALID_REASONS)
    p.add_argument("--iterations", type=int, default=0)
    p.add_argument("--goals-filed", type=int, default=0)
    p.add_argument("--tree-writes", type=int, default=0)
    p.add_argument("--uncommitted-paths", type=int, default=0)
    args = p.parse_args(argv)
    try:
        path = write_summary(
            sid=args.sid, agent=args.agent, reason=args.reason,
            iterations_completed=args.iterations,
            goals_filed=args.goals_filed,
            tree_writes=args.tree_writes,
            uncommitted_paths_at_end=args.uncommitted_paths,
        )
    except (ValueError, OSError) as e:
        print(f"session-summary-write: {e}", file=sys.stderr)
        return 2
    if path is None:
        # Session dir absent — no-op, exit 0 silently.
        return 0
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
