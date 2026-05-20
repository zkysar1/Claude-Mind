"""End-to-end wrapper test for aspirations-complete.sh.

Verify the wrapper talks to the daemon, re-emits advisories to stderr,
and prints the completed aspiration to stdout.

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


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-complete.sh"


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _seed_aspiration(world: Path, asp):
    """Overwrite aspirations.jsonl with a single aspiration dict."""
    path = world / "aspirations.jsonl"
    path.write_text(json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")


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


def _make_asp_all_completed():
    return {
        "id": "asp-001",
        "title": "Test complete",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "scope": "sprint",
        "goals": [
            {"id": "g-001-01", "title": "Done 1", "status": "completed",
             "recurring": False},
            {"id": "g-001-02", "title": "Done 2", "status": "skipped",
             "recurring": False},
        ],
        "progress": {"completed_goals": 1, "total_goals": 2, "recurring_goals": 0},
    }


def test_wrapper_happy_path(running_daemon):
    """Daemon path: prints completed aspiration JSON to stdout, exits 0."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = _make_asp_all_completed()
    _seed_aspiration(world, asp)

    rc, out, err = _run(["asp-001"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["status"] == "completed"
    assert parsed["archived"] is True
    assert parsed["id"] == "asp-001"


def test_wrapper_maturity_warning_to_stderr(running_daemon):
    """Project-scope asp with 0 sessions -> maturity warning on stderr."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = _make_asp_all_completed()
    asp["scope"] = "project"
    asp["sessions_active"] = 0
    _seed_aspiration(world, asp)

    rc, out, err = _run(["asp-001"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    assert "MATURITY WARNING" in err
    # stdout still gets the aspiration
    parsed = json.loads(out)
    assert parsed["status"] == "completed"


def test_wrapper_guard_block_returns_nonzero(running_daemon):
    """Aspiration with unfinished goals -> wrapper exit 1."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Blocked", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Pending", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    rc, out, err = _run(["asp-001"], project_root=project_root)
    assert rc == 1, f"expected exit 1 on guard block, got {rc}"
    assert "unfinished_goals_present" in err


def test_wrapper_force_flag(running_daemon):
    """--force bypasses guards via daemon."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Force", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Pending", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    rc, out, err = _run(["--force", "asp-001"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["status"] == "completed"
