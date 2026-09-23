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


def log_event(project_root: Path, event: str, version: str, **extra: object) -> str:
    """Append one JSON lifecycle record to daemon.log and return the line.

    Single writer for the daemon.log record SHAPE. `server.py::_log_lifecycle`
    delegates here for started/stopped; `__main__.py` uses it for the spawn-time
    REFUSAL records, which until g-115-10336 went to stderr only -- i.e. into the
    spawn log, not daemon.log. That asymmetry is why a reader auditing daemon.log
    saw started events with no matching decision record explaining the gaps.

    Never raises: a lifecycle record must not be able to kill a daemon start.
    """
    import json as _json
    import time as _time

    line = _json.dumps({
        "ts": _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime()),
        "event": event,
        "version": version,
        **extra,
    }, ensure_ascii=False)
    try:
        with daemon_log(project_root).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return line


def find_unreferenced_daemon_pids(project_root: Path) -> list:
    """Live `mind_api.src` processes serving THIS project root that the published
    daemon pair does NOT name.

    The census is box-wide but the keep-set names only this root's pair, so the
    candidates are scoped to daemons whose working directory IS this root
    (`scope_pids_to_root`, g-369-417). Without that scope, on a box that runs one
    daemon per workspace (a sidecar: one for the env, one per character) whichever
    daemon starts second counted the first as unreferenced and refused to start.

    `is_daemon_alive` answers "is the REGISTERED daemon alive?" -- it reads
    daemon.pid. It cannot answer "is ANY daemon alive?", so whenever daemon.pid is
    missing, stale or unparsable beside a genuinely live daemon it returns False,
    the caller proceeds to `clear_runtime_files()`, and that live daemon becomes
    UNREFERENCED: still listening, still burning CPU, reachable by nothing. That
    is the measured g-115-10336 signature (a 17.8h orphan at 24% CPU / 4h17m CPU
    time; a later one at 3.7 GB RSS that OOM-killed a close mid-verify) and the
    guard-6980 one (ten orphans over 18h holding ~4.25 GB).

    Census via `ps -eo pid=,args=` with the match done in PYTHON, deliberately NOT
    via `pgrep -f`:
      * guard-1238 direction 1 (PHANTOM-ALIVE): a `pgrep -f mind_api.src` self-
        matches whenever the pattern is in the probing command line. Here the
        matching happens in-process and `os.getpid()` is dropped explicitly.
      * guard-1238 direction 2 (PHANTOM-DEAD): omitting `-f` matches comm only
        (`python3`), so a module-path pattern returns a confident zero. `args=`
        is the full argv surface, so the pattern and the surface agree by
        construction.
      * The shipped sweeps key on `pgrep -f 'python.* -m mind_api\\.src'`, which
        requires the argv to begin with `python`; a `py -3 -m mind_api.src` launch
        does not match it. Matching on the module token alone has no such gap.

    Returns a sorted list of ints. Empty on any probe failure -- the caller must
    not refuse a start because `ps` was unavailable (fail-open; guard-1562).
    """
    import subprocess  # local: keeps the import off the module-load hot path

    keep = {os.getpid()}
    for reader in (read_pid, read_parent_pid):
        with contextlib.suppress(Exception):
            v = reader(project_root)
            if v:
                keep.add(int(v))

    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:  # noqa: BLE001 -- no ps (Windows), timeout, permissions
        return []

    return scope_pids_to_root(parse_ps_for_daemon_pids(out, keep), project_root, pid_cwd)


def scope_pids_to_root(pids, project_root: Path, cwd_of) -> list:
    """Keep only the candidates whose working directory IS `project_root` ().

    Both spawners `cd` into the project root before `-m mind_api.src`, and the
    sidecar unit sets WorkingDirectory to its workspace, so a daemon's cwd names
    the root it serves. A same-root daemon whose daemon.pid is missing or stale
    (the g-115-10336 signature) is still kept, so the start is still refused.

    `cwd_of(pid)` returns the kernel's cwd path, or None when it cannot be read.
    An unreadable cwd is NOT counted: over-matching here refuses a start, which is
    an outage; under-matching is the orphan leak the guard already tolerated. So
    on a platform with no /proc this census finds nothing (fail-open), as it
    already did where `ps` is missing. A cwd reading '<path> (deleted)' never
    equals a live root, so a removed-worktree orphan is left to the orphan sweep.
    """
    root = os.path.realpath(str(project_root))
    return [pid for pid in pids if cwd_of(pid) == root]


def pid_cwd(pid: int) -> Optional[str]:
    """The working directory of `pid` as the kernel reports it, or None when it is
    unreadable (no /proc on this platform, the process exited, or permission)."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def parse_ps_for_daemon_pids(ps_output: str, keep) -> list:
    """Pure parse half of `find_unreferenced_daemon_pids` -- the matching rule.

    Split out so the rule can be pinned against FIXED `ps` text: the live call
    above can only ever assert against whatever happens to be running on the box,
    and an always-empty matcher passes that assertion identically to a working one
    (the guard-1715 / rb-245 shape -- a zero from an empty population reads the
    same as a clean scan). The regression test drives this function with a fixture
    containing a self-match line, a `py -3` launch and a bare mention, none of
    which can be produced on demand from a live process table.
    """
    keep = set(keep or ())
    found = []
    for raw in ps_output.splitlines():
        line = raw.strip()
        if not line:
            continue
        head, _, args = line.partition(" ")
        # Only an actual module launch counts. A `grep mind_api.src`, an editor
        # holding the file open, or the census command itself all NAME the module
        # without being a daemon -- matching those is guard-1238's PHANTOM-ALIVE
        # direction, and here it would refuse every legitimate start.
        # The token must END the argument -- end-of-string or a following space.
        # A bare substring test matches `-m mind_api.srcfoo` too (measured: it
        # returned that pid), and because this census REFUSES a start, one
        # false positive refuses EVERY start on the box. Severity is inverted
        # from the usual matcher: over-matching here is an outage, under-matching
        # is the orphan leak we already had.
        marker = " -m mind_api.src"
        idx = args.find(marker)
        if idx < 0:
            continue
        tail = args[idx + len(marker):]
        if tail and not tail[0].isspace():
            continue
        try:
            pid = int(head)
        except ValueError:
            continue
        if pid in keep:
            continue
        found.append(pid)
    return sorted(found)


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
