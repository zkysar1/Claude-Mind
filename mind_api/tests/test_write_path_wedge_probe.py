"""The write-path wedge probe ().

WHY THIS EXISTS: GET /v1/admin/health answered 200 in 0.2ms throughout a
total box-wide WM-write freeze, so mind-api-start.sh's idempotent "alive and
responsive -> exit 0" path repaired nothing — a daemon can be liveness-green
and write-dead at the same instant. `file_locks.locked()` takes an UNBOUNDED
`thread_lock.acquire()`, so a holder that never releases parks every other
writer with no surface reporting it.

guard-5163 governs these tests: a discriminator is only shipped when a
fixture exists for EACH state the old signal conflated, the OLD signal is
asserted IDENTICAL across them, and the NEW field is asserted to DIFFER. Both
halves are here on purpose — without the "identical old value" assertion a
future edit could make /health itself flip and nobody would notice the probe
had stopped being the thing under test.
"""
from __future__ import annotations

import builtins
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from mind_api.src import file_locks


# ─── helpers ─────────────────────────────────────────────────────────────────

class _Wedge:
    """Hold file_locks.locked(path) until released — the measured failure."""

    def __init__(self, path):
        self.path = path
        self._release = threading.Event()
        self._held = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with file_locks.locked(self.path):
            self._held.set()
            self._release.wait(60)

    def __enter__(self):
        self._t.start()
        assert self._held.wait(10), "wedge never acquired the lock"
        return self

    def __exit__(self, *exc):
        self._release.set()
        self._t.join(15)
        return False


@pytest.fixture
def victim(tmp_path):
    p = tmp_path / "victim.jsonl"
    p.write_text("{}\n", encoding="utf-8")
    return p


# ─── the discriminator (guard-5163) ──────────────────────────────────────────

def test_idle_write_path_is_not_wedged():
    status = file_locks.write_path_status(wedge_seconds=0.2)
    assert status["wedged"] is False
    assert status["holds_in_flight"] == 0
    assert status["blocked_writers"] == 0
    assert status["longest_hold_path"] is None


def test_held_lock_past_threshold_reads_wedged(victim):
    with _Wedge(victim):
        time.sleep(0.35)
        status = file_locks.write_path_status(wedge_seconds=0.2)
        assert status["wedged"] is True
        assert status["holds_in_flight"] == 1
        assert status["longest_hold_path"] == str(victim.resolve())
        assert status["longest_hold_s"] >= 0.2


def test_held_lock_under_threshold_is_not_wedged(victim):
    """A hold in flight is not a wedge — an ordinary write must never trip it.

    The fail-safe direction matters: a false positive restarts a daemon that
    is serving every agent on the box.
    """
    with _Wedge(victim):
        status = file_locks.write_path_status(wedge_seconds=30.0)
        assert status["wedged"] is False
        assert status["holds_in_flight"] == 1


def test_blocked_writers_are_counted(victim):
    with _Wedge(victim):
        entered = threading.Event()

        def _blocked():
            entered.set()
            with file_locks.locked(victim):
                pass

        t = threading.Thread(target=_blocked, daemon=True)
        t.start()
        assert entered.wait(5)
        time.sleep(0.3)
        assert file_locks.write_path_status(wedge_seconds=0.1)["blocked_writers"] >= 1
    t.join(15)


def test_release_leaks_neither_hold_nor_waiter(victim):
    with _Wedge(victim):
        time.sleep(0.25)
        assert file_locks.write_path_status(wedge_seconds=0.1)["wedged"] is True
    time.sleep(0.1)
    after = file_locks.write_path_status(wedge_seconds=0.1)
    assert after["wedged"] is False
    assert after["holds_in_flight"] == 0
    assert after["blocked_writers"] == 0


def test_waiter_count_does_not_drift_across_many_acquires(victim):
    """The waiter counter decrements on BOTH paths. A counter that only
    decremented on success would drift upward forever and eventually report a
    wedge that is not there."""
    for _ in range(50):
        with file_locks.locked(victim):
            pass
    assert file_locks.write_path_status()["blocked_writers"] == 0
    assert file_locks.write_path_status()["holds_in_flight"] == 0


def test_default_threshold_is_a_minute():
    """No legitimate hold survives a minute: the file-lock timeout is 10s and
    the write queue steals a turn after 30s. Pinned so a future edit cannot
    quietly lower it into false-positive territory."""
    assert file_locks.WEDGE_SECONDS == 60.0


# ─── outcome 3: the probe adds no store round trip ───────────────────────────

def test_probe_performs_no_io(victim):
    """rt_ensure_running's only added work is this call inside /health. If it
    ever opens a file or a socket, the store-independence of /health is gone
    (ready.py: "DO NOT add store checks to /health")."""
    counts = {"open": 0, "stat": 0, "socket": 0}
    real_open, real_stat, real_socket = builtins.open, os.stat, socket.socket

    def _wrap(name, fn):
        def inner(*a, **k):
            counts[name] += 1
            return fn(*a, **k)
        return inner

    builtins.open = _wrap("open", real_open)
    os.stat = _wrap("stat", real_stat)
    socket.socket = _wrap("socket", real_socket)
    try:
        for _ in range(200):
            file_locks.write_path_status()
        observed = dict(counts)
        # positive control (guard-2421): a zero is only believable when the
        # instrument is shown to fire.
        counts.update(open=0, stat=0, socket=0)
        with builtins.open(victim, "r", encoding="utf-8") as fh:
            fh.read()
        os.stat(victim)
        control = dict(counts)
    finally:
        builtins.open, os.stat, socket.socket = real_open, real_stat, real_socket

    assert observed == {"open": 0, "stat": 0, "socket": 0}
    assert control["open"] >= 1 and control["stat"] >= 1


# ─── the endpoints that render the verdict ───────────────────────────────────

def _get(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    req.add_header("X-Mind-Agent", "alpha")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_health_publishes_the_write_path_verdict(running_daemon):
    _, port = running_daemon
    status, body = _get(port, "/v1/admin/health")
    assert status == 200
    assert body["write_path_wedged"] is False
    assert body["write_path"]["holds_in_flight"] == 0


def test_health_flips_and_ready_503s_while_wedged(running_daemon, victim,
                                                  monkeypatch):
    """The whole point, at the surface a consumer reads.

    guard-5163: the OLD signal (health answers 200, ok:true) must be
    IDENTICAL either side of the wedge — it is exactly that invariance that
    made the freeze undetectable — while the NEW field differs.
    """
    _, port = running_daemon
    monkeypatch.setattr(file_locks, "WEDGE_SECONDS", 0.2)

    before_status, before = _get(port, "/v1/admin/health")
    assert (before_status, before["ok"], before["write_path_wedged"]) == (200, True, False)
    assert _get(port, "/v1/admin/ready")[0] == 200

    with _Wedge(victim):
        time.sleep(0.4)
        during_status, during = _get(port, "/v1/admin/health")
        ready_status, ready_body = _get(port, "/v1/admin/ready")

    # OLD signal: identical. NEW field: differs.
    assert (during_status, during["ok"]) == (before_status, before["ok"])
    assert during["write_path_wedged"] is True
    assert during["write_path"]["longest_hold_path"] == str(victim.resolve())
    assert ready_status == 503
    assert ready_body["ready"] is False
    assert ready_body["store_check"] == "skipped-write-path-wedged"

    time.sleep(0.1)
    after_status, after = _get(port, "/v1/admin/health")
    assert (after_status, after["ok"]) == (before_status, before["ok"])
    assert after["write_path_wedged"] is False
    assert _get(port, "/v1/admin/ready")[0] == 200
