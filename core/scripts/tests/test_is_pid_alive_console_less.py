"""test_is_pid_alive_console_less.py — regression for the daemon orphan pileup.

Root cause (decisively probed 2026-05-28): `lifecycle.is_pid_alive()` must report
a LIVE process as alive even when the CALLER is console-less. The daemon is
spawned detached (`disown` / DETACHED_PROCESS) so it has no console. On Windows
os.kill(pid, 0) routes through GenerateConsoleCtrlEvent (signal 0 == CTRL_C_EVENT),
which fails with ERROR_INVALID_HANDLE (errno 9 / winerror 6) for a console-less
caller — so the old is_pid_alive() returned False for EVERY live process. That
false-negative defeated both orphan-prevention mechanisms that gate on this
function (the self-supersession reaper in mind_api/src/__main__.py and the
spawn-time "already running" guard via is_daemon_alive), so superseded daemons
never self-exited and accumulated (32 alive on 2026-05-28).

The fix makes is_pid_alive() console-independent on Windows (OpenProcess +
GetExitCodeProcess, WMI fallback). The decisive regression below runs the probe
from a genuinely console-less subprocess — pre-fix `live` was False, post-fix True.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # core/scripts/tests -> core/scripts -> repo root
sys.path.insert(0, str(PROJECT_ROOT))

from mind_api.src import lifecycle  # noqa: E402


def test_is_pid_alive_self_and_invalid():
    """Cross-platform sanity: this process is alive; non-positive pids are dead."""
    assert lifecycle.is_pid_alive(os.getpid()) is True
    assert lifecycle.is_pid_alive(0) is False
    assert lifecycle.is_pid_alive(-1) is False


def test_is_pid_alive_dead_pid():
    """A process that has fully exited must read as dead (cross-platform)."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    # Give the OS a moment to tear the process object down.
    time.sleep(0.3)
    assert lifecycle.is_pid_alive(p.pid) is False


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="console-less false-negative is Windows-specific (os.kill(pid,0)==CTRL_C_EVENT).",
)
def test_is_pid_alive_from_console_less_caller():
    """THE regression: a console-less caller must still see a live pid as alive.

    Spawns a known-live target, then runs is_pid_alive() from a detached,
    console-less subprocess (the faithful daemon analog) and asserts it sees the
    target as alive. Before the fix this returned False (os.kill -> errno 9), which
    is exactly what let superseded daemons survive their self-supersession check.
    """
    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(40)"])
    try:
        time.sleep(0.5)
        result_file = Path(tempfile.gettempdir()) / f"is_pid_alive_cl_{os.getpid()}.txt"
        result_file.unlink(missing_ok=True)

        child_src = textwrap.dedent(
            f"""
            import sys, ctypes
            sys.path.insert(0, r"{PROJECT_ROOT}")
            from mind_api.src import lifecycle
            has_console = bool(ctypes.windll.kernel32.GetConsoleWindow())
            live = lifecycle.is_pid_alive({target.pid})
            with open(r"{result_file}", "w") as fh:
                fh.write(f"has_console={{has_console}} live={{live}}")
            """
        )
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            [sys.executable, "-c", child_src],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            close_fds=True,
        )

        deadline = time.time() + 15
        while time.time() < deadline and not result_file.exists():
            time.sleep(0.1)
        assert result_file.exists(), "console-less child never wrote its result"
        result = result_file.read_text()
        result_file.unlink(missing_ok=True)

        assert "has_console=False" in result, f"probe was not console-less: {result!r}"
        assert "live=True" in result, (
            "REGRESSION: is_pid_alive() false-negatived a LIVE pid from a "
            f"console-less caller — the daemon-orphan-pileup bug is back: {result!r}"
        )
    finally:
        target.terminate()
