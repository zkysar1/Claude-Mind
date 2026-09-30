""" -- an orphan sweep that cannot see a live deployment must not kill.

THE INCIDENT (2026-09-29). test_live_plant_round_trip, run from a scratch clone,
reached worktree-teardown.sh step 5, which ran `daemon-orphan-sweep.sh --clean`
with the CLONE as PROJECT_ROOT. The sweep scans every mind_api.src process on
the box, but builds its keep-set only from pidfiles near PROJECT_ROOT. The
clone's neighbourhood held none, so the keep-set was empty, every daemon on the
box read ORPH, and --clean killed the live daemons of both deployments on the
box, one of them production.

TWO FIXES, PINNED HERE:
1. worktree-teardown.sh runs no box-wide sweep. Its own reap, by the worktree's
   published PIDs, is the precise half and stays.
2. --clean refuses (exit 3) when no process it would KEEP is alive. From such a
   vantage the sweep sees no live deployment at all, so it cannot tell another
   deployment's daemon from an orphan. --allow-blind is the explicit escape.

SAFETY OF THIS FILE. It runs the REAL sweep, real kills included, against this
box's process table. ORPHAN_SWEEP_SCOPE_TOKEN narrows the candidates to
processes whose command line carries this test's token, so only the fake
processes started here can be candidates. Before anything that can kill, each
test runs a report-only pass and FAILS if any process other than its own fakes
is listed. A sweep that ignores the token (for example the code before this
fix) therefore stops at the report and never reaches --clean.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402

CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
SWEEP_SH = CORE_SCRIPTS / "daemon-orphan-sweep.sh"
TEARDOWN_SH = CORE_SCRIPTS / "worktree-teardown.sh"

LISTED = re.compile(r"\b(KEEP|ORPH)\s+PID=(\d+)")
BLIND_RC = 3


def _fwd(p) -> str:
    """Forward-slash form: bash globbing chokes on Windows backslashes."""
    return str(p).replace("\\", "/")


class Fakes:
    """Stand-in daemons: processes whose command line reads like a daemon's
    (`-m mind_api.src`) plus this test's token, so the sweep's own match finds
    them and the token keeps everything else out."""

    def __init__(self):
        self.token = "g11484-" + uuid.uuid4().hex[:12]
        self.procs = []

    def spawn(self):
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)",
             "-m", "mind_api.src", self.token],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.procs.append(p)
        return p

    def dead_pid(self):
        """A pid that was a fake a moment ago and is not alive now."""
        p = self.spawn()
        p.kill()
        p.wait(timeout=30)
        return p.pid

    def close(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=30)


@pytest.fixture()
def fakes():
    f = Fakes()
    yield f
    f.close()


def _publish(state_dir: Path, child: int, parent=None) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "daemon.pid").write_text(str(child), encoding="utf-8")
    if parent is not None:
        (state_dir / "daemon.parent.pid").write_text(str(parent), encoding="utf-8")
    return state_dir


def _env(fakes, local_state: Path, deploy_parent: Path) -> dict:
    env = dict(os.environ)
    env["RUNTIME_DIR"] = _fwd(local_state)
    env["ORPHAN_SWEEP_DEPLOY_PARENT"] = _fwd(deploy_parent)
    # The parent override alone turns the grandparent root off, so no test reads
    # pytest's shared basetemp (see test_daemon_orphan_sweep_cross_repo.py).
    env.pop("ORPHAN_SWEEP_DEPLOY_GRANDPARENT", None)
    env["ORPHAN_SWEEP_SCOPE_TOKEN"] = fakes.token
    env["STORAGE_BACKEND"] = "local"
    return env


def _sweep(env, *args):
    return subprocess.run(
        [BASH, _fwd(SWEEP_SH), *args], capture_output=True, text=True,
        timeout=180, env=env, cwd=str(PROJECT_ROOT),
    )


def _listed(proc) -> set:
    return {int(m.group(2)) for m in LISTED.finditer(proc.stdout)}


def _only_fakes_visible(env, fakes, expected):
    """The guard that makes a real --clean safe here: a report-only pass must list
    exactly this test's live fakes. Anything else means the token did not narrow
    the scan, and a --clean would reach real daemons."""
    report = _sweep(env)
    listed = _listed(report)
    ours = {p.pid for p in fakes.procs}
    foreign = listed - ours
    if foreign:
        pytest.fail(
            f"the sweep lists processes this test did not start ({sorted(foreign)}): "
            f"ORPHAN_SWEEP_SCOPE_TOKEN did not narrow the scan, so --clean is NOT run.\n"
            f"{report.stdout}{report.stderr}"
        )
    want = {p.pid for p in expected}
    assert listed == want, (
        f"report lists {sorted(listed)}, expected exactly {sorted(want)}\n"
        f"{report.stdout}{report.stderr}"
    )


def _alive(p) -> bool:
    return p.poll() is None


def _dead(p) -> bool:
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        return False
    return True


def test_clean_refuses_when_nothing_is_published(tmp_path, fakes):
    """Outcome 1: the clone's view. No pidfile anywhere the sweep looks, so
    --clean must refuse, exit non-zero, and kill nothing."""
    env = _env(fakes, tmp_path / "clone" / "mind_api" / "state", tmp_path / "nbhd")
    (tmp_path / "nbhd").mkdir()
    victim = fakes.spawn()
    _only_fakes_visible(env, fakes, [victim])

    proc = _sweep(env, "--clean")
    out = proc.stdout + proc.stderr
    assert proc.returncode == BLIND_RC, out
    assert "REFUSED" in proc.stderr, out
    assert _alive(victim), f"--clean killed a process it could not place:\n{out}"


def test_live_deployment_outside_the_neighbourhood_survives_clean(tmp_path, fakes):
    """Outcome 2: the incident exactly. A live deployment publishes its pair
    somewhere the caller's globs never reach; --clean from the caller must keep
    it."""
    deployment = fakes.spawn()
    _publish(tmp_path / "far" / "away" / "deploy" / "mind_api" / "state", deployment.pid)
    env = _env(fakes, tmp_path / "clone" / "mind_api" / "state", tmp_path / "nbhd")
    (tmp_path / "nbhd").mkdir()
    _only_fakes_visible(env, fakes, [deployment])

    proc = _sweep(env, "--clean")
    out = proc.stdout + proc.stderr
    assert proc.returncode == BLIND_RC, out
    assert _alive(deployment), f"--clean killed a live deployment's daemon:\n{out}"


def test_a_stale_pidfile_does_not_unlock_clean(tmp_path, fakes):
    """The keep-set is not empty here, it names a dead pid. That is still a
    blind vantage: the refusal keys on a LIVE keep member, not on an empty file
    list, because scratch areas collect stale pidfiles."""
    stale = fakes.dead_pid()
    _publish(tmp_path / "nbhd" / "old-clone" / "mind_api" / "state", stale)
    victim = fakes.spawn()
    env = _env(fakes, tmp_path / "clone" / "mind_api" / "state", tmp_path / "nbhd")
    _only_fakes_visible(env, fakes, [victim])

    proc = _sweep(env, "--clean")
    out = proc.stdout + proc.stderr
    assert proc.returncode == BLIND_RC, out
    assert _alive(victim), out


def test_clean_still_reaps_an_orphan_beside_a_visible_live_deployment(tmp_path, fakes):
    """Positive control: the refusal is not a blanket off-switch. With a live
    published pair in view, the unpublished process is reaped and the published
    one is kept."""
    live = fakes.spawn()
    _publish(tmp_path / "nbhd" / "deploy" / "mind_api" / "state", live.pid)
    orphan = fakes.spawn()
    env = _env(fakes, tmp_path / "clone" / "mind_api" / "state", tmp_path / "nbhd")
    _only_fakes_visible(env, fakes, [live, orphan])

    proc = _sweep(env, "--clean")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert _dead(orphan), f"the orphan survived --clean:\n{out}"
    assert _alive(live), f"--clean killed the published daemon:\n{out}"


def test_allow_blind_is_the_explicit_escape(tmp_path, fakes):
    """An operator who has checked every ORPH line can still reap from a blind
    vantage, by saying so."""
    env = _env(fakes, tmp_path / "clone" / "mind_api" / "state", tmp_path / "nbhd")
    (tmp_path / "nbhd").mkdir()
    orphan = fakes.spawn()
    _only_fakes_visible(env, fakes, [orphan])

    proc = _sweep(env, "--clean", "--allow-blind")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert _dead(orphan), out


def test_scope_token_must_be_a_plain_word(tmp_path, fakes):
    """The token only ever narrows the scan. A pattern-shaped value is refused
    before any scan, so it cannot quietly stop narrowing. Report mode on
    purpose: this test has no fakes-only guard, so it must never ask for a
    kill (a sweep that ignored the token would reap the whole box)."""
    env = _env(fakes, tmp_path / "s", tmp_path / "nbhd")
    env["ORPHAN_SWEEP_SCOPE_TOKEN"] = "a.*b"
    proc = _sweep(env)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "ORPHAN_SWEEP_SCOPE_TOKEN" in proc.stderr


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True).stdout.strip()


def test_worktree_teardown_kills_nothing_outside_the_worktree(tmp_path, fakes):
    """Outcome 3, pinned: tearing a worktree down must leave every other daemon
    alone. The sweep is given a LIVE keep member here (the RUNTIME_DIR pair), so
    a teardown that still ran `--clean` would not be refused as blind: it would
    reap the unpublished fake, and this test would fail."""
    owner = tmp_path / "owner"
    owner.mkdir()
    _git("init", "-q", "-b", "main", cwd=owner)
    _git("config", "user.email", "t@example.invalid", cwd=owner)
    _git("config", "user.name", "t", cwd=owner)
    (owner / "f.txt").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=owner)
    _git("commit", "-qm", "base", cwd=owner)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "wt/topic", str(wt), cwd=owner)

    live = fakes.spawn()
    local_state = _publish(tmp_path / "live" / "mind_api" / "state", live.pid)
    bystander = fakes.spawn()
    env = _env(fakes, local_state, tmp_path / "nbhd")
    (tmp_path / "nbhd").mkdir()
    _only_fakes_visible(env, fakes, [live, bystander])

    proc = subprocess.run(
        [BASH, _fwd(TEARDOWN_SH), _fwd(wt), "--owner", _fwd(owner), "--force"],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(PROJECT_ROOT),
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert not wt.exists(), out
    assert _alive(bystander), f"worktree teardown killed a process outside the worktree:\n{out}"
    assert _alive(live), out


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
