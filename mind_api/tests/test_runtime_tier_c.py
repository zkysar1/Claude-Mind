"""PR 6 — Tier C inline-script tests.

Three session-read scripts (state-get, mode-get, signal-exists) were inlined
to skip the python startup cost (~300ms on Windows) for trivial file reads.
This file pins their behavior at the subprocess level.

Each script runs in an isolated tmp dir layout — we copy the scripts into
tmp_path/core/scripts/ so the PROJECT_ROOT computed as `dirname/../..` lands
in tmp_path, not the real repo. No daemon involved.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = [
    "session-state-get.sh",
    "session-mode-get.sh",
    "session-signal-exists.sh",
]


def _bash() -> str:
    return shutil.which("bash") or "bash"


@pytest.fixture
def isolated_scripts(tmp_path: Path) -> Path:
    """Stage the inline scripts under tmp_path so they resolve PROJECT_ROOT
    to tmp_path. Returns tmp_path (the new PROJECT_ROOT)."""
    scripts_dir = tmp_path / "core" / "scripts"
    scripts_dir.mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copy(REPO_ROOT / "core" / "scripts" / name, scripts_dir / name)
    return tmp_path


def _run(script_path: Path, args: list[str], env_extra: dict | None = None) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    # Clear MIND_AGENT by default; individual tests opt in.
    env.pop("MIND_AGENT", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [_bash(), script_path.as_posix(), *args],
        env=env, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# session-state-get
# ---------------------------------------------------------------------------

def test_state_get_no_agent(isolated_scripts: Path):
    """MIND_AGENT unset → 'NO_AGENT' on stdout, exit 0."""
    script = isolated_scripts / "core" / "scripts" / "session-state-get.sh"
    rc, out, _ = _run(script, [])
    assert rc == 0
    assert out.strip() == "NO_AGENT"


def test_state_get_uninitialized(isolated_scripts: Path):
    """Agent dir exists but agent-state file absent → 'UNINITIALIZED'."""
    (isolated_scripts / "agents" / "alpha" / "session").mkdir(parents=True)
    script = isolated_scripts / "core" / "scripts" / "session-state-get.sh"
    rc, out, _ = _run(script, [], env_extra={"MIND_AGENT": "alpha"})
    assert rc == 0
    assert out.strip() == "UNINITIALIZED"


def test_state_get_running(isolated_scripts: Path):
    session = isolated_scripts / "agents" / "alpha" / "session"
    session.mkdir(parents=True)
    (session / "agent-state").write_text("RUNNING\n", encoding="utf-8")
    script = isolated_scripts / "core" / "scripts" / "session-state-get.sh"
    rc, out, _ = _run(script, [], env_extra={"MIND_AGENT": "alpha"})
    assert rc == 0
    assert out.strip() == "RUNNING"


def test_state_get_idle(isolated_scripts: Path):
    session = isolated_scripts / "agents" / "alpha" / "session"
    session.mkdir(parents=True)
    (session / "agent-state").write_text("IDLE\n", encoding="utf-8")
    script = isolated_scripts / "core" / "scripts" / "session-state-get.sh"
    rc, out, _ = _run(script, [], env_extra={"MIND_AGENT": "alpha"})
    assert rc == 0
    assert out.strip() == "IDLE"


def test_state_get_strips_crlf(isolated_scripts: Path):
    """File written with Windows line endings still parses to bare 'RUNNING'.
    Defends against the cp1252/CRLF case on real Windows checkouts."""
    session = isolated_scripts / "agents" / "alpha" / "session"
    session.mkdir(parents=True)
    (session / "agent-state").write_bytes(b"RUNNING\r\n")
    script = isolated_scripts / "core" / "scripts" / "session-state-get.sh"
    rc, out, _ = _run(script, [], env_extra={"MIND_AGENT": "alpha"})
    assert rc == 0
    assert out.strip() == "RUNNING"


# ---------------------------------------------------------------------------
# session-mode-get
# ---------------------------------------------------------------------------

def test_mode_get_no_agent(isolated_scripts: Path):
    script = isolated_scripts / "core" / "scripts" / "session-mode-get.sh"
    rc, out, _ = _run(script, [])
    assert rc == 0
    assert out.strip() == "NO_AGENT"


def test_mode_get_defaults_to_reader(isolated_scripts: Path):
    """No agent-mode file → DEFAULT_MODE = reader."""
    (isolated_scripts / "agents" / "alpha" / "session").mkdir(parents=True)
    script = isolated_scripts / "core" / "scripts" / "session-mode-get.sh"
    rc, out, _ = _run(script, [], env_extra={"MIND_AGENT": "alpha"})
    assert rc == 0
    assert out.strip() == "reader"


def test_mode_get_reads_file(isolated_scripts: Path):
    session = isolated_scripts / "agents" / "alpha" / "session"
    session.mkdir(parents=True)
    (session / "agent-mode").write_text("autonomous\n", encoding="utf-8")
    script = isolated_scripts / "core" / "scripts" / "session-mode-get.sh"
    rc, out, _ = _run(script, [], env_extra={"MIND_AGENT": "alpha"})
    assert rc == 0
    assert out.strip() == "autonomous"


# ---------------------------------------------------------------------------
# session-signal-exists
# ---------------------------------------------------------------------------

def test_signal_exists_no_agent(isolated_scripts: Path):
    """Missing MIND_AGENT → exit 1 with stderr error (NOT exit 2 — that's
    reserved for invalid signal names)."""
    script = isolated_scripts / "core" / "scripts" / "session-signal-exists.sh"
    rc, _, err = _run(script, ["loop-active"])
    assert rc == 1
    assert "no agent active" in err


def test_signal_exists_missing_arg(isolated_scripts: Path):
    """No signal name → exit 2 with 'signal name required'."""
    script = isolated_scripts / "core" / "scripts" / "session-signal-exists.sh"
    rc, _, err = _run(script, [], env_extra={"MIND_AGENT": "alpha"})
    assert rc == 2
    assert "signal name required" in err


def test_signal_exists_invalid_name(isolated_scripts: Path):
    """Unknown signal → exit 2 with 'Invalid signal name'."""
    (isolated_scripts / "agents" / "alpha" / "session").mkdir(parents=True)
    script = isolated_scripts / "core" / "scripts" / "session-signal-exists.sh"
    rc, _, err = _run(script, ["bogus-name"], env_extra={"MIND_AGENT": "alpha"})
    assert rc == 2
    assert "Invalid signal name 'bogus-name'" in err


def test_signal_exists_absent(isolated_scripts: Path):
    """Signal name valid, file absent → exit 1, no output."""
    (isolated_scripts / "agents" / "alpha" / "session").mkdir(parents=True)
    script = isolated_scripts / "core" / "scripts" / "session-signal-exists.sh"
    rc, out, err = _run(script, ["loop-active"], env_extra={"MIND_AGENT": "alpha"})
    assert rc == 1
    assert out == ""
    assert err == ""


def test_signal_exists_present(isolated_scripts: Path):
    """Signal file present → exit 0."""
    session = isolated_scripts / "agents" / "alpha" / "session"
    session.mkdir(parents=True)
    (session / "stop-requested").touch()
    script = isolated_scripts / "core" / "scripts" / "session-signal-exists.sh"
    rc, _, _ = _run(script, ["stop-requested"], env_extra={"MIND_AGENT": "alpha"})
    assert rc == 0


def test_signal_exists_all_valid_names_recognized(isolated_scripts: Path):
    """Every name in VALID_SIGNALS is accepted by the bash mirror.
    Locks the case-statement in sync with session.py:36 — when adding a new
    signal upstream, this test fails until the case is updated too."""
    # Mirror — keep in sync with session-signal-exists.sh and session.py:36.
    valid = [
        "loop-active", "stop-loop", "stop-requested", "blocker-cleared",
        "pq-resolved", "board-activity", "email-received", "goal-claim-released",
    ]
    (isolated_scripts / "agents" / "alpha" / "session").mkdir(parents=True)
    script = isolated_scripts / "core" / "scripts" / "session-signal-exists.sh"
    for name in valid:
        rc, _, err = _run(script, [name], env_extra={"MIND_AGENT": "alpha"})
        # Absent signal → exit 1 with NO stderr; invalid name → exit 2 with stderr.
        assert rc == 1, f"{name} should be a recognized name (got rc={rc}, err={err})"
        assert err == "", f"{name} produced stderr: {err}"
