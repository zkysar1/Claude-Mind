"""Daemon lifecycle helpers: atomic PID/port files, liveness probe, free-port pick.

The "is the daemon ready" contract: BOTH `daemon.pid` AND `daemon.port` exist
AND the PID is alive AND a TCP connection to the port succeeds. The wrapper
side uses just file presence + the curl probe; the kill-by-PID recovery path
uses is_pid_alive.

Atomicity is via temp + os.replace, which is atomic on POSIX and on NTFS/Win32
since Windows Vista. This matters when one session bounces the daemon while
another's first call observes the port-file mid-write.
"""
from __future__ import annotations

import contextlib
import errno
import os
import socket
from pathlib import Path
from typing import Optional


# --- File locations --------------------------------------------------------
# All under PROJECT_ROOT/mind_api/state/ — gitignored, per-repo. One daemon per
# repo (Decision 2 in the handoff). Different machines / repos get different
# dirs. Plan v1 step 2.4 (2026-05-19): relocated from PROJECT_ROOT/.runtime
# to PROJECT_ROOT/mind_api/state as part of the daemon/ consolidation. See the
# daemon-only-architecture.md tree node "Phase 2" section.

def runtime_dir(project_root: Path) -> Path:
    """Return the runtime state directory. Creates it if missing."""
    d = project_root / "mind_api" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_file(project_root: Path) -> Path:
    return runtime_dir(project_root) / "daemon.pid"


def port_file(project_root: Path) -> Path:
    return runtime_dir(project_root) / "daemon.port"


def daemon_log(project_root: Path) -> Path:
    return runtime_dir(project_root) / "daemon.log"


def access_log(project_root: Path) -> Path:
    return runtime_dir(project_root) / "access.log"


# --- Atomic writes ---------------------------------------------------------

def _atomic_write_text(target: Path, content: str) -> None:
    """Write `content` to `target` atomically via temp + os.replace.

    os.replace is atomic on POSIX and on NTFS since Vista. The temp file
    lives in the same directory so the rename stays within one filesystem.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def write_pid_and_port_atomic(project_root: Path, pid: int, port: int) -> None:
    """Write PID and PORT files atomically."""
    # Order matters: write port first, then PID. is_daemon_alive() requires
    # BOTH files to exist before reading PID — port-first means a partial
    # publish (port written, PID not yet) is seen as "not alive" rather than
    # "alive but at an unknown PID."
    _atomic_write_text(port_file(project_root), f"{port}\n")
    _atomic_write_text(pid_file(project_root), f"{pid}\n")


def clear_runtime_files(project_root: Path) -> None:
    """Remove the PID + port files — but ONLY when they are not owned by a
    DIFFERENT live daemon.

    CRITICAL — do not remove this ownership guard. A superseded/orphan daemon
    runs this on its way out (server.py serve_forever() finally clause). With
    an unconditional delete it would erase the pid/port that now name the LIVE
    successor daemon, making the live daemon invisible to every wrapper and
    triggering exactly the orphan-respawn cascade this guards against
    (g-115-764). Absent files, or a stale file whose pid is dead, are still
    cleared (the normal pre-spawn / SIGTERM cleanup). Only a file naming a
    different, still-alive process is left untouched. Same liveness invariant
    as the __main__.py self-supersession check: yield to a LIVE successor only.

    The read_pid()->unlink() window is a check-act TOCTOU in isolation, but
    every real caller is causally serialized so it is unreachable: self-
    supersession writes the successor's pid BEFORE the orphan begins exiting;
    mind-api-start.sh confirms the predecessor dead before respawn; concurrent
    spawns serialize on _spawn_lock. Do NOT add locking here — it would guard
    an interleaving the orchestration already prevents.
    """
    owner = read_pid(project_root)
    if owner is not None and owner != os.getpid() and is_pid_alive(owner):
        return
    for f in (pid_file(project_root), port_file(project_root)):
        with contextlib.suppress(FileNotFoundError):
            f.unlink()


# --- Liveness probes -------------------------------------------------------

def is_pid_alive(pid: int) -> bool:
    """Return True if `pid` is a live process on this machine.

    On POSIX: signal 0 (no-op) raises ESRCH if no such process, EPERM if it
    exists but we don't own it. EPERM still means "alive."
    On Windows: os.kill(pid, 0) succeeds for any existing process regardless
    of ownership, and raises OSError(EINVAL) for missing PIDs.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        if e.errno == errno.EPERM:
            return True  # exists, not ours
        # On Windows, missing PID raises EINVAL or "no such process" via WinError.
        # Treat unknown errno as "not alive" — safer than claiming a stale PID.
        return False
    return True


def is_daemon_alive(project_root: Path) -> bool:
    """Return True iff both PID + port files exist AND the PID is alive."""
    pid_p = pid_file(project_root)
    port_p = port_file(project_root)
    if not pid_p.exists() or not port_p.exists():
        return False
    try:
        pid = int(pid_p.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    return is_pid_alive(pid)


def read_port(project_root: Path) -> Optional[int]:
    """Read the port file. Returns None if missing or unparsable."""
    p = port_file(project_root)
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def read_pid(project_root: Path) -> Optional[int]:
    p = pid_file(project_root)
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


# --- Port selection --------------------------------------------------------

def pick_free_port() -> int:
    """Bind to 127.0.0.1:0 and read back the OS-assigned port.

    Closing the socket releases the port; the daemon's server will rebind it
    immediately. There is a microscopic race where another process snatches
    the port between close and rebind. Acceptable for a dev tool — the
    daemon would surface OSError and exit, the wrapper logs and falls back.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()
