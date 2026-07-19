#!/usr/bin/env python3
"""Write a session binding.yaml for Phase 2.6 first-class session dirs.

Invoked by /start (and any other authorized session-binding writer) to:
  1. Create `agents/<name>/sessions/<SID>/` if missing
  2. Write `binding.yaml` with full intel (agent, mode, started_at, started_by)
  3. Optionally remove the legacy `.active-agent-<SID>` file at PROJECT_ROOT
     AND retire any OTHER agent's stale `sessions/<SID>/binding.yaml` for the
     same SID (a re-bound SID must resolve to exactly one agent) if
     `--retire-legacy` is given (g-115-1814)

CLI:
  py -3 core/scripts/session-binding-write.py \\
      --sid <SID> --agent <NAME> --mode <reader|assistant|autonomous> \\
      [--started-by claude-code] [--retire-legacy]

Prints the absolute path of the binding.yaml on success. Exits non-zero with
a stderr diagnostic on validation failure or write error. Stderr is
intentionally human-readable so /start's error path can echo it.
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
    _SESSIONS_DIRNAME,
    _BINDING_FILENAME,
    _agent_dir,
    _agents_root,
    _valid_agent_name,
    _valid_sid_shape,
)

VALID_MODES = ("reader", "assistant", "autonomous")


def _project_root() -> Path:
    # core/scripts/<this>.py -> core/scripts -> core -> project root
    return SCRIPT_DIR.parent.parent


def _write_binding_atomic(target: Path, body: str) -> None:
    """Write `body` to `target` atomically (write+rename).

    The temp file is in the same dir so the rename is a same-FS op.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, target)


def _now_iso_local() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _retire_cross_agent_bindings(project_root: Path, sid: str,
                                 keep_agent: str) -> list[Path]:
    """Remove `sessions/<sid>/binding.yaml` under every agent EXCEPT keep_agent.

    A Claude Code SID maps to exactly ONE agent at a time. When a terminal
    re-binds a SID to a new agent (`/start A` -> `/stop A` -> `/start B` in one
    window), the prior agent's `sessions/<sid>/binding.yaml` is left behind.
    `_session_binding.resolve_binding` scans `agents/*/sessions/<sid>/binding.yaml`
    in filesystem `iterdir()` order and returns the FIRST match, so the stale
    binding can SHADOW the live one — bash-agent-inject then injects the wrong
    MIND_AGENT (observed: an alpha SID resolving to bravo=IDLE, g-115-1814).
    Retiring the cross-agent duplicates here guarantees exactly one binding.yaml
    per SID, making the resolver's iterdir order irrelevant.

    Removes ONLY the binding.yaml (never the per-session dir, which may hold
    another session's scratch). Fail-open: any per-agent error is swallowed —
    the freshly written binding is the authoritative record regardless. Returns
    the list of retired paths (for logging/testing).
    """
    retired: list[Path] = []
    ar = _agents_root(project_root)
    if not ar.is_dir():
        return retired
    try:
        children = list(ar.iterdir())
    except OSError:
        return retired
    for child in children:
        if not child.is_dir():
            continue
        if child.name == keep_agent:
            continue
        if not _valid_agent_name(child.name):
            continue
        stale = child / _SESSIONS_DIRNAME / sid / _BINDING_FILENAME
        if stale.is_file():
            try:
                stale.unlink()
                retired.append(stale)
            except OSError:
                # Non-fatal: the freshly written binding is authoritative.
                pass
    return retired


def write_binding(sid: str, agent: str, mode: str,
                  started_by: str = "claude-code",
                  retire_legacy: bool = False,
                  project_root: Path | None = None) -> Path:
    """Write the binding and return its absolute path."""
    if not _valid_sid_shape(sid):
        raise ValueError(f"invalid SID shape: {sid!r}")
    if not _valid_agent_name(agent):
        raise ValueError(f"invalid agent name: {agent!r}")
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode {mode!r}; expected one of {VALID_MODES}")

    pr = project_root or _project_root()
    agent_dir = _agent_dir(pr, agent)
    if not agent_dir.is_dir():
        raise FileNotFoundError(f"agent dir does not exist: {agent_dir}")
    if not (agent_dir / "local-paths.conf").is_file():
        raise FileNotFoundError(
            f"agent has no local-paths.conf: {agent_dir / 'local-paths.conf'}"
        )

    session_dir = agent_dir / _SESSIONS_DIRNAME / sid
    binding_path = session_dir / _BINDING_FILENAME

    body = (
        f"session_id: {sid}\n"
        f"agent: {agent}\n"
        f"mode: {mode}\n"
        f"started_at: '{_now_iso_local()}'\n"
        f"started_by: {started_by}\n"
    )
    _write_binding_atomic(binding_path, body)

    if retire_legacy:
        legacy = pr / f".active-agent-{sid}"
        if legacy.is_file():
            try:
                legacy.unlink()
            except OSError:
                # Non-fatal: the binding is the authoritative record now.
                pass
        # 4: also retire any OTHER agent's binding.yaml for this SID.
        # A re-bound SID (`/start A` -> `/stop A` -> `/start B`) leaves the prior
        # agent's binding.yaml behind, which can shadow the live one in
        # resolve_binding's iterdir-order scan. /start passes --retire-legacy at
        # each of its 4 binding sites, so the fix reaches the incident path.
        _retire_cross_agent_bindings(pr, sid, keep_agent=agent)

    return binding_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sid", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--mode", required=True, choices=VALID_MODES)
    parser.add_argument("--started-by", default="claude-code")
    parser.add_argument(
        "--retire-legacy",
        action="store_true",
        help="Remove the legacy .active-agent-<SID> file AND retire any other "
             "agent's stale sessions/<SID>/binding.yaml after writing the binding.",
    )
    args = parser.parse_args(argv)
    try:
        path = write_binding(
            sid=args.sid,
            agent=args.agent,
            mode=args.mode,
            started_by=args.started_by,
            retire_legacy=args.retire_legacy,
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"session-binding-write: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"session-binding-write: write failed: {e}", file=sys.stderr)
        return 3
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
