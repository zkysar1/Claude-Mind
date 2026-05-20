"""End-to-end wrapper test for aspirations-complete-intent.sh.

Verify the wrapper talks to the daemon, pipes JSON body, and prints the
completed aspiration to stdout.

Test strategy:
  - running_daemon fixture spawns a daemon in a tmp project_root
  - We override RT_DIR so the wrapper finds the tmp daemon's port file
  - We seed aspirations.jsonl + aspirations.yaml before each call
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
WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-complete-intent.sh"


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _seed_aspiration(world: Path, asp):
    path = world / "aspirations.jsonl"
    path.write_text(json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")


def _ensure_intent_config(project_root: Path):
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "aspirations.yaml"
    if not cfg_path.exists():
        cfg_path.write_text(
            "intent_satisfaction:\n"
            "  min_evidence_by_scope:\n"
            "    sprint: 2\n"
            "    project: 3\n"
            "    initiative: 5\n",
            encoding="utf-8",
        )


def _run(args, *, project_root: Path, agent: str = "alpha", stdin_data: str = ""):
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    proc = subprocess.run(
        [_bash(), WRAPPER.as_posix(), *args],
        env=env, capture_output=True, text=True, check=False,
        input=stdin_data,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _make_intent_asp():
    return {
        "id": "asp-001",
        "title": "Intent test",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "scope": "sprint",
        "motivation": "Build a comprehensive testing framework for daemon endpoints",
        "goals": [
            {"id": "g-001-01", "title": "Done 1", "status": "completed",
             "recurring": False, "verification": {"outcomes": ["tests pass"]}},
            {"id": "g-001-02", "title": "Done 2", "status": "completed",
             "recurring": False, "verification": {"outcomes": ["more tests"]}},
            {"id": "g-001-03", "title": "Leftover", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 2, "total_goals": 3, "recurring_goals": 0},
    }


def _valid_intent_block():
    return {
        "evidence_goal_ids": ["g-001-01", "g-001-02"],
        "rationale": "The comprehensive testing framework is fully built and validated with daemon endpoint tests",
        "superseded_goal_ids": ["g-001-03"],
    }


def test_wrapper_happy_path(running_daemon):
    """Daemon path: prints completed aspiration JSON to stdout, exits 0."""
    project_root, _ = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    _seed_aspiration(world, _make_intent_asp())

    rc, out, err = _run(
        ["asp-001"],
        project_root=project_root,
        stdin_data=json.dumps(_valid_intent_block()),
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["status"] == "completed"
    assert parsed["archived"] is True
    assert "intent_satisfaction" in parsed


def test_wrapper_asp_not_found_returns_nonzero(running_daemon):
    """Unknown aspiration -> wrapper exit 1."""
    project_root, _ = running_daemon
    _ensure_intent_config(project_root)

    rc, out, err = _run(
        ["asp-999"],
        project_root=project_root,
        stdin_data=json.dumps(_valid_intent_block()),
    )
    assert rc == 1, f"expected exit 1, got {rc}"
    assert "aspiration_not_found" in err


def test_wrapper_validation_failure_returns_nonzero(running_daemon):
    """Validation error (bad rationale) -> wrapper exit 1."""
    project_root, _ = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    _seed_aspiration(world, _make_intent_asp())

    block = _valid_intent_block()
    block["rationale"] = "short"

    rc, out, err = _run(
        ["asp-001"],
        project_root=project_root,
        stdin_data=json.dumps(block),
    )
    assert rc == 1, f"expected exit 1, got {rc}"
    assert "intent_validation_failed" in err


def test_wrapper_source_agent(running_daemon):
    """--source agent uses agent-local aspirations.jsonl."""
    project_root, _ = running_daemon
    _ensure_intent_config(project_root)
    agent_dir = project_root / "agents" / "alpha"
    asp = _make_intent_asp()
    asp["id"] = "asp-100"
    for i, g in enumerate(asp["goals"]):
        g["id"] = f"g-100-{i+1:02d}"
    (agent_dir / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")

    block = {
        "evidence_goal_ids": ["g-100-01", "g-100-02"],
        "rationale": "The comprehensive testing framework is fully built and validated with daemon endpoint tests",
        "superseded_goal_ids": ["g-100-03"],
    }
    rc, out, err = _run(
        ["--source", "agent", "asp-100"],
        project_root=project_root,
        stdin_data=json.dumps(block),
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["status"] == "completed"
