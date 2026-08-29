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


def test_running_is_allowed_when_the_runner_sid_is_this_session(agent_dir: Path) -> None:
    (agent_dir / "session" / "running-session-id").write_text("abc123\n", encoding="utf-8")
    _carrier(agent_dir, "abc123")
    r = _run(agent_dir, "RUNNING", sid="abc123")
    assert r.returncode == 0, r.stderr
    assert _state(agent_dir) == "RUNNING"


def test_running_is_refused_without_this_sessions_liveness_carrier(agent_dir: Path) -> None:
    # The triple-write ran but the pre-flip heartbeat-tick did not: the tool-call-cadence
    # tick would never fire for this runner and its lease would starve through /boot.
    (agent_dir / "session" / "running-session-id").write_text("abc123\n", encoding="utf-8")
    r = _run(agent_dir, "RUNNING", sid="abc123")
    assert r.returncode == 1 and "REJECTED" in r.stderr and "carrier" in r.stderr
    assert _state(agent_dir) is None
    _carrier(agent_dir, "abc123")
    assert _run(agent_dir, "RUNNING", sid="abc123").returncode == 0  # positive control


def test_idle_is_never_gated(agent_dir: Path) -> None:
    # Recovery flips RUNNING -> IDLE BEFORE clearing the sid files; the gate must not
    # touch that direction (and a stale sid must not block it either).
    (agent_dir / "session" / "running-session-id").write_text("abc123\n", encoding="utf-8")
    assert _run(agent_dir, "IDLE", sid="zzz999").returncode == 0
    assert _state(agent_dir) == "IDLE"
    (agent_dir / "session" / "running-session-id").unlink()
    assert _run(agent_dir, "IDLE").returncode == 0
    assert _state(agent_dir) == "IDLE"
