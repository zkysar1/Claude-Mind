"""Regression tests for background-jobs.py check_job() status mapping.

The completion_check exit-code -> status contract (run_completion_check docstring:
0=completed, 1=still running, 2=failed, other=unknown) is load-bearing for ALL
long-running background-job monitoring. Before g-115-16 the check_job() dead-PID
branch mapped only 0 and 2; exit 1 ("still running") fell through to "unknown",
which MONITOR-style consumers treat like "failed" -- false-failing a healthy
long-running job whose registered PID is unverifiable on Windows/MSYS (a PID
file holding an MSYS bash PID that WMI/os.kill cannot see). These tests pin the
full mapping so it can never silently regress.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# background-jobs.py has a hyphen in the filename -> load via importlib.
_spec = importlib.util.spec_from_file_location(
    "background_jobs", str(SCRIPTS / "background-jobs.py")
)
bgjobs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bgjobs)


def _job(completion_check="stub"):
    # pid is irrelevant in the dead-PID tests -- pid_alive is monkeypatched.
    return {
        "job_id": "test-job",
        "type": "test",
        "pid": 999999999,
        "launched_at": "2026-06-29T00:00:00",
        "goal_id": "g-test",
        "monitor_goal_id": "g-test-mon",
        "completion_check": completion_check,
    }


@pytest.mark.parametrize(
    "exit_code,expected",
    [
        (0, "completed"),
        (1, "running"),  # the  fix: previously fell through to "unknown"
        (2, "failed"),
        (99, "unknown"),
    ],
)
def test_completion_check_exit_code_maps_to_status(monkeypatch, exit_code, expected):
    # Force the dead-PID branch so the completion_check arbitrates, then stub
    # run_completion_check to isolate check_job's exit-code -> status mapping.
    monkeypatch.setattr(bgjobs, "pid_alive", lambda pid: False)
    monkeypatch.setattr(
        bgjobs, "run_completion_check", lambda cmd: (exit_code, '{"status":"probe"}')
    )
    result = bgjobs.check_job(_job())
    assert result["status"] == expected, (
        f"completion_check exit {exit_code} -> {result['status']!r} "
        f"(expected {expected!r})"
    )


def test_exit1_running_does_not_trigger_output_artifact_gate(monkeypatch):
    # The output-sanity gate must run ONLY on status == "completed". A "running"
    # result (exit 1) must not be downgraded to "failed" by an artifact check.
    monkeypatch.setattr(bgjobs, "pid_alive", lambda pid: False)
    monkeypatch.setattr(bgjobs, "run_completion_check", lambda cmd: (1, "{}"))

    def _boom(_artifacts):
        raise AssertionError("artifact gate must not run for a 'running' status")

    monkeypatch.setattr(bgjobs, "check_output_artifacts", _boom)
    job = _job()
    job["output_artifacts"] = [{"path": "C:/nope.jsonl", "min_bytes": 1}]
    result = bgjobs.check_job(job)
    assert result["status"] == "running"


def test_alive_pid_short_circuits_to_running(monkeypatch):
    # When the registered PID is verifiably alive, status is "running" WITHOUT
    # consulting the completion_check at all.
    monkeypatch.setattr(bgjobs, "pid_alive", lambda pid: True)

    def _must_not_run(cmd):
        raise AssertionError("completion_check must not run when PID is alive")

    monkeypatch.setattr(bgjobs, "run_completion_check", _must_not_run)
    result = bgjobs.check_job(_job())
    assert result["status"] == "running"
    assert "check_output" not in result
