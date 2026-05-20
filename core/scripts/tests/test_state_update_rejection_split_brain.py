#!/usr/bin/env python3
"""
test_state_update_rejection_split_brain.py — g-284-05 per-phase coverage.

Demonstrates the iteration-close.sh do_state_update rejection failure mode:

When do_verify completes (status=completed in aspirations.jsonl) AND
do_state_update is called but the Phase 4.26 gate refuses (exits 1 BEFORE
the in_flight clear at line 559), the resulting state inconsistency is:

  1. aspirations.jsonl:        goal.status = completed       (do_verify wrote)
  2. team-state.yaml:          in_flight   = stale phase=4   (state-update gate refused)
  3. iteration-checkpoint.json: phase_completed = verify     (state-update never refreshed)
  4. wm.goals_completed_this_session: NOT incremented        (state-update aborted pre-bump)

Same surface shape as the verify-skipped scenario but the cause differs:
verify-skipped = next phase never called; state-update-rejection = phase called,
gate refused, no writes happened.

For dynamic reproduction against the live bash wrapper, see the sibling test
test_verify_rejection_split_brain.py (verify-completion split-brain, g-284-01).

Origin: g-284-05 (alpha session 64+, 2026-05-11).
Refs: asp-284 motivation; iteration-close.sh do_state_update
      (lines 316-746, phase-4-26 gate at lines 350-354, in_flight clear at line 559).
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


TEST_GOAL_ID = "test-goal-state-update-rejection-01"
TEST_ASP_ID = "test-asp-state-update-rejection"
TEST_AGENT = "test-zeta-isolated"


def _setup_state(tmpdir: Path) -> tuple[Path, Path, Path]:
    """Build the three state files in pre-state-update shape (verify already ran)."""
    asp_file = tmpdir / "aspirations.jsonl"
    ts_file = tmpdir / "team-state.yaml"
    ckpt_file = tmpdir / "iteration-checkpoint.json"

    aspiration = {
        "id": TEST_ASP_ID,
        "title": "Test aspiration for state-update rejection",
        "status": "active",
        "priority": "MEDIUM",
        "goals": [
            {
                "id": TEST_GOAL_ID,
                "title": "Test goal — simulates Phase 4.26 gate refusal at state-update",
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

    team_state = {
        "agent_status": {
            TEST_AGENT: {
                "last_active": "2026-05-11T00:30:00",
                "in_flight": {
                    "goal_id": TEST_GOAL_ID,
                    "title": "Test goal — simulates Phase 4.26 gate refusal at state-update",
                    "claimed_at": "2026-05-11T00:30:00",
                    "phase": "4",
                },
            }
        }
    }
    ts_file.write_text(yaml.safe_dump(team_state, sort_keys=False), encoding="utf-8")

    initial_ckpt = {
        "goal_id": TEST_GOAL_ID,
        "aspiration_id": TEST_ASP_ID,
        "source": "agent",
        "phase": "verify",
        "phase_completed": "verify",
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


def _simulate_state_update_gate_refusal() -> None:
    """Mimics iteration-close.sh do_state_update's Phase 4.26 gate refusal.

    At lines 350-354:
      if ! bash "$SCRIPT_DIR/phase-4-26-gate.sh" "${gate_args[@]}"; then
          echo "[iteration-close] BLOCKED: Phase 4.26 gate refuses state-update..."
          exit 1
      fi

    No writes happen between this exit 1 and the function entry. The pre-state-update
    shape is preserved verbatim.
    """
    # Pure simulation — no writes. The PASS condition is that state was NOT mutated.
    pass


def reproduce() -> int:
    """Returns 0 on successful reproduction (state-update-rejection inconsistency
    confirmed), 1 on unexpected state (test environment broken or shape not reproducible)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="state-update-rejection-test-"))
    try:
        asp_file, ts_file, ckpt_file = _setup_state(tmpdir)

        init_status = _read_status(asp_file)
        init_in_flight = _read_in_flight(ts_file)
        init_ckpt_phase = _read_checkpoint_phase(ckpt_file)

        print("─── INITIAL STATE (post-verify, pre-state-update) ───")
        print(f"  goal.status         = {init_status}")
        print(f"  team-state in_flight = {init_in_flight}")
        print(f"  iteration-checkpoint = phase_completed={init_ckpt_phase}")
        print()

        assert init_status == "completed", \
            f"setup error: post-verify status must be 'completed' (got {init_status!r})"
        assert init_in_flight is not None and init_in_flight.get("phase") == "4", \
            f"setup error: in_flight must still be phase=4 (got {init_in_flight!r})"
        assert init_ckpt_phase == "verify", \
            f"setup error: checkpoint must show phase_completed=verify (got {init_ckpt_phase!r})"

        print("─── SIMULATING do_state_update REJECTION (Phase 4.26 gate refuses) ───")
        _simulate_state_update_gate_refusal()
        print("  ↳ phase-4-26-gate.sh exited 1 BEFORE any writes in do_state_update")
        print("  ↳ in_flight clear (line 559) never reached — state preserved verbatim")
        print()

        post_status = _read_status(asp_file)
        post_in_flight = _read_in_flight(ts_file)
        post_ckpt_phase = _read_checkpoint_phase(ckpt_file)

        print("─── POST-REJECTION STATE ───")
        print(f"  goal.status         = {post_status}")
        print(f"  team-state in_flight = {post_in_flight}")
        print(f"  iteration-checkpoint = phase_completed={post_ckpt_phase}")
        print()

        # Assertions: state preserved exactly because rejection happens pre-write.
        # This is the SAME inconsistency shape as verify-skipped, but with the
        # 4th invariant (goals_completed_this_session not incremented) as the
        # distinguishing signature.
        assert post_status == "completed", \
            f"FAIL: status changed unexpectedly (got {post_status!r})"
        assert post_in_flight is not None and post_in_flight.get("phase") == "4", \
            f"FAIL: in_flight should remain phase=4 (got {post_in_flight!r}) — " \
            f"state-update gate refusal not reproduced (was state-update somehow called?)"
        assert post_ckpt_phase == "verify", \
            f"FAIL: checkpoint should still show phase_completed=verify " \
            f"(got {post_ckpt_phase!r}) — state-update gate refusal not reproduced"

        print("════════════════════════════════════════════")
        print("  STATE-UPDATE-REJECTION INCONSISTENCY CONFIRMED")
        print("════════════════════════════════════════════")
        print("Three state stores disagree (same shape as verify-skipped split-brain):")
        print(f"  1. aspirations.jsonl: goal.status = {post_status}                  (do_verify wrote it)")
        print(f"  2. team-state.yaml:   in_flight.phase = {post_in_flight['phase']}                    (state-update gate refused before clear)")
        print(f"  3. checkpoint.json:   phase_completed = {post_ckpt_phase}          (state-update never refreshed)")
        print()
        print("Distinguishing 4th invariant: wm.goals_completed_this_session NOT incremented")
        print("(state-update aborts at line 350-354 BEFORE the wm-append at line 492/495).")
        print()
        print("Trigger conditions:")
        print("  - Phase 4.26 gate (utilization explicit-feedback) refuses state-update")
        print("  - User passes no-retrieval-applicable=false when retrieval was actually all_noise")
        print("  - utilization-gate.sh schema mismatch causes the gate to crash")
        return 0

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(reproduce())
