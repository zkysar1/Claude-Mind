"""Wrapper tests for aspirations-meta-update.sh (PR 52).

These tests exercise the wrapper end-to-end via subprocess, verifying that the
daemon-aware wrapper correctly:
  - Routes a single field update through the daemon
  - Coerces value types (true -> bool, integers, JSON objects)
  - Passes --source agent through to the endpoint
  - agent-aspirations-meta-update.sh delegates correctly
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-meta-update.sh"
AGENT_WRAPPER = REPO_ROOT / "core" / "scripts" / "agent-aspirations-meta-update.sh"


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _run(args, *, project_root: Path, agent: str = "alpha"):
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    proc = subprocess.run(
        [_bash(), WRAPPER.as_posix(), *args],
        env=env, capture_output=True, text=True, check=False, timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _run_agent(args, *, project_root: Path, agent: str = "alpha"):
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    proc = subprocess.run(
        [_bash(), AGENT_WRAPPER.as_posix(), *args],
        env=env, capture_output=True, text=True, check=False, timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_wrapper_single_field(running_daemon):
    """Wrapper sets a single field via the daemon."""
    project_root, _ = running_daemon
    meta_path = project_root / "world" / "aspirations-meta.json"
    meta_path.write_text(json.dumps({"session_count": 0}), encoding="utf-8")
    rc, out, err = _run(["session_count", "7"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    data = json.loads(out)
    assert data["session_count"] == 7


def test_wrapper_value_coercion(running_daemon):
    """Wrapper coerces true -> bool, integers, JSON objects."""
    project_root, _ = running_daemon
    meta_path = project_root / "world" / "aspirations-meta.json"
    meta_path.write_text(json.dumps({"session_count": 0}), encoding="utf-8")

    # Boolean
    rc, out, err = _run(["flag", "true"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    data = json.loads(out)
    assert data["flag"] is True

    # Integer
    rc, out, err = _run(["session_count", "42"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    data = json.loads(out)
    assert data["session_count"] == 42


def test_wrapper_source_agent(running_daemon):
    """Wrapper with --source agent writes to agent dir."""
    project_root, _ = running_daemon
    agent_meta = project_root / "agents" / "alpha" / "aspirations-meta.json"
    if agent_meta.exists():
        agent_meta.unlink()
    rc, out, err = _run(["--source", "agent", "session_count", "99"],
                        project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    data = json.loads(out)
    assert data["session_count"] == 99
    assert agent_meta.exists()


def test_agent_wrapper_shortcut(running_daemon):
    """agent-aspirations-meta-update.sh delegates with --source agent."""
    project_root, _ = running_daemon
    agent_meta = project_root / "agents" / "alpha" / "aspirations-meta.json"
    if agent_meta.exists():
        agent_meta.unlink()
    rc, out, err = _run_agent(["session_count", "55"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    data = json.loads(out)
    assert data["session_count"] == 55
    assert agent_meta.exists()
