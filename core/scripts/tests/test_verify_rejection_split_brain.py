#!/usr/bin/env python3
"""
test_verify_rejection_split_brain.py — g-284-01 deterministic reproduction.

Demonstrates the iteration-close.sh do_verify split-brain failure mode
(asp-284 motivation; session-62 g-115-489 incident):

When do_verify completes (writes goal.status=completed and related fields)
AND do_state_update is skipped — for ANY reason (user rejection of the
state-update bash call, autocompact between phases, runner crash, LLM error
forgetting to call the next phase) — three state stores end up disagreeing:

  1. aspirations.jsonl:        goal.status = completed       (do_verify wrote)
  2. team-state.yaml:          in_flight   = stale phase=4   (do_state_update never cleared)
  3. iteration-checkpoint.json: phase_completed = verify     (do_state_update never refreshed)

The reproduction does NOT mutate real state. It uses an in-memory temp dir
and performs the literal file-write that aspirations.py's cmd_update_goal
performs against aspirations.jsonl, then asserts the post-condition that
matches the session-62 incident.

For dynamic reproduction against the live bash wrapper, see asp-284 sibling
goals g-284-02 (do_verify atomic-write fix) and g-284-05 (per-phase unit
tests covering each rejection point).

Origin: g-284-01 (zeta session 64, 2026-05-10).
Refs: asp-284 motivation; iteration-close.sh do_verify (lines 156-265),
      do_state_update (lines 267+, in_flight clear at line 507-508).
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


TEST_GOAL_ID = "test-goal-split-brain-01"
TEST_ASP_ID = "test-asp-split-brain"
TEST_AGENT = "test-zeta-isolated"


def _setup_state(tmpdir: Path) -> tuple[Path, Path, Path]:
    """Build the three state files in initial pre-verify shape."""
    asp_file = tmpdir / "aspirations.jsonl"
    ts_file = tmpdir / "team-state.yaml"
    ckpt_file = tmpdir / "iteration-checkpoint.json"

    aspiration = {
        "id": TEST_ASP_ID,
        "title": "Test aspiration for split-brain reproduction",
        "status": "active",
        "priority": "MEDIUM",
        "goals": [
            {
                "id": TEST_GOAL_ID,
                "title": "Test goal — simulates session-62 g-115-489 shape",
                "status": "in-progress",
                "priority": "MEDIUM",
                "category": "test",
                "participants": ["agent"],
                "recurring": False,
                "claimed_by": TEST_AGENT,
                "claimed_at": "2026-05-10T14:00:00",
                "verification": {"outcomes": ["pass"], "preconditions": [], "checks": []},
            }
        ],
    }
    asp_file.write_text(json.dumps(aspiration) + "\n", encoding="utf-8")

    team_state = {
        "agent_status": {
            TEST_AGENT: {
                "last_active": "2026-05-10T14:00:00",
                "in_flight": {
                    "goal_id": TEST_GOAL_ID,
                    "title": "Test goal — simulates session-62 g-115-489 shape",
                    "claimed_at": "2026-05-10T14:00:00",
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
        "phase": "selected",
        "selected_at": "2026-05-10T14:00:00",
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


def _simulate_do_verify_status_write(asp_file: Path) -> None:
    """Mimics the bash do_verify literal write at iteration-close.sh:219-221.

    aspirations-update-goal.sh --source agent <goal> status completed
    routes through aspirations.py cmd_update_goal which atomically rewrites
    the JSONL with goal[status] = completed.
    """
    data = json.loads(asp_file.read_text(encoding="utf-8"))
    for g in data["goals"]:
        if g["id"] == TEST_GOAL_ID:
            g["status"] = "completed"
            g["completed_date"] = "2026-05-10"
            g["last_modified"] = "2026-05-10T14:30:00"
    asp_file.write_text(json.dumps(data) + "\n", encoding="utf-8")


def reproduce() -> int:
    """Returns 0 on successful reproduction (split-brain confirmed), 1 on
    unexpected state (test environment broken or shape not reproducible)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="verify-rejection-test-"))
    try:
        asp_file, ts_file, ckpt_file = _setup_state(tmpdir)

        # Capture initial state
        init_status = _read_status(asp_file)
        init_in_flight = _read_in_flight(ts_file)
        init_ckpt_phase = _read_checkpoint_phase(ckpt_file)

        print("─── INITIAL STATE ───")
        print(f"  goal.status         = {init_status}")
        print(f"  team-state in_flight = {init_in_flight}")
        print(f"  iteration-checkpoint = phase={init_ckpt_phase}")
        print()

        assert init_status == "in-progress", \
            f"setup error: initial status must be 'in-progress' (got {init_status!r})"
        assert init_in_flight is not None and init_in_flight.get("phase") == "4", \
            f"setup error: initial in_flight must be phase=4 (got {init_in_flight!r})"
        assert init_ckpt_phase == "selected", \
            f"setup error: initial checkpoint phase must be 'selected' (got {init_ckpt_phase!r})"

        # Phase: do_verify executes (literally what iteration-close.sh:219-221 does
        # when --status completed is passed). This is the FIRST write of
        # the would-be sequence: status → team-state clear → checkpoint refresh.
        print("─── SIMULATING do_verify (status write) ───")
        _simulate_do_verify_status_write(asp_file)

        # SIMULATE REJECTION: bash dies here (user rejection, kill, autocompact,
        # LLM-error skipping the next phase call). The subsequent calls that
        # would happen (team-state-clear-in-flight.sh, _checkpoint_refresh
        # state_update) NEVER FIRE.
        print("  ↳ do_verify wrote goal.status=completed to aspirations.jsonl")
        print("  ↳ SIMULATED REJECTION — do_state_update never called")
        print()

        # Capture post-rejection state
        post_status = _read_status(asp_file)
        post_in_flight = _read_in_flight(ts_file)
        post_ckpt_phase = _read_checkpoint_phase(ckpt_file)

        print("─── POST-REJECTION STATE ───")
        print(f"  goal.status         = {post_status}")
        print(f"  team-state in_flight = {post_in_flight}")
        print(f"  iteration-checkpoint = phase={post_ckpt_phase}")
        print()

        # Split-brain assertions — these are the three disagreements that
        # asp-284 motivation documents from the  session-62 incident.
        assert post_status == "completed", \
            f"FAIL: do_verify did not flip status to completed (got {post_status!r})"
        assert post_in_flight is not None and post_in_flight.get("phase") == "4", \
            f"FAIL: in_flight should be stale phase=4 (got {post_in_flight!r}) — " \
            f"split-brain not reproduced (was state-update somehow called?)"
        assert post_ckpt_phase == "selected", \
            f"FAIL: iteration-checkpoint should still show selected " \
            f"(got phase={post_ckpt_phase!r}) — split-brain not reproduced"

        print("════════════════════════════════════════════")
        print("  SPLIT-BRAIN REPRODUCED DETERMINISTICALLY")
        print("════════════════════════════════════════════")
        print("Three state stores disagree:")
        print(f"  1. aspirations.jsonl: goal.status = {post_status}              (do_verify wrote it)")
        print(f"  2. team-state.yaml:   in_flight.phase = {post_in_flight['phase']}                (do_state_update never cleared)")
        print(f"  3. checkpoint.json:   phase_completed = {post_ckpt_phase}      (do_state_update never refreshed)")
        print()
        print("Matches asp-284 motivation: g-115-489 session-62 incident shape.")
        print()
        print("Trigger conditions (any of):")
        print("  - User rejects/denies the state-update bash call after verify succeeded")
        print("  - Autocompact happens between verify and state-update phases")
        print("  - Runner crash/SIGTERM between verify and state-update")
        print("  - LLM error: forgets to invoke state-update after verify")
        print("  - LLM follows a buggy digest path that calls verify but not state-update")
        print()
        print("Fix surface: sibling goals g-284-02/03/04/05 cover atomic-write,")
        print("graceful-stop in_flight clear, recovery-instruction output, per-phase tests.")
        return 0

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(reproduce())
