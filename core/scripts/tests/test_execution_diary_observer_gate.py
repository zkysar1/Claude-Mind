""": execution-diary observer-session gate.

Verifies that execution-diary.py cmd_append and _emit_phase_marker
silently drop writes when MIND_SID is set AND running-session-id
exists AND the two differ. This is the runner/observer boundary —
observer sessions (assistant/reader coexisting with the autonomous
runner) must not contaminate the runner's diary stream.

Pattern parallels guard-135 / guard-340 / guard-517 and the 7th
witness in session-save-id.sh; canonical incident: 2026-05-10 bravo
observer wrote phase_start phase-4-execute for g-001-01, creating a
dangling phase pair detected by phase-cost-report.py.

Fail-open at every degraded condition:
- MIND_SID empty → fall through (e.g., bootstrap before any session)
- running-session-id absent → fall through (no runner to protect)
- Stored runner SID matches MIND_SID → fall through (this IS the runner)

Run: py -3 core/scripts/tests/test_execution_diary_observer_gate.py
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
        sandbox = Path(tempfile.mkdtemp(prefix=f"diary_observer_test_{test_fn.__name__}_"))
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


def run_diary(*args, sid=None):
    """Invoke execution-diary.py with optional MIND_SID override."""
    env = os.environ.copy()
    if sid is None:
        env.pop("MIND_SID", None)
    else:
        env["MIND_SID"] = sid
    return subprocess.run(
        [sys.executable, str(DIARY_SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8",
        env=env,
    )


def diary_line_count(agent_dir):
    f = agent_dir / "session" / "execution-diary.jsonl"
    if not f.exists():
        return 0
    return sum(1 for line in f.open(encoding="utf-8") if line.strip())


def write_runner_sid(agent_dir, sid):
    (agent_dir / "session" / "running-session-id").write_text(sid, encoding="utf-8")


@with_sandbox
def test_observer_phase_start_dropped(sandbox, agent_dir):
    """SIDs differ → phase_start silently dropped."""
    write_runner_sid(agent_dir, "runner-sid-A")
    baseline = diary_line_count(agent_dir)
    r = run_diary("phase-start", "phase-4-execute", "--goal", "g-test", sid="observer-sid-B")
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}: {r.stderr}"
    assert r.stdout == "", f"expected empty stdout, got: {r.stdout!r}"
    after = diary_line_count(agent_dir)
    assert after == baseline, f"diary grew {baseline}→{after} despite observer SID"


@with_sandbox
def test_observer_append_dropped(sandbox, agent_dir):
    """SIDs differ → stdin append silently dropped."""
    write_runner_sid(agent_dir, "runner-sid-A")
    baseline = diary_line_count(agent_dir)
    env = os.environ.copy()
    env["MIND_SID"] = "observer-sid-B"
    r = subprocess.run(
        [sys.executable, str(DIARY_SCRIPT), "append"],
        input='{"entry_type":"finding","content":"observer write"}',
        capture_output=True, text=True, encoding="utf-8",
        env=env,
    )
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}: {r.stderr}"
    assert r.stdout == "", f"expected empty stdout, got: {r.stdout!r}"
    after = diary_line_count(agent_dir)
    assert after == baseline, f"diary grew {baseline}→{after} despite observer SID"


@with_sandbox
def test_runner_phase_start_appends(sandbox, agent_dir):
    """SIDs match → phase_start appends normally."""
    write_runner_sid(agent_dir, "runner-sid-A")
    baseline = diary_line_count(agent_dir)
    r = run_diary("phase-start", "phase-4-execute", "--goal", "g-test", sid="runner-sid-A")
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}: {r.stderr}"
    assert "ok: phase_start" in r.stdout, f"expected ok output, got: {r.stdout!r}"
    after = diary_line_count(agent_dir)
    assert after == baseline + 1, f"diary did not grow: {baseline}→{after}"


@with_sandbox
def test_empty_sid_falls_through(sandbox, agent_dir):
    """MIND_SID empty + runner-file present → fall through (bootstrap)."""
    write_runner_sid(agent_dir, "runner-sid-A")
    baseline = diary_line_count(agent_dir)
    r = run_diary("phase-start", "phase-test", sid="")
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}: {r.stderr}"
    after = diary_line_count(agent_dir)
    assert after == baseline + 1, f"empty SID should fall through: {baseline}→{after}"


@with_sandbox
def test_no_runner_file_falls_through(sandbox, agent_dir):
    """running-session-id absent → fall through (no runner to protect)."""
    # Do NOT create running-session-id
    baseline = diary_line_count(agent_dir)
    r = run_diary("phase-start", "phase-test", "--goal", "g-test", sid="any-sid")
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}: {r.stderr}"
    after = diary_line_count(agent_dir)
    assert after == baseline + 1, f"absent runner-file should fall through: {baseline}→{after}"


@with_sandbox
def test_observer_phase_end_dropped(sandbox, agent_dir):
    """SIDs differ → phase_end also silently dropped (matters for pairing)."""
    write_runner_sid(agent_dir, "runner-sid-A")
    # First, runner writes a phase_start so there's a pair to protect.
    r0 = run_diary("phase-start", "phase-4-execute", "--goal", "g-test", sid="runner-sid-A")
    assert r0.returncode == 0
    baseline = diary_line_count(agent_dir)
    # Now observer tries to close it — must NOT contaminate.
    r = run_diary("phase-end", "phase-4-execute", "--goal", "g-test", sid="observer-sid-B")
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}: {r.stderr}"
    assert r.stdout == "", f"expected empty stdout, got: {r.stdout!r}"
    after = diary_line_count(agent_dir)
    assert after == baseline, f"diary grew {baseline}→{after} despite observer phase_end"


def fork_body_wm(agent_dir, sid):
    """Materialise the worker-Body activation signal for `sid` — the forked
    per-session working-memory.yaml that `_paths.body_state_path` keys on
    (g-306-136). Also pre-touch claim-renewal-last so the shared heartbeat
    tick never reaches the real heartbeat-tick.sh from a sandbox (g-115-5310)."""
    body_dir = agent_dir / "sessions" / sid
    body_dir.mkdir(parents=True, exist_ok=True)
    (body_dir / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")
    (agent_dir / "session" / "claim-renewal-last").touch()


def diary_last_entry(agent_dir):
    f = agent_dir / "session" / "execution-diary.jsonl"
    lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else None


def run_append(agent_dir, sid, content):
    env = os.environ.copy()
    env["MIND_SID"] = sid
    return subprocess.run(
        [sys.executable, str(DIARY_SCRIPT), "append"],
        input=json.dumps({"entry_type": "finding", "goal_id": "g-test", "content": content}),
        capture_output=True, text=True, encoding="utf-8",
        env=env,
    )


@with_sandbox
def test_worker_body_append_lands_with_body_sid(sandbox, agent_dir):
    """: a forked worker Body — SID differs from the runner's BY
    CONSTRUCTION, forked WM present — is a co-runner, not an observer. Its
    append LANDS in the agent-wide stream, stamped `body_sid`, and the
    runner-heartbeat is NOT touched (that file is the reducer's liveness)."""
    write_runner_sid(agent_dir, "runner-sid-A")
    fork_body_wm(agent_dir, "worker-sid-B")
    (agent_dir / "session" / "agent-state").write_text("RUNNING", encoding="utf-8")
    hb = agent_dir / "session" / "runner-heartbeat"
    hb.write_text("", encoding="utf-8")
    old = 1_000_000_000.0
    os.utime(hb, (old, old))
    baseline = diary_line_count(agent_dir)
    r = run_append(agent_dir, "worker-sid-B", "worker write")
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}: {r.stderr}"
    assert r.stdout.startswith("ok: finding"), f"expected ok output, got: {r.stdout!r}"
    after = diary_line_count(agent_dir)
    assert after == baseline + 1, f"worker append did not land: {baseline}→{after}"
    entry = diary_last_entry(agent_dir)
    assert entry.get("body_sid") == "worker-sid-B", f"missing body_sid stamp: {entry}"
    assert hb.stat().st_mtime == old, "a worker Body must not refresh the reducer's runner-heartbeat"


@with_sandbox
def test_same_sid_without_forked_wm_is_still_an_observer(sandbox, agent_dir):
    """Positive control for the exemption: the SAME foreign SID with no forked
    WM is an observer and stays dropped — the exemption is keyed on the Body
    activation signal, not on the SID differing."""
    write_runner_sid(agent_dir, "runner-sid-A")
    baseline = diary_line_count(agent_dir)
    r = run_append(agent_dir, "worker-sid-B", "observer write")
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}: {r.stderr}"
    assert r.stdout == "", f"expected empty stdout, got: {r.stdout!r}"
    after = diary_line_count(agent_dir)
    assert after == baseline, f"diary grew {baseline}→{after} for an observer SID"


@with_sandbox
def test_worker_body_phase_marker_lands_with_body_sid(sandbox, agent_dir):
    """The phase-marker path takes the same exemption and the same stamp."""
    write_runner_sid(agent_dir, "runner-sid-A")
    fork_body_wm(agent_dir, "worker-sid-B")
    baseline = diary_line_count(agent_dir)
    r = run_diary("phase-start", "phase-4-execute", "--goal", "g-test", sid="worker-sid-B")
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}: {r.stderr}"
    assert "ok: phase_start" in r.stdout, f"expected ok output, got: {r.stdout!r}"
    after = diary_line_count(agent_dir)
    assert after == baseline + 1, f"worker phase marker did not land: {baseline}→{after}"
    assert diary_last_entry(agent_dir).get("body_sid") == "worker-sid-B"


def main():
    print("g-115-633: execution-diary observer-session gate")
    tests = [
        test_observer_phase_start_dropped,
        test_observer_append_dropped,
        test_runner_phase_start_appends,
        test_empty_sid_falls_through,
        test_no_runner_file_falls_through,
        test_observer_phase_end_dropped,
        test_worker_body_append_lands_with_body_sid,
        test_same_sid_without_forked_wm_is_still_an_observer,
        test_worker_body_phase_marker_lands_with_body_sid,
    ]
    results = [t() for t in tests]
    passed = sum(results)
    total = len(results)
    print(f"\n{passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
