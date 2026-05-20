"""End-to-end wrapper test for aspirations-complete-by.sh.

Verify the wrapper talks to the daemon, prints the completed goal to
stdout, and handles the --key-finding cross-write to team-state.yaml.

Test strategy:
  - running_daemon fixture spawns a daemon in a tmp project_root
  - We override RT_DIR so the wrapper finds the tmp daemon's port file
  - We seed aspirations.jsonl with test data before each call
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-complete-by.sh"


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _seed_aspiration(world: Path, asp):
    path = world / "aspirations.jsonl"
    path.write_text(json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")


def _run(args, *, project_root: Path, agent: str = "alpha"):
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    proc = subprocess.run(
        [_bash(), WRAPPER.as_posix(), *args],
        env=env, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _make_asp_with_goal():
    return {
        "id": "asp-001",
        "title": "Test",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Goal to complete",
             "status": "in-progress", "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }


def test_wrapper_happy_path(running_daemon):
    """Daemon path: prints completed goal JSON to stdout, exits 0."""
    project_root, _ = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_goal())

    rc, out, err = _run(["g-001-01", "alpha"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["status"] == "completed"
    assert parsed["completed_by"] == "alpha"


def test_wrapper_goal_not_found_returns_nonzero(running_daemon):
    """Unknown goal -> wrapper exit 1."""
    project_root, _ = running_daemon

    rc, out, err = _run(["g-999-01", "alpha"], project_root=project_root)
    assert rc == 1, f"expected exit 1, got {rc}"
    assert "goal_not_found" in err


def test_wrapper_key_finding_cross_write(running_daemon):
    """--key-finding flag propagates to team-state.yaml."""
    project_root, _ = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_goal())

    rc, out, err = _run(
        ["g-001-01", "alpha", "--key-finding", "Found important pattern"],
        project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["status"] == "completed"

    # Verify team-state was updated
    ts_path = world / "team-state.yaml"
    with open(ts_path, "r", encoding="utf-8") as f:
        ts = yaml.safe_load(f)
    completions = ts.get("recent_completions", [])
    assert len(completions) == 1
    assert completions[0]["key_finding"] == "Found important pattern"
    assert completions[0]["goal_id"] == "g-001-01"


def test_wrapper_agent_defaults_to_env(running_daemon):
    """Agent name defaults to MIND_AGENT when not passed as positional."""
    project_root, _ = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_goal())

    rc, out, err = _run(["g-001-01"], project_root=project_root, agent="zeta")
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["completed_by"] == "zeta"


def test_wrapper_source_agent(running_daemon):
    """--source agent uses agent-local aspirations.jsonl."""
    project_root, _ = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    asp = {
        "id": "asp-100",
        "title": "Agent local",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "goals": [
            {"id": "g-100-01", "title": "Agent goal",
             "status": "in-progress", "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    (agent_dir / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")

    rc, out, err = _run(
        ["--source", "agent", "g-100-01", "alpha"],
        project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["status"] == "completed"
    assert parsed["completed_by"] == "alpha"
