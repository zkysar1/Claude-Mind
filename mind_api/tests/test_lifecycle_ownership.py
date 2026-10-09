"""clear_runtime_files() ownership-guard regression tests.

The guard is the correctness linchpin of the self-supersession census reaper
(__main__.py): a superseded/orphan daemon must clear its OWN stale files but
must NEVER delete the pid/port that now name the LIVE successor daemon — doing
so makes the live daemon invisible to every wrapper and triggers the
orphan-respawn cascade (g-115-764). These tests pin the four cases.
"""
from __future__ import annotations

import contextlib
import gc
import os
import socket
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


# --- : the finally clause must release the SOCKET before the FILES ---
#
# Defect (guard-6154, echo/cc-03 2026-09-06): Server.start()'s finally cleared
# the discovery files while the listening socket was still bound. serve_forever()
# returning does not mean the process is dying — it runs in a background thread,
# and server.stop() (the only server_close() caller) runs LATER in the main
# thread. Result: alive process + held port + no pid/port/parent.pid = the
# guard-5681 orphan, and on a pinned port one orphan wedges every later restart.

def _wait_published(project_root, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lifecycle.is_daemon_alive(project_root):
            return True
        time.sleep(0.01)
    return False


def test_finally_releases_listening_socket_before_clearing_files(project_root):
    """Normal exit: the port is genuinely re-bindable and the files are gone.

    Re-binding the exact port is the arbiter — an assertion that the files were
    removed says nothing about whether the socket was released, which is the
    half that manufactures the orphan.
    """
    import socket
    import threading
    from mind_api.src.server import Server

    srv = Server(project_root=project_root, port=0)
    t = threading.Thread(target=srv.start, daemon=True)
    t.start()
    assert _wait_published(project_root), "daemon never published pid/port"
    port = lifecycle.read_port(project_root)

    srv._http.shutdown()          # make serve_forever() return -> finally runs
    t.join(timeout=5.0)
    assert not t.is_alive(), "server thread did not exit"

    pid_p, port_p = _files(project_root)
    assert not pid_p.exists() and not port_p.exists(), "files should be cleared"

    # THE POSITIVE CONTROL: the port must actually be free now.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))   # raises EADDRINUSE if still held
    finally:
        probe.close()


def test_runtime_files_retained_when_socket_release_fails(project_root):
    """THE NEW INVARIANT: if the socket cannot be confirmed released, the
    discovery files are KEPT so the process stays findable.

    An unreachable-but-findable daemon is recoverable — daemon-orphan-sweep.sh
    builds its keep-set from exactly these files. An unfindable one holding a
    pinned port is the total-work-stoppage class (guard-6154). If this
    assertion ever flips back to "cleared", the orphan factory is reopened.
    """
    import threading
    from mind_api.src.server import Server

    srv = Server(project_root=project_root, port=0)
    t = threading.Thread(target=srv.start, daemon=True)
    t.start()
    assert _wait_published(project_root), "daemon never published pid/port"

    real_close = srv._http.server_close

    def _boom():
        raise OSError(9, "simulated close failure")

    srv._http.server_close = _boom
    try:
        srv._http.shutdown()      # serve_forever() returns -> finally runs
        t.join(timeout=5.0)
        assert not t.is_alive(), "server thread did not exit"

        pid_p, port_p = _files(project_root)
        assert pid_p.exists(), "pid file must be RETAINED when release is unconfirmed"
        assert port_p.exists(), "port file must be RETAINED when release is unconfirmed"
    finally:
        srv._http.server_close = real_close
        with contextlib.suppress(Exception):
            real_close()


# --- : a pid published by ANOTHER host is not ours to judge --------------
#
# Defect: on a state dir shared across hosts (a vessel mind-workspace on a network filesystem) the pid in
# daemon.pid can belong to a daemon on the PEER host. is_pid_alive() runs on THIS host and
# reads that daemon as dead, so every "stale -> clear" path deleted a live peer's files
# (spawn.log: "stale PID file ... cleaning up", 32 s after the peer started). daemon.host
# names the publishing host and its mtime is the heartbeat. Shell twin and the launcher
# gates: core/scripts/tests/test_daemon_foreign_host_hold.py.

PEER_HOST = "peer-host-g374159"


def _publish_as(project_root, host, age_seconds=0.0, pid=None):
    """The runtime files a daemon on `host` leaves, with its heartbeat `age_seconds` old.

    The pid is dead on THIS host unless given: that is exactly what the peer's live
    daemon looks like from here.
    """
    lifecycle.write_pid_and_port_atomic(
        project_root, _dead_pid() if pid is None else pid, 54321)
    marker = lifecycle.host_file(project_root)
    marker.write_text(host + "\n", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(marker, (stamp, stamp))
    return marker


def _all_four(pr):
    return (lifecycle.pid_file(pr), lifecycle.port_file(pr),
            lifecycle.parent_pid_file(pr), lifecycle.host_file(pr))


def test_publish_records_this_host(project_root):
    """The daemon names itself next to its pid: that is what a peer reads."""
    lifecycle.write_pid_and_port_atomic(project_root, os.getpid(), 54321)

    assert lifecycle.host_file(project_root).read_text(encoding="utf-8").strip() \
        == socket.gethostname().strip().lower()
    assert lifecycle.read_host(project_root) == lifecycle.local_host_id()
    assert lifecycle.foreign_host_hold(project_root) is None   # our own marker holds nothing


def test_clear_preserves_a_live_peer_hosts_files(project_root):
    """THE REGRESSION: the peer's pid reads dead here, and its files must still survive."""
    _publish_as(project_root, PEER_HOST, age_seconds=5)

    assert lifecycle.foreign_host_hold(project_root) == PEER_HOST
    lifecycle.clear_runtime_files(project_root)

    pid_p, port_p = _files(project_root)
    assert pid_p.exists(), "a live peer host's pid file was deleted"
    assert port_p.exists(), "a live peer host's port file was deleted"
    assert lifecycle.host_file(project_root).exists()


def test_clear_still_removes_a_stale_same_host_pid_and_its_marker(project_root):
    """The other half of the outcome: a genuinely stale pid of THIS host is still cleaned."""
    lifecycle.write_pid_and_port_atomic(project_root, _dead_pid(), 54321)

    lifecycle.clear_runtime_files(project_root)

    for f in _all_four(project_root):
        assert not f.exists(), f"{f.name} survived a same-host stale clean"


def test_clear_takes_over_once_the_peers_heartbeat_has_lapsed(project_root):
    """Instance replacement: a terminated peer leaves files forever, they must not be held forever."""
    _publish_as(project_root, PEER_HOST, age_seconds=lifecycle.HOST_HOLD_SECONDS + 60)

    assert lifecycle.foreign_host_hold(project_root) is None
    lifecycle.clear_runtime_files(project_root)

    for f in _all_four(project_root):
        assert not f.exists(), f"{f.name} of a lapsed peer was held"


def test_hold_window_boundary(project_root):
    """Held strictly inside HOST_HOLD_SECONDS, released at it. `now` pins the clock."""
    marker = _publish_as(project_root, PEER_HOST)
    t0 = marker.stat().st_mtime

    assert lifecycle.foreign_host_hold(project_root, now=t0 + lifecycle.HOST_HOLD_SECONDS - 1) == PEER_HOST
    assert lifecycle.foreign_host_hold(project_root, now=t0 + lifecycle.HOST_HOLD_SECONDS) is None


def test_daemon_without_a_marker_keeps_the_old_behaviour(project_root):
    """Every daemon that predates the marker: a dead pid is stale, exactly as before."""
    lifecycle.write_pid_and_port_atomic(project_root, _dead_pid(), 54321)
    lifecycle.host_file(project_root).unlink()

    assert lifecycle.read_host(project_root) is None
    assert lifecycle.foreign_host_hold(project_root) is None
    lifecycle.clear_runtime_files(project_root)

    pid_p, port_p = _files(project_root)
    assert not pid_p.exists() and not port_p.exists()


def test_an_empty_marker_is_not_a_hold(project_root):
    """An empty or blank marker names nobody: do not hold on a file that says nothing."""
    marker = _publish_as(project_root, PEER_HOST)
    marker.write_text("  \n", encoding="utf-8")

    assert lifecycle.read_host(project_root) is None
    assert lifecycle.foreign_host_hold(project_root) is None


def test_host_comparison_ignores_case(project_root):
    """Windows hostnames are case-insensitive and the shell twin lowercases both sides."""
    marker = _publish_as(project_root, PEER_HOST)
    marker.write_text(lifecycle.local_host_id().upper() + "\n", encoding="utf-8")

    assert lifecycle.foreign_host_hold(project_root) is None


def test_heartbeat_refreshes_only_while_the_pid_file_names_this_process(project_root):
    """touch_host_marker keeps a live daemon visible and never refreshes a successor's marker."""
    marker = _publish_as(project_root, lifecycle.local_host_id(), age_seconds=500, pid=os.getpid())
    lifecycle.touch_host_marker(project_root)
    assert time.time() - marker.stat().st_mtime < 30, "owner's heartbeat did not refresh the marker"

    other = os.getpid() + 1          # any pid that is not this process
    _publish_as(project_root, lifecycle.local_host_id(), age_seconds=500, pid=other)
    before = marker.stat().st_mtime
    lifecycle.touch_host_marker(project_root)
    assert marker.stat().st_mtime == before, "a superseded daemon refreshed its successor's marker"


def test_real_server_publishes_the_marker_and_clears_it_on_exit(project_root):
    """End to end through Server.start(): publish writes daemon.host, the finally clause removes it."""
    import threading
    from mind_api.src.server import Server

    srv = Server(project_root=project_root, port=0)
    t = threading.Thread(target=srv.start, daemon=True)
    t.start()
    assert _wait_published(project_root), "daemon never published pid/port"
    assert lifecycle.read_host(project_root) == lifecycle.local_host_id()

    srv._http.shutdown()
    t.join(timeout=5.0)
    assert not t.is_alive(), "server thread did not exit"
    assert not lifecycle.host_file(project_root).exists(), "marker outlived its daemon"
