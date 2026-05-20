"""End-to-end wrapper test for aspirations-add-goal.sh.

Verify the wrapper talks to the daemon, re-emits advisories to stderr,
and prints the persisted goal to stdout.

Test strategy:
  - running_daemon fixture spawns a daemon in a tmp project_root
  - We override RT_DIR so the wrapper finds the tmp daemon's port file
    instead of REPO_ROOT/mind_api/state/
  - We exec the real wrapper from REPO_ROOT — PROJECT_ROOT it resolves
    points at REPO_ROOT, but RT_DIR override redirects daemon discovery
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-add-goal.sh"


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _run(args, *, stdin: str, project_root: Path, agent: str = "alpha"):
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MSYS_NO_PATHCONV"] = "1"
    # Redirect daemon discovery at the tmp daemon spawned by running_daemon.
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    proc = subprocess.run(
        [_bash(), WRAPPER.as_posix(), *args],
        env=env, input=stdin, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_wrapper_happy_path(running_daemon):
    """Daemon path: prints goal JSON to stdout, exits 0."""
    project_root, _ = running_daemon
    goal = {
        "title": "Investigate: wrapper happy path",
        "status": "pending",
        "origin_signal": "user_directive",
        "description": "x" * 100,
    }
    rc, out, err = _run(["asp-001"], stdin=json.dumps(goal),
                        project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["title"] == "Investigate: wrapper happy path"
    assert parsed["id"].startswith("g-001-")
    # Daemon-side mutator runs through the wrapper
    assert parsed["intended_agent"] == "zeta"
    # No warnings → stderr should not contain the description-length warning
    assert "description short" not in err


def test_wrapper_surfaces_advisory_warning_to_stderr(running_daemon):
    """Short description → wrapper re-emits the advisory to stderr."""
    project_root, _ = running_daemon
    goal = {
        "title": "Short",
        "status": "pending",
        "origin_signal": "user_directive",
        "description": "tiny",
    }
    rc, out, err = _run(["asp-001"], stdin=json.dumps(goal),
                        project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    assert "description short" in err
    # stdout still gets the persisted goal record (advisory is warn-only)
    parsed = json.loads(out)
    assert parsed["title"] == "Short"


def test_wrapper_forwards_override_header(running_daemon):
    """--override-no-investigate maps to X-Mind-Override-No-Investigate
    header which the daemon honors."""
    project_root, _ = running_daemon
    goal = {
        "title": "Apply: NPC behavior fix",
        "status": "pending",
        "origin_signal": "user_directive",
        "category": "npc-cognition",
        # no discovered_by — gate would block without override
    }
    rc, out, err = _run(
        ["--override-no-investigate", "emergency hotfix", "asp-001"],
        stdin=json.dumps(goal), project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["title"] == "Apply: NPC behavior fix"


def test_wrapper_gate_block_returns_nonzero(running_daemon):
    """Daemon 400 → wrapper exit 1, stderr carries the gate error body."""
    project_root, _ = running_daemon
    goal = {
        "title": "Apply: NPC behavior fix",
        "status": "pending",
        "origin_signal": "user_directive",
        "category": "npc-cognition",
        # missing discovered_by, no override
    }
    rc, out, err = _run(["asp-001"], stdin=json.dumps(goal),
                        project_root=project_root)
    assert rc == 1, f"expected exit 1 on gate block, got {rc}"
    assert "scaffolded_exploration_blocked" in err
