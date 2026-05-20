"""Wrapper subprocess tests for aspirations-add.sh (PR 49).

Covers:
  - Happy path: wrapper invokes daemon and prints aspiration JSON
  - Warnings emitted to stderr
  - Gate block returns non-zero exit code
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-add.sh"


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _run(args, *, project_root: Path, agent: str = "alpha", stdin: str = ""):
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    proc = subprocess.run(
        [_bash(), WRAPPER.as_posix(), *args],
        env=env, input=stdin, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_wrapper_happy_path(running_daemon):
    """Daemon path: prints aspiration JSON to stdout, exits 0."""
    project_root, _ = running_daemon
    asp = {
        "id": "asp-070",
        "title": "Wrapper test",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "goals": [
            {"id": "g-070-01", "title": "G1", "status": "pending",
             "origin_signal": "user_directive"},
        ],
    }
    rc, out, err = _run(["--source", "world"],
                        project_root=project_root, stdin=json.dumps(asp))
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["id"] == "asp-070"
    assert len(parsed["goals"]) == 1


def test_wrapper_surfaces_warnings_to_stderr(running_daemon):
    """Goals with user participants but no user_leg_scope emit a warning."""
    project_root, _ = running_daemon
    asp = {
        "id": "asp-071",
        "title": "Wrapper warn test",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "goals": [
            {"id": "g-071-01", "title": "G1", "status": "pending",
             "participants": ["user"],
             "origin_signal": "user_directive"},
        ],
    }
    rc, out, err = _run(["--source", "world"],
                        project_root=project_root, stdin=json.dumps(asp))
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    # user_leg_scope advisory should appear on stderr
    assert "user_leg_scope" in err.lower() or "user" in err.lower()


def test_wrapper_gate_block_returns_nonzero(running_daemon):
    """Origin-signal gate block → wrapper exits non-zero."""
    project_root, _ = running_daemon
    asp = {
        "id": "asp-073",
        "title": "Gate block test",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "goals": [
            {"id": "g-073-01", "title": "No signal", "status": "pending"},
        ],
    }
    rc, out, err = _run(["--source", "agent"],
                        project_root=project_root, stdin=json.dumps(asp))
    assert rc == 1, f"expected exit 1 on gate block, got {rc}"
    assert "origin_signal" in err.lower()
