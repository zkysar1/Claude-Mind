"""Pins the un-gateable-registration warning in background-jobs.py cmd_register ().

``cmd_has_pending`` counts a job as pending only when the PID is alive AND at
least one completion mechanism exists (``monitor_goal_id`` OR
``completion_check``). That strictness is deliberate — it is the anti-zombie
rule that stops a dead-PID or never-completing registration from suppressing
recovery-gate and stop-hook forever, and these tests do NOT relax it.

The defect it left behind was purely diagnostic: ``cmd_register`` stored both
fields but printed a plain ``registered:`` success line even when BOTH were
absent, so the caller got a success message for a registration structurally
incapable of the one thing it was registered for. Downstream the agent believes
turn-end is permitted, Gate 2.6 BLOCKs instead, and the loop busy-spins for the
length of the external wait (~20 turns over 32min —
``run-full-suite-after-deep-code.md``). ``guard-1619`` documents the same shape
on the PID axis and its remedy is honor-system ("assert has-pending returns 0
after registering"); this warning makes the tool say it instead.

The load-bearing test is ``test_warning_fires_exactly_when_has_pending_refuses``
— it pins the COUPLING between the two predicates rather than the warning's
wording, so the pair cannot drift apart. Asserting the message text alone would
pin a copy of the condition and hold no resolving power over ``cmd_has_pending``
(guard-1866); the parametrised cases below carry the positive control that the
detector is not simply always-quiet or always-loud.
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
    "background_jobs_reg", str(SCRIPTS / "background-jobs.py")
)
bgjobs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bgjobs)


def _args(monitor_goal=None, completion_check=None, job_id="j1"):
    return argparse.Namespace(
        id=job_id,
        type="test-suite",
        goal="g-test",
        pid=4242,
        monitor_goal=monitor_goal,
        completion_check=completion_check,
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
    """Run cmd_register and return its stderr. log() writes to stderr."""
    bgjobs.cmd_register(args)
    return capsys.readouterr().err


# ------------------------------------------------------------------ the warning

@pytest.mark.parametrize(
    "monitor_goal,completion_check,should_warn",
    [
        (None, None, True),               # neither -> un-gateable
        ("", "", True),                   # empty strings are the same case
        ("g-mon", None, False),           # monitor alone is enough
        (None, "probe.sh --check", False),  # completion_check alone is enough
        ("g-mon", "probe.sh --check", False),  # both
    ],
)
def test_warns_only_when_no_completion_mechanism(
    jobs_path, capsys, monitor_goal, completion_check, should_warn
):
    err = _register(capsys, _args(monitor_goal, completion_check))

    # The registration itself always succeeds — this is a warning, not an error.
    # Standalone infrastructure legitimately registers without a completion
    # mechanism, so turning this into a failure would break those callers.
    assert "registered: j1" in err

    if should_warn:
        assert "WARN" in err, (
            "registration with no completion mechanism printed no warning — the "
            "caller cannot tell that has-pending will refuse this job"
        )
        # Name the consequence and the fix, not just the fact.
        assert "has-pending" in err and "Gate 2.6" in err
        assert "--completion-check" in err and "--monitor-goal" in err
    else:
        assert "WARN" not in err, (
            "warned on a registration that DOES carry a completion mechanism — "
            "a warning that fires on the healthy case trains readers to ignore it"
        )


def test_registration_is_never_blocked_by_the_warning(jobs_path, capsys):
    """The warning must not change what lands on disk."""
    _register(capsys, _args(None, None))
    jobs = bgjobs.read_data()["jobs"]
    assert len(jobs) == 1, "the un-gateable job was not registered"
    assert jobs[0]["job_id"] == "j1"
    assert jobs[0]["monitor_goal_id"] is None
    assert jobs[0]["completion_check"] is None


# --------------------------------------------------- the coupling (load-bearing)

@pytest.mark.parametrize(
    "monitor_goal,completion_check",
    [
        (None, None),
        ("", ""),
        ("g-mon", None),
        (None, "probe.sh --check"),
        ("g-mon", "probe.sh --check"),
    ],
)
def test_warning_fires_exactly_when_has_pending_refuses(
    jobs_path, capsys, monkeypatch, monitor_goal, completion_check
):
    """The warning's condition and cmd_has_pending's predicate must agree.

    This is the invariant worth pinning: the warning exists to predict
    has-pending's verdict at the only moment the caller can still fix it. If the
    two predicates ever drift, the warning becomes either a false alarm or —
    worse — silent on exactly the registration that will strand the loop.

    PID is forced alive so the completion-mechanism half is the ONLY thing
    arbitrating; without that, has-pending would refuse every case for the
    unrelated dead-PID reason (guard-1619's axis) and this test would pass
    vacuously against a warning that never fired at all.
    """
    monkeypatch.setattr(bgjobs, "pid_alive", lambda pid: True)

    warned = "WARN" in _register(capsys, _args(monitor_goal, completion_check))

    with pytest.raises(SystemExit) as exc:
        bgjobs.cmd_has_pending(argparse.Namespace())
    has_pending_refuses = exc.value.code == 1

    assert warned == has_pending_refuses, (
        f"warning ({warned}) and has-pending refusal ({has_pending_refuses}) "
        f"disagree for monitor_goal={monitor_goal!r} "
        f"completion_check={completion_check!r} — the warning no longer predicts "
        f"the gate it exists to predict"
    )


def test_has_pending_semantics_are_unchanged_by_a_dead_pid(jobs_path, capsys, monkeypatch):
    """Positive control for the coupling test's monkeypatch.

    The test above forces pid_alive True. That patch is load-bearing, so prove
    the unpatched predicate still refuses on a dead PID — otherwise the coupling
    test could be passing against a has-pending that ignores liveness entirely,
    and this file would have silently relaxed the anti-zombie rule it promises
    not to touch.
    """
    monkeypatch.setattr(bgjobs, "pid_alive", lambda pid: False)
    _register(capsys, _args("g-mon", "probe.sh --check"))
    # Non-degeneracy: an EMPTY job list also exits 1, so without this the
    # assertion below would pass against a registration that never landed.
    assert bgjobs.read_data()["jobs"], "no job registered — the exit-1 below is vacuous"
    with pytest.raises(SystemExit) as exc:
        bgjobs.cmd_has_pending(argparse.Namespace())
    assert exc.value.code == 1, (
        "a dead-PID job with a full completion mechanism was counted pending — "
        "the anti-zombie rule regressed"
    )
