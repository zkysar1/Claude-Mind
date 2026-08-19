"""test_recurring_loop_state_mutate.py — regression for Magic Wand #1.

Exercises the four mutation blocks in recurring-loop-state-mutate.py against
synthetic working-memory.yaml fixtures. Each case constructs an isolated WM
state in a tempdir, runs the gate as a subprocess (the same way recurring-
close.sh invokes it), and asserts the post-mutation values match the
expected post-flip outcome class plus the four blocks' arithmetic.

Cases mirror the digest's Phase 4.1 Block A/B/C/D specification:

  Block A (per-goal streak):
    A1: outcome=routine, streak<threshold → streak += 1, outcome stays routine
    A2: outcome=routine, streak == threshold-1 → streak += 1 hits threshold,
         outcome flips to deep, streak resets to 0
    A3: outcome=deep → streak resets to 0 unconditionally

  Block B (session signals):
    B-routine: routine_streak_global += 1, routine_count_total += 1,
               productive_streak = 0
    B-deep:    routine_streak_global = 0, productive_streak += 1,
               consecutive_blocked_sleeps = 0

  Block C (global + ratio anti-drift):
    C1: routine_streak_global >= recurring.routine_streak_global_ceiling
         (default 5; lowered from 8 on 2026-05-12) → outcome flips to deep,
         routine_streak_global resets to 0
    C2: ratio check (>0.80 routine and goals_completed>=6) → flips to deep

  Block D (count after reclassification):
    D1: deep outcome → productive_goals += 1
    D2: routine outcome → productive_goals stays
    D3: goals_completed += 1 always (whether routine or deep)

Pattern: subprocess + tempdir (no global WM mutation), like
test_capability_gate_user_only_precondition.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
# core/scripts -> core -> project root. _paths.py uses the same derivation.
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
GATE_PY = CORE_SCRIPTS / "recurring-loop-state-mutate.py"

sys.path.insert(0, str(CORE_SCRIPTS))
from _paths import agent_dir as _agent_dir  # noqa: E402


def _make_wm(agent_dir: Path, loop_state: dict) -> Path:
    """Write a working-memory.yaml fixture under agent_dir; return its path."""
    session_dir = agent_dir / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    wm_path = session_dir / "working-memory.yaml"
    wm = {
        "slots": {
            "loop_state": loop_state,
        },
    }
    wm_path.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")
    return wm_path


def _run_gate(agent_name: str, goal_id: str, outcome: str) -> tuple[int, str, str]:
    """Run the gate as a subprocess. Returns (rc, stdout, stderr).

    _paths.py derives PROJECT_ROOT from its own file location and is NOT
    overridable via env. So the test agent directory must live at the actual
    PROJECT_ROOT/<agent_name>; MIND_AGENT then resolves AGENT_DIR correctly.
    """
    env = os.environ.copy()
    env["MIND_AGENT"] = agent_name
    # guard-862 / guard-3375 (): on a worker Body, bash-agent-inject
    # sets BODY_WM_PATH to the LIVE per-Body WM, and wm.wm_path() checks it
    # FIRST — outranking MIND_AGENT entirely. Inheriting it made the gate
    # mutate live Body loop_state and fail all 8 cases against live counters
    # (measured cc-07 2026-08-17). Pin it to the fixture so the subprocess
    # resolves the sandbox WM on every box class.
    env["BODY_WM_PATH"] = str(
        _agent_dir(agent_name) / "session" / "working-memory.yaml"
    )

    proc = subprocess.run(
        [sys.executable, str(GATE_PY), "--goal-id", goal_id, "--outcome", outcome],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr


def _read_loop_state(wm_path: Path) -> dict:
    wm = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
    return ((wm.get("slots") or {}).get("loop_state") or {})


def _make_loop_state(
    *,
    goals_completed: int = 0,
    productive_goals: int = 0,
    routine_streaks: dict | None = None,
    routine_streak_global: int = 0,
    routine_count_total: int = 0,
    productive_streak: int = 0,
    consecutive_blocked_sleeps: int = 0,
) -> dict:
    return {
        "goals_completed_this_session": goals_completed,
        "productive_goals_this_session": productive_goals,
        "routine_streaks": routine_streaks or {},
        "signals": {
            "routine_streak_global": routine_streak_global,
            "routine_count_total": routine_count_total,
            "productive_streak": productive_streak,
            "consecutive_blocked_sleeps": consecutive_blocked_sleeps,
        },
    }


# Case definitions — (id, outcome_in, initial_loop_state, expected post-gate values).
# `final_outcome` is checked against subprocess stdout. The remainder are read
# from working-memory.yaml after the gate runs.
CASES = [
    # Block A1 — under threshold, no flip, streak increments
    {
        "id": "A1-routine-no-flip",
        "outcome_in": "routine",
        "goal_id": "g-115-105",
        "initial": _make_loop_state(routine_streaks={"g-115-105": 1}),
        "expect": {
            "final_outcome": "routine",
            "per_goal_streak": 2,
            "routine_streak_global": 1,
            "routine_count_total": 1,
            "productive_streak": 0,
            "goals_completed": 1,
            "productive_goals": 0,
        },
    },
    # Block A2 — at threshold, flip routine→deep, streak resets
    {
        "id": "A2-routine-flip-at-threshold",
        "outcome_in": "routine",
        "goal_id": "g-115-105",
        "initial": _make_loop_state(routine_streaks={"g-115-105": 4}),
        "expect": {
            "final_outcome": "deep",
            "per_goal_streak": 0,
            # Block A flipped to deep, so Block B takes the deep branch:
            "routine_streak_global": 0,
            "routine_count_total": 0,
            "productive_streak": 1,
            "goals_completed": 1,
            "productive_goals": 1,
        },
    },
    # Block A3 — deep outcome resets streak unconditionally
    {
        "id": "A3-deep-resets-streak",
        "outcome_in": "deep",
        "goal_id": "g-115-105",
        "initial": _make_loop_state(routine_streaks={"g-115-105": 3}),
        "expect": {
            "final_outcome": "deep",
            "per_goal_streak": 0,
            "routine_streak_global": 0,
            "routine_count_total": 0,
            "productive_streak": 1,
            "goals_completed": 1,
            "productive_goals": 1,
        },
    },
    # Block B-deep — consecutive_blocked_sleeps reset, productive_streak increment
    {
        "id": "B-deep-resets-blocked-sleeps",
        "outcome_in": "deep",
        "goal_id": "g-001-10",
        "initial": _make_loop_state(
            routine_streak_global=4,
            productive_streak=2,
            consecutive_blocked_sleeps=7,
        ),
        "expect": {
            "final_outcome": "deep",
            "per_goal_streak": 0,
            "routine_streak_global": 0,
            "routine_count_total": 0,
            "productive_streak": 3,
            "goals_completed": 1,
            "productive_goals": 1,
            "consecutive_blocked_sleeps": 0,
        },
    },
    # Block C1 — global streak >=8 forces deep
    {
        "id": "C1-global-streak-flips-deep",
        "outcome_in": "routine",
        "goal_id": "g-115-15",
        "initial": _make_loop_state(
            routine_streak_global=7,
            routine_count_total=7,
            goals_completed=7,
        ),
        "expect": {
            # Block A: routine, streak 0→1, no flip (under threshold).
            # Block B (routine branch): global 7→8, total 7→8, productive_streak=0.
            # Block C: global >=8 → flips to deep, global resets to 0.
            # Block D: deep → productive_goals += 1.
            "final_outcome": "deep",
            "per_goal_streak": 1,
            "routine_streak_global": 0,
            "routine_count_total": 8,
            "productive_streak": 0,
            "goals_completed": 8,
            "productive_goals": 1,
        },
    },
    # Block C2 — ratio check at >0.80
    {
        "id": "C2-ratio-flips-deep",
        "outcome_in": "routine",
        "goal_id": "g-115-15",
        "initial": _make_loop_state(
            goals_completed=9,
            routine_count_total=8,  # 8/9 = 0.89 BEFORE this iteration
            routine_streak_global=2,
        ),
        "expect": {
            # Block A: routine, streak 0→1, no flip.
            # Block B (routine branch): global 2→3, total 8→9, productive_streak=0.
            # Block C: global<8 (no flip). Ratio check uses pre-Block-D
            # goals_completed (still 9), so it's 9/9 = 1.0 > 0.80 → flip to deep.
            # Block D: deep → productive_goals += 1, goals_completed → 10.
            "final_outcome": "deep",
            "per_goal_streak": 1,
            # Block C ratio flip does NOT reset routine_streak_global (only the
            # global-streak ceiling C1 does). routine_count_total stays at 9.
            "routine_streak_global": 3,
            "routine_count_total": 9,
            "productive_streak": 0,
            "goals_completed": 10,
            "productive_goals": 1,
        },
    },
    # Block D — routine outcome does NOT increment productive_goals
    {
        "id": "D-routine-no-productive-increment",
        "outcome_in": "routine",
        "goal_id": "g-115-105",
        "initial": _make_loop_state(productive_goals=4),
        "expect": {
            "final_outcome": "routine",
            "per_goal_streak": 1,
            "routine_streak_global": 1,
            "routine_count_total": 1,
            "productive_streak": 0,
            "goals_completed": 1,
            "productive_goals": 4,  # unchanged
        },
    },
    # Edge case — empty loop_state still works (all defaults zero)
    {
        "id": "edge-empty-loop-state",
        "outcome_in": "routine",
        "goal_id": "g-new",
        "initial": {},  # No signals, no routine_streaks, no counters
        "expect": {
            "final_outcome": "routine",
            "per_goal_streak": 1,
            "routine_streak_global": 1,
            "routine_count_total": 1,
            "productive_streak": 0,
            "goals_completed": 1,
            "productive_goals": 0,
        },
    },
]


def main() -> int:
    failures = []

    for case in CASES:
        cid = case["id"]
        # _paths.py hardcodes PROJECT_ROOT — the test agent dir MUST live at
        # the real project root. Use a session-unique name + cleanup in finally.
        agent_name = f"_mw1-test-{uuid.uuid4().hex[:8]}"
        agent_dir = _agent_dir(agent_name)
        agent_dir.mkdir(parents=True, exist_ok=True)

        try:
            wm_path = _make_wm(agent_dir, case["initial"])
            rc, stdout, stderr = _run_gate(agent_name, case["goal_id"], case["outcome_in"])

            if rc != 0:
                failures.append(
                    f"[FAIL] {cid}: gate exited rc={rc}\n"
                    f"  stderr: {stderr}"
                )
                continue

            ls = _read_loop_state(wm_path)
            signals = ls.get("signals") or {}
            streaks = ls.get("routine_streaks") or {}

            actual = {
                "final_outcome": stdout,
                "per_goal_streak": streaks.get(case["goal_id"], 0),
                "routine_streak_global": signals.get("routine_streak_global", 0),
                "routine_count_total": signals.get("routine_count_total", 0),
                "productive_streak": signals.get("productive_streak", 0),
                "goals_completed": ls.get("goals_completed_this_session", 0),
                "productive_goals": ls.get("productive_goals_this_session", 0),
            }
            if "consecutive_blocked_sleeps" in case["expect"]:
                actual["consecutive_blocked_sleeps"] = signals.get(
                    "consecutive_blocked_sleeps", 0
                )

            mismatches = []
            for k, expected in case["expect"].items():
                if actual.get(k) != expected:
                    mismatches.append(f"{k}={actual.get(k)!r} (expected {expected!r})")

            if mismatches:
                failures.append(
                    f"[FAIL] {cid}: {', '.join(mismatches)}\n"
                    f"  stderr: {stderr.strip()}"
                )
            else:
                print(
                    f"  [PASS] {cid}: rc={rc} outcome={stdout} "
                    f"streak={actual['per_goal_streak']} "
                    f"global={actual['routine_streak_global']} "
                    f"goals={actual['goals_completed']}"
                )
        finally:
            # Clean up the test agent dir under PROJECT_ROOT regardless of pass/fail.
            shutil.rmtree(agent_dir, ignore_errors=True)

    print()
    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)}/{len(CASES)} test(s) failed")
        return 1
    print(f"All {len(CASES)} loop-state-mutate cases verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
