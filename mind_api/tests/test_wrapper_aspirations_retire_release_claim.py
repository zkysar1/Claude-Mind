"""End-to-end wrapper tests for aspirations-retire/release/claim (PR 9b).

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
WRAPPER_RETIRE = REPO_ROOT / "core" / "scripts" / "aspirations-retire.sh"
WRAPPER_RELEASE = REPO_ROOT / "core" / "scripts" / "aspirations-release.sh"
WRAPPER_CLAIM = REPO_ROOT / "core" / "scripts" / "aspirations-claim.sh"


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _seed_aspiration(world: Path, asp):
    path = world / "aspirations.jsonl"
    path.write_text(json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")


def _run(wrapper, args, *, project_root: Path, agent: str = "alpha"):
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    proc = subprocess.run(
        [_bash(), wrapper.as_posix(), *args],
        env=env, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# retire wrapper tests
# ---------------------------------------------------------------------------

def test_wrapper_retire_happy_path(running_daemon):
    """Daemon path: prints retired aspiration JSON to stdout, exits 0."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Retire me", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Done", "status": "completed",
             "recurring": False},
        ],
        "progress": {"completed_goals": 1, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    rc, out, err = _run(WRAPPER_RETIRE, ["asp-001"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["status"] == "retired"
    assert parsed["archived"] is True


def test_wrapper_retire_guard_block(running_daemon):
    """Aspiration with recurring goals -> wrapper exit 1."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Has recurring", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Recurring", "status": "pending",
             "recurring": True, "interval_hours": 24},
        ],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 1},
    }
    _seed_aspiration(world, asp)

    rc, out, err = _run(WRAPPER_RETIRE, ["asp-001"], project_root=project_root)
    assert rc == 1, f"expected exit 1, got {rc}"
    assert "recurring_goals_present" in err


# ---------------------------------------------------------------------------
# release wrapper tests
# ---------------------------------------------------------------------------

def test_wrapper_release_happy_path(running_daemon):
    """Daemon path: prints released goal JSON to stdout, exits 0."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Test", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Claimed", "status": "in-progress",
             "recurring": False, "claimed_by": "alpha",
             "claimed_at": "2026-05-10T10:00:00"},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    rc, out, err = _run(WRAPPER_RELEASE, ["g-001-01"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert "claimed_by" not in parsed
    assert "claimed_at" not in parsed


def test_wrapper_release_not_found(running_daemon):
    """Unknown goal -> wrapper exit 1."""
    project_root, _ = running_daemon
    rc, out, err = _run(WRAPPER_RELEASE, ["g-999-01"], project_root=project_root)
    assert rc == 1, f"expected exit 1, got {rc}"
    assert "goal_not_found" in err


# ---------------------------------------------------------------------------
# claim wrapper tests
# ---------------------------------------------------------------------------

def test_wrapper_claim_happy_path(running_daemon):
    """Daemon path: prints claimed goal JSON to stdout, exits 0."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Test", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Claimable", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    rc, out, err = _run(WRAPPER_CLAIM, ["g-001-01", "alpha"],
                        project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["claimed_by"] == "alpha"
    assert "claimed_at" in parsed


def test_wrapper_claim_cross_lane_refused(running_daemon):
    """Goal routed to bravo, alpha claims without --cross-lane -> exit 2 (T2.2)."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Test", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Routed", "status": "pending",
             "recurring": False, "intended_agent": "bravo"},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    rc, out, err = _run(WRAPPER_CLAIM, ["g-001-01", "alpha"],
                        project_root=project_root)
    assert rc == 2, f"expected exit 2, got {rc}"
    assert "cross_lane_refused" in err
