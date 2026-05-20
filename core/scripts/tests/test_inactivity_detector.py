"""test_inactivity_detector.py — goal-completion silence detector.

Covers LifingPolls plan item 3 (2026-05-08).

Lanes:
  1. Latest completion within threshold → no-op
  2. Latest completion past threshold → Investigate filed
  3. Existing inactivity Investigate (pending) → dedup, no double-file
  4. Empty world → no-op (no completions to compare)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
DETECTOR = CORE_SCRIPTS / "inactivity-detector.py"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _daemon_fixture import DaemonFixture  # noqa: E402


def _make_world_with_target_asp(tmp: Path,
                                  *,
                                  latest_completion: datetime | None = None,
                                  target_already_has_invest: bool = False
                                  ) -> tuple[Path, Path]:
    world = tmp / "world"
    world.mkdir()
    target_goals = []
    if target_already_has_invest:
        target_goals.append({
            "id": "g-001-99",
            "title": "Investigate: Inactivity — no goal completions in 8.0h",
            "description": "earlier inactivity probe",
            "status": "pending",
            "priority": "MEDIUM",
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "origin_signal": "investigate:inactivity-silence",
            "achievedCount": 0,
            "participants": ["agent"],
        })
    target_asp = {
        "id": "asp-001",
        "title": "Maintain agent health",
        "motivation": "Test target",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-08T12:00:00",
        "goals": target_goals or [{
            "id": "g-001-01",
            "title": "Filler",
            "description": "Filler",
            "status": "pending",
            "priority": "MEDIUM",
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "origin_signal": "user_directive",
            "achievedCount": 0,
            "participants": ["agent"],
        }],
    }
    work_asp = {
        "id": "asp-100",
        "title": "Some work",
        "motivation": "Test",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-01T12:00:00",
        "goals": [{
            "id": "g-100-01",
            "title": "Done thing",
            "description": "Done",
            "status": "completed" if latest_completion else "pending",
            "completed_date": (latest_completion.isoformat(timespec="seconds")
                                if latest_completion else None),
            "priority": "MEDIUM",
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "origin_signal": "user_directive",
            "achievedCount": 0,
            "participants": ["agent"],
        }],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(target_asp, ensure_ascii=False) + "\n")
        f.write(json.dumps(work_asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    (world / "reasoning-bank.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world, agent_dir


def _run(world: Path, agent_dir: Path, *, silence_hours: float = 6.0,
         dry_run: bool = False):
    env = os.environ.copy()
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent_dir)
    args = [sys.executable, str(DETECTOR),
            "--silence-hours", str(silence_hours),
            "--target-asp", "asp-001"]
    if dry_run:
        args.append("--dry-run")
    proc = subprocess.run(args, capture_output=True, text=True,
                          timeout=15, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _read_target_goals(world: Path) -> list[dict]:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        if asp.get("id") == "asp-001":
            return asp["goals"]
    return []


# ---- Tests ----------------------------------------------------------------


def test_recent_completion_no_op():
    """Latest completion within threshold → detector exits 0 without filing."""
    with tempfile.TemporaryDirectory() as tmpd:
        recent = datetime.now() - timedelta(hours=1)
        world, agent_dir = _make_world_with_target_asp(
            Path(tmpd), latest_completion=recent)
        rc, out, err = _run(world, agent_dir, silence_hours=6.0)
        assert rc == 0, err
        goals = _read_target_goals(world)
        invest = [g for g in goals if "inactivity" in g.get("title", "").lower()]
        assert len(invest) == 0


def test_silence_files_investigate():
    """Latest completion past threshold → Investigate filed."""
    with tempfile.TemporaryDirectory() as tmpd:
        old = datetime.now() - timedelta(hours=10)
        world, agent_dir = _make_world_with_target_asp(
            Path(tmpd), latest_completion=old)
        with DaemonFixture(world) as df:
            rc, out, err = _run(world, agent_dir, silence_hours=6.0)
            assert rc == 0, err
            goals = _read_target_goals(world)
            invest = [g for g in goals if "inactivity" in g.get("title", "").lower()]
            # Filter out pre-existing
            new = [g for g in invest if g["id"] != "g-001-99"]
            assert len(new) == 1, [g.get("title") for g in invest]


def test_dedup_existing_pending_invest():
    """Existing pending inactivity Investigate prevents re-fire."""
    with tempfile.TemporaryDirectory() as tmpd:
        old = datetime.now() - timedelta(hours=10)
        world, agent_dir = _make_world_with_target_asp(
            Path(tmpd), latest_completion=old, target_already_has_invest=True)
        rc, out, err = _run(world, agent_dir, silence_hours=6.0)
        assert rc == 0, err
        assert "dedup hit" in out, out
        goals = _read_target_goals(world)
        invest = [g for g in goals if "inactivity" in g.get("title", "").lower()]
        # Only the pre-existing one
        assert len(invest) == 1
        assert invest[0]["id"] == "g-001-99"


def test_no_completions_anywhere_no_op():
    """Fresh world (no completions found) → detector exits 0."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_with_target_asp(
            Path(tmpd), latest_completion=None)
        rc, out, err = _run(world, agent_dir, silence_hours=6.0)
        assert rc == 0, err
        assert "no goal completions found" in out, out


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
