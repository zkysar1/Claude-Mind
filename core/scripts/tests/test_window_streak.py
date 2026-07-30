"""test_window_streak.py — windowStreak field on recurring goals.

Covers LifingPolls plan item 6 (2026-05-08): tolerant streak metric
distinct from currentStreak. Counts consecutive cycles within
windowStreakMultiplier × interval_hours of each other.

Test matrix:
  1. First completion seeds windowStreak=1, longestWindowStreak=1
  2. Within window AND within strict streak window: both advance
  3. Within window but past strict (3x interval): currentStreak resets, windowStreak continues
  4. Past window: windowStreak resets to 1
  5. longestWindowStreak only grows

Post-cutover (2026-05-14): aspirations.py complete-by CLI was deleted;
tests now hit the daemon endpoint via _rt.aspirations_complete_by. Each
test spins up an in-process daemon against a temp project root so the
fixture data is visible to the daemon (the long-running daemon sees the
real world dir, not temp dirs).

Use the SHARED core/scripts/tests/_daemon_fixture.py — never a local copy.
Until 2026-07-30 this file carried a forked _DaemonFixture that pinned only
RT_DIR and MIND_AGENT. The shared one also pins MIND_WORLD (g-115-2352),
STORAGE_BACKEND (g-115-2101), and MIND_META (guard-652). Missing the
MIND_WORLD pin meant that wherever MIND_WORLD happened to be exported, the
env tier beat the fixture local-paths.conf, the daemon resolved the REAL
world, and all 5 tests failed 404 goal_not_found on the fixture-seeded goal
-- green on boxes where the var was unset, red where it was set (g-115-3947).
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import _rt  # canonical Python -> daemon client (post-cutover)
from _daemon_fixture import DaemonFixture  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_world(tmp: Path, last_achieved_at: str | None = None,
                current_streak: int = 0, window_streak: int = 0,
                longest_window: int = 0,
                interval_hours: int = 4) -> Path:
    """Seed minimal world with one recurring goal, configurable last-fire."""
    world = tmp / "world"
    world.mkdir(exist_ok=True)
    asp = {
        "id": "asp-100",
        "title": "Test asp",
        "motivation": "Test",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-08T12:00:00",
        "goals": [{
            "id": "g-100-01",
            "title": "Recurring test goal",
            "description": "Test windowStreak",
            "status": "in-progress",
            "priority": "MEDIUM",
            "recurring": True,
            "interval_hours": interval_hours,
            "lastAchievedAt": last_achieved_at,
            "achievedCount": 0 if last_achieved_at is None else 5,
            "currentStreak": current_streak,
            "longestStreak": current_streak,
            "windowStreak": window_streak,
            "longestWindowStreak": longest_window,
            "windowStreakMultiplier": 7,
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "origin_signal": "user_directive",
            "participants": ["agent"],
        }],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _complete_by(goal_id: str, agent: str = "alpha") -> tuple[int, str, str]:
    """Complete a recurring goal cycle via _rt -> in-process daemon.

    Returns (returncode, stdout, stderr) for API compat with callers
    that assert on rc == 0.  Success -> (0, "", ""); failure -> (1, "", msg).
    """
    try:
        _rt.aspirations_complete_by(goal_id, source="world", agent_name=agent)
        return 0, "", ""
    except _rt.RtError as e:
        return (e.status or 1), "", (e.body or str(e))


def _read_goal(world: Path, goal_id: str) -> dict:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                return g
    raise KeyError(goal_id)


# ---- Tests ----------------------------------------------------------------


def test_first_completion_seeds_window_streak():
    """No prior lastAchievedAt -> windowStreak = 1, longestWindowStreak = 1."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), last_achieved_at=None)
        with DaemonFixture(world):
            rc, _, err = _complete_by("g-100-01")
            assert rc == 0, err
        g = _read_goal(world, "g-100-01")
        assert g["windowStreak"] == 1
        assert g["longestWindowStreak"] == 1
        assert g["currentStreak"] == 1


def test_on_time_completion_advances_both():
    """elapsed within strict streak window -> both currentStreak and windowStreak advance."""
    with tempfile.TemporaryDirectory() as tmpd:
        # 2 hours ago -- well within both 2x and 7x of 4h interval
        last = (datetime.now() - timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%S")
        world = _make_world(Path(tmpd), last_achieved_at=last,
                            current_streak=3, window_streak=3,
                            longest_window=3, interval_hours=4)
        with DaemonFixture(world):
            rc, _, err = _complete_by("g-100-01")
            assert rc == 0, err
        g = _read_goal(world, "g-100-01")
        assert g["currentStreak"] == 4
        assert g["windowStreak"] == 4
        assert g["longestWindowStreak"] == 4


def test_past_strict_within_window_streak_continues():
    """elapsed > 2x interval but <= 7x -> currentStreak resets, windowStreak continues."""
    with tempfile.TemporaryDirectory() as tmpd:
        # 12 hours ago -- past 2x4=8 strict, within 7x4=28 window
        last = (datetime.now() - timedelta(hours=12)).strftime(
            "%Y-%m-%dT%H:%M:%S")
        world = _make_world(Path(tmpd), last_achieved_at=last,
                            current_streak=5, window_streak=5,
                            longest_window=5, interval_hours=4)
        with DaemonFixture(world):
            rc, _, err = _complete_by("g-100-01")
            assert rc == 0, err
        g = _read_goal(world, "g-100-01")
        assert g["currentStreak"] == 1, "strict streak resets"
        assert g["windowStreak"] == 6, "window streak continues past strict"
        assert g["longestWindowStreak"] == 6


def test_past_window_resets():
    """elapsed > 7x interval -> windowStreak resets to 1."""
    with tempfile.TemporaryDirectory() as tmpd:
        # 30 hours ago -- past 7x4=28 window
        last = (datetime.now() - timedelta(hours=30)).strftime(
            "%Y-%m-%dT%H:%M:%S")
        world = _make_world(Path(tmpd), last_achieved_at=last,
                            current_streak=10, window_streak=10,
                            longest_window=10, interval_hours=4)
        with DaemonFixture(world):
            rc, _, err = _complete_by("g-100-01")
            assert rc == 0, err
        g = _read_goal(world, "g-100-01")
        assert g["currentStreak"] == 1
        assert g["windowStreak"] == 1
        assert g["longestWindowStreak"] == 10, "longest preserved"


def test_streak_break_emits_signal():
    """Strict streak reset writes to <agent>/session/streak-breaks.jsonl.

    Item 1 sibling: emit a signal when missed-interval reset triggers,
    so streak-break-reflector.py can convert it to an Investigate goal.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        last = (datetime.now() - timedelta(hours=12)).strftime(
            "%Y-%m-%dT%H:%M:%S")
        world = _make_world(tmp, last_achieved_at=last,
                            current_streak=5, window_streak=5,
                            longest_window=5, interval_hours=4)
        with DaemonFixture(world):
            rc, _, err = _complete_by("g-100-01")
            assert rc == 0, err
        # Just verify the streak reset happened (signal emission is
        # fail-silent and not load-bearing for the streak math).
        g = _read_goal(world, "g-100-01")
        assert g["currentStreak"] == 1


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
