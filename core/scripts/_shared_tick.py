#!/usr/bin/env python3
"""Shared-heartbeat tick plumbing — ONE interval, ONE pytest chokepoint, ONE spawn.

`heartbeat-tick.sh` owns every liveness leg (per-Body carrier, agent-wide
runner-heartbeat, team-state last_active, runner-claim renewal, reducer
self-fence). This module owns what a CALLER of that script needs and must not
re-derive on its own:

  * SHARED_HEARTBEAT_INTERVAL_S and the `due()` predicate over a stamp file;
  * the pytest chokepoint (g-115-5310): the tick's team-state leg is the
    phantom-shard writer, so under pytest it fires only with the explicit opt-in;
  * `spawn_detached()`, the fire-and-forget form a hook may use — a PreToolUse
    hook sits on the critical path of EVERY tool call, and a tick that waits on a
    slow daemon would time the hook out and drop the MIND_AGENT injection for
    that call (guard-1562 shape: a liveness courtesy must never block work).

Callers — keep this list current, it is the caller inventory (g-115-8200):
  * core/scripts/execution-diary.py `_tick_shared_heartbeat_if_due` — on every
    diary write, synchronous (g-306-233).
  * core/scripts/bash-agent-inject.py `_maybe_tick_heartbeat` — before every
    Bash tool call, detached. This is what makes a runner's freshness
    independent of its iteration length: a served 27B whose precheck alone ran
    past OWNERSHIP_STALE_SECONDS read as a crashed reducer, and its worker Body
    parked (measured 2026-08-28, coach on zc-03).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# How long either liveness signal may go unrefreshed before a caller fires a
# tick. Must stay FAR below BOTH windows it protects, so a tick can fail several
# times over and still be retried inside the shorter one:
#   reducer — OWNERSHIP_STALE_SECONDS      3900s (peer may break the claim)
#   worker  — foreign-SID grace           7200s (sweep pops the claim)
SHARED_HEARTBEAT_INTERVAL_S = 600

# Lines kept when the detached tick's log is rotated (same shape as
# bash-inject-misses.jsonl: bounded, never a growing file).
_LOG_KEEP_LINES = 200
_LOG_ROTATE_BYTES = 100_000


def pytest_suppressed() -> bool:
    """True when a tick must NOT fire because this process is a pytest test.

    The chain is heartbeat-tick.sh -> team-state-update.sh -> daemon shard
    write, and the DAEMON resolves its own world path, so no env var set in a
    test process can redirect it: a test that reaches the tick materialises a
    REAL row in the live fleet roster. The opt-in exists for the one suite that
    stages a relocated PROJECT_ROOT with a stub recorder in place of the tick.
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and not os.environ.get(
        "MIND_DIARY_SHARED_TICK_TEST")


def due(*stamps: Path, now: float | None = None) -> bool:
    """True when EVERY stamp is at least the interval old (a missing stamp is
    infinitely old). Passing two stamps lets two callers share one window."""
    t = time.time() if now is None else now
    for stamp in stamps:
        try:
            if t - stamp.stat().st_mtime < SHARED_HEARTBEAT_INTERVAL_S:
                return False
        except OSError:
            continue
    return True


def _rotate(log_path: Path) -> None:
    try:
        if log_path.exists() and log_path.stat().st_size > _LOG_ROTATE_BYTES:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > _LOG_KEEP_LINES:
                log_path.write_text("\n".join(lines[-_LOG_KEEP_LINES:]) + "\n",
                                    encoding="utf-8")
    except Exception:
        pass


def spawn_detached(script_dir: Path, agent: str, sid: str, *, body_only: bool,
                   log_path: Path, cwd: Path) -> None:
    """Fire heartbeat-tick.sh and return without waiting.

    The child runs in its own session (POSIX) / detached process group
    (Windows) so it outlives the hook process that spawned it, and its stdout +
    stderr land in `log_path` — never on the hook's stdout, which is the hook
    JSON channel. `--body-only` refreshes only this SID's per-Body carrier and
    exits before the agent-wide runner signal, which only the reducer may
    advance. Fail-open on every path: a liveness courtesy must never block the
    tool call it rides on.
    """
    try:
        from _runtime_bash import bash_cmd  # guard-580: never a bare "bash" argv[0]

        env = dict(os.environ)
        env["MIND_AGENT"] = agent
        env["MIND_SID"] = sid
        args = ["--body-only"] if body_only else []
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(log_path)
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} tick sid={sid} "
                      f"{'body-only' if body_only else 'full'}\n")
            log.flush()
            subprocess.Popen(
                bash_cmd(str(Path(script_dir) / "heartbeat-tick.sh"), *args),
                env=env, cwd=str(cwd), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, close_fds=True, **kwargs)
    except Exception:
        pass
