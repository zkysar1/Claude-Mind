#!/usr/bin/env python3
"""test_loop_state_counter_advance.py —  regression test.

Pins the counter-advance invariant that g-283-03's shape-invariance test
did NOT pin: after iteration-close.sh writes loop_state via
loop-state-bump-counters.py, the goals_completed and productive_goals
INTEGER counters must advance by 1 (deep outcome) or by goals_completed
only (routine outcome).

Origin of bug: g-283-04 retired the LLM-side wm-set loop_state mirror at
LOOP_CONTINUE. Pre-retirement, the LLM wrote loop_state.goals_completed
and loop_state.productive_goals every iteration. Post-retirement, no bash
writer existed for these specific fields (recurring-loop-state-mutate.py
writes the *_this_session siblings; productivity-stop-gate.sh reads the
non-suffixed names). g-283-03's regression test pinned BYTE-EQUAL
serialization shape but did NOT call any writer — it just compared two
dump paths. So the counter-never-advances regression slipped through.

This test invokes the helper directly against a tempdir-isolated WM file
and asserts the post-call values match expectations.

Refs: g-283-06 (this fix), g-283-04 (the regression), g-283-03 (the test
that passed despite the gap), core/scripts/loop-state-bump-counters.py.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("FAIL: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


SCRIPT_DIR = Path(__file__).resolve().parent.parent  # core/scripts/
HELPER = SCRIPT_DIR / "loop-state-bump-counters.py"


def _seed_wm(tmpdir: Path, goals_completed: int, productive_goals: int) -> Path:
    """Write a minimal WM with loop_state seeded."""
    session_dir = tmpdir / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    wm = {
        "slots": {
            "loop_state": {
                "goals_completed": goals_completed,
                "productive_goals": productive_goals,
                "evolutions": 0,
                "signals": {
                    "routine_streak_global": 0,
                    "productive_streak": 0,
                    "routine_count_total": 0,
                },
                "routine_streaks": {},
            },
            "active_context": {"session_id": "test-session"},
        }
    }
    wm_path = session_dir / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")
    return wm_path


def _read_loop_state(wm_path: Path) -> dict:
    wm = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
    return wm["slots"]["loop_state"]


def _run_helper(tmpdir: Path, outcome: str = None, extra_args=None) -> int:
    """Invoke loop-state-bump-counters.py with AGENT_DIR pointed at tmpdir.

    _paths.py honors MIND_AGENT_DIR (test-only env override) — set it directly
    rather than trying to override PROJECT_ROOT (which is computed from
    script location and isn't env-overridable).

    `outcome` adds `--outcome <outcome>` when given; `extra_args` (list) appends
    arbitrary flags (g-115-1561: --goal-id / --reset-alignment / --evolution-fired).
    """
    env = os.environ.copy()
    env["MIND_AGENT_DIR"] = str(tmpdir)
    # MIND_AGENT still resolved from environment for consistency but the
    # path override above is what actually points the helper at our tmpdir.
    env["MIND_AGENT"] = "test-agent-isolated"
    cmd = ["py", "-3", str(HELPER)]
    if outcome is not None:
        cmd += ["--outcome", outcome]
    if extra_args:
        cmd += list(extra_args)
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    return result.returncode, result.stderr


def test_deep_advances_both() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        wm_path = _seed_wm(tmpdir, goals_completed=10, productive_goals=8)
        rc, stderr = _run_helper(tmpdir, "deep")
        if rc != 0:
            print(f"FAIL: deep — exit {rc}, stderr: {stderr}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        if ls["goals_completed"] != 11:
            print(
                f"FAIL: deep — goals_completed expected 11, got {ls['goals_completed']}",
                file=sys.stderr,
            )
            return False
        if ls["productive_goals"] != 9:
            print(
                f"FAIL: deep — productive_goals expected 9, got {ls['productive_goals']}",
                file=sys.stderr,
            )
            return False
        print("PASS: deep outcome advances both counters")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_routine_advances_only_total() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        wm_path = _seed_wm(tmpdir, goals_completed=10, productive_goals=8)
        rc, stderr = _run_helper(tmpdir, "routine")
        if rc != 0:
            print(f"FAIL: routine — exit {rc}, stderr: {stderr}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        if ls["goals_completed"] != 11:
            print(
                f"FAIL: routine — goals_completed expected 11, got {ls['goals_completed']}",
                file=sys.stderr,
            )
            return False
        if ls["productive_goals"] != 8:
            print(
                f"FAIL: routine — productive_goals expected 8 (unchanged), got {ls['productive_goals']}",
                file=sys.stderr,
            )
            return False
        print("PASS: routine outcome advances only goals_completed")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_fail_open_on_missing_wm() -> bool:
    """No WM file → exit 0 (fail-open), no crash."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        # Don't seed WM — session/ dir is missing entirely
        rc, stderr = _run_helper(tmpdir, "deep")
        if rc != 0:
            print(f"FAIL: missing-wm — expected exit 0, got {rc}", file=sys.stderr)
            return False
        print("PASS: missing WM file → fail-open exit 0")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── 1: orphaned-accumulator writers ────────────────────────────────

def test_touched_and_alignment_advance() -> bool:
    """--goal-id adds aspiration_id to touched AND increments alignment_check_at."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        wm_path = _seed_wm(tmpdir, goals_completed=5, productive_goals=3)
        rc, stderr = _run_helper(tmpdir, "deep", ["--goal-id", "g-115-1561"])
        if rc != 0:
            print(f"FAIL: touched — exit {rc}, stderr: {stderr}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        if ls.get("touched") != ["asp-115"]:
            print(f"FAIL: touched expected ['asp-115'], got {ls.get('touched')}", file=sys.stderr)
            return False
        if ls.get("alignment_check_at") != 1:
            print(f"FAIL: alignment_check_at expected 1, got {ls.get('alignment_check_at')}", file=sys.stderr)
            return False
        print("PASS: --goal-id writes touched + increments alignment_check_at")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_touched_dedups_within_aspiration() -> bool:
    """Two distinct goals in the same aspiration → touched holds asp-id once;
    alignment increments per distinct goal (idempotency keys on goal_id)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        wm_path = _seed_wm(tmpdir, goals_completed=5, productive_goals=3)
        _run_helper(tmpdir, "deep", ["--goal-id", "g-115-1561"])
        _run_helper(tmpdir, "routine", ["--goal-id", "g-115-1562"])
        ls = _read_loop_state(wm_path)
        if ls.get("touched") != ["asp-115"]:
            print(f"FAIL: dedup — touched expected ['asp-115'], got {ls.get('touched')}", file=sys.stderr)
            return False
        if ls.get("alignment_check_at") != 2:
            print(f"FAIL: dedup — alignment expected 2 (two distinct goals), got {ls.get('alignment_check_at')}", file=sys.stderr)
            return False
        print("PASS: touched dedups within aspiration; alignment counts distinct goals")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_reset_alignment_zeroes() -> bool:
    """--reset-alignment zeroes alignment_check_at (bash-owned cadence reset)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        wm_path = _seed_wm(tmpdir, goals_completed=5, productive_goals=3)
        _run_helper(tmpdir, "deep", ["--goal-id", "1"])  # alignment -> 1
        rc, stderr = _run_helper(tmpdir, None, ["--reset-alignment"])
        if rc != 0:
            print(f"FAIL: reset — exit {rc}, stderr: {stderr}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        if ls.get("alignment_check_at") != 0:
            print(f"FAIL: reset — alignment expected 0, got {ls.get('alignment_check_at')}", file=sys.stderr)
            return False
        print("PASS: --reset-alignment zeroes alignment_check_at")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_evolution_fired_increments() -> bool:
    """--evolution-fired increments evolutions and marks last_evolution_at."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        wm_path = _seed_wm(tmpdir, goals_completed=10, productive_goals=8)
        rc, stderr = _run_helper(tmpdir, None, ["--evolution-fired"])
        if rc != 0:
            print(f"FAIL: evolution — exit {rc}, stderr: {stderr}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        if ls.get("evolutions") != 1:
            print(f"FAIL: evolution — evolutions expected 1, got {ls.get('evolutions')}", file=sys.stderr)
            return False
        if ls.get("last_evolution_at") != 10:
            print(f"FAIL: evolution — last_evolution_at expected 10, got {ls.get('last_evolution_at')}", file=sys.stderr)
            return False
        print("PASS: --evolution-fired increments evolutions + marks last_evolution_at")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    results = [
        test_deep_advances_both(),
        test_routine_advances_only_total(),
        test_fail_open_on_missing_wm(),
        test_touched_and_alignment_advance(),
        test_touched_dedups_within_aspiration(),
        test_reset_alignment_zeroes(),
        test_evolution_fired_increments(),
    ]
    if all(results):
        print("\n════════════════════════════════════════════")
        print(f"  ALL {len(results)} TESTS PASS — counter-advance invariant pinned")
        print("════════════════════════════════════════════")
        return 0
    fail_count = sum(1 for r in results if not r)
    print(f"\nFAIL: {fail_count}/{len(results)} test(s) failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
