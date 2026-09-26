"""`session-mode-set.sh reader|assistant` refuses under a RUNNING runner ().

agent-mode is agent-wide, so a write from ANY session re-modes the live runner. Twice an
observer session ran /start's IDLE-branch assistant block against a RUNNING agent (a
peer-deployment observer 2026-08-28; a hosted conversation session 2026-09-25) and flipped
agent-mode to assistant while agent-state stayed RUNNING, so the runner stopped claiming
work. The refusal lives in `session.py::refuse_demotion_while_running`. It fires only
for reader|assistant, only while agent-state is RUNNING, and only when no stop is in
flight: /stop's own path (stop-target-mode, stop-requested) and graceful-stop's
checkpoint each exempt it, and the IDLE branches of /stop and /start never reach it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SESSION_PY = PROJECT_ROOT / "core" / "scripts" / "session.py"
STOP_MARKERS = ("stop-requested", "stop-target-mode", "stop-checkpoint.json")


def _mode_set(agent_dir: Path, mode: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("MIND_SID", "MIND_AGENT_DIR")}
    env.update({"MIND_AGENT": "gate-probe", "MIND_AGENT_DIR": str(agent_dir),
                "STORAGE_BACKEND": "local"})
    return subprocess.run([sys.executable, str(SESSION_PY), "mode", "set", mode],
                          cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    d = tmp_path / "gate-probe"
    (d / "session").mkdir(parents=True)
    (d / "session" / "agent-mode").write_text("autonomous\n", encoding="utf-8")
    return d


def _set_state(agent_dir: Path, state: str) -> None:
    (agent_dir / "session" / "agent-state").write_text(state + "\n", encoding="utf-8")


def _mode(agent_dir: Path) -> str:
    return (agent_dir / "session" / "agent-mode").read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("mode", ["reader", "assistant"])
def test_running_without_stop_in_flight_refuses_and_leaves_mode(agent_dir, mode):
    _set_state(agent_dir, "RUNNING")
    r = _mode_set(agent_dir, mode)
    assert r.returncode == 1
    assert "REJECTED" in r.stderr and "OBSERVER" in r.stderr
    assert _mode(agent_dir) == "autonomous"


def test_running_accepts_autonomous(agent_dir):
    _set_state(agent_dir, "RUNNING")
    r = _mode_set(agent_dir, "autonomous")
    assert r.returncode == 0, r.stderr
    assert _mode(agent_dir) == "autonomous"


@pytest.mark.parametrize("marker", STOP_MARKERS)
def test_running_with_stop_in_flight_allows_demotion(agent_dir, marker):
    _set_state(agent_dir, "RUNNING")
    (agent_dir / "session" / marker).write_text("assistant\n", encoding="utf-8")
    r = _mode_set(agent_dir, "assistant")
    assert r.returncode == 0, r.stderr
    assert _mode(agent_dir) == "assistant"


@pytest.mark.parametrize("state", ["IDLE", None])
def test_not_running_allows_demotion(agent_dir, state):
    if state is not None:
        _set_state(agent_dir, state)
    r = _mode_set(agent_dir, "reader")
    assert r.returncode == 0, r.stderr
    assert _mode(agent_dir) == "reader"
