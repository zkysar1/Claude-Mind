"""Pin : recovery instructions must not assert unread goal state.

The state-update / learning-gate cases of _print_recovery_instructions in
iteration-close.sh previously printed "Goal X has status=completed (verify
succeeded)" / "Goal X is closed" UNCONDITIONALLY on any rc!=0 from those
phases. The common way to get rc=2 is entry validation rejecting the call
(missing --goal/--source) AFTER _CURRENT_PHASE is set — nothing ran, verify
never fired, and the goal may still be pending with a live claim (measured:
bravo 2026-07-30, g-115-4084 sat pending with a live claim ~40min on the
strength of "No goal-status revert needed").

The fix probes the live record (_probe_goal_status) and branches three ways:
completed → original text (true there); readable-but-not-completed → honest
"verify has NOT marked it completed"; unreadable → UNKNOWN, asserting neither.

These tests exercise the UNREADABLE branch with the literal production arg
shape (guard-920): a validation-rejecting invocation, where the goal id is
either nonexistent or unparseable. Under pytest the daemon wrapper refuses to
spawn (PYTEST_CURRENT_TEST set, RUNTIME_DIR unset — g-115-3329), so the probe
fails closed to "" exactly as it does in production when the read fails. The
completed/pending branches require live queue state and were verified by hand
probes at fix time; the load-bearing pin here is that the OLD unconditional
text can no longer print without a completed read backing it.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _runtime_bash import BASH  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "iteration-close.sh"
FAKE_AGENT_DIR = Path(__file__).resolve().parents[3] / "agents" / "ic-recovery-test-agent"


@pytest.fixture(autouse=True, scope="module")
def _cleanup_fake_agent_dir():
    """Remove the fake agent's residue dir (fresh-eyes finding, 2026-07-30).

    On a box with a LIVE daemon, the script's EXIT-trap writes (execution
    diary, heartbeat) reach the daemon — the g-115-3329 refusal blocks
    SPAWNS, not calls to an already-running daemon — and the daemon
    materializes agents/ic-recovery-test-agent/, a phantom agent every
    cross-agent glob consumer then enumerates. Pointing RT_DIR at a tmp dir
    is NOT the fix: rt_spawn would then spawn a real orphan daemon per call
    (the refusal guards only the shared dir). Transient existence during
    this module's few seconds is acceptable; permanent residue is not.
    """
    shutil.rmtree(FAKE_AGENT_DIR, ignore_errors=True)
    yield
    shutil.rmtree(FAKE_AGENT_DIR, ignore_errors=True)

OLD_STATE_UPDATE_ASSERTION = "has status=completed (verify succeeded)"
OLD_LEARNING_GATE_ASSERTION = "is closed (verify + state-update done)"


def _run(phase: str, goal_id: str):
    env = os.environ.copy()
    env["STORAGE_BACKEND"] = "local"  # guard-955: mandatory pin for any test runner
    # Fake agent binding: the script exits before dispatch (no recovery
    # block) when MIND_AGENT is unset, and a REAL agent name would append
    # phase-marker rows to that agent's live execution diary via the EXIT
    # trap. A nonexistent agent reaches the recovery block (verified at fix
    # time). NOT fully hermetic: with a live daemon the trap's writes land
    # and materialize the fake agent dir — the module fixture above removes
    # it (fresh-eyes finding, 2026-07-30).
    env["MIND_AGENT"] = "ic-recovery-test-agent"
    # Validation-reject shape: --goal given, --source omitted → exit 2 from the
    # phase entry check, AFTER _CURRENT_PHASE is set → recovery block prints.
    proc = subprocess.run(
        [BASH, SCRIPT.as_posix(), "--phase", phase, "--goal", goal_id],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    return proc


def test_state_update_unreadable_goal_does_not_assert_completed():
    proc = _run("state-update", "g-999999-99")
    assert proc.returncode == 2, proc.stderr
    assert "verify state UNKNOWN" in proc.stderr
    assert "asserting neither direction" in proc.stderr
    assert OLD_STATE_UPDATE_ASSERTION not in proc.stderr
    assert "No goal-status revert needed" not in proc.stderr


def test_state_update_unparseable_goal_id_takes_unreadable_branch():
    # g-xw-* ids carry no derivable aspiration id — the probe's regex guard
    # must fall through to the unreadable branch, not crash the trap.
    proc = _run("state-update", "g-xw-20260101T000000-01")
    assert proc.returncode == 2, proc.stderr
    assert "verify state UNKNOWN" in proc.stderr
    assert OLD_STATE_UPDATE_ASSERTION not in proc.stderr


def test_learning_gate_unreadable_goal_does_not_assert_closed():
    proc = _run("learning-gate", "g-999999-99")
    assert proc.returncode == 2, proc.stderr
    assert "closure state UNKNOWN" in proc.stderr
    assert OLD_LEARNING_GATE_ASSERTION not in proc.stderr


def test_verify_case_unchanged_hedge_survives():
    # The verify case was already honest ("may be in indeterminate state") —
    # the goal's sibling audit keeps it untouched; pin the hedge so a future
    # edit does not import the unconditional-assertion shape there.
    proc = _run("verify", "g-999999-99")
    assert proc.returncode == 2, proc.stderr
    assert "may be in indeterminate state" in proc.stderr
