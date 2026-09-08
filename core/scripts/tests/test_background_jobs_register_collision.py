"""Pins cmd_register's id-collision behaviour in background-jobs.py ().

``cmd_register`` used to log ``already registered`` and return rc=0 on ANY id
collision, without updating the row or tracking the pid it was handed. A caller
that checks only rc read that silent no-op as success while its live job went
untracked — so stop-hook Gate 2.6 did not hold the turn open for it, and
``roblox-bridge.py::_register_self``'s docstring claim that register "upserts"
was simply false.

The systematically-exposed population is RECURRING goals: a deterministic job id
collides with its own orphaned row from the previous cycle every single time
(measured on g-306-284, alpha/cc-04 2026-08-30 — an 8.8h-old STOPPED row with a
confirmed-dead pid swallowed a live suite's registration, and the only tell was
register saying rc=0 while ``has-pending`` said rc=1).

The load-bearing test is ``test_rc0_always_means_the_store_names_the_passed_pid``.
It pins the CONTRACT — rc=0 implies the row names your pid, and a non-zero rc
implies the store is untouched — rather than any one branch's wording, so the
three branches cannot drift out of agreement with what a caller infers from rc.
Asserting the message text alone would pin a copy of the condition and hold no
resolving power over what actually lands on disk (guard-1866); the per-branch
tests below carry the positive control that the refusal is neither always-on nor
always-off.
"""
import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# background-jobs.py has a hyphen in the filename -> load via importlib.
_spec = importlib.util.spec_from_file_location(
    "background_jobs_collision", str(SCRIPTS / "background-jobs.py")
)
bgjobs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bgjobs)

INCUMBENT_PID = 4242
OTHER_PID = 9999


def _args(pid, job_id="j1", goal="g-test"):
    return argparse.Namespace(
        id=job_id,
        type="test-suite",
        goal=goal,
        pid=pid,
        monitor_goal=None,
        completion_check="probe.sh --check",
        metadata=None,
        output_artifacts=None,
    )


@pytest.fixture
def jobs_path(tmp_path, monkeypatch):
    """Redirect the module-level JOBS_PATH at a tmp file (the IO helpers read
    the global, so patching the module attribute is sufficient)."""
    p = tmp_path / "session" / "background-jobs.yaml"
    monkeypatch.setattr(bgjobs, "JOBS_PATH", p)
    return p


def _register(capsys, args):
    """Run cmd_register; return (exit_code, stderr). log() writes to stderr.

    A plain return is rc=0 — cmd_register only raises SystemExit on the refusal.
    """
    try:
        bgjobs.cmd_register(args)
        code = 0
    except SystemExit as exc:  # the REFUSED branch
        code = exc.code
    return code, capsys.readouterr().err


def _rows(job_id="j1"):
    return [j for j in bgjobs.read_data()["jobs"] if j.get("job_id") == job_id]


def _seed(capsys, monkeypatch, incumbent_alive):
    """Register the incumbent, then pin pid_alive's verdict about it."""
    monkeypatch.setattr(bgjobs, "pid_alive", lambda pid: True)
    code, _ = _register(capsys, _args(INCUMBENT_PID))
    assert code == 0 and _rows(), "seed registration did not land — later asserts would be vacuous"
    monkeypatch.setattr(bgjobs, "pid_alive", lambda pid: incumbent_alive)


# ------------------------------------------------------------- the three branches

def test_same_pid_is_an_idempotent_no_op(jobs_path, capsys, monkeypatch):
    """A genuine re-arm: nothing to change, and nothing is left untracked."""
    _seed(capsys, monkeypatch, incumbent_alive=True)

    code, err = _register(capsys, _args(INCUMBENT_PID))

    assert code == 0
    assert "no change" in err
    rows = _rows()
    assert len(rows) == 1, "an idempotent re-arm duplicated the row"
    assert rows[0]["pid"] == INCUMBENT_PID


def test_dead_incumbent_is_reaped_and_the_live_job_takes_the_id(jobs_path, capsys, monkeypatch):
    """The recurring-goal case: the orphan is provably dead, so the id is free.

    This is the branch whose absence made ``_register_self``'s "upserts" claim
    false, and the one every deterministic-id recurring goal hits each cycle.
    """
    _seed(capsys, monkeypatch, incumbent_alive=False)

    code, err = _register(capsys, _args(OTHER_PID))

    assert code == 0
    assert "reaping stale row" in err
    rows = _rows()
    assert len(rows) == 1, "reap left the orphan behind beside the new row"
    assert rows[0]["pid"] == OTHER_PID, (
        "the live job did not take the id — this is the silent no-op the fix removes"
    )


def test_live_incumbent_with_a_different_pid_is_refused_loudly(jobs_path, capsys, monkeypatch):
    """Two live jobs cannot share one id, and neither one is ours to drop.

    rc=2 matches the REFUSED convention heartbeat-tick.sh uses and leaves rc=1
    free. The incumbent must survive intact — a refusal that half-applied would
    be worse than the no-op it replaces.
    """
    _seed(capsys, monkeypatch, incumbent_alive=True)

    code, err = _register(capsys, _args(OTHER_PID))

    assert code == 2, "a collision with a LIVE incumbent did not refuse"
    assert "REFUSED" in err
    # Name the consequence and the fix, not just the fact.
    assert "--id" in err and "Deregister" in err
    rows = _rows()
    assert len(rows) == 1 and rows[0]["pid"] == INCUMBENT_PID, (
        "the refusal mutated the store — it must be a no-op on disk"
    )


# ------------------------------------------------------- the contract (load-bearing)

@pytest.mark.parametrize(
    "incumbent_alive,new_pid",
    [
        (True, INCUMBENT_PID),   # idempotent re-arm
        (False, INCUMBENT_PID),  # same pid, incumbent believed dead
        (False, OTHER_PID),      # reap
        (True, OTHER_PID),       # refuse
    ],
)
def test_rc0_always_means_the_store_names_the_passed_pid(
    jobs_path, capsys, monkeypatch, incumbent_alive, new_pid
):
    """rc=0 => the row names YOUR pid. rc!=0 => the store is untouched.

    This is the invariant the defect actually violated: register returned rc=0
    while the row named a DIFFERENT process, so every caller that checks only rc
    — which is every caller, since the message goes to stderr — concluded its
    live job was tracked when it was not. Pinning it across all four
    (liveness x pid) combinations means a future branch cannot reintroduce a
    silent no-op without failing here, whatever it chooses to log.
    """
    _seed(capsys, monkeypatch, incumbent_alive=incumbent_alive)
    before = bgjobs.read_data()["jobs"]

    code, _ = _register(capsys, _args(new_pid))

    if code == 0:
        rows = _rows()
        assert len(rows) == 1, f"rc=0 left {len(rows)} rows for one id"
        assert rows[0]["pid"] == new_pid, (
            "register returned success while the row names a different pid — "
            "the caller believes a job is tracked that is not"
        )
    else:
        assert bgjobs.read_data()["jobs"] == before, (
            "a non-zero rc must leave the store exactly as it was"
        )


def test_refusal_is_not_always_on(jobs_path, capsys, monkeypatch):
    """Positive control for the contract test's parametrisation.

    A cmd_register that refused unconditionally would satisfy the rc!=0 limb of
    every case above without ever writing anything, so prove a fresh id on an
    empty store still registers normally.
    """
    monkeypatch.setattr(bgjobs, "pid_alive", lambda pid: True)

    code, err = _register(capsys, _args(INCUMBENT_PID, job_id="fresh"))

    assert code == 0 and "REFUSED" not in err
    assert _rows("fresh")[0]["pid"] == INCUMBENT_PID
