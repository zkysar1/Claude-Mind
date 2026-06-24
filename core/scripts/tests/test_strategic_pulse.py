"""test_strategic_pulse.py — portfolio-shape detectors.

Covers LifingPolls plan item 10 (2026-05-08).

Lanes:
  1. Empty / healthy state → no patterns
  2. Tail consolidation: 5+ aspirations ≥75% complete → pattern fires
  3. Work-class skew: hygiene >2× target → pattern fires
  4. Idle aspiration: all goals blocked → pattern fires
  5. Aged aspiration: no completion in 60+ days → pattern fires
  6. Strategic drift (US-06, g-305-06): recent self-initiated goals mostly
     NOT moving the objective (outcome_class proxy) → pattern fires; silent
     below the window, on a healthy ratio, on user-directed-only goals (US-01
     composition), and on outcome_class data gaps.
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
PULSE = CORE_SCRIPTS / "strategic-pulse-detectors.py"


def _make_world(tmp: Path, asps: list[dict]) -> tuple[Path, Path]:
    world = tmp / "world"
    world.mkdir()
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        for a in asps:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world, agent_dir


def _make_asp(asp_id: str, *, completed: int = 0, total: int = 0,
              created: str = "2026-05-01T12:00:00",
              status: str = "active",
              all_blocked: bool = False,
              work_class: str = "product",
              latest_completion: str | None = None,
              outcome_classes: list[str] | None = None,
              goal_source: str = "user") -> dict:
    goals = []
    for i in range(total):
        if i < completed:
            g = {
                "id": f"{asp_id.replace('asp-', 'g-')}-{i+1:02d}",
                "title": f"goal {i+1}",
                "description": "test",
                "status": "completed",
                "completed_date": latest_completion or "2026-05-08T12:00:00",
                "priority": "MEDIUM",
                "blocked_by": [],
                "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
                "origin_signal": "user_directive",
                "achievedCount": 1,
                "participants": ["agent"],
                "work_class": work_class,
            }
            g["goal_source"] = goal_source
            if outcome_classes is not None:
                g["outcome_class"] = outcome_classes[i % len(outcome_classes)]
        elif all_blocked:
            g = {
                "id": f"{asp_id.replace('asp-', 'g-')}-{i+1:02d}",
                "title": f"goal {i+1}",
                "description": "test",
                "status": "blocked",
                "priority": "MEDIUM",
                "blocked_by": ["g-other-99"],
                "blocked_since": "2026-04-01T12:00:00",
                "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
                "origin_signal": "user_directive",
                "achievedCount": 0,
                "participants": ["agent"],
                "work_class": work_class,
            }
        else:
            g = {
                "id": f"{asp_id.replace('asp-', 'g-')}-{i+1:02d}",
                "title": f"goal {i+1}",
                "description": "test",
                "status": "pending",
                "priority": "MEDIUM",
                "blocked_by": [],
                "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
                "origin_signal": "user_directive",
                "achievedCount": 0,
                "participants": ["agent"],
                "work_class": work_class,
            }
        goals.append(g)
    return {
        "id": asp_id,
        "title": f"Test {asp_id}",
        "motivation": "Test",
        "scope": "project",
        "priority": "MEDIUM",
        "status": status,
        "created": created,
        "goals": goals,
    }


def _run_pulse(world: Path, agent_dir: Path) -> list[dict]:
    env = os.environ.copy()
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent_dir)
    proc = subprocess.run(
        [sys.executable, str(PULSE), "--json"],
        capture_output=True, text=True, timeout=15, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ---- Tests ----------------------------------------------------------------


def test_healthy_portfolio_no_patterns():
    """A balanced healthy portfolio (no skew, no idle, no aged, no tail)
    surfaces no patterns."""
    with tempfile.TemporaryDirectory() as tmpd:
        # 1 aspiration in early stage with mixed work_class
        asps = [
            _make_asp("asp-100", completed=2, total=10, work_class="product"),
            _make_asp("asp-101", completed=1, total=10, work_class="framework"),
            _make_asp("asp-102", completed=1, total=10, work_class="research"),
            _make_asp("asp-103", completed=1, total=10, work_class="hygiene"),
        ]
        world, agent_dir = _make_world(Path(tmpd), asps)
        pulses = _run_pulse(world, agent_dir)
        # No patterns expected
        pattern_names = [p["pattern"] for p in pulses]
        assert "tail_consolidation" not in pattern_names
        assert "idle_aspirations" not in pattern_names


def test_tail_consolidation_fires_at_5_high_completion():
    """5+ aspirations ≥75% complete trigger tail_consolidation."""
    with tempfile.TemporaryDirectory() as tmpd:
        asps = [_make_asp(f"asp-{200+i}", completed=8, total=10)
                for i in range(6)]
        world, agent_dir = _make_world(Path(tmpd), asps)
        pulses = _run_pulse(world, agent_dir)
        names = [p["pattern"] for p in pulses]
        assert "tail_consolidation" in names
        tc = next(p for p in pulses if p["pattern"] == "tail_consolidation")
        assert tc["evidence"]["high_completion_count"] == 6


def test_work_class_skew_fires():
    """One work_class >2× its target triggers work_class_skew.

    Default target for hygiene is 0.15. We create lots of hygiene goals so
    actual hygiene fraction is >0.30 (2× target).
    """
    with tempfile.TemporaryDirectory() as tmpd:
        asps = [_make_asp(f"asp-{300+i}", completed=0, total=20,
                          work_class="hygiene")
                for i in range(2)]
        # Add a tiny bit of product to make total >0
        asps.append(_make_asp("asp-310", completed=0, total=2,
                              work_class="product"))
        world, agent_dir = _make_world(Path(tmpd), asps)
        pulses = _run_pulse(world, agent_dir)
        names = [p["pattern"] for p in pulses]
        assert "work_class_skew" in names, names
        skew = next(p for p in pulses if p["pattern"] == "work_class_skew")
        skewed_classes = [s["work_class"]
                          for s in skew["evidence"]["skewed_classes"]]
        assert "hygiene" in skewed_classes


def test_idle_aspiration_fires():
    """An active aspiration with all goals blocked triggers idle_aspirations."""
    with tempfile.TemporaryDirectory() as tmpd:
        asps = [
            _make_asp("asp-400", completed=0, total=3, all_blocked=True),
        ]
        world, agent_dir = _make_world(Path(tmpd), asps)
        pulses = _run_pulse(world, agent_dir)
        names = [p["pattern"] for p in pulses]
        assert "idle_aspirations" in names
        idle = next(p for p in pulses if p["pattern"] == "idle_aspirations")
        assert idle["evidence"]["idle_count"] >= 1


def test_aged_aspiration_fires():
    """Aspiration with last completion >60d ago triggers aged_aspirations."""
    with tempfile.TemporaryDirectory() as tmpd:
        # Old completion + old created date. Aged threshold is 60 days.
        old_date = (datetime.now() - timedelta(days=90)).strftime(
            "%Y-%m-%dT%H:%M:%S")
        asps = [
            _make_asp("asp-500", completed=2, total=10,
                      created=old_date,
                      latest_completion=old_date),
        ]
        world, agent_dir = _make_world(Path(tmpd), asps)
        pulses = _run_pulse(world, agent_dir)
        names = [p["pattern"] for p in pulses]
        assert "aged_aspirations" in names
        aged = next(p for p in pulses if p["pattern"] == "aged_aspirations")
        assert aged["evidence"]["aged_count"] == 1


def test_strategic_drift_fires_on_low_substance_ratio():
    """5 self-initiated goals, 2 deep / 3 routine → moved_ratio 0.4 < 0.5
    → strategic_drift fires and names the aspiration."""
    with tempfile.TemporaryDirectory() as tmpd:
        asps = [_make_asp("asp-600", completed=5, total=5,
                          goal_source="agent-self",
                          outcome_classes=["deep", "deep", "routine",
                                           "routine", "routine"])]
        world, agent_dir = _make_world(Path(tmpd), asps)
        pulses = _run_pulse(world, agent_dir)
        names = [p["pattern"] for p in pulses]
        assert "strategic_drift" in names, names
        sd = next(p for p in pulses if p["pattern"] == "strategic_drift")
        ex = sd["evidence"]["examples"][0]
        assert ex["asp_id"] == "asp-600"
        assert ex["moved_ratio"] == 0.4
        assert ex["window_size"] == 5


def test_strategic_drift_silent_when_substance_healthy():
    """5 self-initiated goals, 4 deep / 1 routine → ratio 0.8 ≥ 0.5
    → no strategic_drift flag."""
    with tempfile.TemporaryDirectory() as tmpd:
        asps = [_make_asp("asp-601", completed=5, total=5,
                          goal_source="agent-self",
                          outcome_classes=["deep", "deep", "deep",
                                           "deep", "routine"])]
        world, agent_dir = _make_world(Path(tmpd), asps)
        pulses = _run_pulse(world, agent_dir)
        assert "strategic_drift" not in [p["pattern"] for p in pulses]


def test_strategic_drift_requires_full_window():
    """Only 4 qualifying goals (< window of 5), all routine → insufficient
    signal → no flag (a short run must not trip the detector)."""
    with tempfile.TemporaryDirectory() as tmpd:
        asps = [_make_asp("asp-602", completed=4, total=4,
                          goal_source="agent-self",
                          outcome_classes=["routine"])]
        world, agent_dir = _make_world(Path(tmpd), asps)
        pulses = _run_pulse(world, agent_dir)
        assert "strategic_drift" not in [p["pattern"] for p in pulses]


def test_strategic_drift_excludes_user_directed_goals():
    """5 routine goals but goal_source='user' → US-01 filter excludes them →
    window empty → no flag (user-directed work is not agent strategic drift)."""
    with tempfile.TemporaryDirectory() as tmpd:
        asps = [_make_asp("asp-603", completed=5, total=5,
                          goal_source="user",
                          outcome_classes=["routine"])]
        world, agent_dir = _make_world(Path(tmpd), asps)
        pulses = _run_pulse(world, agent_dir)
        assert "strategic_drift" not in [p["pattern"] for p in pulses]


def test_strategic_drift_data_gap_does_not_flag():
    """5 self-initiated goals with NO outcome_class (and no
    outcome_signal_source) → all excluded as undecidable → no flag. A data
    gap must never manufacture a false drift flag."""
    with tempfile.TemporaryDirectory() as tmpd:
        asps = [_make_asp("asp-604", completed=5, total=5,
                          goal_source="agent-self",
                          outcome_classes=None)]
        world, agent_dir = _make_world(Path(tmpd), asps)
        pulses = _run_pulse(world, agent_dir)
        assert "strategic_drift" not in [p["pattern"] for p in pulses]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
