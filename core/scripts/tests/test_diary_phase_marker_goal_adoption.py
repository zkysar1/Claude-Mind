""": phase-marker goal-id adoption from a space-embedded phase string.

Incident (2026-07-31, foxtrot): the Phase-4 marker was invoked with the goal id
quoted INTO the phase argument — `phase-start "phase-4-execute g-115-4204"` —
and the writer accepted it silently. Consequences, all from one malformed call:

  1. entry.goal_id was EMPTY, so stranded-claim-sweep's primary keep-signal
     (`_diary_has_entry_after`, which matches the structured goal_id field)
     found no post-claim diary entry and released a live mid-execution claim
     at 07:37:54. The same session later completed the goal without the claim
     (the g-115-4232 completed-without-claim incident).
  2. `_maintain_execute_in_flight` compares `phase == "phase-4-execute"`
     exactly; the padded string never matched, so the execute-in-flight
     recovery suppressor was never armed during a 30-min execution.
  3. Phase-cost FIFO pairing keyed on the padded phase name.

Fix under test: `_emit_phase_marker` splits a whitespace-embedded phase,
adopts a goal-shaped token into goal_id (explicit --goal wins), folds other
extras into the note, and hands every consumer the CLEAN phase name.

Run: py -3 core/scripts/tests/test_diary_phase_marker_goal_adoption.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DIARY_SCRIPT = SCRIPT_DIR / "execution-diary.py"


def with_sandbox(test_fn):
    """Tmp AGENT_DIR sandbox with the session/ scaffold (mirrors
    test_execution_diary_heartbeat_sync.py)."""
    def wrapped():
        # These names are `test_*` at module level, so pytest COLLECTS them as
        # well as main() running them. Swallowing the AssertionError would make
        # pytest report PASS on a broken test (measured: `assert False` under
        # this shape = "1 passed"), so under pytest the failure must propagate
        # and the return value must be None (return-not-None is a warning today
        # and an error in future pytest).
        under_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
        sandbox = Path(tempfile.mkdtemp(prefix=f"diary_goal_test_{test_fn.__name__}_"))
        agent_dir = sandbox / "alpha-test"
        (agent_dir / "session").mkdir(parents=True)
        (sandbox / "world-test").mkdir()
        (sandbox / "meta-test").mkdir()

        prior_env = {k: os.environ.get(k) for k in
                     ("MIND_AGENT", "MIND_WORLD", "MIND_META",
                      "MIND_AGENT_DIR", "MIND_SID")}
        os.environ["MIND_AGENT"] = "alpha-test"
        os.environ["MIND_WORLD"] = str(sandbox / "world-test")
        os.environ["MIND_META"] = str(sandbox / "meta-test")
        os.environ["MIND_AGENT_DIR"] = str(agent_dir)
        os.environ.pop("MIND_SID", None)
        (agent_dir / "session" / "agent-state").write_text(
            "RUNNING", encoding="utf-8")

        try:
            test_fn(agent_dir)
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


def run_diary(*args):
    return subprocess.run(
        [sys.executable, str(DIARY_SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8",
        env=os.environ.copy(),
    )


def last_entry(agent_dir):
    diary = agent_dir / "session" / "execution-diary.jsonl"
    lines = [l for l in diary.read_text(encoding="utf-8").splitlines() if l.strip()]
    return json.loads(lines[-1])


def sentinel(agent_dir):
    return agent_dir / "session" / "execute-in-flight"


@with_sandbox
def test_space_embedded_goal_adopted(agent_dir):
    """The incident shape: phase quoted together with the goal id."""
    r = run_diary("phase-start", "phase-4-execute g-115-4204")
    assert r.returncode == 0, f"marker failed: {r.stderr}"
    e = last_entry(agent_dir)
    assert e["phase"] == "phase-4-execute", f"phase not cleaned: {e['phase']!r}"
    assert e.get("goal_id") == "g-115-4204", \
        f"goal_id not adopted: {e.get('goal_id')!r}"
    # Consumer 2: the exact-match execute-in-flight sentinel must arm.
    assert sentinel(agent_dir).exists(), \
        "execute-in-flight sentinel not armed — cleaned phase did not reach " \
        "_maintain_execute_in_flight"


@with_sandbox
def test_explicit_goal_wins_over_embedded(agent_dir):
    """--goal is authoritative; the embedded token folds into the note."""
    r = run_diary("phase-start", "phase-4-execute g-999-99",
                  "--goal", "g-111-11")
    assert r.returncode == 0, f"marker failed: {r.stderr}"
    e = last_entry(agent_dir)
    assert e["phase"] == "phase-4-execute"
    assert e.get("goal_id") == "g-111-11", \
        f"explicit --goal lost: {e.get('goal_id')!r}"
    assert "g-999-99" in str(e.get("content", "")), \
        "embedded token silently dropped — must fold into the note"


@with_sandbox
def test_clean_phase_unchanged(agent_dir):
    """Regression: the well-formed call is byte-identical in behavior."""
    r = run_diary("phase-start", "phase-4-execute", "--goal", "g-1-01")
    assert r.returncode == 0, f"marker failed: {r.stderr}"
    e = last_entry(agent_dir)
    assert e["phase"] == "phase-4-execute"
    assert e.get("goal_id") == "g-1-01"
    assert sentinel(agent_dir).exists()


@with_sandbox
def test_non_goal_extra_folds_to_note(agent_dir):
    """A non-goal-shaped extra token must not become goal_id."""
    r = run_diary("phase-start", "phase-4-execute somejunk")
    assert r.returncode == 0, f"marker failed: {r.stderr}"
    e = last_entry(agent_dir)
    assert e["phase"] == "phase-4-execute"
    assert not e.get("goal_id"), \
        f"non-goal token wrongly adopted: {e.get('goal_id')!r}"
    assert "somejunk" in str(e.get("content", ""))


@with_sandbox
def test_tab_embedded_goal_adopted(agent_dir):
    """Any whitespace, not just a literal space (fresh-eyes-code finding).

    The first fix tested `" " in phase` while splitting with `.split()`, which
    splits on ANY whitespace — so a tab-separated goal id skipped adoption and
    reproduced the original defect verbatim.
    """
    r = run_diary("phase-start", "phase-4-execute\tg-115-4204")
    assert r.returncode == 0, f"marker failed: {r.stderr}"
    e = last_entry(agent_dir)
    assert e["phase"] == "phase-4-execute", f"phase not cleaned: {e['phase']!r}"
    assert e.get("goal_id") == "g-115-4204", \
        f"goal_id not adopted from tab-separated phase: {e.get('goal_id')!r}"
    assert sentinel(agent_dir).exists()


@with_sandbox
def test_whitespace_only_phase_refused(agent_dir):
    """An all-whitespace phase must hit the required-arg guard, not emit an
    entry with an empty phase (the guard used to run before the strip)."""
    r = run_diary("phase-start", "   ")
    assert r.returncode == 2, \
        f"whitespace-only phase should be refused rc=2, got rc={r.returncode}"


@with_sandbox
def test_phase_end_cleans_and_disarms(agent_dir):
    """phase-end with the embedded shape must clean the phase so the
    sentinel disarms."""
    r1 = run_diary("phase-start", "phase-4-execute", "--goal", "g-2-02")
    assert r1.returncode == 0
    assert sentinel(agent_dir).exists()
    r2 = run_diary("phase-end", "phase-4-execute g-2-02")
    assert r2.returncode == 0, f"phase-end failed: {r2.stderr}"
    e = last_entry(agent_dir)
    assert e["phase"] == "phase-4-execute"
    assert e.get("goal_id") == "g-2-02"
    assert not sentinel(agent_dir).exists(), \
        "sentinel not disarmed — padded phase never matched phase-4-execute"


def main():
    tests = [
        test_space_embedded_goal_adopted,
        test_explicit_goal_wins_over_embedded,
        test_clean_phase_unchanged,
        test_non_goal_extra_folds_to_note,
        test_tab_embedded_goal_adopted,
        test_whitespace_only_phase_refused,
        test_phase_end_cleans_and_disarms,
    ]
    results = [t() for t in tests]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
