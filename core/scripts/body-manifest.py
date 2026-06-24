#!/usr/bin/env python3
"""Read/write the per-Body `body-manifest.yaml` (Phase 1B, Mind/Body — ).

A Body is a forked instance of the Mind keyed by `unitKey` (locally the session
SID). Its `agents/<mindKey>/sessions/<unitKey>/body-manifest.yaml` records:
  {unitKey, mindKey, env_id, role, body_state, started_at, forked_wm_hash}

This is the manifest's SOLE writer — /start FORK-BODY calls `write`, stop-hook
calls `set-state`. Schema + lifecycle: `core/config/conventions/session-state.md`
"Phase 1B - Body Manifest". Design SSOT: tree node `mind-engine-identity-bridge`.

REDUCER-AWARE FORK (the backward-compatibility keystone):
  The reducer is the worker Body holding `running-session-id` (derived, not
  stored). The reducer does NOT fork its WM (it IS the canonical Mind WM) — its
  manifest carries `forked_wm_hash: null` and no body-WM-file is created, so
  Phase 1A routing (which keys on the body-WM-FILE's existence) returns the
  agent-wide path. Only a NON-reducer worker (a 2nd+ worker once a reducer
  already holds `running-session-id`) forks: FORK-BODY `cp`s the Mind WM as the
  Body's baseline, records its sha256, and the body-WM-file's existence flips
  routing to the per-Body path. Observers never fork (read-only). With exactly
  one Body (the reducer) this is inert — today's behavior, unchanged.

CLI:
  py -3 core/scripts/body-manifest.py write --sid <unitKey> --agent <mindKey>
        [--env-id local] [--role worker|observer]
  py -3 core/scripts/body-manifest.py read     --sid <unitKey> --agent <mindKey>
  py -3 core/scripts/body-manifest.py set-state --sid <unitKey> --agent <mindKey> <state>
  py -3 core/scripts/body-manifest.py is-reducer --sid <unitKey> --agent <mindKey>

`write` prints the manifest path; `read` prints the manifest as JSON; `set-state`
prints the path; `is-reducer` prints `true`/`false`. Non-zero exit + stderr
diagnostic on validation/IO failure (human-readable for /start's error path).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _session_binding import (  # noqa: E402
    _SESSIONS_DIRNAME,
    _agent_dir,
    _valid_agent_name,
    _valid_sid_shape,
)

# Singular agent-wide state dir. Sync point: CLAUDE.md "Agent-dir Resolution"
# SESSION_DIRNAME (currently "session"). Inlined here (not sourced) to keep this
# session-boundary helper self-contained, mirroring the inlined-copy pattern the
# other session-state scripts use.
_STATE_DIRNAME = "session"
_MANIFEST_FILENAME = "body-manifest.yaml"
_WM_FILENAME = "working-memory.yaml"
# : the fork-time WM snapshot (the 3-way-delta common ancestor). Written
# byte-faithfully at fork beside the live (mutating) body WM; read by
# body-merge.generalize_down. body-merge references this as bm._BASELINE_FILENAME.
_BASELINE_FILENAME = "forked-wm-baseline.yaml"
# : genuine-close sentinel. A worker Body writes this in its session dir
# when its loop GENUINELY terminates (no more work), distinguishing a real close
# from a mere between-turns turn-end. The stop-hook marks closed-pending-merge
# only when it is present, then consumes it.
_CLOSE_SENTINEL_FILENAME = "body-closing"

VALID_ROLES = ("worker", "observer")
VALID_STATES = ("active", "closed-pending-merge", "merged", "closed-stale")
# Manifest field order — deterministic render keeps diffs stable.
_FIELD_ORDER = (
    "unitKey", "mindKey", "env_id", "role",
    "body_state", "started_at", "forked_wm_hash",
)


def _project_root() -> Path:
    # core/scripts/<this>.py -> core/scripts -> core -> project root
    return SCRIPT_DIR.parent.parent


def _now_iso_local() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _write_atomic(target: Path, body: str) -> None:
    """Atomic write+rename within the same dir (same-FS rename)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, target)


def _write_atomic_bytes(target: Path, data: bytes) -> None:
    """Byte-faithful atomic write (write_bytes — NO newline translation).

    The WM fork must be a byte-exact `cp` of the Mind WM so `forked_wm_hash`
    stays a valid merge baseline; text-mode write_text would translate
    \\n->\\r\\n on Windows and corrupt the hash invariant (g-306-62 test catch).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _unlink_quiet(p: Path) -> None:
    """Best-effort unlink (consume a sentinel); a missing file is not an error."""
    try:
        p.unlink()
    except OSError:
        pass


def _render_manifest(data: dict) -> str:
    """Render the manifest dict to deterministic YAML (fixed field order).

    Hand-rendered (not yaml.safe_dump) to control field order and quoting,
    matching session-binding-write.py's style. Values are simple scalars.
    """
    lines = []
    for k in _FIELD_ORDER:
        v = data.get(k)
        if v is None:
            lines.append(f"{k}: null")
        elif k in ("started_at",):
            lines.append(f"{k}: '{v}'")
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n"


def _agent_paths(agent: str, sid: str, project_root: Path | None = None):
    pr = project_root or _project_root()
    if not _valid_agent_name(agent):
        raise ValueError(f"invalid agent name: {agent!r}")
    if not _valid_sid_shape(sid):
        raise ValueError(f"invalid SID shape: {sid!r}")
    adir = _agent_dir(pr, agent)
    if not adir.is_dir():
        raise FileNotFoundError(f"agent dir does not exist: {adir}")
    session_dir = adir / _SESSIONS_DIRNAME / sid
    state_dir = adir / _STATE_DIRNAME
    return adir, session_dir, state_dir


def _read_running_sid(state_dir: Path) -> str:
    """The reducer SOT: agents/<mindKey>/session/running-session-id (or '')."""
    p = state_dir / "running-session-id"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_reducer(sid: str, agent: str, project_root: Path | None = None) -> bool:
    """True iff this unitKey holds running-session-id (the derived reducer)."""
    _, _, state_dir = _agent_paths(agent, sid, project_root)
    rsid = _read_running_sid(state_dir)
    return bool(rsid) and rsid == sid


def write_manifest(sid: str, agent: str, env_id: str = "local",
                   role: str = "worker",
                   project_root: Path | None = None) -> Path:
    """Write the Body manifest; fork the WM only for a non-reducer worker.

    Returns the manifest path. Idempotent on body_state (a re-write resets a
    Body to active — only /start calls this, once per session).
    """
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role {role!r}; expected one of {VALID_ROLES}")
    adir, session_dir, state_dir = _agent_paths(agent, sid, project_root)

    # Fork decision: a worker that is NOT the reducer forks its WM. The reducer
    # (rsid empty -> this body will claim it; OR rsid == sid -> resumed reducer)
    # uses the agent-wide WM. Observers never fork (read-only). With one Body
    # this is always False -> backward-compatible.
    rsid = _read_running_sid(state_dir)
    fork_needed = (role == "worker") and bool(rsid) and (rsid != sid)

    forked_wm_hash = None
    if fork_needed:
        agent_wm = state_dir / _WM_FILENAME
        body_wm = session_dir / _WM_FILENAME
        baseline_wm = session_dir / _BASELINE_FILENAME
        # The Mind WM at fork is this Body's baseline (the common ancestor for
        # generalize-down's 3-way delta). Empty bytes when no Mind WM exists yet.
        wm_bytes = agent_wm.read_bytes() if agent_wm.is_file() else b""
        forked_wm_hash = hashlib.sha256(wm_bytes).hexdigest()
        # cp the Mind WM as this Body's LIVE WM, BYTE-FAITHFULLY (the
        # body-WM-file's existence is what flips Phase 1A routing to the per-Body
        # path; the hash must match the copied bytes). This copy then DIVERGES
        # as the Body works.
        _write_atomic_bytes(body_wm, wm_bytes)
        # : ALSO snapshot the same fork-time bytes as an IMMUTABLE
        # baseline. The live body_wm above mutates; this copy stays the common
        # ancestor so generalize-down computes each counter's net delta
        # (reducer + (body - baseline)) instead of a baseline-double-counting SUM.
        _write_atomic_bytes(baseline_wm, wm_bytes)

    data = {
        "unitKey": sid,
        "mindKey": agent,
        "env_id": env_id,
        "role": role,
        "body_state": "active",
        "started_at": _now_iso_local(),
        "forked_wm_hash": forked_wm_hash,
    }
    manifest_path = session_dir / _MANIFEST_FILENAME
    _write_atomic(manifest_path, _render_manifest(data))
    return manifest_path


def read_manifest(sid: str, agent: str, project_root: Path | None = None) -> dict:
    """Load + return the manifest dict. Raises FileNotFoundError if absent."""
    import yaml  # local import: read/set-state need it; write/is-reducer don't.
    _, session_dir, _ = _agent_paths(agent, sid, project_root)
    manifest_path = session_dir / _MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no body-manifest: {manifest_path}")
    return yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}


def set_state(sid: str, agent: str, new_state: str,
              project_root: Path | None = None) -> Path:
    """Mutate body_state in place (preserving every other field). Returns path."""
    if new_state not in VALID_STATES:
        raise ValueError(
            f"invalid body_state {new_state!r}; expected one of {VALID_STATES}")
    data = read_manifest(sid, agent, project_root)
    data["body_state"] = new_state
    _, session_dir, _ = _agent_paths(agent, sid, project_root)
    manifest_path = session_dir / _MANIFEST_FILENAME
    _write_atomic(manifest_path, _render_manifest(data))
    return manifest_path


def close_body_on_genuine(sid: str, agent: str,
                          project_root: Path | None = None) -> str:
    """Mark a worker Body closed-pending-merge IFF this turn-end is a GENUINE close.

    Phase 2B (g-306-70): the stop-hook fires at EVERY not-runner turn-end, but a
    worker Body that does multiple work-units across turns must NOT be queued for
    merge after turn 1 — doing so loses turns 2+ of WM divergence (the reducer
    merges + marks `merged`, then the worker keeps diverging into a now-merged
    manifest that the sessions-pass never revisits). The genuine close is
    signalled by the worker writing a `body-closing` sentinel in its session dir
    when its loop truly terminates (no more work / final STOP). This helper is
    the small, testable decision the stop-hook delegates to (the stop-hook keeps
    a bash `[ -f sentinel ]` pre-guard so the dormant single-runner case stays
    zero-py3 — the sentinel never exists there).

    Returns one of:
      'no-forked-wm' — no per-Body WM file (a reducer/observer never forked) -> noop
      'no-sentinel'  — a mere between-turns turn-end (no sentinel) -> NOT closed
      'no-manifest'  — sentinel present but manifest missing (sentinel consumed) -> noop
      'not-active'   — genuine close but body_state already != active (consumed) -> noop
      'marked'       — genuine close + active -> body_state set closed-pending-merge

    The sentinel is consumed (deleted) on every genuine-close branch so a re-fire
    cannot re-mark. Idempotent and fail-safe by design.
    """
    _, session_dir, _ = _agent_paths(agent, sid, project_root)
    body_wm = session_dir / _WM_FILENAME
    if not body_wm.is_file():
        return "no-forked-wm"  # reducer/observer: never forked, nothing to close
    sentinel = session_dir / _CLOSE_SENTINEL_FILENAME
    if not sentinel.is_file():
        return "no-sentinel"  # between-turns turn-end, not a genuine close
    # Genuine close: consume the sentinel on every branch below.
    try:
        data = read_manifest(sid, agent, project_root)
    except FileNotFoundError:
        _unlink_quiet(sentinel)
        return "no-manifest"
    if data.get("body_state") != "active":
        _unlink_quiet(sentinel)
        return "not-active"  # already closed/merged -> don't re-queue
    set_state(sid, agent, "closed-pending-merge", project_root)
    _unlink_quiet(sentinel)
    return "marked"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("write", "read", "set-state", "is-reducer", "close-body-on-genuine"):
        sp = sub.add_parser(name)
        sp.add_argument("--sid", required=True)
        sp.add_argument("--agent", required=True)
        if name == "write":
            sp.add_argument("--env-id", default="local")
            sp.add_argument("--role", default="worker", choices=VALID_ROLES)
        if name == "set-state":
            sp.add_argument("state", choices=VALID_STATES)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "write":
            path = write_manifest(args.sid, args.agent, args.env_id, args.role)
            print(path)
        elif args.cmd == "read":
            print(json.dumps(read_manifest(args.sid, args.agent)))
        elif args.cmd == "set-state":
            print(set_state(args.sid, args.agent, args.state))
        elif args.cmd == "is-reducer":
            print("true" if is_reducer(args.sid, args.agent) else "false")
        elif args.cmd == "close-body-on-genuine":
            print(close_body_on_genuine(args.sid, args.agent))
    except (ValueError, FileNotFoundError) as e:
        print(f"body-manifest: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"body-manifest: io failed: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
