"""test_cross_aspiration_support.py — supports[] scoring criterion.

Covers LifingPolls plan item 2 (2026-05-08).

Lanes:
  1. Goal without supports → cross_aspiration_support = 0.0 (additive default)
  2. Goal supports one near-complete aspiration → bonus reflects ratio²
  3. Goal supports one early aspiration → bonus is small
  4. Goal supports multiple aspirations → bonus sums but caps at 2.0
  5. Goal supports a non-existent asp_id → that contribution is 0
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
SELECTOR = CORE_SCRIPTS / "goal-selector.py"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _daemon_fixture import DaemonFixture  # noqa: E402


def _make_world(tmp: Path, *, target_supports: list[str] | None = None,
                supported_aspirations: list[dict] | None = None) -> tuple[Path, Path]:
    """Build a world with one candidate goal supporting given aspirations."""
    world = tmp / "world"
    world.mkdir()

    # The candidate goal is in asp-100 with supports = target_supports.
    candidate_goal = {
        "id": "g-100-01",
        "title": "Candidate goal",
        "description": "Test cross-aspiration support",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "achievedCount": 0,
        "participants": ["agent"],
        "category": "test",
    }
    if target_supports is not None:
        candidate_goal["supports"] = target_supports

    candidate_asp = {
        "id": "asp-100",
        "title": "Test parent",
        "motivation": "Parent of candidate",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-08T12:00:00",
        "goals": [candidate_goal],
    }

    asps = [candidate_asp]
    if supported_aspirations:
        asps.extend(supported_aspirations)

    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        for a in asps:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    (world / "pipeline.jsonl").write_text("", encoding="utf-8")
    (world / "pipeline-archive.jsonl").write_text("", encoding="utf-8")
    (world / "guardrails.jsonl").write_text("", encoding="utf-8")
    (world / "reasoning-bank.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world, agent_dir


def _make_supported_asp(asp_id: str, completed: int, total: int) -> dict:
    """Build a supporting aspiration with given completion fraction.

    g-115-887 note: every goal carries `category: "test"` so the goal-
    selector's category-suggest subprocess fallback (~2s per uncategorized
    pending goal on Windows) does NOT fire. test_supports_early (9 pending
    goals × ~2s = ~18s) was timing out at the 20s test budget without
    this. Pre-categorizing keeps the test under 5s and removes the
    subprocess-startup-time flake.
    """
    goals = []
    for i in range(total):
        status = "completed" if i < completed else "pending"
        g = {
            "id": f"{asp_id.replace('asp-', 'g-')}-{i+1:02d}",
            "title": f"goal {i+1}",
            "description": "test",
            "status": status,
            "priority": "MEDIUM",
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "origin_signal": "user_directive",
            "achievedCount": 1 if status == "completed" else 0,
            "participants": ["agent"],
            "category": "test",
        }
        if status == "completed":
            g["completed_date"] = "2026-05-08"
        goals.append(g)
    return {
        "id": asp_id,
        "title": f"Supported {asp_id}",
        "motivation": "Test",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-04-01T12:00:00",
        "goals": goals,
    }


def _run_selector(world: Path, agent_dir: Path) -> list[dict]:
    env = os.environ.copy()
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent_dir)
    proc = subprocess.run(
        [sys.executable, str(SELECTOR), "select"],
        capture_output=True, text=True, timeout=20, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _candidate_score(scored: list[dict], goal_id: str) -> dict:
    for s in scored:
        if s.get("goal_id") == goal_id:
            return s
    raise KeyError(goal_id)


# ---- Tests ----------------------------------------------------------------


def test_no_supports_zero_bonus():
    """Goal without supports[] gets 0 in cross_aspiration_support."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world(Path(tmpd))
        scored = _run_selector(world, agent_dir)
        s = _candidate_score(scored, "g-100-01")
        assert s["raw"]["cross_aspiration_support"] == 0.0


def test_supports_near_complete_aspiration_boosts():
    """Supporting an aspiration at 90% completion gives a substantial bonus."""
    with tempfile.TemporaryDirectory() as tmpd:
        # asp-200 at 9/10 = 0.9 ratio, 0.9² × 2.5 × 0.3 = 0.6075
        asp_200 = _make_supported_asp("asp-200", completed=9, total=10)
        world, agent_dir = _make_world(
            Path(tmpd), target_supports=["asp-200"],
            supported_aspirations=[asp_200])
        scored = _run_selector(world, agent_dir)
        s = _candidate_score(scored, "g-100-01")
        # Expected: 0.9² × 2.5 × 0.3 = 0.6075 → rounded to 0.608 in raw
        assert s["raw"]["cross_aspiration_support"] > 0.5
        assert s["raw"]["cross_aspiration_support"] < 0.7


def test_supports_early_aspiration_small_bonus():
    """Supporting an early-stage aspiration (1/10) gives a tiny bonus."""
    with tempfile.TemporaryDirectory() as tmpd:
        asp_200 = _make_supported_asp("asp-200", completed=1, total=10)
        world, agent_dir = _make_world(
            Path(tmpd), target_supports=["asp-200"],
            supported_aspirations=[asp_200])
        with DaemonFixture(world) as df:
            scored = _run_selector(world, agent_dir)
            s = _candidate_score(scored, "g-100-01")
            # 0.1² × 2.5 × 0.3 = 0.0075 → very small
            assert s["raw"]["cross_aspiration_support"] < 0.05


def test_multiple_supports_cap_at_2():
    """Many supports cap at +2.0."""
    with tempfile.TemporaryDirectory() as tmpd:
        # 5 near-complete supported asps: each ~0.6 → would sum to 3.0 raw,
        # capped at 2.0
        supported = [_make_supported_asp(f"asp-{200+i}", completed=9, total=10)
                     for i in range(5)]
        target_supports = [a["id"] for a in supported]
        world, agent_dir = _make_world(
            Path(tmpd), target_supports=target_supports,
            supported_aspirations=supported)
        scored = _run_selector(world, agent_dir)
        s = _candidate_score(scored, "g-100-01")
        assert s["raw"]["cross_aspiration_support"] == 2.0


def test_nonexistent_asp_contributes_zero():
    """Supporting a non-existent asp_id contributes 0, no error."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world(
            Path(tmpd), target_supports=["asp-999-nonexistent"])
        scored = _run_selector(world, agent_dir)
        s = _candidate_score(scored, "g-100-01")
        assert s["raw"]["cross_aspiration_support"] == 0.0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
