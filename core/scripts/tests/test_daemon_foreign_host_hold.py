"""test_daemon_foreign_host_hold.py —  regression test.

A pid recorded in daemon.pid can belong to a daemon on ANOTHER host: a vessel
mind-workspace keeps its state dir on a shared network filesystem. Every "is this pid stale?" decision
ran `kill -0` on THIS host, read the peer's live daemon as dead, and removed its
pid/port files (spawn.log line 256, 2026-09-25: "stale PID file ... cleaning up", 32 s
after the peer started and while it was still serving). When the number happened to
name a local process, the same pid was signalled.

The daemon now writes daemon.host (its hostname) beside the pid and refreshes that file's
mtime every 10 s. ONE shell predicate (core/scripts/_daemon_host_hold.sh) classifies the
recorded owner as local | held | stale and is sourced by BOTH spawn/kill sites:

  * core/scripts/mind-api-start.sh   (direct / hook / /start launcher)
  * core/scripts/_runtime.sh         (rt_spawn and rt_daemon_kill, the wrapper auto-respawn)

held   -> refuse: no clean, no signal, no spawn over it
stale  -> the files go, the pid is never signalled
local  -> unchanged (this includes every daemon that predates the marker)

The Python half (lifecycle.foreign_host_hold, clear_runtime_files, touch_host_marker) is
tested in mind_api/tests/test_lifecycle_ownership.py. This file pins the shell half and
the pairing between the two.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402

CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
START_SH = CORE_SCRIPTS / "mind-api-start.sh"
RUNTIME_SH = CORE_SCRIPTS / "_runtime.sh"
HOLD_SH = CORE_SCRIPTS / "_daemon_host_hold.sh"
LIFECYCLE_PY = PROJECT_ROOT / "mind_api" / "src" / "lifecycle.py"
MAIN_PY = PROJECT_ROOT / "mind_api" / "src" / "__main__.py"

THIS_HOST = socket.gethostname().strip().lower()
PEER = "peer-host-g374159"

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="drives the POSIX kill path of the launcher with a python3 PATH shim",
)


def _env(**overrides) -> dict:
    """Env with every runtime-isolation override removed, then re-applied."""
    env = dict(os.environ)
    for k in ("RT_DIR", "RUNTIME_DIR", "RT_PID_FILE", "RT_PORT_FILE", "RT_SPAWN_LOG",
              "MIND_ALLOW_SHARED_DAEMON_FROM_TEST"):
        env.pop(k, None)
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def _no_python_env(tmp_path: Path, **overrides) -> dict:
    """Env whose `python3 --version` fails, so the launcher aborts with 'no usable Python
    launcher' AFTER the clean/kill decisions under test and BEFORE it could spawn a daemon.
    Anything else still reaches the real python3."""
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    real = shutil.which("python3") or sys.executable
    (shim / "python3").write_text(
        f'#!/bin/sh\n[ "$1" = "--version" ] && exit 1\nexec "{real}" "$@"\n', encoding="utf-8")
    (shim / "python3").chmod(0o755)
    env = _env(**overrides)
    env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    return env


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _publish(rt_dir: Path, host, age_seconds: float, pid: int, port: int | None = None) -> Path:
    """The runtime files a daemon on `host` leaves, its heartbeat `age_seconds` old.
    host=None leaves no daemon.host at all: a daemon that predates the marker."""
    rt_dir.mkdir(parents=True, exist_ok=True)
    (rt_dir / "daemon.pid").write_text(f"{pid}\n", encoding="utf-8")
    (rt_dir / "daemon.port").write_text(f"{port or _free_port()}\n", encoding="utf-8")
    marker = rt_dir / "daemon.host"
    if host is not None:
        marker.write_text(host + "\n", encoding="utf-8")
        stamp = time.time() - age_seconds
        os.utime(marker, (stamp, stamp))
    return marker


class _Bystander:
    """A live local process whose pid a peer host's daemon.pid happens to name."""

    def __enter__(self):
        self.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, 0)
                break
            except OSError:
                time.sleep(0.01)
        return self.proc

    def __exit__(self, *exc):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


class _Responder:
    """A local HTTP listener answering every GET with 200. The launcher's health probe is a
    curl on 127.0.0.1:<published port>, so this is what makes a daemon.pid read as healthy."""

    def __enter__(self):
        import http.server
        import threading

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"status": "ok"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _wait_dead(proc: subprocess.Popen, seconds: float = 5.0) -> bool:
    try:
        proc.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


# ── the predicate ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("host, age, expected", [
    (None, 0, "local"),                     # predates the marker
    ("", 0, "local"),                       # empty marker names nobody
    (THIS_HOST, 0, "local"),
    (THIS_HOST.upper(), 0, "local"),        # case-insensitive, like the Python twin
    (PEER, 0, "held"),
    (PEER, 119, "held"),                    # strictly inside the window
    (PEER, 120, "stale"),                   # the edge itself: held only while age < 120, like the Python twin
    (PEER, 121, "stale"),                   # past it
    (PEER, 100000, "stale"),                # a terminated instance's files
])
def test_predicate_classifies_the_recorded_owner(tmp_path, host, age, expected):
    marker = _publish(tmp_path, host, age, pid=_dead_pid())
    if host == "":
        marker.write_text("\n", encoding="utf-8")
    proc = subprocess.run(
        [BASH, "-c", 'set -euo pipefail; source "$1"; daemon_host_hold "$2"; echo "$_dhh_state"',
         "x", HOLD_SH.as_posix(), (tmp_path / "daemon.pid").as_posix()],
        capture_output=True, text=True, timeout=30, env=_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected, (host, age, proc.stdout, proc.stderr)


# ── mind-api-start.sh ───────────────────────────────────────────────────────

@posix_only
def test_launcher_refuses_to_clean_or_spawn_over_a_live_peers_files(tmp_path):
    """THE REGRESSION: B's start finds the peer's files, reads its pid as dead, and used to
    delete them. It must exit non-zero and leave every byte alone."""
    marker = _publish(tmp_path, PEER, age_seconds=5, pid=_dead_pid())
    before = {n: (tmp_path / n).read_bytes() for n in ("daemon.pid", "daemon.port", "daemon.host")}
    mtime = marker.stat().st_mtime

    proc = subprocess.run(
        [BASH, str(START_SH)], capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT), env=_no_python_env(tmp_path, RUNTIME_DIR=tmp_path),
    )

    assert proc.returncode == 1, f"stdout={proc.stdout[:300]} stderr={proc.stderr[:600]}"
    assert "REFUSED" in proc.stderr and PEER in proc.stderr, proc.stderr[:600]
    for name, content in before.items():
        assert (tmp_path / name).read_bytes() == content, f"{name} was touched by a refused start"
    assert marker.stat().st_mtime == mtime
    log = (tmp_path / "spawn.log").read_text(encoding="utf-8")
    assert "REFUSED" in log
    assert "spawning daemon" not in log and "no usable Python launcher" not in log, (
        "the launcher went past the refusal"
    )


@posix_only
@pytest.mark.parametrize("age, refused", [(5, True), (500, False)], ids=["live-peer", "lapsed-peer"])
def test_launcher_with_a_peer_marker_but_no_pid_file(tmp_path, age, refused):
    """A peer publishes port, parent pid, host and THEN pid, so a launcher that arrives inside that
    window (or after something removed the pid file) sees a peer's marker and no pid at all. Every
    refusal branch needs a pid, and the clean below them ran unconditionally: it deleted the live
    peer's heartbeat marker and port file, then spawned a second daemon over them (fresh-eyes
    finding on g-374-159). The fresh marker must refuse; a lapsed one must still be taken over."""
    marker = _publish(tmp_path, PEER, age_seconds=age, pid=_dead_pid())
    (tmp_path / "daemon.pid").unlink()
    before = {n: (tmp_path / n).read_bytes() for n in ("daemon.port", "daemon.host")}
    mtime = marker.stat().st_mtime

    proc = subprocess.run(
        [BASH, str(START_SH)], capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT), env=_no_python_env(tmp_path, RUNTIME_DIR=tmp_path),
    )

    if refused:
        assert proc.returncode == 1, f"stdout={proc.stdout[:300]} stderr={proc.stderr[:600]}"
        assert "REFUSED" in proc.stderr and PEER in proc.stderr, proc.stderr[:600]
        for name, content in before.items():
            assert (tmp_path / name).read_bytes() == content, f"{name} was touched by a refused start"
        assert marker.stat().st_mtime == mtime
        log = (tmp_path / "spawn.log").read_text(encoding="utf-8")
        assert "REFUSED" in log
        assert "no usable Python launcher" not in log, "the launcher went past the refusal"
    else:
        assert "REFUSED" not in proc.stderr, proc.stderr[:600]
        assert "no usable Python launcher" in proc.stderr, "did not reach the spawn step"
        for name in before:
            assert not (tmp_path / name).exists(), f"{name} of a lapsed peer was kept"


@posix_only
def test_launcher_takes_over_once_the_peers_heartbeat_has_lapsed(tmp_path):
    """Instance replacement: files of a TERMINATED peer must not be held forever."""
    _publish(tmp_path, PEER, age_seconds=500, pid=_dead_pid())

    proc = subprocess.run(
        [BASH, str(START_SH)], capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT), env=_no_python_env(tmp_path, RUNTIME_DIR=tmp_path),
    )

    assert "REFUSED" not in proc.stderr, proc.stderr[:600]
    assert "no usable Python launcher" in proc.stderr, "did not reach the spawn step"
    for name in ("daemon.pid", "daemon.port", "daemon.host"):
        assert not (tmp_path / name).exists(), f"{name} of a lapsed peer was kept"


@posix_only
def test_launcher_never_signals_a_pid_a_peer_published(tmp_path):
    """A peer's pid that happens to name a LOCAL process must not be SIGTERMed or SIGKILLed."""
    with _Bystander() as bystander:
        _publish(tmp_path, PEER, age_seconds=500, pid=bystander.pid)

        proc = subprocess.run(
            [BASH, str(START_SH)], capture_output=True, text=True, timeout=60,
            cwd=str(PROJECT_ROOT), env=_no_python_env(tmp_path, RUNTIME_DIR=tmp_path),
        )

        assert not _wait_dead(bystander, 1.0), (
            f"the launcher killed a local process because a peer host's daemon.pid named it. "
            f"stderr={proc.stderr[:400]}"
        )
    log = (tmp_path / "spawn.log").read_text(encoding="utf-8")
    assert "without signalling it" in log, log[-600:]


@posix_only
@pytest.mark.parametrize("marker_host", [THIS_HOST, None], ids=["same-host", "no-marker"])
def test_control_launcher_still_recycles_a_local_unresponsive_pid(tmp_path, marker_host):
    """POSITIVE CONTROL for the test above. The same layout with a LOCAL owner (or a daemon
    that predates the marker) must still be signalled: a harness that cannot see a kill
    proves nothing about the absence of one."""
    with _Bystander() as bystander:
        _publish(tmp_path, marker_host, age_seconds=0, pid=bystander.pid)

        subprocess.run(
            [BASH, str(START_SH)], capture_output=True, text=True, timeout=60,
            cwd=str(PROJECT_ROOT), env=_no_python_env(tmp_path, RUNTIME_DIR=tmp_path),
        )

        assert _wait_dead(bystander, 5.0), "the control layout was not recycled: harness cannot see a kill"
    log = (tmp_path / "spawn.log").read_text(encoding="utf-8")
    assert "alive but not responding" in log, log[-600:]


# ── mind-api-start.sh --restart (the recycle of a HEALTHY daemon) ───────────

def _restart_env(tmp_path: Path) -> dict:
    """The recycle's two unrelated gates are switched off: the claim-liveness probe reads the
    live box's team-state, and the rate floor reads RT_DIR/last-restart."""
    return _no_python_env(tmp_path, RUNTIME_DIR=tmp_path,
                          MIND_RESTART_FORCE_STALE_CLAIM=1, MIND_RESTART_FORCE_RATE=1)


@posix_only
def test_launcher_restart_refuses_to_recycle_a_live_peers_daemon(tmp_path):
    """--restart of a HEALTHY daemon signals the pid in daemon.pid and deletes its files. The
    health probe is a curl on 127.0.0.1, so a local listener on the published port makes a peer's
    files read as a healthy daemon. A peer with a live heartbeat is not this host's to recycle."""
    with _Bystander() as bystander, _Responder() as listener:
        _publish(tmp_path, PEER, age_seconds=5, pid=bystander.pid, port=listener.port)
        before = {n: (tmp_path / n).read_bytes() for n in ("daemon.pid", "daemon.port", "daemon.host")}

        proc = subprocess.run(
            [BASH, str(START_SH), "--restart"], capture_output=True, text=True, timeout=60,
            cwd=str(PROJECT_ROOT), env=_restart_env(tmp_path),
        )

        assert proc.returncode == 1, f"stdout={proc.stdout[:300]} stderr={proc.stderr[:600]}"
        assert "REFUSED" in proc.stderr and PEER in proc.stderr, proc.stderr[:600]
        assert not _wait_dead(bystander, 1.0), "--restart killed a local process a peer's daemon.pid named"
    for name, content in before.items():
        assert (tmp_path / name).read_bytes() == content, f"{name} was touched by a refused --restart"
    assert "recycling for fresh code" not in (tmp_path / "spawn.log").read_text(encoding="utf-8")


@posix_only
def test_launcher_restart_never_signals_a_lapsed_peers_pid(tmp_path):
    """Past the hold window the files may go, but the pid number is still not this host's."""
    with _Bystander() as bystander, _Responder() as listener:
        _publish(tmp_path, PEER, age_seconds=500, pid=bystander.pid, port=listener.port)

        proc = subprocess.run(
            [BASH, str(START_SH), "--restart"], capture_output=True, text=True, timeout=60,
            cwd=str(PROJECT_ROOT), env=_restart_env(tmp_path),
        )

        assert not _wait_dead(bystander, 1.0), (
            f"--restart killed a local process a lapsed peer's daemon.pid named. stderr={proc.stderr[:400]}"
        )
    assert "REFUSED" not in proc.stderr, proc.stderr[:600]
    assert "no usable Python launcher" in proc.stderr, "did not reach the spawn step"
    for name in ("daemon.pid", "daemon.port", "daemon.host"):
        assert not (tmp_path / name).exists(), f"{name} of a lapsed peer was kept"
    assert "not signalling PID" in (tmp_path / "spawn.log").read_text(encoding="utf-8")


@posix_only
@pytest.mark.parametrize("marker_host", [THIS_HOST, None], ids=["same-host", "no-marker"])
def test_control_launcher_restart_still_recycles_a_local_daemon(tmp_path, marker_host):
    """POSITIVE CONTROL for the two tests above: the same healthy layout owned by THIS host (or
    by a daemon that predates the marker) must still be signalled, or those tests prove nothing."""
    with _Bystander() as bystander, _Responder() as listener:
        _publish(tmp_path, marker_host, age_seconds=0, pid=bystander.pid, port=listener.port)

        subprocess.run(
            [BASH, str(START_SH), "--restart"], capture_output=True, text=True, timeout=90,
            cwd=str(PROJECT_ROOT), env=_restart_env(tmp_path),
        )

        assert _wait_dead(bystander, 10.0), "the control layout was not recycled: harness cannot see a kill"
    assert "recycling for fresh code" in (tmp_path / "spawn.log").read_text(encoding="utf-8")


# ── _runtime.sh ─────────────────────────────────────────────────────────────

def _run_runtime(tmp_path: Path, call: str):
    script = f'set -u\nsource "{RUNTIME_SH.as_posix()}"\n{call}\necho "RC=$?"\n'
    return subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, timeout=60, cwd=str(PROJECT_ROOT),
        env=_env(RT_DIR=tmp_path, RT_PID_FILE=tmp_path / "daemon.pid",
                 RT_PORT_FILE=tmp_path / "daemon.port", RT_SPAWN_LOG=tmp_path / "spawn.log"),
    )


def test_rt_spawn_refuses_to_touch_a_live_peers_files(tmp_path):
    """rt_spawn must refuse, return 0 (the set -e contract), and leave the files alone."""
    marker = _publish(tmp_path, PEER, age_seconds=5, pid=_dead_pid())
    before = {n: (tmp_path / n).read_bytes() for n in ("daemon.pid", "daemon.port", "daemon.host")}

    proc = _run_runtime(tmp_path, "rt_spawn")

    assert "RC=0" in proc.stdout, f"stdout={proc.stdout[:300]} stderr={proc.stderr[:300]}"
    log = (tmp_path / "spawn.log").read_text(encoding="utf-8")
    assert "REFUSED" in log and PEER in log, log[-600:]
    assert "attempting daemon start" not in log, "rt_spawn went past the refusal"
    for name, content in before.items():
        assert (tmp_path / name).read_bytes() == content, f"{name} was touched by a refused spawn"
    assert marker.exists()


@posix_only
@pytest.mark.parametrize("marker_host, age, killed, pid_file_kept", [
    (PEER, 5, False, True),        # held: touch nothing
    (PEER, 500, False, False),     # stale: the files go, the pid is never signalled
    (THIS_HOST, 0, True, False),   # CONTROL: a local owner is still reaped
    (None, 0, True, False),        # CONTROL: so is a daemon that predates the marker
], ids=["held", "stale", "control-same-host", "control-no-marker"])
def test_rt_daemon_kill_signals_only_pids_this_host_owns(tmp_path, marker_host, age, killed, pid_file_kept):
    with _Bystander() as bystander:
        _publish(tmp_path, marker_host, age_seconds=age, pid=bystander.pid)

        proc = _run_runtime(tmp_path, "rt_daemon_kill")

        assert "RC=0" in proc.stdout, proc.stderr[:400]
        assert _wait_dead(bystander, 5.0) is killed, (
            f"rt_daemon_kill {'did not reap' if killed else 'killed'} the pid "
            f"(marker={marker_host!r}, age={age}s)"
        )
    assert (tmp_path / "daemon.pid").exists() is pid_file_kept


# ── drift guards: the pairing and the call sites ───────────────────────────

def test_hold_window_constants_pair_and_outlast_the_heartbeat():
    """_DHH_HOLD_SECONDS (shell) == HOST_HOLD_SECONDS (Python), and the window is wide enough
    that a handful of missed 10 s heartbeats cannot release a live daemon's files."""
    shell = int(re.search(r"^_DHH_HOLD_SECONDS=(\d+)", HOLD_SH.read_text(encoding="utf-8"), re.M).group(1))
    py = int(re.search(r"^HOST_HOLD_SECONDS = (\d+)", LIFECYCLE_PY.read_text(encoding="utf-8"), re.M).group(1))
    beat = int(re.search(r"^_SUPERSEDE_CHECK_SECONDS = (\d+)", MAIN_PY.read_text(encoding="utf-8"), re.M).group(1))
    assert shell == py, f"the twins drifted: shell={shell}s python={py}s"
    assert py >= 6 * beat, f"{py}s window vs {beat}s heartbeat: too tight for NFS attribute-cache lag"


def test_daemon_main_loop_beats_the_heart():
    """The heartbeat lives in the daemon's idle supersession poll. Without this call a live
    daemon's marker ages out after the hold window and a peer takes its files."""
    src = MAIN_PY.read_text(encoding="utf-8")
    loop = src.split("while not shutdown.wait(timeout=_SUPERSEDE_CHECK_SECONDS):", 1)[1].split("server.stop()", 1)[0]
    assert "lifecycle.touch_host_marker(project_root)" in loop


def test_both_chokepoints_share_one_predicate():
    for path, call in ((START_SH, 'daemon_host_hold "$PID_FILE"'), (RUNTIME_SH, 'daemon_host_hold "$RT_PID_FILE"')):
        src = path.read_text(encoding="utf-8")
        assert "_daemon_host_hold.sh" in src, f"{path.name}: lost the shared predicate"
        assert call in src, f"{path.name}: lost the classification call"
        assert "g-374-159" in src, f"{path.name}: lost the traceability marker"


def test_launcher_classifies_the_owner_before_it_probes_the_pid():
    """`held` and `stale` must be decided BEFORE `kill -0`: a peer's pid probed locally either
    reads dead (its files are deleted) or collides with an unrelated local process."""
    src = START_SH.read_text(encoding="utf-8")
    held = src.index('elif [ "$_dhh_state" = "held" ]; then')
    stale = src.index('elif [ "$_dhh_state" = "stale" ]; then')
    probe = src.index('elif _is_pid_alive "$existing_pid"; then')
    assert held < stale < probe


def test_launcher_gates_the_signal_on_the_resolved_owner():
    """Whatever sets need_recycle, only a pid THIS host published is signalled."""
    src = START_SH.read_text(encoding="utf-8")
    assert 'if [ "$need_recycle" = "1" ] && [ "$_dhh_state" = "local" ]; then' in src
