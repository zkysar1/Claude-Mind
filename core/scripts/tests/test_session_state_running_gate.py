"""`session-state-set.sh RUNNING` refuses without a runner sid (rb-323 / guard-403).

The invariant "state=RUNNING implies fresh heartbeat AND non-empty SID files" was
prose-enforced in /start's Step 3 ordering until 2026-08-29, when a paged
`/start --recover` on a small model skipped the runner triple-write, acquired the
claim and flipped RUNNING. stop-hook.sh routes the runner on
`agents/<agent>/session/running-session-id`, so that reducer would have died silently
at its first text-only turn end. The gate lives in `session.py::require_runner_sid`
and fires ONLY for the RUNNING value: IDLE stays writable (every recovery path flips
RUNNING -> IDLE before clearing the manifest — the inverse ordering pinned by
test_recovery_ordering_invariant.py).

Fourth precondition (2026-09-21), `session.py::require_autonomous_mode`: RUNNING also
requires `agent-mode` == autonomous. A served small-model /start dropped the mode step
with no error, flipped RUNNING past the three runner checks, and ran 34 minutes with the
file absent (absence reads as `reader`), so every autonomous-gated hook stayed silent. It
is checked LAST, so the runner refusals below keep naming their own step first, and the
tests reach the allowed path through the REAL mode setter — the remedy the refusal names.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SESSION_PY = PROJECT_ROOT / "core" / "scripts" / "session.py"


def _run(agent_dir: Path, *args: str, sid: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("MIND_SID", "MIND_AGENT_DIR")}
    env.update({
        "MIND_AGENT": "gate-probe",
        "MIND_AGENT_DIR": str(agent_dir),
        "STORAGE_BACKEND": "local",
    })
    if sid is not None:
        env["MIND_SID"] = sid
    return subprocess.run(
        [sys.executable, str(SESSION_PY), "state", "set", *args],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    d = tmp_path / "gate-probe"
    (d / "session").mkdir(parents=True)
    return d


def _set_mode(agent_dir: Path, mode: str = "autonomous") -> None:
    """What /start's mode step leaves behind, written by the production setter itself."""
    env = {k: v for k, v in os.environ.items() if k not in ("MIND_SID", "MIND_AGENT_DIR")}
    env.update({"MIND_AGENT": "gate-probe", "MIND_AGENT_DIR": str(agent_dir),
                "STORAGE_BACKEND": "local"})
    r = subprocess.run([sys.executable, str(SESSION_PY), "mode", "set", mode],
                       cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def _state(agent_dir: Path) -> str | None:
    p = agent_dir / "session" / "agent-state"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def test_running_is_refused_without_running_session_id(agent_dir: Path) -> None:
    r = _run(agent_dir, "RUNNING")
    assert r.returncode == 1, r.stderr
    assert "REJECTED" in r.stderr and "running-session-id" in r.stderr
    assert "triple-write" in r.stderr  # names the missing step, not just the file
    assert _state(agent_dir) is None  # nothing written


def test_running_is_refused_on_an_empty_running_session_id(agent_dir: Path) -> None:
    (agent_dir / "session" / "running-session-id").write_text("\n", encoding="utf-8")
    r = _run(agent_dir, "RUNNING")
    assert r.returncode == 1 and "REJECTED" in r.stderr
    assert _state(agent_dir) is None


def test_running_is_allowed_once_the_runner_sid_is_written(agent_dir: Path) -> None:
    (agent_dir / "session" / "running-session-id").write_text("abc123\n", encoding="utf-8")
    _set_mode(agent_dir)
    r = _run(agent_dir, "RUNNING")  # no MIND_SID in the env: presence is enough
    assert r.returncode == 0, r.stderr
    assert _state(agent_dir) == "RUNNING"


def test_running_is_refused_when_the_runner_sid_is_another_session(agent_dir: Path) -> None:
    # A stale file left by a crashed runner whose manifest-clear did not run.
    (agent_dir / "session" / "running-session-id").write_text("abc123\n", encoding="utf-8")
    r = _run(agent_dir, "RUNNING", sid="zzz999")
    assert r.returncode == 1 and "REJECTED" in r.stderr and "stale" in r.stderr
    assert _state(agent_dir) is None


def _carrier(agent_dir: Path, sid: str) -> None:
    (agent_dir / "session" / f"body-heartbeat-{sid}.json").write_text(
        '{"sid": "%s", "agent": "gate-probe"}\n' % sid, encoding="utf-8")


def _bind(agent_dir: Path, sid: str) -> None:
    """What /start Step 0 (session-binding-write.sh) leaves behind: the per-session dir."""
    d = agent_dir / "sessions" / sid
    d.mkdir(parents=True)
    (d / "binding.yaml").write_text("agent: gate-probe\nmode: autonomous\n", encoding="utf-8")


def test_running_is_allowed_when_the_runner_sid_is_this_session(agent_dir: Path) -> None:
    (agent_dir / "session" / "running-session-id").write_text("abc123\n", encoding="utf-8")
    _bind(agent_dir, "abc123")
    _carrier(agent_dir, "abc123")
    _set_mode(agent_dir)
    r = _run(agent_dir, "RUNNING", sid="abc123")
    assert r.returncode == 0, r.stderr
    assert _state(agent_dir) == "RUNNING"


def test_running_is_refused_without_the_bound_session_dir_and_names_step_0(agent_dir: Path) -> None:
    # /start Step 0 (session-binding-write.sh) skipped: heartbeat-tick writes the carrier
    # only under sessions/<SID>/, so the carrier refusal's remedy could never succeed and
    # a small model's next move was to hand-write the carrier (2026-08-30, coach/zc-03).
    # The binding must be checked FIRST and the refusal must name the binding step —
    # even when a (hand-written) carrier is already present.
    (agent_dir / "session" / "running-session-id").write_text("abc123\n", encoding="utf-8")
    _carrier(agent_dir, "abc123")
    _set_mode(agent_dir)  # present, so the positive control below isolates the binding
    r = _run(agent_dir, "RUNNING", sid="abc123")
    assert r.returncode == 1 and "REJECTED" in r.stderr
    assert "session-binding-write.sh" in r.stderr and "Step 0" in r.stderr
    assert "by hand" in r.stderr
    assert _state(agent_dir) is None
    _bind(agent_dir, "abc123")
    assert _run(agent_dir, "RUNNING", sid="abc123").returncode == 0  # positive control


def test_running_is_refused_without_this_sessions_liveness_carrier(agent_dir: Path) -> None:
    # The triple-write and the binding ran but the pre-flip heartbeat-tick did not: the
    # tool-call-cadence tick would never fire for this runner and its lease would starve
    # through /boot.
    (agent_dir / "session" / "running-session-id").write_text("abc123\n", encoding="utf-8")
    _bind(agent_dir, "abc123")
    _set_mode(agent_dir)  # present, so the positive control below isolates the carrier
    r = _run(agent_dir, "RUNNING", sid="abc123")
    assert r.returncode == 1 and "REJECTED" in r.stderr and "carrier" in r.stderr
    assert "session-binding-write.sh" not in r.stderr  # the binding is present; name the tick
    assert _state(agent_dir) is None
    _carrier(agent_dir, "abc123")
    assert _run(agent_dir, "RUNNING", sid="abc123").returncode == 0  # positive control


def _everything_but_the_mode(agent_dir: Path, sid: str = "abc123") -> None:
    """The measured half-start: the runner claim ran whole, the mode step never did."""
    (agent_dir / "session" / "running-session-id").write_text(sid + "\n", encoding="utf-8")
    _bind(agent_dir, sid)  # binding.yaml says `mode: autonomous`, as it did in the incident
    _carrier(agent_dir, sid)


def test_running_is_refused_without_a_recorded_mode_and_names_the_mode_step(agent_dir: Path) -> None:
    # 2026-09-21: a served small-model /start dropped the mode step silently. All three runner
    # checks passed, RUNNING flipped, and the agent ran 34 minutes reading as `reader`. The
    # binding's own `mode: autonomous` must NOT satisfy the check: consumers read agent-mode.
    _everything_but_the_mode(agent_dir)
    r = _run(agent_dir, "RUNNING", sid="abc123")
    assert r.returncode == 1 and "REJECTED" in r.stderr and "agent-mode" in r.stderr
    assert "absent" in r.stderr and "reader" in r.stderr  # says what the absence MEANS
    assert "session-mode-set.sh autonomous" in r.stderr  # names the missing step
    assert "by hand" in r.stderr
    assert _state(agent_dir) is None  # nothing written
    _set_mode(agent_dir)  # the remedy the refusal names, through the production setter
    assert _run(agent_dir, "RUNNING", sid="abc123").returncode == 0  # positive control
    assert _state(agent_dir) == "RUNNING"


@pytest.mark.parametrize("mode", ["reader", "assistant"])
def test_running_is_refused_under_any_other_recorded_mode(agent_dir: Path, mode: str) -> None:
    # A stale mode from the last /stop (assistant is where /stop lands) is the same defect.
    _everything_but_the_mode(agent_dir)
    _set_mode(agent_dir, mode)
    r = _run(agent_dir, "RUNNING", sid="abc123")
    assert r.returncode == 1 and "REJECTED" in r.stderr
    assert f"'{mode}'" in r.stderr and "session-mode-set.sh autonomous" in r.stderr
    assert _state(agent_dir) is None


def test_the_mode_check_runs_without_a_session_id_in_the_env_too(agent_dir: Path) -> None:
    # The binding and carrier checks need $MIND_SID to name their files; the mode check
    # needs nothing, so a caller with no sid in its env is held to it all the same.
    (agent_dir / "session" / "running-session-id").write_text("abc123\n", encoding="utf-8")
    r = _run(agent_dir, "RUNNING")
    assert r.returncode == 1 and "agent-mode" in r.stderr
    assert _state(agent_dir) is None


def test_a_runner_refusal_still_names_its_own_step_first_when_the_mode_is_missing_too(
    agent_dir: Path,
) -> None:
    # The mode is checked LAST. With two steps skipped, the model is sent back to the runner
    # claim first and meets the mode refusal on its next try: one missing step per refusal.
    r = _run(agent_dir, "RUNNING", sid="abc123")
    assert r.returncode == 1 and "running-session-id" in r.stderr
    assert "agent-mode" not in r.stderr
    assert _state(agent_dir) is None


def test_the_yank_reversal_can_never_meet_the_mode_refusal() -> None:
    # recovery-yank-reverse.sh is the one caller of `state set RUNNING` outside /start. Its
    # gate (recovery_yank.py preconditions) already refuses unless agent-mode is autonomous,
    # so the new check is unreachable from it. Pinned on the source: if that precondition is
    # ever dropped, a reversal would start failing at the flip and rolling back, silently.
    src = (PROJECT_ROOT / "core" / "scripts" / "recovery_yank.py").read_text(encoding="utf-8")
    assert 'sess / "agent-mode"' in src and "not autonomous" in src


def test_idle_is_never_gated(agent_dir: Path) -> None:
    # Recovery flips RUNNING -> IDLE BEFORE clearing the sid files; the gate must not
    # touch that direction (and a stale sid must not block it either). No mode is ever
    # recorded in this test, so it also proves the mode check leaves IDLE alone.
    (agent_dir / "session" / "running-session-id").write_text("abc123\n", encoding="utf-8")
    assert _run(agent_dir, "IDLE", sid="zzz999").returncode == 0
    assert _state(agent_dir) == "IDLE"
    (agent_dir / "session" / "running-session-id").unlink()
    assert _run(agent_dir, "IDLE").returncode == 0
    assert _state(agent_dir) == "IDLE"
