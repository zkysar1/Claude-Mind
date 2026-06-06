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

from .agent_paths import assert_not_cruft


# --- File locations --------------------------------------------------------
# All under PROJECT_ROOT/mind_api/state/ — gitignored, per-repo. One daemon per
# repo (Decision 2 in the handoff). Different machines / repos get different
# dirs. Plan v1 step 2.4 (2026-05-19): relocated from PROJECT_ROOT/.runtime
# to PROJECT_ROOT/mind_api/state as part of the daemon/ consolidation. See the
# daemon-only-architecture.md tree node "Phase 2" section.

def runtime_dir(project_root: Path) -> Path:
    """Return the runtime state directory. Creates it if missing.

    Honors RUNTIME_DIR (B16): an absolute override points the daemon's
    runtime files (daemon.pid/port/parent.pid, logs) at an ISOLATED directory so
    a subprocess-spawning daemon-integration test never hijacks the live daemon's
    PROJECT_ROOT/mind_api/state — the daemon-storm failure mode where two daemons
    fight over daemon.port (2026-05-31). Unset (the production default) ->
    PROJECT_ROOT/mind_api/state, byte-identical to prior behavior, so the override
    is dormant for the live daemon. Same env contract as
    core/scripts/owncloud_sync.py and mind-api-start.sh's RT_DIR."""
    override = os.environ.get("RUNTIME_DIR")
    d = Path(override) if override else (project_root / "mind_api" / "state")
    assert_not_cruft(d, "mkdir (runtime_dir)")
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_file(project_root: Path) -> Path:
    return runtime_dir(project_root) / "daemon.pid"


def port_file(project_root: Path) -> Path:
    return runtime_dir(project_root) / "daemon.port"


def parent_pid_file(project_root: Path) -> Path:
    # Stores the py.exe launcher PID (Windows) or shell PID (POSIX) — the
    # PROCESS PARENT of the daemon at startup, as reported by os.getppid().
    # Read by kill paths so they can force-kill the parent without a
    # Win32 Get-CimInstance.ParentProcessId lookup (which silently no-ops
    # when the child has already exited — the root cause of 's
    # 17% kill-failure rate). The kill path still does a CommandLine
    # sanity check on the PID before Stop-Process to guard against PID
    # reuse.
    return runtime_dir(project_root) / "daemon.parent.pid"


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
    assert_not_cruft(target.parent, "mkdir (_atomic_write_text)")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def write_pid_and_port_atomic(project_root: Path, pid: int, port: int,
                              parent_pid: Optional[int] = None) -> None:
    """Write PID, PORT, and (optionally) parent-PID files atomically.

    Order matters: write port FIRST, then parent_pid, then PID. is_daemon_alive()
    requires BOTH port + pid files to exist before reading PID — port-first
    means a partial publish (port written, PID not yet) is seen as "not alive"
    rather than "alive but at an unknown PID." The parent_pid file is written
    between port and PID so by the time PID is published (and is_daemon_alive
    starts returning True), parent_pid is also already on disk for the kill
    path to read.

    parent_pid==None or parent_pid<=0 is treated as "no parent to track" and
    the parent.pid file is removed (so a recycle path doesn't read a stale
    value from a prior daemon). Callers on Windows should pass os.getppid()
    (the py.exe launcher); callers running the daemon foreground in a
    debugger / pytest may pass None.
    """
    _atomic_write_text(port_file(project_root), f"{port}\n")
    if parent_pid is not None and parent_pid > 0:
        _atomic_write_text(parent_pid_file(project_root), f"{parent_pid}\n")
    else:
        with contextlib.suppress(FileNotFoundError):
            parent_pid_file(project_root).unlink()
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
    for f in (pid_file(project_root), port_file(project_root),
              parent_pid_file(project_root)):
        with contextlib.suppress(FileNotFoundError):
            f.unlink()


# --- Liveness probes -------------------------------------------------------

def is_pid_alive(pid: int) -> bool:
    """Return True if `pid` is a live process on this machine.

    POSIX: signal 0 (no-op) raises ESRCH if no such process, EPERM if it
    exists but we don't own it. EPERM still means "alive."

    Windows: os.kill(pid, 0) is NOT a liveness probe. Signal 0 is CTRL_C_EVENT,
    so os.kill routes through GenerateConsoleCtrlEvent, which raises
    OSError(errno 9 / winerror 6 ERROR_INVALID_HANDLE) whenever the *calling*
    process has no console. The daemon is always spawned detached (`disown` /
    DETACHED_PROCESS) and is therefore console-less — so os.kill(pid, 0) reports
    every live process as DEAD. That false-negative silently defeated BOTH
    orphan-prevention mechanisms that gate on this function — the runtime
    self-supersession reaper (__main__.py) and the spawn-time "already running"
    refusal (is_daemon_alive) — so superseded daemons never self-exited and piled
    up (32 alive on 2026-05-28; g-115-764 lineage). Probe via OpenProcess instead
    (console-independent). Verified by a console-less-caller probe: os.kill(live,
    0) -> errno 9/winerror 6, OpenProcess(live) -> alive. The ctypes OpenProcess
    path (no subprocess) is the primary Windows probe rather than the os.kill+WMI
    shape of core/scripts/background-jobs.py pid_alive, because in the
    console-less daemon os.kill ALWAYS fails — an os.kill-first design would spawn
    a PowerShell/WMI subprocess on every supersession poll. WMI is kept only as a
    fallback for OpenProcess failures (e.g. cross-elevation access-denied).
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        if e.errno == errno.EPERM:
            return True  # exists, not ours
        return False
    return True


def _win_pid_alive(pid: int) -> bool:
    """Console-independent Windows liveness probe (see is_pid_alive rationale).

    Primary: OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess
    — pure ctypes, no subprocess, no console dependency. A handle that opens AND
    whose exit code is STILL_ACTIVE (259) means alive; a readable non-259 exit
    code means the process has exited. If OpenProcess fails (process gone OR
    access-denied — indistinguishable from the return value alone), defer to the
    authoritative WMI fallback. Fail-open direction matches the original "safer
    to under-report dead than claim a stale PID" intent.
    """
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE,
                                                ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # Process gone, or unopenable (access-denied). WMI answers either way.
            return _win_pid_alive_wmi(pid)
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True  # handle opened but exit code unreadable -> assume alive
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return _win_pid_alive_wmi(pid)


def _win_pid_alive_wmi(pid: int) -> bool:
    """WMI Win32_Process liveness fallback. Mirrors core/scripts/background-jobs.py
    _win_pid_exists — duplicated rather than imported because mind_api/src
    (Layer 1) must not import from core/scripts (see core/BOUNDARY.md). Returns
    False on any query failure (under-report rather than claim a stale PID)."""
    import subprocess
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"if (Get-CimInstance Win32_Process -Filter 'ProcessId={pid}') "
             f"{{ 'ALIVE' }} else {{ 'DEAD' }}"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and "ALIVE" in result.stdout
    except Exception:
        return False


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


def read_parent_pid(project_root: Path) -> Optional[int]:
    """Read the daemon's launcher (py.exe) PID written at spawn.

    Used by kill paths to force-kill the parent without a Win32 lookup that
    silently fails when the child has exited. None when the file is absent
    (older daemon, daemon spawned without a launcher, foreground/debugger
    run) — kill paths treat None as "skip parent kill, child kill alone is
    safe."
    """
    p = parent_pid_file(project_root)
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
