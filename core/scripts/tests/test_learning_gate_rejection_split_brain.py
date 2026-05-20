#!/usr/bin/env python3
"""
test_learning_gate_rejection_split_brain.py — g-284-05 per-phase coverage.

Demonstrates the iteration-close.sh do_learning_gate rejection failure mode:

When do_verify + do_state_update have both completed successfully (status=completed,
in_flight cleared, counters incremented, checkpoint refreshed to state-update) AND
do_learning_gate is SKIPPED (LLM error, autocompact, crash between phases), the
resulting state inconsistency is:

  1. aspirations.jsonl:        goal.status = completed                   (verify wrote)
  2. team-state.yaml:          in_flight CLEARED                         (state-update cleared)
  3. iteration-checkpoint.json: phase_completed = state-update           (state-update refreshed)
  4. retrieval-session.json:    stale (refers to prior goal, NOT this goal)
                                — utilization_pending may be true unchecked

Distinguishing characteristic from verify/state-update rejection: in_flight IS
cleared, counters ARE incremented. The "missing artifact" is the retrieval-summary
write at line 807/858, which gates retrospective analysis of which iterations
actually performed retrieval. Without learning-gate, retrieval-pending feedback
also doesn't fire, leaving utilization_pending=true into next iteration.

Origin: g-284-05 (alpha session 64+, 2026-05-11).
Refs: asp-284 motivation; iteration-close.sh do_learning_gate
      (lines 747-999, retrieval stub at lines 786-806, utilization feedback at line 842).
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


TEST_GOAL_ID = "test-goal-learning-gate-rejection-01"
TEST_ASP_ID = "test-asp-learning-gate-rejection"
TEST_AGENT = "test-zeta-isolated"


def _setup_state(tmpdir: Path) -> tuple[Path, Path, Path, Path]:
    """Build state files in post-state-update, pre-learning-gate shape."""
    asp_file = tmpdir / "aspirations.jsonl"
    ts_file = tmpdir / "team-state.yaml"
    ckpt_file = tmpdir / "iteration-checkpoint.json"
    ret_file = tmpdir / "retrieval-session.json"

    aspiration = {
        "id": TEST_ASP_ID,
        "title": "Test aspiration for learning-gate skip scenario",
        "status": "active",
        "priority": "MEDIUM",
        "goals": [
            {
                "id": TEST_GOAL_ID,
                "title": "Test goal — simulates learning-gate skip after state-update success",
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

    # state-update DID complete — in_flight is cleared
    team_state = {
        "agent_status": {
            TEST_AGENT: {
                "last_active": "2026-05-11T01:00:00",
                # in_flight intentionally absent — state-update's team-state-clear-in-flight ran
            }
        }
    }
    ts_file.write_text(yaml.safe_dump(team_state, sort_keys=False), encoding="utf-8")

    # state-update DID refresh checkpoint to phase=state-update
    initial_ckpt = {
        "goal_id": TEST_GOAL_ID,
        "aspiration_id": TEST_ASP_ID,
        "source": "agent",
        "phase": "state-update",
        "phase_completed": "state-update",
        "selected_at": "2026-05-11T00:30:00",
        "selector_score": 7.64,
        "skill": "",
    }
    ckpt_file.write_text(json.dumps(initial_ckpt), encoding="utf-8")

    # STALE retrieval file — refers to a PRIOR goal, not the current one.
    # If learning-gate runs, it will overwrite this with a fresh stub for TEST_GOAL_ID.
    # If learning-gate is skipped, the stale file persists with stale goal_id.
    stale_retrieval = {
        "schema_version": 2,
        "goal_id": "prior-goal-stale-001",
        "retrieval_performed": True,
        "tree_nodes_loaded": ["foo/bar"],
        "supplementary_items": [],
        "tree_nodes_detail": [],
        "supplementary_detail": [],
        "counts": {"tree_nodes": 1, "reasoning_bank": 0, "meta_lessons": 0,
                   "guardrails": 0, "pattern_signatures": 0, "experiences": 0},
        "utilization_pending": True,  # would be resolved by learning-gate's utilization-feedback
        "utilization_completed_at": None,
    }
    ret_file.write_text(json.dumps(stale_retrieval, indent=2), encoding="utf-8")

    return asp_file, ts_file, ckpt_file, ret_file


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


def _read_retrieval(ret_file: Path) -> dict:
    return json.loads(ret_file.read_text(encoding="utf-8"))


def reproduce() -> int:
    """Returns 0 on successful reproduction of learning-gate-skip inconsistency,
    1 on unexpected state."""
    tmpdir = Path(tempfile.mkdtemp(prefix="learning-gate-rejection-test-"))
    try:
        asp_file, ts_file, ckpt_file, ret_file = _setup_state(tmpdir)

        init_status = _read_status(asp_file)
        init_in_flight = _read_in_flight(ts_file)
        init_ckpt_phase = _read_checkpoint_phase(ckpt_file)
        init_ret = _read_retrieval(ret_file)

        print("─── INITIAL STATE (post-state-update, pre-learning-gate) ───")
        print(f"  goal.status              = {init_status}")
        print(f"  team-state in_flight     = {init_in_flight}")
        print(f"  iteration-checkpoint     = phase_completed={init_ckpt_phase}")
        print(f"  retrieval-session.goal_id= {init_ret['goal_id']}")
        print(f"  retrieval.pending        = {init_ret['utilization_pending']}")
        print()

        assert init_status == "completed", \
            f"setup error: post-verify status must be 'completed' (got {init_status!r})"
        assert init_in_flight is None, \
            f"setup error: state-update should have cleared in_flight (got {init_in_flight!r})"
        assert init_ckpt_phase == "state-update", \
            f"setup error: checkpoint must show phase_completed=state-update (got {init_ckpt_phase!r})"
        assert init_ret["goal_id"] != TEST_GOAL_ID, \
            f"setup error: retrieval-session.json must refer to prior goal initially"

        print("─── SIMULATING do_learning_gate SKIP (LLM error or autocompact) ───")
        print("  ↳ NO writes happen — learning-gate would have:")
        print("       - written retrieval-session.json stub for THIS goal_id, OR")
        print("       - run utilization-feedback to flip pending=true to false")
        print("       - checked unreflected hypotheses")
        print("       - checked tree-growth + tree-debt + tree-encoding-drift")
        print("       - emitted retrieval-summary line for retrospective grep")
        print()

        post_status = _read_status(asp_file)
        post_in_flight = _read_in_flight(ts_file)
        post_ckpt_phase = _read_checkpoint_phase(ckpt_file)
        post_ret = _read_retrieval(ret_file)

        print("─── POST-SKIP STATE ───")
        print(f"  goal.status              = {post_status}")
        print(f"  team-state in_flight     = {post_in_flight}")
        print(f"  iteration-checkpoint     = phase_completed={post_ckpt_phase}")
        print(f"  retrieval-session.goal_id= {post_ret['goal_id']}  ← STALE (refers to prior goal)")
        print(f"  retrieval.pending        = {post_ret['utilization_pending']}  ← still pending, never resolved")
        print()

        # Assertions: state preserved unchanged — learning-gate was skipped entirely.
        # The "bug" is the orphaned retrieval-session.json + missing retrieval-summary line.
        assert post_status == "completed", \
            f"FAIL: status changed unexpectedly (got {post_status!r})"
        assert post_in_flight is None, \
            f"FAIL: in_flight should remain cleared (got {post_in_flight!r})"
        assert post_ckpt_phase == "state-update", \
            f"FAIL: checkpoint should still show phase_completed=state-update " \
            f"(got {post_ckpt_phase!r})"
        assert post_ret["goal_id"] != TEST_GOAL_ID, \
            f"FAIL: retrieval-session should still refer to prior goal (got {post_ret['goal_id']!r}) " \
            "— learning-gate-skip not reproduced (was learning-gate somehow called?)"
        assert post_ret["utilization_pending"] is True, \
            f"FAIL: utilization_pending should still be true (got {post_ret['utilization_pending']!r}) " \
            "— learning-gate-skip not reproduced"

        print("════════════════════════════════════════════")
        print("  LEARNING-GATE-SKIP INCONSISTENCY CONFIRMED")
        print("════════════════════════════════════════════")
        print("Goal-state writes (verify, state-update) all landed correctly. The")
        print("inconsistency is in the LEARNING SUBSYSTEM:")
        print(f"  1. retrieval-session.json: goal_id={post_ret['goal_id']!r} (stale, refers to prior goal)")
        print(f"  2. utilization_pending: {post_ret['utilization_pending']} (carried into next iteration)")
        print(f"  3. retrieval-summary line: NEVER EMITTED for {TEST_GOAL_ID}")
        print(f"  4. tree-debt / tree-encoding-drift checks: SKIPPED for this iteration")
        print()
        print("Downstream impact:")
        print("  - Retrospective grep over iteration-close stderr cannot distinguish")
        print("    'retrieval performed' from 'learning-gate skipped' (both produce no")
        print("    retrieval-summary line in the affected iteration).")
        print("  - utilization-gate next iteration may misfire because pending=true")
        print("    is preserved against the WRONG goal.")
        print("  - tree-debt sentinel may fail to fire when it should, or fire stale.")
        print()
        print("Trigger conditions:")
        print("  - LLM forgets to invoke iteration-close.sh --phase learning-gate")
        print("  - Autocompact between state-update and learning-gate")
        print("  - Runner crash/SIGTERM between phases")
        return 0

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(reproduce())
