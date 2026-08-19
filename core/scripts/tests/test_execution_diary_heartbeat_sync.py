""": execution-diary heartbeat-sync regression test.

Verifies the symmetry fix landed in commit e382107 (2026-05-14): every
successful diary write also advances runner-heartbeat mtime, so that
long single-goal executions (>15 min between iteration boundaries) do
NOT trigger recovery-gate false positive RUNNING->IDLE flips.

Failure mode this guards against: heartbeat-tick.sh fires only at Phase
-0.5 of each iteration, but diary writes fire at every phase boundary
(sub-minute granularity). Without the fix, a single goal that runs >15
min stales heartbeat while diary stays fresh; recovery-gate sees the
heartbeat-stale signal (Condition 2) + diary-fresh (Condition 2.7) +
others and may still flip on aged sessions. With the fix, the two
staleness signals advance together: every diary append touches the
heartbeat file.

Canonical incident: zeta session 70 iteration 2 (g-271-19), 75-min
single-goal execution, recovery-gate flipped RUNNING->IDLE mid-goal.
See commit e382107 fix #2 + execution-diary.py:_advance_heartbeat
docstring for the design rationale (direct Path.touch over subprocess
heartbeat-tick.sh — Windows + Git Bash MIND_AGENT propagation gap).

Tests:
  - phase_start advances heartbeat (state=RUNNING)
  - phase_end advances heartbeat (state=RUNNING)
  - cmd_append advances heartbeat (state=RUNNING)
  - state=IDLE: heartbeat NOT advanced (mirrors heartbeat-tick.sh gate)
  - state-file absent: heartbeat IS advanced (fail-open, missing state
    means agent is bootstrapping, not idle)
  - Multiple sequential appends advance heartbeat each time (simulates
    a 20-min single-goal execution at 1-min diary cadence)

Run: py -3 core/scripts/tests/test_execution_diary_heartbeat_sync.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
DIARY_SCRIPT = SCRIPT_DIR / "execution-diary.py"


def with_sandbox(test_fn):
    """Spin up a tmp AGENT_DIR sandbox with the session/ scaffold."""
    def wrapped():
        # These names are `test_*` at module level, so pytest COLLECTS them as
        # well as main() running them. Swallowing the AssertionError would make
        # pytest report PASS on a broken test (measured under this exact shape:
        # a deliberately broken assertion here reported rc=0), so under pytest
        # the failure must propagate and the return value must be None
        # (return-not-None is a warning today and an error in future pytest).
        under_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
        sandbox = Path(tempfile.mkdtemp(prefix=f"diary_hb_test_{test_fn.__name__}_"))
        agent_dir = sandbox / "alpha-test"
        (agent_dir / "session").mkdir(parents=True)
        world_dir = sandbox / "world-test"
        world_dir.mkdir()
        meta_dir = sandbox / "meta-test"
        meta_dir.mkdir()

        prior_env = {k: os.environ.get(k) for k in
                     ("MIND_AGENT", "MIND_WORLD", "MIND_META",
                      "MIND_AGENT_DIR", "MIND_SID")}
        os.environ["MIND_AGENT"] = "alpha-test"
        os.environ["MIND_WORLD"] = str(world_dir)
        os.environ["MIND_META"] = str(meta_dir)
        os.environ["MIND_AGENT_DIR"] = str(agent_dir)
        os.environ.pop("MIND_SID", None)

        # Suppress _advance_heartbeat's SHARED tick (execution-diary.py:94), which
        # spawns heartbeat-tick.sh. It is rate-limited by this stamp file, so a
        # fresh stamp makes the call return before the subprocess (see
        # _tick_shared_heartbeat_if_due). Two independent reasons, both measured
        # on cc-07 2026-08-10 ():
        #
        # 1. THE SANDBOX CANNOT REACH THAT SUBPROCESS'S GATE. heartbeat-tick.sh
        #    refuses under agent-state=IDLE, but it asks session-state-get.sh,
        #    which is IRREDUCIBLY LOCAL: it inlines _APD="agents" and resolves
        #    $PROJECT_ROOT/agents/<agent>, so MIND_AGENT_DIR is invisible to it.
        #    Against this sandbox it returns UNINITIALIZED, the `= "IDLE"` compare
        #    never matches, and the gate falls open. That is a property of the
        #    FIXTURE, not of the writer: on a real box the same gate holds, and was
        #    verified holding on a live IDLE worker (heartbeat-tick.sh rc=2).
        #
        # 2. FALLING THROUGH THAT GATE WRITES PRODUCTION DATA. Past it,
        #    heartbeat-tick.sh runs team-state-update.sh, live-phase-emit.sh and a
        #    DDB runner-claim heartbeat. MIND_AGENT="alpha-test" has no
        #    local-paths.conf, so _paths.sh falls through to the first available one
        #    and WORLD_DIR resolves to the REAL world — the sandbox never contained
        #    it. Measured: this file created a phantom `alpha-test` row in the live
        #    team-state (fleet roster), which liveness-check.sh then reported as
        #    verdict "alive". The graveyard holds SEVEN alpha-test retirements
        #    across two days, i.e. it has been recreated and swept repeatedly.
        #
        # So the IDLE test below verifies the gate _advance_heartbeat OWNS. The
        # subprocess's own gate is heartbeat-tick.sh's contract, tested against a
        # real agent dir, and is not reachable from an env-redirected sandbox.
        (agent_dir / "session" / "claim-renewal-last").touch()

        try:
            test_fn(sandbox, agent_dir)
            print(f"  [PASS] {test_fn.__name__}")
            return None if under_pytest else True
        except AssertionError as e:
            print(f"  [FAIL] {test_fn.__name__}: {e}")
            traceback.print_exc()
            if under_pytest:
                raise
            return False
        except Exception as e:
            print(f"  [ERROR] {test_fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            if under_pytest:
                raise
            return False
        finally:
            for k, v in prior_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            shutil.rmtree(sandbox, ignore_errors=True)
    wrapped.__name__ = test_fn.__name__
    return wrapped


def set_state(agent_dir, state):
    """Write agent-state file (RUNNING / IDLE)."""
    (agent_dir / "session" / "agent-state").write_text(state, encoding="utf-8")


def seed_old_heartbeat(agent_dir, age_seconds=1800):
    """Create runner-heartbeat with mtime old enough to be stale (default 30min).

    Returns the seeded mtime so the test can assert it advanced.
    """
    hb = agent_dir / "session" / "runner-heartbeat"
    hb.touch()
    old_ts = time.time() - age_seconds
    os.utime(hb, (old_ts, old_ts))
    return hb.stat().st_mtime


def read_heartbeat_mtime(agent_dir):
    hb = agent_dir / "session" / "runner-heartbeat"
    if not hb.exists():
        return None
    return hb.stat().st_mtime


def run_diary(*args):
    return subprocess.run(
        [sys.executable, str(DIARY_SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8",
        env=os.environ.copy(),
    )


@with_sandbox
def test_phase_start_advances_heartbeat_under_running(sandbox, agent_dir):
    """state=RUNNING + phase-start -> heartbeat advances."""
    set_state(agent_dir, "RUNNING")
    old = seed_old_heartbeat(agent_dir, age_seconds=1800)
    r = run_diary("phase-start", "phase-4-execute", "--goal", "g-test")
    assert r.returncode == 0, f"phase-start failed: {r.stderr}"
    new = read_heartbeat_mtime(agent_dir)
    assert new is not None, "heartbeat file disappeared"
    assert new > old, f"heartbeat did not advance: old={old:.2f} new={new:.2f}"


@with_sandbox
def test_phase_end_advances_heartbeat_under_running(sandbox, agent_dir):
    """state=RUNNING + phase-end -> heartbeat advances."""
    set_state(agent_dir, "RUNNING")
    # Need a phase_start first so the end pairs cleanly (greedy pairing — not
    # strictly required for the heartbeat assertion but matches realistic flow).
    run_diary("phase-start", "phase-4-execute", "--goal", "g-test")
    old = seed_old_heartbeat(agent_dir, age_seconds=1800)
    r = run_diary("phase-end", "phase-4-execute", "--goal", "g-test")
    assert r.returncode == 0, f"phase-end failed: {r.stderr}"
    new = read_heartbeat_mtime(agent_dir)
    assert new > old, f"heartbeat did not advance: old={old:.2f} new={new:.2f}"


@with_sandbox
def test_append_advances_heartbeat_under_running(sandbox, agent_dir):
    """state=RUNNING + cmd_append -> heartbeat advances."""
    set_state(agent_dir, "RUNNING")
    old = seed_old_heartbeat(agent_dir, age_seconds=1800)
    r = subprocess.run(
        [sys.executable, str(DIARY_SCRIPT), "append"],
        input='{"entry_type":"finding","content":"long-running goal step"}',
        capture_output=True, text=True, encoding="utf-8",
        env=os.environ.copy(),
    )
    assert r.returncode == 0, f"append failed: {r.stderr}"
    new = read_heartbeat_mtime(agent_dir)
    assert new > old, f"heartbeat did not advance: old={old:.2f} new={new:.2f}"


@with_sandbox
def test_idle_state_does_not_advance_heartbeat(sandbox, agent_dir):
    """state=IDLE -> heartbeat NOT advanced (mirrors heartbeat-tick.sh gate).

    Critical invariant: if recovery-gate has already flipped state to IDLE,
    a subsequent diary write must NOT re-falsify liveness. Otherwise the
    heartbeat_without_running desync (session-manifest.yaml) reappears via
    a new path — same bug, different writer.

    SCOPE (g-115-5700, stated because a test that silently covers less than its
    name implies is worse than one that covers nothing): this asserts the gate at
    execution-diary.py:85 — the one _advance_heartbeat owns. The shared tick two
    lines below it sits deliberately OUTSIDE that gate and is suppressed by the
    fixture; see the stamp-file comment in with_sandbox for why the sandbox cannot
    evaluate its gate and why letting it run writes to the real world.

    This test therefore does NOT prove the agent-wide heartbeat is safe under
    IDLE end-to-end — it proves this writer does not advance it by its own hand.
    The other half lives in heartbeat-tick.sh's own IDLE gate (its `exit 2`).

    HISTORY: from cd14fa03b (g-306-233, which added the shared tick) until
    2026-08-10 this test failed on every box, and the swallow-decorator then in
    with_sandbox converted the failure into a pytest PASS — so it read green while
    asserting nothing. It was measured failing only through main(). The failure was
    always the fixture escaping its sandbox, never a leak in the writer.
    """
    set_state(agent_dir, "IDLE")
    old = seed_old_heartbeat(agent_dir, age_seconds=1800)
    r = run_diary("phase-start", "phase-test", "--goal", "g-test")
    assert r.returncode == 0, f"phase-start failed: {r.stderr}"
    new = read_heartbeat_mtime(agent_dir)
    # Diary write itself MUST succeed (fail-open), but heartbeat MUST stay old.
    assert abs(new - old) < 0.01, (
        f"heartbeat advanced under IDLE state: old={old:.2f} new={new:.2f} "
        "(state gate broken — would re-introduce heartbeat-without-running desync)"
    )


@with_sandbox
def test_missing_state_file_advances_heartbeat(sandbox, agent_dir):
    """state-file absent -> heartbeat IS advanced (fail-open, bootstrapping)."""
    # Do NOT call set_state — agent-state file absent
    old = seed_old_heartbeat(agent_dir, age_seconds=1800)
    r = run_diary("phase-start", "phase-test", "--goal", "g-test")
    assert r.returncode == 0, f"phase-start failed: {r.stderr}"
    new = read_heartbeat_mtime(agent_dir)
    assert new > old, (
        f"heartbeat did not advance with state-file absent: old={old:.2f} new={new:.2f} "
        "(bootstrap path broken — first iteration before agent-state lands would fail)"
    )


@with_sandbox
def test_long_goal_sequential_appends_advance_heartbeat_each_call(sandbox, agent_dir):
    """Simulates  75-min single-goal execution: 5 sequential diary
    writes spaced apart all advance heartbeat. Pre-fix, only the first
    would touch (because heartbeat-tick fires only at iteration boundary);
    post-fix, every diary write advances heartbeat directly.
    """
    set_state(agent_dir, "RUNNING")
    seed_old_heartbeat(agent_dir, age_seconds=1800)
    mtimes = []
    for i in range(5):
        # Backdate heartbeat to seeded-old between calls so each fresh tick
        # advances against a known old reference (otherwise filesystem mtime
        # granularity could blur consecutive sub-second touches).
        hb = agent_dir / "session" / "runner-heartbeat"
        old_ts = time.time() - (1800 - i)  # decreasing age each iteration
        os.utime(hb, (old_ts, old_ts))
        before = read_heartbeat_mtime(agent_dir)
        r = run_diary("phase-start", f"phase-{i}-test", "--goal", "g-long")
        assert r.returncode == 0, f"iter {i} phase-start failed: {r.stderr}"
        after = read_heartbeat_mtime(agent_dir)
        assert after > before, (
            f"iter {i}: heartbeat did not advance: before={before:.2f} after={after:.2f} "
            "(simulates long-goal execution — if this fails, recovery-gate would "
            "false-positive flip RUNNING->IDLE)"
        )
        mtimes.append(after)
    # Sanity: each successive heartbeat is monotonically non-decreasing.
    for a, b in zip(mtimes, mtimes[1:]):
        assert b >= a, f"heartbeat went backward: {a:.2f} -> {b:.2f}"


def main():
    print("g-115-713: execution-diary heartbeat-sync regression")
    tests = [
        test_phase_start_advances_heartbeat_under_running,
        test_phase_end_advances_heartbeat_under_running,
        test_append_advances_heartbeat_under_running,
        test_idle_state_does_not_advance_heartbeat,
        test_missing_state_file_advances_heartbeat,
        test_long_goal_sequential_appends_advance_heartbeat_each_call,
    ]
    results = [t() for t in tests]
    passed = sum(results)
    total = len(results)
    print(f"\n{passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
