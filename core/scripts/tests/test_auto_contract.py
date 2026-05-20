"""test_auto_contract.py — auto-contract recurring intervals via cargo-cult-detector.

Covers LifingPolls plan item 4 (2026-05-08): when a recurring goal's
consecutive_deep counter reaches threshold (default 3), the detector's
--contract-mode shrinks interval_hours by divisor (default 1.5×),
capped at floor_ratio × original_interval_hours (default 0.33×).

Lanes:
  1. Above floor: contract divides interval, resets consecutive_deep
  2. At/below floor: files an Idea proposing rebase, resets counter
  3. Original interval recorded on first contract
  4. Dedup: existing Rebase Idea suppresses re-fire
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
DETECTOR = CORE_SCRIPTS / "cargo-cult-detector.py"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _daemon_fixture import DaemonFixture  # noqa: E402


def _make_world(tmp: Path, *, interval_hours: float = 4.0,
                consecutive_deep: int = 0,
                original_interval_hours: float | None = None,
                extra_goals: list | None = None) -> Path:
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": "g-100-01",
        "title": "Recurring deep-prone goal",
        "description": "Test contract",
        "status": "pending",
        "priority": "MEDIUM",
        "recurring": True,
        "interval_hours": interval_hours,
        "consecutive_routine": 0,
        "consecutive_deep": consecutive_deep,
        "achievedCount": consecutive_deep + 1,
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    if original_interval_hours is not None:
        goal["original_interval_hours"] = original_interval_hours
    goals = [goal]
    if extra_goals:
        goals.extend(extra_goals)
    asp = {
        "id": "asp-100",
        "title": "Test asp",
        "motivation": "Test",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-08T12:00:00",
        "goals": goals,
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _run_contract(world: Path, goal_id: str, dry_run: bool = False):
    env = os.environ.copy()
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "alpha"
    args = [sys.executable, str(DETECTOR), goal_id, "--contract-mode",
            "--source", "world"]
    if dry_run:
        args.append("--dry-run")
    proc = subprocess.run(args, capture_output=True, text=True,
                          timeout=15, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _read_goal(world: Path, goal_id: str):
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                return g, asp
    return None, None


# ---- Tests ----------------------------------------------------------------


def test_above_floor_contracts_and_records_original():
    """4h / 1.5 = 2.67h, floor=0.33×4=1.32h → contract fires, records original."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), interval_hours=4.0,
                            consecutive_deep=3)
        rc, out, err = _run_contract(world, "g-100-01")
        assert rc == 0, err
        assert "auto-contracted" in out, out
        g, _ = _read_goal(world, "g-100-01")
        assert g["interval_hours"] == 2.67, g["interval_hours"]
        assert g["original_interval_hours"] == 4.0, g.get("original_interval_hours")
        assert g["consecutive_deep"] == 0


def test_repeated_contract_uses_recorded_original():
    """A second contract uses original_interval_hours as cap, not current."""
    with tempfile.TemporaryDirectory() as tmpd:
        # Simulate already-contracted state: 2.67h with original=4.0
        world = _make_world(Path(tmpd), interval_hours=2.67,
                            consecutive_deep=3,
                            original_interval_hours=4.0)
        rc, out, err = _run_contract(world, "g-100-01")
        assert rc == 0, err
        # 2.67 / 1.5 = 1.78, floor=0.33×4=1.32 → above floor → contracts
        g, _ = _read_goal(world, "g-100-01")
        assert g["interval_hours"] == 1.78, g["interval_hours"]
        assert g["original_interval_hours"] == 4.0


def test_at_floor_files_rebase_idea():
    """4h with already-contracted to 1.5h → next contract would be 1.0h, below floor 1.32h → Idea."""
    with tempfile.TemporaryDirectory() as tmpd:
        # interval already at 1.5, original 4.0 → proposed 1.5/1.5=1.0, floor=1.32
        # 1.0 < 1.32 → floor hit
        world = _make_world(Path(tmpd), interval_hours=1.5,
                            consecutive_deep=3,
                            original_interval_hours=4.0)
        with DaemonFixture(world) as df:
            rc, out, err = _run_contract(world, "g-100-01")
            assert rc == 0, err
            assert "floor HIT" in out or "Rebase original" in out, out
            g, asp = _read_goal(world, "g-100-01")
            # interval unchanged
            assert g["interval_hours"] == 1.5
            # consecutive_deep reset after Idea filed
            assert g["consecutive_deep"] == 0
            # Idea filed on the aspiration
            idea_titles = [g.get("title", "") for g in asp["goals"]]
            assert any("Rebase original interval" in t for t in idea_titles), idea_titles


def test_dedup_existing_rebase_idea_suppresses():
    """If a Rebase Idea already exists, second floor-hit doesn't double-file."""
    with tempfile.TemporaryDirectory() as tmpd:
        # Pre-existing Rebase Idea (status pending)
        existing_idea = {
            "id": "g-100-99",
            "title": "Idea: Rebase original interval for g-100-01",
            "description": "pre-existing",
            "status": "pending",
            "priority": "MEDIUM",
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "origin_signal": "investigate:contract-floor:g-100-01",
            "participants": ["agent"],
        }
        world = _make_world(Path(tmpd), interval_hours=1.5,
                            consecutive_deep=3,
                            original_interval_hours=4.0,
                            extra_goals=[existing_idea])
        rc, out, err = _run_contract(world, "g-100-01")
        assert rc == 0, err
        assert "dedup hit" in out, out
        # Counter still reset
        g, asp = _read_goal(world, "g-100-01")
        assert g["consecutive_deep"] == 0
        # Only one Rebase idea
        rebase_count = sum(
            1 for g in asp["goals"]
            if "Rebase original interval" in g.get("title", "")
        )
        assert rebase_count == 1


def test_dry_run_no_writes():
    """--dry-run prints what would happen but doesn't write."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), interval_hours=4.0,
                            consecutive_deep=3)
        rc, out, err = _run_contract(world, "g-100-01", dry_run=True)
        assert rc == 0, err
        assert "DRY-RUN" in out, out
        g, _ = _read_goal(world, "g-100-01")
        # Unchanged
        assert g["interval_hours"] == 4.0
        assert g["consecutive_deep"] == 3


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
