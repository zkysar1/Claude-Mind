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


# ── : orphaned-accumulator writers ────────────────────────────────

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
        _run_helper(tmpdir, "deep", ["--goal-id", ""])  # alignment -> 1
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


# ── : non-recurring streak ownership (--recurring false) ───────────
# Before , streaks (routine_streak_global, routine_count_total,
# productive_streak, routine_streaks[id], consecutive_blocked_sleeps) + the
# _this_session counters had a RECURRING bash writer (recurring-loop-state-
# mutate.py) but NO non-recurring one — the digest told the LLM to apply Block
# A/B/C/D manually, but the LOOP_CONTINUE contract forbids the LLM from
# persisting loop_state, so they drifted on interrupted/manual closes. These
# tests pin that loop-state-bump-counters.py --recurring false now owns them,
# and --recurring true (or omitted) does NOT touch them (recurring path owns it;
# a double-apply would corrupt cargo-cult detection).


def _seed_wm_streaks(tmpdir: Path, *, routine_streak_global=0,
                     routine_count_total=0, productive_streak=0,
                     consecutive_blocked_sleeps=0, routine_streaks=None,
                     goals_completed_this_session=0,
                     productive_goals_this_session=0,
                     goals_completed=10, productive_goals=8) -> Path:
    """Seed a WM with full signals + _this_session counters for the
    non-recurring streak-path tests (g-115-1785)."""
    session_dir = tmpdir / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    wm = {
        "slots": {
            "loop_state": {
                "goals_completed": goals_completed,
                "productive_goals": productive_goals,
                "goals_completed_this_session": goals_completed_this_session,
                "productive_goals_this_session": productive_goals_this_session,
                "signals": {
                    "routine_streak_global": routine_streak_global,
                    "productive_streak": productive_streak,
                    "routine_count_total": routine_count_total,
                    "consecutive_blocked_sleeps": consecutive_blocked_sleeps,
                },
                "routine_streaks": routine_streaks or {},
            },
            "active_context": {"session_id": "test-session"},
        }
    }
    wm_path = session_dir / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")
    return wm_path


def test_nonrecurring_routine_advances_streaks() -> bool:
    """--recurring false + routine: global++, total++, productive_streak=0,
    per-goal streak++, goals_completed_this_session++ (pgts unchanged)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        wm_path = _seed_wm_streaks(tmpdir, routine_streak_global=2,
                                   routine_count_total=5, productive_streak=1,
                                   goals_completed_this_session=4,
                                   productive_goals_this_session=2)
        rc, stderr = _run_helper(tmpdir, "routine",
                                 ["--goal-id", "g-999-01", "--recurring", "false"])
        if rc != 0:
            print(f"FAIL: nr-routine — exit {rc}, stderr: {stderr}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        s = ls["signals"]
        checks = [
            (s["routine_streak_global"], 3, "routine_streak_global"),
            (s["routine_count_total"], 6, "routine_count_total"),
            (s["productive_streak"], 0, "productive_streak reset"),
            (ls["routine_streaks"].get("g-999-01"), 1, "routine_streaks[g-999-01]"),
            (ls["goals_completed_this_session"], 5, "goals_completed_this_session"),
            (ls["productive_goals_this_session"], 2, "productive_goals_this_session (unchanged)"),
        ]
        for got, exp, name in checks:
            if got != exp:
                print(f"FAIL: nr-routine — {name} expected {exp}, got {got}", file=sys.stderr)
                return False
        print("PASS: non-recurring routine advances streak counters")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_nonrecurring_deep_resets_global() -> bool:
    """--recurring false + deep: global->0, productive_streak++, cbs->0,
    per-goal streak->0, productive_goals_this_session++."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        wm_path = _seed_wm_streaks(tmpdir, routine_streak_global=3,
                                   routine_count_total=6, productive_streak=0,
                                   consecutive_blocked_sleeps=4,
                                   routine_streaks={"g-999-02": 2},
                                   goals_completed_this_session=6,
                                   productive_goals_this_session=2)
        rc, stderr = _run_helper(tmpdir, "deep",
                                 ["--goal-id", "g-999-02", "--recurring", "false"])
        if rc != 0:
            print(f"FAIL: nr-deep — exit {rc}, stderr: {stderr}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        s = ls["signals"]
        checks = [
            (s["routine_streak_global"], 0, "routine_streak_global reset"),
            (s["productive_streak"], 1, "productive_streak"),
            (s["consecutive_blocked_sleeps"], 0, "consecutive_blocked_sleeps reset"),
            (s["routine_count_total"], 6, "routine_count_total (unchanged on deep)"),
            (ls["routine_streaks"].get("g-999-02"), 0, "routine_streaks[g-999-02] reset"),
            (ls["productive_goals_this_session"], 3, "productive_goals_this_session"),
        ]
        for got, exp, name in checks:
            if got != exp:
                print(f"FAIL: nr-deep — {name} expected {exp}, got {got}", file=sys.stderr)
                return False
        print("PASS: non-recurring deep resets global + advances productive_streak")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_nonrecurring_ceiling_reset() -> bool:
    """A routine close that pushes global to the ceiling (default 5) resets it
    to 0 (anti-runaway) — no outcome flip required, total still advances."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        # global=4 -> routine close makes it 5 (== ceiling) -> reset to 0.
        wm_path = _seed_wm_streaks(tmpdir, routine_streak_global=4,
                                   routine_count_total=10,
                                   goals_completed_this_session=10)
        rc, stderr = _run_helper(tmpdir, "routine",
                                 ["--goal-id", "g-999-03", "--recurring", "false"])
        if rc != 0:
            print(f"FAIL: nr-ceiling — exit {rc}, stderr: {stderr}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        s = ls["signals"]
        if s["routine_streak_global"] != 0:
            print(f"FAIL: nr-ceiling — global expected 0 after hitting ceiling, got {s['routine_streak_global']}", file=sys.stderr)
            return False
        if s["routine_count_total"] != 11:
            print(f"FAIL: nr-ceiling — total expected 11 (advanced), got {s['routine_count_total']}", file=sys.stderr)
            return False
        print("PASS: non-recurring ceiling reset (global -> 0 at ceiling)")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_recurring_true_skips_streaks() -> bool:
    """--recurring true: streaks UNTOUCHED (recurring-loop-state-mutate.py owns
    them); only the base goals_completed/productive_goals bump happens. A
    double-apply here would corrupt cargo-cult detection."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        wm_path = _seed_wm_streaks(tmpdir, routine_streak_global=2,
                                   routine_count_total=5,
                                   goals_completed_this_session=4,
                                   goals_completed=10, productive_goals=8)
        rc, stderr = _run_helper(tmpdir, "routine",
                                 ["--goal-id", "g-999-04", "--recurring", "true"])
        if rc != 0:
            print(f"FAIL: rec-skip — exit {rc}, stderr: {stderr}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        s = ls["signals"]
        if s["routine_streak_global"] != 2 or s["routine_count_total"] != 5:
            print(f"FAIL: rec-skip — streaks mutated (global={s['routine_streak_global']}, total={s['routine_count_total']}) — recurring path must NOT touch them", file=sys.stderr)
            return False
        if ls["goals_completed_this_session"] != 4:
            print(f"FAIL: rec-skip — goals_completed_this_session changed ({ls['goals_completed_this_session']}) — recurring path must NOT touch it", file=sys.stderr)
            return False
        if "g-999-04" in ls.get("routine_streaks", {}):
            print("FAIL: rec-skip — routine_streaks got a per-goal entry on recurring path", file=sys.stderr)
            return False
        if ls["goals_completed"] != 11:
            print(f"FAIL: rec-skip — base goals_completed expected 11, got {ls['goals_completed']}", file=sys.stderr)
            return False
        print("PASS: recurring=true skips streak block (base bump still runs)")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_nonrecurring_omitted_recurring_skips_streaks() -> bool:
    """--recurring OMITTED (unknown/failed lookup) skips streaks (fail-safe) —
    only the base bump runs. Mirrors iteration-close.sh's flag-omission on a
    failed aspiration lookup."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        wm_path = _seed_wm_streaks(tmpdir, routine_streak_global=2,
                                   routine_count_total=5,
                                   goals_completed_this_session=4)
        rc, stderr = _run_helper(tmpdir, "routine", ["--goal-id", "g-999-06"])
        if rc != 0:
            print(f"FAIL: nr-omitted — exit {rc}, stderr: {stderr}", file=sys.stderr)
            return False
        ls = _read_loop_state(wm_path)
        s = ls["signals"]
        if s["routine_streak_global"] != 2 or s["routine_count_total"] != 5:
            print(f"FAIL: nr-omitted — streaks mutated without --recurring false (global={s['routine_streak_global']})", file=sys.stderr)
            return False
        if ls["goals_completed"] != 11:
            print(f"FAIL: nr-omitted — base goals_completed expected 11, got {ls['goals_completed']}", file=sys.stderr)
            return False
        print("PASS: --recurring omitted → skips streaks (fail-safe), base bump runs")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_nonrecurring_idempotent_streaks() -> bool:
    """Re-running the same goal_id does NOT double-advance streaks (rides the
    counted_goals_this_session idempotency gate shared with the base bump)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-bump-test-"))
    try:
        wm_path = _seed_wm_streaks(tmpdir, routine_streak_global=2,
                                   routine_count_total=5,
                                   goals_completed_this_session=4)
        _run_helper(tmpdir, "routine", ["--goal-id", "g-999-05", "--recurring", "false"])
        _run_helper(tmpdir, "routine", ["--goal-id", "", "--recurring", "false"])  # retry
        ls = _read_loop_state(wm_path)
        s = ls["signals"]
        if s["routine_streak_global"] != 3:
            print(f"FAIL: nr-idem — global expected 3 (advanced once), got {s['routine_streak_global']}", file=sys.stderr)
            return False
        if s["routine_count_total"] != 6:
            print(f"FAIL: nr-idem — total expected 6 (advanced once), got {s['routine_count_total']}", file=sys.stderr)
            return False
        if ls["goals_completed_this_session"] != 5:
            print(f"FAIL: nr-idem — goals_completed_this_session expected 5 (advanced once), got {ls['goals_completed_this_session']}", file=sys.stderr)
            return False
        print("PASS: non-recurring streaks are idempotent per goal_id")
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
        # : non-recurring streak ownership
        test_nonrecurring_routine_advances_streaks(),
        test_nonrecurring_deep_resets_global(),
        test_nonrecurring_ceiling_reset(),
        test_recurring_true_skips_streaks(),
        test_nonrecurring_omitted_recurring_skips_streaks(),
        test_nonrecurring_idempotent_streaks(),
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
