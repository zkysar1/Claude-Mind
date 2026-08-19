"""The cross-machine LEASE must be renewed on the diary cadence — .

WHAT BROKE (measured 2026-08-06, alpha/cc-04, live reducer executing goals
normally): `runner-heartbeat` was 6 seconds old while the DDB claim heartbeat was
2794 seconds old and aging 1:1 with wall clock. Invoked by hand, the renewal
worked and reset the age to 0s — so the renewal path was never broken, only
unreached.

The cause is a cadence split that grew in two steps:

  * 2026-05-14 moved the LOCAL heartbeat off `heartbeat-tick.sh` (Phase -0.5,
    once per iteration) onto every diary write, because a 75-minute goal staled
    it between iteration boundaries and tripped false-positive recovery.
  * the DDB claim renewal — and later the reducer self-fence — were then added
    INSIDE `heartbeat-tick.sh`, i.e. onto the cadence that had already been
    measured too slow for exactly this reason.

So the fast signal is the one that makes a box LOOK alive, and the slow signal is
the one that keeps the distributed lock. A goal longer than
OWNERSHIP_STALE_SECONDS (3900s) drops the lease while the reducer is healthy;
a peer `/start` then reads a free claim and comes up as a SECOND REDUCER, and the
self-fence that would catch it is behind the same slow cadence.

WHY THE EXISTING GUARD CANNOT SEE IT: g-306-221's `claim-heartbeat-failure`
marker records a leg that RAN AND FAILED. Here the leg never runs, so the marker
stays absent and the box reports healthy the entire time. `test_claim_heartbeat_
failure_visibility.py` stays green throughout — it stubs the tick and asserts on
its internals, and this defect is upstream of the tick being called at all
(guard-1943: pinning the writer says nothing about the wiring).

WHAT THESE TESTS PIN — the CALL SITE and its rate limit, not the renewal:
  1. a diary write with no prior renewal FIRES the shared tick (the defect: it
     never fired between iteration boundaries).
  2. a second diary write moments later does NOT fire it — the hot path must not
     spawn a subprocess per breadcrumb.
  3. once the stamp ages past the interval it fires AGAIN, so a long goal is
     covered for its whole duration and not just at its start.
  4. an IDLE agent NEVER fires it. A worker Body is IDLE by design, and a worker
     must never renew the reducer's claim (worker_execute.py) — the pre-existing
     state gate is what enforces this and it must keep covering the new call.
  5. a FAILING tick still leaves the diary entry written and the command exit 0.
     Load-bearing: lease renewal must never convert into a lost breadcrumb
     (guard-1562 — fail-open to fail-closed is its own hazard).

HERMETIC for the reason the sibling file documents: the MIND_AGENT_DIR seam
alone is not enough, so each test stages a RELOCATED PROJECT_ROOT, COPIES
core/scripts (never symlinks — guard-2534), and overwrites `heartbeat-tick.sh`
with a recorder so nothing reaches a real daemon or a real DDB table.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _bash_cmd():
    sys.path.insert(0, str(REPO / "core" / "scripts"))
    from _runtime_bash import bash_cmd  # noqa: E402

    return bash_cmd


def _stage(tmp_path, *, agent="alpha", state="RUNNING", tick_rc=0):
    """Relocated PROJECT_ROOT whose heartbeat-tick.sh is a recorder, not the tick."""
    root = tmp_path / "repo"
    shutil.copytree(
        REPO / "core" / "scripts",
        root / "core" / "scripts",
        ignore=shutil.ignore_patterns("tests", "__pycache__", ".python-shim", "*.pyc"),
    )
    (root / "core" / "config").mkdir(parents=True, exist_ok=True)
    sess = root / "agents" / agent / "session"
    sess.mkdir(parents=True)
    (sess / "agent-state").write_text(state, encoding="utf-8")
    (root / ".env.local").write_text("STORAGE_BACKEND=own-cloud\n", encoding="utf-8")

    # The recorder. One line per invocation — the count IS the assertion.
    log = root / "tick-invocations.log"
    tick = root / "core" / "scripts" / "heartbeat-tick.sh"
    tick.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "tick" >> "{log.as_posix()}"\n'
        f"exit {tick_rc}\n",
        encoding="utf-8",
    )
    tick.chmod(0o755)
    return root, sess, log


def _append(root, agent="alpha", content="probe"):
    env = dict(os.environ)
    env.update(
        {
            "MIND_AGENT": agent,
            "MIND_AGENT_DIR": str(root / "agents" / agent),
            "PROJECT_ROOT": str(root),
            "STORAGE_BACKEND": "own-cloud",
            "MIND_SID": "",
            # : execution-diary.py refuses the shared tick under
            # PYTEST_CURRENT_TEST, because that subprocess is the phantom
            # team-state-shard writer. THIS suite is the sanctioned opt-in: it is
            # the one place that legitimately counts tick invocations, and it is
            # safe because _stage() relocates PROJECT_ROOT and overwrites
            # heartbeat-tick.sh with a recorder stub (see above) — the tick
            # invoked here can never reach the live world. Do NOT copy this var
            # into a test that runs against the real core/scripts.
            "MIND_DIARY_SHARED_TICK_TEST": "1",
        }
    )
    payload = json.dumps({"entry_type": "observation", "content": content})
    return subprocess.run(
        [sys.executable, str(root / "core" / "scripts" / "execution-diary.py"), "append"],
        input=payload, capture_output=True, text=True, timeout=120,
        env=env, cwd=str(root),
    )


def _ticks(log):
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def test_diary_write_renews_the_lease(tmp_path):
    """1. THE DEFECT: between iteration boundaries this fired zero times."""
    root, sess, log = _stage(tmp_path)
    r = _append(root)
    assert r.returncode == 0, f"diary append failed: {r.stderr[-400:]}"
    assert _ticks(log) == 1, (
        "a diary write did not invoke heartbeat-tick.sh, so the DDB lease is "
        "renewed ONLY at iteration boundaries while runner-heartbeat stays fresh "
        "continuously. A goal longer than OWNERSHIP_STALE_SECONDS then drops the "
        "claim under a healthy reducer and a peer /start becomes a second reducer."
    )
    assert (sess / "claim-renewal-last").exists(), "rate-limit stamp was not written"


def test_second_write_is_rate_limited(tmp_path):
    """2. The hot path must not spawn a subprocess per breadcrumb."""
    root, sess, log = _stage(tmp_path)
    _append(root, content="first")
    _append(root, content="second")
    _append(root, content="third")
    assert _ticks(log) == 1, (
        f"expected exactly 1 tick across 3 rapid diary writes, got {_ticks(log)} — "
        "the rate limit is not holding and every breadcrumb now costs a subprocess"
    )


def test_it_fires_again_once_the_interval_lapses(tmp_path):
    """3. A long goal must stay covered for its DURATION, not just at its start."""
    root, sess, log = _stage(tmp_path)
    _append(root, content="first")
    assert _ticks(log) == 1
    stamp = sess / "claim-renewal-last"
    old = time.time() - 3600  # one hour: past any sane interval, under 3900s
    os.utime(stamp, (old, old))
    _append(root, content="after-the-interval")
    assert _ticks(log) == 2, (
        "the lease was renewed once and then never again — a goal running longer "
        "than the interval would still age its claim out mid-execution"
    )


def test_idle_worker_ticks_for_body_liveness(tmp_path):
    """4. A worker Body is IDLE BY DESIGN, and that is exactly why it must tick.

    Gating the tick on RUNNING is what froze per-Body liveness for the duration
    of a long unit. The tick is safe to fire here because heartbeat-tick carries
    its own state gate with the two signals on OPPOSITE sides of it: the
    per-Body heartbeat above (g-306-208), the claim renewal below. So an IDLE
    Body refreshes its own liveness and exits 2 before reaching the claim.
    """
    root, sess, log = _stage(tmp_path, state="IDLE")
    r = _append(root)
    assert r.returncode == 0, f"diary append failed on an IDLE agent: {r.stderr[-400:]}"
    assert _ticks(log) == 1, (
        "an IDLE worker did NOT tick, so its per-Body heartbeat only refreshes "
        "once per worker-loop cycle. A unit longer than stranded-claim-sweep's "
        "120-minute foreign-SID grace then has its claim popped mid-execution."
    )


def test_idle_worker_does_not_touch_the_agent_wide_heartbeat(tmp_path):
    """4b. The paired negative — the gate that MUST still hold.

    Separated from the test above on purpose: those two assertions are the whole
    safety argument for firing the tick on IDLE, and a single test asserting
    both would let a future edit satisfy one by breaking the other. A fresh
    agent-wide runner-heartbeat against agent-state=IDLE is the 2026-05-13
    `heartbeat_without_running` desync (guard-543).
    """
    root, sess, log = _stage(tmp_path, state="IDLE")
    _append(root)
    assert not (sess / "runner-heartbeat").exists(), (
        "an IDLE agent's agent-wide runner-heartbeat was touched — that is the "
        "desync the state gate exists to prevent, and widening the tick must "
        "not have widened this too"
    )


def test_failing_tick_still_writes_the_diary_entry(tmp_path):
    """5. Fail-open: a breadcrumb must never be lost to lease plumbing."""
    root, sess, log = _stage(tmp_path, tick_rc=1)
    r = _append(root, content="must-survive")
    assert r.returncode == 0, (
        f"a failing heartbeat-tick broke the diary write (rc={r.returncode}) — "
        f"fail-open was inverted into fail-closed. stderr: {r.stderr[-400:]}"
    )
    diary = sess / "execution-diary.jsonl"
    assert diary.exists(), "diary file absent after a failing tick"
    assert "must-survive" in diary.read_text(encoding="utf-8"), (
        "the diary entry was lost when lease renewal failed"
    )
