#!/usr/bin/env python3
"""
test_productivity_check_rejection_split_brain.py — g-284-05 per-phase coverage.

Demonstrates the iteration-close.sh do_productivity_check rejection failure mode:

When do_verify + do_state_update + do_learning_gate have all completed successfully
(status=completed, in_flight cleared, counters incremented, retrieval-summary
emitted, checkpoint refreshed to learning-gate) AND do_productivity_check is
SKIPPED (LLM error after learning-gate, autocompact, crash), the resulting state
inconsistency is:

  1. aspirations.jsonl:        goal.status = completed                   (verify wrote)
  2. team-state.yaml:          in_flight CLEARED                         (state-update cleared)
  3. iteration-checkpoint.json: phase_completed = learning-gate          (learning-gate refreshed) ← FILE STILL EXISTS
  4. productivity-stop-gate:   NEVER EVALUATED — agent may exceed productivity floor invisibly

Distinguishing characteristic: the iteration-checkpoint.json is NOT deleted
(productivity-check is responsible for line 1061 `rm -f iteration-checkpoint.json`).
A stale anchor surviving into the next iteration causes postcompact-restore to
treat it as an authoritative in-flight claim, surfacing the g-255-03 anchor-stale
class of bug.

Additionally: the ITERATION COMPLETE imperative line (lines 1073-1075) is NEVER
emitted, so the LLM may not realize the canonical loop-continuation trigger fired —
risking a terminal-text-loop-death (rb-496 family).

Origin: g-284-05 (alpha session 64+, 2026-05-11).
Refs: asp-284 motivation; iteration-close.sh do_productivity_check
      (lines 1000-1076, iteration-checkpoint rm at line 1061, ITERATION COMPLETE imperative at lines 1072-1075).
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("FAIL: PyYAML not installed (required for team-state.yaml read/write)", file=sys.stderr)
    sys.exit(2)


TEST_GOAL_ID = "test-goal-productivity-check-rejection-01"
TEST_ASP_ID = "test-asp-productivity-check-rejection"
TEST_AGENT = "test-zeta-isolated"


def _setup_state(tmpdir: Path) -> tuple[Path, Path, Path]:
    """Build state files in post-learning-gate, pre-productivity-check shape."""
    asp_file = tmpdir / "aspirations.jsonl"
    ts_file = tmpdir / "team-state.yaml"
    ckpt_file = tmpdir / "iteration-checkpoint.json"

    aspiration = {
        "id": TEST_ASP_ID,
        "title": "Test aspiration for productivity-check skip scenario",
        "status": "active",
        "priority": "MEDIUM",
        "goals": [
            {
                "id": TEST_GOAL_ID,
                "title": "Test goal — simulates productivity-check skip after learning-gate success",
                "status": "completed",
                "completed_date": "2026-05-11",
                "last_modified": "2026-05-11T01:00:00",
                "priority": "MEDIUM",
                "category": "test",
                "participants": ["agent"],
                "recurring": False,
                "claimed_by": TEST_AGENT,
                "claimed_at": "2026-05-11T00:30:00",
                "verification": {"outcomes": ["pass"], "preconditions": [], "checks": []},
            }
        ],
    }
    asp_file.write_text(json.dumps(aspiration) + "\n", encoding="utf-8")

    # state-update DID complete — in_flight cleared
    team_state = {
        "agent_status": {
            TEST_AGENT: {
                "last_active": "2026-05-11T01:00:00",
            }
        }
    }
    ts_file.write_text(yaml.safe_dump(team_state, sort_keys=False), encoding="utf-8")

    # learning-gate WOULD have refreshed checkpoint to phase=learning-gate.
    # Without productivity-check, this file is NOT deleted at line 1061.
    initial_ckpt = {
        "goal_id": TEST_GOAL_ID,
        "aspiration_id": TEST_ASP_ID,
        "source": "agent",
        "phase": "learning-gate",
        "phase_completed": "learning-gate",
        "selected_at": "2026-05-11T00:30:00",
        "selector_score": 7.64,
        "skill": "",
    }
    ckpt_file.write_text(json.dumps(initial_ckpt), encoding="utf-8")

    return asp_file, ts_file, ckpt_file


def _read_status(asp_file: Path) -> str:
    data = json.loads(asp_file.read_text(encoding="utf-8"))
    for g in data["goals"]:
        if g["id"] == TEST_GOAL_ID:
            return g["status"]
    raise AssertionError("test goal not found")


def _read_in_flight(ts_file: Path) -> dict | None:
    data = yaml.safe_load(ts_file.read_text(encoding="utf-8"))
    return data["agent_status"][TEST_AGENT].get("in_flight")


def _read_checkpoint_phase(ckpt_file: Path) -> str:
    if not ckpt_file.exists():
        return "<missing>"
    data = json.loads(ckpt_file.read_text(encoding="utf-8"))
    return data.get("phase_completed", data.get("phase", "<unset>"))


def reproduce() -> int:
    """Returns 0 on successful reproduction of productivity-check-skip
    inconsistency, 1 on unexpected state."""
    tmpdir = Path(tempfile.mkdtemp(prefix="productivity-check-rejection-test-"))
    try:
        asp_file, ts_file, ckpt_file = _setup_state(tmpdir)

        init_status = _read_status(asp_file)
        init_in_flight = _read_in_flight(ts_file)
        init_ckpt_phase = _read_checkpoint_phase(ckpt_file)
        init_ckpt_exists = ckpt_file.exists()

        print("─── INITIAL STATE (post-learning-gate, pre-productivity-check) ───")
        print(f"  goal.status              = {init_status}")
        print(f"  team-state in_flight     = {init_in_flight}")
        print(f"  iteration-checkpoint.json exists = {init_ckpt_exists}")
        print(f"  iteration-checkpoint phase = {init_ckpt_phase}")
        print()

        assert init_status == "completed", \
            f"setup error: status must be 'completed' (got {init_status!r})"
        assert init_in_flight is None, \
            f"setup error: in_flight should be cleared (got {init_in_flight!r})"
        assert init_ckpt_phase == "learning-gate", \
            f"setup error: checkpoint must show phase_completed=learning-gate (got {init_ckpt_phase!r})"
        assert init_ckpt_exists, "setup error: iteration-checkpoint.json must exist initially"

        print("─── SIMULATING do_productivity_check SKIP ───")
        print("  ↳ NO writes/deletes happen — productivity-check would have:")
        print("       - run experience-staleness-check (advisory)")
        print("       - run decision-rules-staleness (advisory)")
        print("       - run recurring-precondition-sweep")
        print("       - run productivity-stop-gate (may set stop-requested)")
        print("       - DELETE iteration-checkpoint.json (line 1061)")
        print("       - EMIT '═══ ITERATION COMPLETE ═══' imperative (line 1073)")
        print()

        post_status = _read_status(asp_file)
        post_in_flight = _read_in_flight(ts_file)
        post_ckpt_phase = _read_checkpoint_phase(ckpt_file)
        post_ckpt_exists = ckpt_file.exists()

        print("─── POST-SKIP STATE ───")
        print(f"  goal.status              = {post_status}")
        print(f"  team-state in_flight     = {post_in_flight}")
        print(f"  iteration-checkpoint.json exists = {post_ckpt_exists}  ← STILL EXISTS (should be deleted)")
        print(f"  iteration-checkpoint phase = {post_ckpt_phase}  ← STALE anchor surviving into next iteration")
        print()

        # Assertions: state preserved exactly — productivity-check skipped entirely.
        # The "bug" is the orphaned iteration-checkpoint.json + missing
        # ITERATION COMPLETE imperative.
        assert post_status == "completed", \
            f"FAIL: status changed unexpectedly (got {post_status!r})"
        assert post_in_flight is None, \
            f"FAIL: in_flight should remain cleared (got {post_in_flight!r})"
        assert post_ckpt_phase == "learning-gate", \
            f"FAIL: checkpoint should still show phase_completed=learning-gate " \
            f"(got {post_ckpt_phase!r})"
        assert post_ckpt_exists, \
            f"FAIL: iteration-checkpoint.json should still exist " \
            "— productivity-check-skip not reproduced (was the rm somehow called?)"

        print("════════════════════════════════════════════")
        print("  PRODUCTIVITY-CHECK-SKIP INCONSISTENCY CONFIRMED")
        print("════════════════════════════════════════════")
        print("Goal-state writes all landed correctly. The inconsistencies are:")
        print(f"  1. iteration-checkpoint.json: STILL EXISTS at phase={post_ckpt_phase}")
        print(f"     — postcompact-restore on next session will treat as authoritative claim")
        print(f"     — surfaces the g-255-03 anchor-stale class of bug")
        print(f"  2. ITERATION COMPLETE imperative: NEVER EMITTED")
        print(f"     — LLM may not call Skill(aspirations) and silently kill the loop")
        print(f"     — surfaces the rb-496 terminal-text loop-death class")
        print(f"  3. productivity-stop-gate: NEVER EVALUATED")
        print(f"     — long-running sessions can drift past productivity floor invisibly")
        print(f"  4. recurring-precondition-sweep: NEVER RAN (in this iteration)")
        print(f"     — overdue_ratio inflation can persist one more iteration")
        print(f"  5. experience-staleness + decision-rules-staleness checks: SKIPPED")
        print()
        print("Trigger conditions:")
        print("  - LLM forgets to invoke iteration-close.sh --phase productivity-check")
        print("  - Autocompact between learning-gate and productivity-check")
        print("  - Runner crash/SIGTERM between phases")
        return 0

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(reproduce())
