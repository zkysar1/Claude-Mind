#!/usr/bin/env python3
"""test_loop_state_bump_idempotency.py —  regression test.

Pins the idempotency invariant: when loop-state-bump-counters.py is
invoked twice with the same --goal-id within a session, the counters
must advance by exactly 1, not 2.

Origin of bug: in bravo session 66, do_state_update returned rc=127 on
its first attempt; the retry succeeded; loop-state-bump-counters.py
therefore ran TWICE for the SAME goal, double-bumping goals_completed.
The companion test_loop_state_counter_advance.py pinned single-call
behavior but no test exercised the retry path.

Fix (g-115-664):
  - Helper now accepts --goal-id <id>
  - Maintains loop_state.counted_goals_this_session (list)
  - Second call with the same goal_id is a no-op (exit 0)

Backward-compat: when --goal-id is omitted (legacy callers), the bump
remains unconditional. This is also tested.

Refs: g-115-664, g-283-06, core/scripts/loop-state-bump-counters.py.
"""
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


def _seed_wm(
    tmpdir: Path,
    goals_completed: int,
    productive_goals: int,
    counted_goals=None,
) -> Path:
    """Write a minimal WM with loop_state seeded."""
    session_dir = tmpdir / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    loop_state = {
        "goals_completed": goals_completed,
        "productive_goals": productive_goals,
        "evolutions": 0,
        "signals": {
            "routine_streak_global": 0,
            "productive_streak": 0,
            "routine_count_total": 0,
        },
        "routine_streaks": {},
    }
    if counted_goals is not None:
        loop_state["counted_goals_this_session"] = counted_goals
    wm = {
        "slots": {
            "loop_state": loop_state,
            "active_context": {"session_id": "test-session"},
        }
    }
    wm_path = session_dir / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")
    return wm_path


def _read_loop_state(wm_path: Path) -> dict:
    wm = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
    return wm["slots"]["loop_state"]


def _run_helper(tmpdir: Path, outcome: str, goal_id: str = None):
    env = os.environ.copy()
    env["MIND_AGENT_DIR"] = str(tmpdir)
    env["MIND_AGENT"] = "test-agent-isolated"
    cmd = ["py", "-3", str(HELPER), "--outcome", outcome]
    if goal_id is not None:
        cmd += ["--goal-id", goal_id]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    return result.returncode, result.stderr


def test_double_invocation_same_goal_id_increments_once() -> bool:
    """Two invocations with the same --goal-id → counters bump by exactly 1."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-idem-"))
    try:
        wm_path = _seed_wm(tmpdir, goals_completed=10, productive_goals=8)
        rc1, _ = _run_helper(tmpdir, "deep", goal_id="g-test-001")
        if rc1 != 0:
            print(f"FAIL: first call rc {rc1}", file=sys.stderr)
            return False
        rc2, stderr2 = _run_helper(tmpdir, "deep", goal_id="g-test-001")
        if rc2 != 0:
            print(f"FAIL: second call rc {rc2}", file=sys.stderr)
            return False
        if "idempotent no-op" not in stderr2:
            print(
                f"FAIL: second call should print 'idempotent no-op', got: {stderr2}",
                file=sys.stderr,
            )
            return False
        ls = _read_loop_state(wm_path)
        if ls["goals_completed"] != 11:
            print(
                f"FAIL: goals_completed expected 11, got {ls['goals_completed']}",
                file=sys.stderr,
            )
            return False
        if ls["productive_goals"] != 9:
            print(
                f"FAIL: productive_goals expected 9, got {ls['productive_goals']}",
                file=sys.stderr,
            )
            return False
        counted = ls.get("counted_goals_this_session", [])
        if counted != ["g-test-001"]:
            print(
                f"FAIL: counted_goals_this_session expected ['g-test-001'], got {counted}",
                file=sys.stderr,
            )
            return False
        print("PASS: two calls same goal_id → single bump + tracked in set")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_different_goal_ids_each_increment() -> bool:
    """Two invocations with DIFFERENT --goal-id → counters bump by 2."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-idem-"))
    try:
        wm_path = _seed_wm(tmpdir, goals_completed=10, productive_goals=8)
        rc1, _ = _run_helper(tmpdir, "deep", goal_id="g-test-001")
        rc2, _ = _run_helper(tmpdir, "deep", goal_id="g-test-002")
        if rc1 != 0 or rc2 != 0:
            print(f"FAIL: rc1={rc1}, rc2={rc2}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        if ls["goals_completed"] != 12:
            print(
                f"FAIL: goals_completed expected 12, got {ls['goals_completed']}",
                file=sys.stderr,
            )
            return False
        if ls["productive_goals"] != 10:
            print(
                f"FAIL: productive_goals expected 10, got {ls['productive_goals']}",
                file=sys.stderr,
            )
            return False
        counted = sorted(ls.get("counted_goals_this_session", []))
        if counted != ["g-test-001", "g-test-002"]:
            print(
                f"FAIL: counted_goals_this_session expected both ids, got {counted}",
                file=sys.stderr,
            )
            return False
        print("PASS: two calls different goal_ids → double bump + both tracked")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_no_goal_id_preserves_unconditional_bump() -> bool:
    """Legacy callers (no --goal-id) keep working — two calls = two bumps."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-idem-"))
    try:
        wm_path = _seed_wm(tmpdir, goals_completed=10, productive_goals=8)
        rc1, _ = _run_helper(tmpdir, "deep")
        rc2, _ = _run_helper(tmpdir, "deep")
        if rc1 != 0 or rc2 != 0:
            print(f"FAIL: rc1={rc1}, rc2={rc2}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        if ls["goals_completed"] != 12:
            print(
                f"FAIL: legacy mode expected 12, got {ls['goals_completed']}",
                file=sys.stderr,
            )
            return False
        if ls["productive_goals"] != 10:
            print(
                f"FAIL: legacy mode productive_goals expected 10, got {ls['productive_goals']}",
                file=sys.stderr,
            )
            return False
        if "counted_goals_this_session" in ls:
            print(
                f"FAIL: legacy mode should NOT populate counted_goals_this_session, got {ls.get('counted_goals_this_session')}",
                file=sys.stderr,
            )
            return False
        print("PASS: no --goal-id preserves legacy unconditional bump")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_preseeded_counted_set_prevents_first_bump() -> bool:
    """If goal_id is already in counted set when called once, exit 0 and no bump."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-idem-"))
    try:
        wm_path = _seed_wm(
            tmpdir,
            goals_completed=10,
            productive_goals=8,
            counted_goals=["g-test-pre"],
        )
        rc, stderr = _run_helper(tmpdir, "deep", goal_id="g-test-pre")
        if rc != 0:
            print(f"FAIL: rc {rc}, stderr: {stderr}", file=sys.stderr)
            return False
        if "idempotent no-op" not in stderr:
            print(
                f"FAIL: expected idempotent no-op stderr, got: {stderr}",
                file=sys.stderr,
            )
            return False
        ls = _read_loop_state(wm_path)
        if ls["goals_completed"] != 10 or ls["productive_goals"] != 8:
            print(
                f"FAIL: counters should be unchanged, got "
                f"goals_completed={ls['goals_completed']} productive_goals={ls['productive_goals']}",
                file=sys.stderr,
            )
            return False
        print("PASS: preseeded counted set prevents first bump")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _run_verify(tmpdir: Path, goal_id: str):
    """Invoke --verify-counted (read-only, no --outcome); return (rc, stderr)."""
    env = os.environ.copy()
    env["MIND_AGENT_DIR"] = str(tmpdir)
    env["MIND_AGENT"] = "test-agent-isolated"
    cmd = ["py", "-3", str(HELPER), "--verify-counted", goal_id]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    return result.returncode, result.stderr


def test_verify_counted_present_exit0() -> bool:
    """0: GOAL_ID present in counted set → exit 0 (bump landed)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-verify-"))
    try:
        _seed_wm(
            tmpdir, goals_completed=11, productive_goals=9,
            counted_goals=["g-test-vc1"],
        )
        rc, _ = _run_verify(tmpdir, "g-test-vc1")
        if rc != 0:
            print(f"FAIL: present goal expected exit 0, got {rc}", file=sys.stderr)
            return False
        print("PASS: verify-counted present → exit 0")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_verify_counted_absent_exit1() -> bool:
    """0: loop_state present, GOAL_ID absent → exit 1 (no-op detected)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-verify-"))
    try:
        _seed_wm(
            tmpdir, goals_completed=11, productive_goals=9,
            counted_goals=["g-other"],
        )
        rc, _ = _run_verify(tmpdir, "g-test-vc-absent")
        if rc != 1:
            print(f"FAIL: absent goal expected exit 1, got {rc}", file=sys.stderr)
            return False
        print("PASS: verify-counted absent → exit 1")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_verify_counted_no_counted_list_exit1() -> bool:
    """0: loop_state present but counted list missing → exit 1 (absent).

    This is the first-goal-of-session shape: a bump that should have created
    counted_goals_this_session=[goal] silently no-op'd, so the key never appears.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-verify-"))
    try:
        # counted_goals=None omits the key entirely (see _seed_wm).
        _seed_wm(tmpdir, goals_completed=10, productive_goals=8)
        rc, _ = _run_verify(tmpdir, "g-test-vc-nolist")
        if rc != 1:
            print(
                f"FAIL: missing counted list expected exit 1, got {rc}",
                file=sys.stderr,
            )
            return False
        print("PASS: verify-counted with no counted list → exit 1")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_verify_counted_missing_wm_exit0() -> bool:
    """0: WM file absent → exit 0 (indeterminate; conservative no re-fire)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-verify-"))
    try:
        # Deliberately do NOT seed a WM — the file does not exist.
        rc, _ = _run_verify(tmpdir, "g-test-vc-nowm")
        if rc != 0:
            print(
                f"FAIL: missing WM expected exit 0 (indeterminate), got {rc}",
                file=sys.stderr,
            )
            return False
        print("PASS: verify-counted with missing WM → exit 0 (indeterminate)")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_refire_recovers_simulated_noop() -> bool:
    """0: simulate a silent first-call no-op (counters frozen, goal
    absent from the counted set), then prove the iteration-close.sh self-heal
    sequence verify→re-fire→verify recovers the undercount."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-refire-"))
    try:
        # State AFTER a silent no-op: loop_state exists, counters frozen at 10/8,
        # target goal NOT in counted set (the bump's lock-acquire or WM-write
        # silently failed under bg latency).
        wm_path = _seed_wm(
            tmpdir, goals_completed=10, productive_goals=8,
            counted_goals=["g-earlier"],
        )
        # 1) verify detects the no-op (goal confidently absent → exit 1).
        rc_v1, _ = _run_verify(tmpdir, "g-test-refire")
        if rc_v1 != 1:
            print(f"FAIL: pre-refire verify expected 1, got {rc_v1}", file=sys.stderr)
            return False
        # 2) re-fire the bump foreground — recovers the undercount.
        rc_r, _ = _run_helper(tmpdir, "deep", goal_id="g-test-refire")
        if rc_r != 0:
            print(f"FAIL: re-fire bump rc {rc_r}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        if ls["goals_completed"] != 11 or ls["productive_goals"] != 9:
            print(
                f"FAIL: re-fire should advance to 11/9, got "
                f"{ls['goals_completed']}/{ls['productive_goals']}",
                file=sys.stderr,
            )
            return False
        if "g-test-refire" not in ls.get("counted_goals_this_session", []):
            print("FAIL: re-fire should add goal to counted set", file=sys.stderr)
            return False
        # 3) verify now confirms the goal landed (exit 0) — no second re-fire.
        rc_v2, _ = _run_verify(tmpdir, "g-test-refire")
        if rc_v2 != 0:
            print(f"FAIL: post-refire verify expected 0, got {rc_v2}", file=sys.stderr)
            return False
        print(
            "PASS: verify→re-fire→verify self-heals the simulated no-op "
            "(undercount recovered)"
        )
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    results = [
        test_double_invocation_same_goal_id_increments_once(),
        test_different_goal_ids_each_increment(),
        test_no_goal_id_preserves_unconditional_bump(),
        test_preseeded_counted_set_prevents_first_bump(),
        test_verify_counted_present_exit0(),
        test_verify_counted_absent_exit1(),
        test_verify_counted_no_counted_list_exit1(),
        test_verify_counted_missing_wm_exit0(),
        test_refire_recovers_simulated_noop(),
    ]
    if all(results):
        print("\n════════════════════════════════════════════")
        print(f"  ALL {len(results)} TESTS PASS — idempotency invariant pinned")
        print("════════════════════════════════════════════")
        return 0
    fail_count = sum(1 for r in results if not r)
    print(f"\nFAIL: {fail_count}/{len(results)} test(s) failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
