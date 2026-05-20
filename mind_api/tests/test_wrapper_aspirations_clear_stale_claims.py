"""End-to-end wrapper test for aspirations-clear-stale-claims.sh (PR 50).

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
WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-clear-stale-claims.sh"


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
        "goals": goals or [],
        "progress": {"completed_goals": 0, "total_goals": 0},
    }


def _make_goal(goal_id: str, status: str = "completed", **kwargs) -> Dict[str, Any]:
    g = {"id": goal_id, "title": f"Goal {goal_id}", "status": status}
    g.update(kwargs)
    return g


def test_wrapper_happy_path(running_daemon):
    """Daemon path: prints cleared count to stdout, exits 0."""
    project_root, _ = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       claimed_by="alpha", claimed_at="2026-05-01T00:00:00"),
        ]),
    ])

    rc, out, err = _run([], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    assert "cleared 1 records" in out


def test_wrapper_no_claims(running_daemon):
    """Nothing to clear → prints 'cleared 0 records'."""
    project_root, _ = running_daemon
    rc, out, err = _run([], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    assert "cleared 0 records" in out


def test_wrapper_dry_run(running_daemon):
    """--dry-run flag: prints 'would clear' without modifying files."""
    project_root, _ = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       claimed_by="alpha", claimed_at="2026-05-01T00:00:00"),
        ]),
    ])

    rc, out, err = _run(["--dry-run"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    assert "would clear 1 records" in out

    # Verify file was NOT modified
    import json as _json
    items = []
    for line in (world / "aspirations.jsonl").read_text().splitlines():
        if line.strip():
            items.append(_json.loads(line))
    goal = items[0]["goals"][0]
    assert goal["claimed_by"] == "alpha"


def test_wrapper_source_agent(running_daemon):
    """--source agent flag is forwarded correctly."""
    project_root, _ = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _write_jsonl(agent_dir / "aspirations.jsonl", [
        _make_asp("asp-100", "active", goals=[
            _make_goal("g-100-01", "completed",
                       claimed_by="alpha", claimed_at="2026-05-01T00:00:00"),
        ]),
    ])

    rc, out, err = _run(["--source", "agent"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    assert "cleared 1 records" in out
