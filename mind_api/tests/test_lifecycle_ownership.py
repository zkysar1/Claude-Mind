"""clear_runtime_files() ownership-guard regression tests.

The guard is the correctness linchpin of the self-supersession census reaper
(__main__.py): a superseded/orphan daemon must clear its OWN stale files but
must NEVER delete the pid/port that now name the LIVE successor daemon — doing
so makes the live daemon invisible to every wrapper and triggers the
orphan-respawn cascade (g-115-764). These tests pin the four cases.
"""
from __future__ import annotations

import gc
import os
import subprocess
import sys
import time

from mind_api.src import lifecycle


def _dead_pid() -> int:
    """A pid that is_pid_alive() genuinely reports dead.

    On Windows, subprocess.Popen holds the child's process handle open until
    the object is finalized, and OpenProcess (hence os.kill(pid,0)) succeeds
    on a terminated-but-handle-held process. Drop the ref + gc so the handle
    closes, then poll until the OS releases the pid.
    """
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
    proc.wait()
    pid = proc.pid
    # del + gc is the SINGLE mechanism that releases the Windows process
    # handle (Popen finalizer → _handle.Close()); do not add a redundant
    # proc.__exit__()/close() — with no pipes it only re-wait()s.
    del proc
    gc.collect()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and lifecycle.is_pid_alive(pid):
        time.sleep(0.02)
    assert not lifecycle.is_pid_alive(pid), "could not obtain a genuinely dead pid"
    return pid


def _files(pr):
    return lifecycle.pid_file(pr), lifecycle.port_file(pr)


def test_clear_removes_own_files(project_root):
    """Normal SIGTERM/stop path: the pid file names us → both files cleared."""
    lifecycle.write_pid_and_port_atomic(project_root, os.getpid(), 54321)
    pid_p, port_p = _files(project_root)
    assert pid_p.exists() and port_p.exists()

    lifecycle.clear_runtime_files(project_root)

    assert not pid_p.exists()
    assert not port_p.exists()


def test_clear_removes_stale_dead_pid_files(project_root):
    """Pre-spawn cleanup: a crashed daemon left a dead-pid file → cleared."""
    lifecycle.write_pid_and_port_atomic(project_root, _dead_pid(), 54321)

    lifecycle.clear_runtime_files(project_root)

    pid_p, port_p = _files(project_root)
    assert not pid_p.exists()
    assert not port_p.exists()


def test_clear_preserves_foreign_live_pid_files(project_root):
    """THE linchpin: files naming a DIFFERENT live process are left intact.

    An orphan running clear_runtime_files() on its way out must not erase the
    successor daemon's pid/port. If this assertion ever flips, the orphan
    self-reap will black-hole the live daemon.
    """
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        # Spin until the child is actually scheduled/alive.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not lifecycle.is_pid_alive(live.pid):
            time.sleep(0.01)
        assert lifecycle.is_pid_alive(live.pid)
        assert live.pid != os.getpid()

        lifecycle.write_pid_and_port_atomic(project_root, live.pid, 54321)
        lifecycle.clear_runtime_files(project_root)

        pid_p, port_p = _files(project_root)
        assert pid_p.exists(), "orphan deleted the live successor's pid file"
        assert port_p.exists(), "orphan deleted the live successor's port file"
        assert lifecycle.read_pid(project_root) == live.pid
    finally:
        live.terminate()
        live.wait()


def test_clear_is_noop_when_absent(project_root):
    """No files present → no error (idempotent)."""
    lifecycle.clear_runtime_files(project_root)  # must not raise
    pid_p, port_p = _files(project_root)
    assert not pid_p.exists()
    assert not port_p.exists()
