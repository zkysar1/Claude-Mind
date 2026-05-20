"""End-to-end wrapper test for aspirations-archive.sh (PR 9d).

Test strategy:
  - running_daemon fixture spawns a daemon in a tmp project_root
  - We override RT_DIR so the wrapper finds the tmp daemon's port file
  - We exec the real wrapper from REPO_ROOT
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-archive.sh"


def _bash() -> str:
    return shutil.which("bash") or "bash"


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


def _write_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=True) + "\n")


def _make_asp(asp_id: str, status: str = "active", goals=None) -> Dict[str, Any]:
    return {
        "id": asp_id,
        "title": f"Test {asp_id}",
        "status": status,
        "priority": "LOW",
        "archived": status in ("completed", "retired"),
        "goals": goals or [],
        "progress": {"completed_goals": 0, "total_goals": 0},
    }


def _make_goal(goal_id: str, status: str = "completed", **kwargs) -> Dict[str, Any]:
    g = {"id": goal_id, "title": f"Goal {goal_id}", "status": status}
    g.update(kwargs)
    return g


def test_wrapper_happy_path(running_daemon):
    """Daemon path: prints archived count to stdout, exits 0."""
    project_root, _ = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "completed", goals=[
            _make_goal("g-001-01", "completed"),
        ]),
    ])

    rc, out, err = _run([], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    assert out.strip() == "1"


def test_wrapper_empty_sweep(running_daemon):
    """Nothing to archive → prints 0."""
    project_root, _ = running_daemon
    rc, out, err = _run([], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    assert out.strip() == "0"


def test_wrapper_warnings_to_stderr(running_daemon):
    """Recovery warnings are emitted to stderr."""
    project_root, _ = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "completed", goals=[
            _make_goal("g-001-01", "completed", recurring=True, interval_hours=24),
        ]),
    ])

    rc, out, err = _run([], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    assert out.strip() == "0"
    assert "WARNING" in err
    assert "Recovering" in err


def test_wrapper_source_agent(running_daemon):
    """--source agent flag is forwarded correctly."""
    project_root, _ = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _write_jsonl(agent_dir / "aspirations.jsonl", [
        _make_asp("asp-100", "completed", goals=[
            _make_goal("g-100-01", "completed"),
        ]),
    ])

    rc, out, err = _run(["--source", "agent"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    assert out.strip() == "1"
