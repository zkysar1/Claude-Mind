"""run-full-suite refuses a pinned worktree beside a live daemon, and treats
its shared log dir as shared (g-115-8901).

WHY THIS FILE EXISTS. guard-5866 was created 2026-09-03T04:29 and the run that
tripped it launched ~12h LATER. The knowledge was not missing; the always-on
PreToolUse block that exists to carry it (_full_suite_imperative.py) lagged the
guardrail by 29 HOURS. Hand-maintained prose in a second file is a delivery
channel that fails silently, so the condition is decided in the tool. These
tests pin the decision, not the prose.

Two properties matter more than the happy path, and each has its own negative
control here:

  * FAIL-OPEN (guard-142). A gate that blocks work because of its own bug is
    worse than the problem it catches. Every probe error must read as "not
    established", never as a refusal.
  * THE WINDOWS LIVENESS SHAPE. os.kill(dead_pid, 0) raises a bare OSError on
    Windows, so tree_lock._pid_alive returns None -- NOT False -- for a dead
    holder (verified on this box: _pid_alive(999999) -> None). A lock that only
    broke on an explicit False would wedge every future run on the platform
    this runner is most used on. test_unknown_liveness_past_the_ttl pins the
    TTL fallback that prevents it.

COLLECTION-SAFETY: importlib-loads the hyphenated module; every test is pure or
tmp_path-scoped. No live daemon is contacted, no suite is spawned, and the two
main() tests monkeypatch the step after the gate so a bypass is proved by
reaching it rather than by an absent return code.
"""
from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "run_full_suite_gate", CORE_SCRIPTS / "run-full-suite.py")
rfs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rfs)


# --- Layer 1: the worktree + live-daemon refusal ----------------------------

def _patch_pair(monkeypatch, git_dir, common_dir):
    monkeypatch.setattr(
        rfs, "_git_dir_pair",
        lambda root: None if git_dir is None else (Path(git_dir), Path(common_dir)))


def test_worktree_plus_live_daemon_refuses(monkeypatch, tmp_path):
    _patch_pair(monkeypatch, tmp_path / ".git/worktrees/wt", tmp_path / ".git")
    monkeypatch.setattr(rfs, "_live_daemon_port", lambda root: 61194)
    detail = rfs.worktree_daemon_refusal(tmp_path)
    assert detail and "LINKED WORKTREE" in detail and "61194" in detail


def test_main_checkout_never_refuses(monkeypatch, tmp_path):
    """Negative control. Without it, a gate that fires unconditionally would
    pass the test above."""
    _patch_pair(monkeypatch, tmp_path / ".git", tmp_path / ".git")
    monkeypatch.setattr(rfs, "_live_daemon_port", lambda root: 61194)
    assert rfs.worktree_daemon_refusal(tmp_path) is None


def test_worktree_without_a_live_daemon_never_refuses(monkeypatch, tmp_path):
    """Second negative control: a worktree is legitimate on a box with no
    daemon, and that is the documented remedy for pinning a tree."""
    _patch_pair(monkeypatch, tmp_path / ".git/worktrees/wt", tmp_path / ".git")
    monkeypatch.setattr(rfs, "_live_daemon_port", lambda root: None)
    assert rfs.worktree_daemon_refusal(tmp_path) is None


def test_unreadable_git_fails_open(monkeypatch, tmp_path):
    """guard-142: the gate's OWN dependency error must never block a run."""
    _patch_pair(monkeypatch, None, None)
    monkeypatch.setattr(
        rfs, "_live_daemon_port",
        lambda root: pytest.fail("must not probe the port after git failed"))
    assert rfs.worktree_daemon_refusal(tmp_path) is None


@pytest.mark.parametrize("stub", [
    lambda *a, **k: None,                       # the shape that actually broke it
    lambda *a, **k: object(),                   # no .returncode at all
    lambda *a, **k: (_ for _ in ()).throw(OSError("no git")),
])
def test_a_hostile_subprocess_run_still_fails_open(monkeypatch, tmp_path, stub):
    """REGRESSION PIN (). The first version wrapped only the
    subprocess.run() call, leaving `p.returncode` outside the try -- so a
    subprocess.run stubbed to return None raised AttributeError straight out of
    a gate that guard-142 requires to fail OPEN. 19 sibling tests in
    test_run_full_suite_chunk_spawn.py went red on a gate that is supposed to be
    invisible to them. The whole per-flag block belongs inside the try."""
    monkeypatch.setattr(rfs.subprocess, "run", stub)
    assert rfs._git_dir_pair(tmp_path) is None
    assert rfs.worktree_daemon_refusal(tmp_path) is None

@pytest.mark.parametrize("body", ["", "not-a-port", "0", "70000"])
def test_absent_or_garbage_port_file_is_not_a_live_daemon(tmp_path, body):
    state = tmp_path / "mind_api" / "state"
    state.mkdir(parents=True)
    if body:
        (state / "daemon.port").write_text(body, encoding="utf-8")
    assert rfs._live_daemon_port(tmp_path) is None


def test_stale_port_file_with_nothing_listening_is_not_a_live_daemon(tmp_path):
    """The port FILE is not the signal -- a stale file outlives its daemon, and
    refusing on one would block runs on a box that has no daemon at all."""
    state = tmp_path / "mind_api" / "state"
    state.mkdir(parents=True)
    with socket.socket() as s:              # bind, read the port, then release it
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    (state / "daemon.port").write_text(str(dead_port), encoding="utf-8")
    assert rfs._live_daemon_port(tmp_path) is None


def test_a_real_listener_is_detected(tmp_path):
    """Positive control for the test above: same code path, live socket. Without
    it, a _live_daemon_port that always returned None would look correct."""
    state = tmp_path / "mind_api" / "state"
    state.mkdir(parents=True)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        (state / "daemon.port").write_text(str(port), encoding="utf-8")
        assert rfs._live_daemon_port(tmp_path) == port
    finally:
        srv.close()


def test_main_returns_the_setup_exit_code_when_the_gate_fires(monkeypatch, tmp_path):
    monkeypatch.setattr(rfs, "worktree_daemon_refusal", lambda root: "because")
    monkeypatch.setattr(
        rfs, "rotate_prior_logs",
        lambda out: pytest.fail("a refused run must not touch the prior logs"))
    assert rfs.main(["--out", str(tmp_path)]) == 3


def test_the_override_gets_past_the_gate(monkeypatch, tmp_path):
    """Proved by REACHING the next step, not by the absence of a return code
    that half a dozen other failures could also produce."""
    sentinel = RuntimeError("reached the rotation step")

    def _boom(out):
        raise sentinel

    monkeypatch.setattr(rfs, "worktree_daemon_refusal", lambda root: "because")
    monkeypatch.setattr(rfs, "rotate_prior_logs", _boom)
    with pytest.raises(RuntimeError) as excinfo:
        rfs.main(["--out", str(tmp_path), "--override-worktree-daemon", "why"])
    assert excinfo.value is sentinel


# --- Layer 4b: liveness that actually answers on Windows --------------------
# The 12h TTL was the ONLY escape from a lock whose holder died, because
# tree_lock._pid_alive cannot distinguish dead from unknown on Windows. The
# first production crash of this lock (2026-09-04, pid 23180 killed mid-chunk,
# atexit never fired) proved the wedge is real rather than theoretical.


def test_a_live_pid_reads_alive():
    """Positive control. Without it, a probe that always returned False would
    pass the dead-pid test below and silently disable the lock entirely."""
    assert rfs._pid_alive_platform(os.getpid()) is True


def test_a_genuinely_dead_pid_reads_dead_not_unknown():
    """THE REGRESSION PIN. Spawn, reap, then probe -- a pid that certainly
    existed and certainly does not now, rather than a large number guessed to
    be free."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert rfs._pid_alive_platform(p.pid) is False, (
        "a reaped pid must read False, not None -- None means the run lock "
        "falls through to the 12h TTL and wedges the runner")


def test_the_default_wiring_frees_a_dead_holders_lock():
    """End-to-end with NO injected pid_alive: the default must be the platform
    probe, not tree_lock's. Injecting a stub in every other test would hide a
    regression in exactly this wiring."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    v = rfs.evaluate_run_lock({"pid": p.pid, "started_at": time.time()},
                              my_pid=os.getpid(), now=time.time())
    assert v["blocked"] is False, \
        "a dead holder must not block a fresh run: %s" % v["reason"]
    assert "gone" in v["reason"]


@pytest.mark.skipif(os.name != "nt", reason="the ctypes branch is Windows-only")
def test_an_unreadable_probe_stays_unknown_and_keeps_blocking(monkeypatch):
    """Fail-direction is UNCHANGED when the probe cannot tell.

    'Cannot tell' must never be reported as 'dead' -- that would hand the log
    dir to a second run while the first is still writing it, which is the
    collision this whole lock exists to prevent.

    ctypes is imported INSIDE _pid_alive_platform (it is Windows-only, and a
    module-level import would be dead weight everywhere else), so the patch
    goes on the real module -- the function resolves the same object.
    """
    import ctypes
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: 1 / 0)
    assert rfs._pid_alive_platform(os.getpid()) is None
    v = rfs.evaluate_run_lock({"pid": 4242, "started_at": time.time()},
                              my_pid=os.getpid(), now=time.time())
    assert v["blocked"] is True and "UNCONFIRMED" in v["reason"]


def test_non_windows_delegates_to_the_portable_probe(monkeypatch):
    """On POSIX the ctypes branch must not run at all -- os.kill answers there,
    and this keeps the two platforms from silently diverging."""
    monkeypatch.setattr(rfs.os, "name", "posix")
    seen = []
    import tree_lock
    monkeypatch.setattr(tree_lock, "_pid_alive",
                        lambda pid: seen.append(pid) or True)
    assert rfs._pid_alive_platform(4242) is True
    assert seen == [4242], "the POSIX path must go through tree_lock._pid_alive"

# --- Layer 4a: the run lock -------------------------------------------------

def _alive(pid):
    return True


def _dead(pid):
    return False


def _unknown(pid):
    """The Windows dead-pid shape: os.kill raises a bare OSError there."""
    return None


def test_a_confirmed_live_holder_and_an_unconfirmed_one_read_differently():
    """Both block, but they call for OPPOSITE operator actions.

    On Windows every dead holder lands in the `alive is None` branch (os.kill
    raises a bare OSError), so a message that reads equally confident in both
    cases sends someone away to wait out a holder that died hours ago -- for up
    to the 12h TTL. The block is right; the certainty is not."""
    rec = {"pid": 4242, "started_at": time.time() - 600}
    live = rfs.evaluate_run_lock(rec, my_pid=1, now=time.time(),
                                 pid_alive=lambda p: True)
    unknown = rfs.evaluate_run_lock(rec, my_pid=1, now=time.time(),
                                    pid_alive=lambda p: None)
    assert live["blocked"] is True and unknown["blocked"] is True
    assert "UNCONFIRMED" in unknown["reason"]
    assert "UNCONFIRMED" not in live["reason"], \
        "a confirmed-live holder must not be hedged: %s" % live["reason"]

def test_no_lock_is_not_blocked():
    v = rfs.evaluate_run_lock(None, 111, time.time(), pid_alive=_alive)
    assert v["blocked"] is False


@pytest.mark.parametrize("record", ["nonsense", 7, [], None])
def test_malformed_lock_is_not_blocked(record):
    v = rfs.evaluate_run_lock(record, 111, time.time(), pid_alive=_alive)
    assert v["blocked"] is False


def test_our_own_lock_is_not_blocked():
    rec = {"pid": 111, "started_at": time.time()}
    v = rfs.evaluate_run_lock(rec, 111, time.time(), pid_alive=_alive)
    assert v["blocked"] is False


def test_a_live_foreign_holder_blocks():
    now = time.time()
    rec = {"pid": 222, "started_at": now - 60}
    v = rfs.evaluate_run_lock(rec, 111, now, pid_alive=_alive)
    assert v["blocked"] is True and "222" in v["reason"]


def test_a_dead_holder_does_not_block():
    now = time.time()
    rec = {"pid": 222, "started_at": now - 60}
    v = rfs.evaluate_run_lock(rec, 111, now, pid_alive=_dead)
    assert v["blocked"] is False


def test_unknown_liveness_inside_the_ttl_blocks():
    now = time.time()
    rec = {"pid": 222, "started_at": now - 60}
    v = rfs.evaluate_run_lock(rec, 111, now, pid_alive=_unknown)
    assert v["blocked"] is True


def test_unknown_liveness_past_the_ttl_does_not_block():
    """THE WINDOWS WEDGE CASE. A dead holder reads as None there, so the TTL is
    the only path that ever frees the lock. Without this branch the runner
    would refuse forever after a single crashed run."""
    now = time.time()
    rec = {"pid": 222, "started_at": now - (rfs.RUN_LOCK_TTL_SECONDS + 1)}
    v = rfs.evaluate_run_lock(rec, 111, now, pid_alive=_unknown)
    assert v["blocked"] is False and "stale" in v["reason"]


def test_ttl_exceeds_the_longest_measured_run():
    """A TTL shorter than a real run lets a second run steal the lock
    mid-flight -- the exact collision it exists to prevent. Longest measured:
    10h01m (alpha, DESKTOP-O91DLK2, 24 chunks, 2026-09-03)."""
    assert rfs.RUN_LOCK_TTL_SECONDS >= 11 * 3600


def test_lock_round_trip_and_peer_safety(tmp_path):
    rfs.take_run_lock(tmp_path, pid=333, now=time.time())
    assert rfs.read_run_lock(tmp_path)["pid"] == 333
    assert rfs.release_run_lock(tmp_path, pid=444) is False, "never release a peer lock"
    assert rfs.read_run_lock(tmp_path) is not None
    assert rfs.release_run_lock(tmp_path, pid=333) is True
    assert rfs.read_run_lock(tmp_path) is None


def test_unreadable_lock_reads_as_absent(tmp_path):
    (tmp_path / rfs.RUN_LOCK_NAME).write_text("{{{ not json", encoding="utf-8")
    assert rfs.read_run_lock(tmp_path) is None


# --- Layer 4b: rotation instead of deletion ---------------------------------

def _seed_run(out, n=3):
    out.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (out / ("chunk-%02d.log" % i)).write_text("run %d" % i, encoding="utf-8")
    (out / rfs.HALVES_RECORD).write_text('{"half": "x"}', encoding="utf-8")


def test_rotation_clears_the_top_level_and_keeps_the_evidence(tmp_path):
    _seed_run(tmp_path)
    moved = rfs.rotate_prior_logs(tmp_path)
    assert moved == 4
    assert list(tmp_path.glob("chunk-*.log")) == [], "top level must be clear"
    assert not (tmp_path / rfs.HALVES_RECORD).exists()
    prev = tmp_path / "prev"
    assert (prev / "chunk-00.log").read_text(encoding="utf-8") == "run 0"
    assert (prev / rfs.HALVES_RECORD).exists()


def test_rotation_is_bounded_to_one_slot(tmp_path):
    """An unbounded archive in a temp dir is a disk leak, and a pruner would put
    a delete path back. One slot: the previous run survives, the one before it
    does not, deliberately."""
    _seed_run(tmp_path, n=1)
    rfs.rotate_prior_logs(tmp_path)
    (tmp_path / "chunk-00.log").write_text("second run", encoding="utf-8")
    rfs.rotate_prior_logs(tmp_path)
    prev = tmp_path / "prev"
    assert (prev / "chunk-00.log").read_text(encoding="utf-8") == "second run"
    assert len(list(prev.glob("chunk-*.log"))) == 1


def test_rotation_with_nothing_to_move_is_a_noop(tmp_path):
    assert rfs.rotate_prior_logs(tmp_path) == 0
    assert not (tmp_path / "prev").exists()


def test_an_unmovable_log_is_left_alone_never_deleted(tmp_path, monkeypatch):
    """Falling back to unlink() on a failed move would restore the exact
    destruction this function exists to stop."""
    _seed_run(tmp_path, n=1)

    def _refuse(self, target):
        raise OSError("busy")

    monkeypatch.setattr(Path, "replace", _refuse)
    assert rfs.rotate_prior_logs(tmp_path) == 0
    assert (tmp_path / "chunk-00.log").exists(), "must survive a failed rotation"
