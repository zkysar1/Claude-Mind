"""Entry point: `python3 -m mind_api.src [--port N] [--foreground]`.

The daemon resolves PROJECT_ROOT as the parent of `mind_api/` containing this
module — same convention the existing shell scripts use. Port defaults to 0
(OS-assigned), discoverable via mind_api/state/daemon.port.

The daemon never daemonises itself. Backgrounding is the wrapper's job
(`_runtime.sh` uses `nohup` on POSIX, `start /B` on Windows). The daemon
just binds, writes PID/port, and serves until the process exits.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__
from . import lifecycle
from .server import Server

# Stale spawn-lock TTL. If a crashed previous spawn left the lock file behind,
# any spawn attempt older than this is treated as dead and the file is reused.
# 30s is much longer than a healthy spawn (≤2s for bind + ready) but short
# enough that operator-visible delays remain bounded.
_SPAWN_LOCK_STALE_SECONDS = 30

# Self-supersession poll interval (seconds). The daemon's idle main thread
# re-reads mind_api/state/daemon.pid every interval; if it no longer names this
# process a newer daemon has taken over and this one exits (orphan self-reap).
# One tiny file read per interval on an otherwise-blocked thread — far below
# any operator-perceptible orphan-pileup window.
_SUPERSEDE_CHECK_SECONDS = 10


def _project_root() -> Path:
    """Resolve PROJECT_ROOT — the directory containing `mind_api/`."""
    return Path(__file__).resolve().parent.parent.parent


@contextlib.contextmanager
def _spawn_lock(project_root: Path):
    """Serialize the is_daemon_alive() → write_pid_and_port_atomic() window.

    Closes the TOCTOU race observed in mind_api/state/daemon.log: 14 events
    between 2026-05-12 and 2026-05-14 where two daemons started within 30s
    of each other (closest 2s apart). Two concurrent rt_spawn calls both
    passed is_daemon_alive() before either had written the new PID/port,
    producing orphan daemons.

    Implementation: O_CREAT|O_EXCL on a lock file. If acquisition fails AND
    the existing lock file is older than _SPAWN_LOCK_STALE_SECONDS, the
    previous spawn is presumed dead and we steal the lock by unlinking and
    retrying. Cross-platform — no fcntl, no msvcrt, no third-party.
    """
    lock_path = lifecycle.runtime_dir(project_root) / "daemon.spawn.lock"
    acquired = False
    fd = None
    for attempt in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            acquired = True
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0
            if age > _SPAWN_LOCK_STALE_SECONDS:
                with contextlib.suppress(FileNotFoundError):
                    lock_path.unlink()
                continue  # retry the O_EXCL create
            raise RuntimeError(
                f"another daemon spawn is in progress (lock {lock_path.name} "
                f"age {age:.1f}s); refusing to race"
            )
    if not acquired:
        raise RuntimeError(f"could not acquire {lock_path.name}")
    try:
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mind_api.src",
        description="Framework runtime daemon (long-running localhost HTTP).",
    )
    parser.add_argument("--port", type=int, default=0,
                        help="Bind port (default 0 = OS-assigned)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    project_root = _project_root()

    # Acquire spawn lock BEFORE the alive-check so concurrent rt_spawn calls
    # serialize. The second caller sees the live daemon written by the first
    # and exits via the "already running" branch below.
    try:
        with _spawn_lock(project_root):
            if lifecycle.is_daemon_alive(project_root):
                port = lifecycle.read_port(project_root)
                print(f"[runtime] daemon already running on port {port}; refusing to start",
                      file=sys.stderr)
                return 2

            lifecycle.clear_runtime_files(project_root)
            server = Server(project_root=project_root, port=args.port)
            # Server.start() writes PID + port files. The spawn lock is held
            # through that write so a racing spawn sees alive() on retry.
            shutdown = threading.Event()

            # CRITICAL: serve_forever runs in a background thread so signals
            # (delivered to the main thread) can call server.stop() without
            # deadlocking. Calling ThreadingHTTPServer.shutdown() from the
            # same thread as serve_forever() deadlocks: shutdown() waits on
            # an Event that only serve_forever()'s finally clause sets, but
            # serve_forever can never run because the main thread is blocked
            # inside shutdown(). Do not "simplify" this by running
            # server.start() in the main thread.
            def _handle_signal(signum, frame):  # pragma: no cover — signals are runtime
                shutdown.set()

            # SIGHUP added to close the orphan-on-parent-exit path observed
            # in mind_api/state/daemon.log: 80 "started" events vs zero "stopped"
            # events since 2026-05-12. On Git Bash + Windows, `disown` does
            # not fully detach; parent shell exit delivers SIGHUP, which
            # without a handler kills the daemon before its "stopped" log
            # write. A handler turns SIGHUP into a graceful shutdown.
            for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):
                sig = getattr(signal, sig_name, None)
                if sig is not None:
                    try:
                        signal.signal(sig, _handle_signal)
                    except (ValueError, OSError):
                        pass

            server_thread = threading.Thread(target=server.start, daemon=True)
            server_thread.start()

            # Wait for the daemon to be FULLY published — both bind succeeded
            # AND PID/port files written to disk. Using is_daemon_alive (not
            # bare actual_port) closes the narrow race where server.start sets
            # actual_port at server.py:273 but write_pid_and_port_atomic runs at
            # line 278 — a concurrent rt_spawn racing in that 5-line window
            # would see no PID file and spawn a second daemon. Hold the spawn
            # lock through write_pid_and_port_atomic so future spawns see
            # is_daemon_alive() == True and bail via the "already running"
            # branch. DO NOT relax this back to checking actual_port alone.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if lifecycle.is_daemon_alive(project_root) or not server_thread.is_alive():
                    break
                time.sleep(0.01)

            if not lifecycle.is_daemon_alive(project_root):
                print("[runtime] failed to bind (server thread exited)", file=sys.stderr)
                return 1
    except RuntimeError as e:
        print(f"[runtime] {e}", file=sys.stderr)
        return 2

    # Spawn lock released. Serving happens outside the lock so concurrent
    # alive-check spawns see the new PID/port immediately.
    #
    # Self-supersession (the zero-risk census reaper): instead of an
    # unconditional shutdown.wait(), poll the PID file. If it names a
    # DIFFERENT, still-alive process, a newer daemon has superseded this one
    # (rt_spawn / mind-api-start.sh wrote its PID over ours) — so THIS process
    # is now an orphan and exits itself. A daemon can ONLY ever stop ITSELF
    # here, and only when a live successor positively owns the PID file, so
    # this can never kill the live daemon (its own PID is the one in the file
    # → the branch is never taken). read_pid()==None (file mid-rewrite or
    # absent) is fail-open: keep serving, re-check next tick. Pairs with the
    # clear_runtime_files() ownership guard so the superseded exit does not
    # delete the successor's PID/port files.
    while not shutdown.wait(timeout=_SUPERSEDE_CHECK_SECONDS):
        # serve_forever() died (catastrophic socket/loop failure — NOT the
        # signal/supersession path); its finally already cleared our files.
        # Exit so mind-api-start.sh respawns, instead of lingering as a hung
        # not-serving zombie (itself the orphan class  targets).
        # Symmetric with the startup readiness loop's is_alive() check — keep
        # both: a supervisor that ignores its supervised thread dying is the
        # asymmetry, not the safety.
        if not server_thread.is_alive():
            print("[runtime] server thread exited unexpectedly; shutting down",
                  file=sys.stderr)
            break
        owner = lifecycle.read_pid(project_root)
        if owner is not None and owner != os.getpid() and lifecycle.is_pid_alive(owner):
            print(f"[runtime] superseded by live pid {owner} (mine {os.getpid()}); "
                  f"shutting down", file=sys.stderr)
            break
    server.stop()
    server_thread.join(timeout=5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
