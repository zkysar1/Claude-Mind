"""test_complete_by_recurring_status_reset.py —  regression test.

Pins the RECURRING-GOAL STATUS RESET invariant of the complete-by daemon
handler (mind_api/src/endpoints/aspirations_write.py::complete_by):

  recurring goal    -> complete_by -> status == "pending"   (cycle back)
  non-recurring goal -> complete_by -> status == "completed" (terminal)

and the corollary that this reset is OUTCOME-INDEPENDENT: complete_by takes
NO deep/routine outcome parameter, so a recurring goal returns to "pending"
regardless of how the loop classified the close.

── Why this test exists (the investigation that motivated it) ───────────────
g-115-1785 added loop-state-bump-counters.py `--recurring false` streak
ownership plus iteration-close.sh do_state_update plumbing. g-115-1786 asked
two questions:

  Part 1 (flag plumbing): is iteration-close.sh's `--recurring false`
    pass-through tested end-to-end?  -> the STREAK-BLOCK behavior is already
    unit-tested (test_loop_state_counter_advance.py drives the helper directly;
    the fail-safe "unknown recurring -> flag omitted -> streaks skipped" is
    pinned there too). Only the 1-line bash guard
    `[[ "${recurring:-}" == "false" ]]` in do_state_update is unwired, and its
    sole end-to-end exercise needs the heavy daemon_integration subprocess path
    — disproportionate to a 1-line guard whose two halves (lookup + helper) are
    independently covered.

  Part 2 (status reset): is there a SYSTEMATIC bug where a DEEP recurring close
    leaves status stuck in-progress?  Evidence was g-250-167 (recurring, deep
    close) observed with achievedCount=1 + lastAchievedAt advanced but status
    stuck in-progress, while routine closes the same session reset to pending.

    DISPROVEN by reading complete_by: the recurring branch sets
    status="pending", achievedCount+=1, and pops claimed_by/claimed_at in ONE
    atomic block (aspirations_write.py:2529-2581), and complete_by has NO
    outcome parameter — so "deep-close skips the reset" is architecturally
    impossible. Deep and routine both route through do_verify -> complete-by ->
    the same outcome-independent reset. g-250-167 was a one-off transient
    (interrupted close or a post-close re-claim re-setting in-progress), already
    fixed inline — NOT a systematic close-path bug.

── The gap this test closes ─────────────────────────────────────────────────
test_window_streak.py EXERCISES complete_by on a recurring goal (it seeds
status="in-progress" then completes) but asserts ONLY windowStreak/currentStreak
— it never asserts status == "pending". That is the g-283-03 shape: a test that
passes the reset path through without pinning the field the incident was about.
This test pins the status field so a future regression that leaves recurring
goals in a terminal/stuck status (the goals-fail-to-recur failure mode) fails
loud.

Hermetic: uses the in-process DaemonFixture (no daemon_integration marker) —
runs in the daemon-safe `-m "not daemon_integration"` subset.

Refs: g-115-1786 (this test), g-250-167 (the transient that raised the
question), g-115-1785 (the flag plumbing), test_window_streak.py (the
canonical harness + the unasserted-status gap), test_loop_state_counter_advance.py
(helper streak-block coverage), mind_api/src/endpoints/aspirations_write.py:2481
(complete_by — the handler under test).

Run: py -3 -m pytest core/scripts/tests/test_complete_by_recurring_status_reset.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _daemon_fixture import DaemonFixture  # shared in-process daemon ()

CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))
import _rt  # canonical Python -> daemon client


# ── Seed helpers ─────────────────────────────────────────────────────────────

def _seed_world(tmp: Path, *, recurring: bool,
                last_achieved_at: str | None = None,
                achieved: int = 0) -> Path:
    """Seed a minimal world with one goal in status=in-progress + claimed.

    The goal starts claimed (claimed_by/claimed_at set) so the test can also
    assert complete_by releases the claim — the recurring cycle must return the
    goal to a selectable state, not leave it pinned to the completing agent.
    """
    world = tmp / "world"
    world.mkdir(exist_ok=True)
    goal = {
        "id": "g-900-01",
        "title": "Recurring test goal" if recurring else "One-shot test goal",
        "description": "status-reset invariant probe",
        "status": "in-progress",
        "priority": "MEDIUM",
        "claimed_by": "alpha",
        "claimed_at": "2026-07-08T00:00:00",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    if recurring:
        goal.update({
            "recurring": True,
            "interval_hours": 24,
            "lastAchievedAt": last_achieved_at,
            "achievedCount": achieved,
            "currentStreak": 0,
            "longestStreak": 0,
            "windowStreak": 0,
            "longestWindowStreak": 0,
            "windowStreakMultiplier": 7,
        })
    else:
        goal["recurring"] = False
    asp = {
        "id": "asp-900",
        "title": "Test asp",
        "motivation": "Test",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-07-08T12:00:00",
        "goals": [goal],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


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


def _complete_by(goal_id: str, agent: str = "alpha") -> None:
    """Complete a goal cycle via _rt -> in-process daemon. Raises on error."""
    _rt.aspirations_complete_by(goal_id, source="world", agent_name=agent)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_recurring_complete_by_resets_status_to_pending():
    """The core  invariant: a recurring goal cycles back to
    status="pending" (NOT stuck in-progress, NOT terminal completed), with
    achievedCount incremented and the claim released."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), recurring=True,
                            last_achieved_at=None, achieved=0)
        with DaemonFixture(world):
            _complete_by("g-900-01")
        g = _read_goal(world, "g-900-01")

        # THE pinned invariant test_window_streak.py never asserted:
        assert g["status"] == "pending", (
            f"recurring goal must cycle back to pending, got {g['status']!r} "
            "(in-progress here would be the goals-fail-to-recur bug that "
            "g-250-167 raised)")
        # Cycle-tracking fields advanced in the same atomic block:
        assert g["achievedCount"] == 1, "achievedCount must increment 0 -> 1"
        assert g["currentStreak"] == 1, "first completion seeds currentStreak=1"
        # Claim released so the goal is selectable again next cycle:
        assert "claimed_by" not in g, "complete_by must pop claimed_by"
        assert "claimed_at" not in g, "complete_by must pop claimed_at"


def test_recurring_reset_is_outcome_independent_and_repeatable():
    """complete_by has NO outcome parameter, so the recurring->pending reset
    cannot differ by deep vs routine. Completing the SAME recurring goal twice
    returns it to pending BOTH times with achievedCount 0 -> 1 -> 2 — the
    architectural fact that disproves the "deep-close skips the reset"
    hypothesis (g-250-167)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), recurring=True,
                            last_achieved_at=None, achieved=0)
        with DaemonFixture(world):
            _complete_by("g-900-01")
            g1 = _read_goal(world, "g-900-01")
            assert g1["status"] == "pending"
            assert g1["achievedCount"] == 1

            # Second cycle: the goal is pending + unclaimed; complete it again.
            # Same handler, same (absent) outcome arg -> same reset.
            _complete_by("g-900-01")
            g2 = _read_goal(world, "g-900-01")
            assert g2["status"] == "pending", (
                "second cycle must ALSO return to pending — the reset is "
                "outcome-independent by construction (no outcome param)")
            assert g2["achievedCount"] == 2, "achievedCount must reach 2"


def test_non_recurring_complete_by_completes_not_pending():
    """The reset is GATED on the recurring flag: a non-recurring goal through
    complete_by lands terminal (status="completed"), NOT "pending". Guards
    against a future regression that resets the status universally instead of
    only for recurring goals."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), recurring=False)
        with DaemonFixture(world):
            _complete_by("g-900-01")
        g = _read_goal(world, "g-900-01")

        assert g["status"] == "completed", (
            f"non-recurring goal must complete terminally, got {g['status']!r}")
        assert g["status"] != "pending", "non-recurring must NOT cycle to pending"
        assert g.get("completed_date"), "completed_date must be stamped"
        assert "claimed_by" not in g, "complete_by must pop claimed_by"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
