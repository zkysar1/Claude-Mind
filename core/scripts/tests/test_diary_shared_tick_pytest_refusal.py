""" / : the diary's shared heartbeat tick must REFUSE under pytest.

WHY THIS FILE EXISTS, measured not theorised. `execution-diary.py`'s
`_tick_shared_heartbeat_if_due()` shells out to `heartbeat-tick.sh`, which runs
`team-state-update.sh`, which reaches the daemon, which writes
`world/team-state/agents/<MIND_AGENT>.yaml`. A test binding a fake agent name
therefore materialises a REAL row in the LIVE fleet roster — `_agents.py`
globs that directory to build ACTIVE_AGENTS, so `liveness-check.sh` then
certifies the fixture as "alive". Measured 2026-08-18: 8 of 13 shards in the
live roster were test fixtures, and the graveyard holds seven `alpha-test`
retirements across two days.

THE OBVIOUS FIXES DO NOT WORK, and each was measured before this chokepoint was
chosen (full trace in the g-115-5220 block of `conftest.py`):
  * `STORAGE_BACKEND=local` stops the S3-KEY collision — guard-955's actual
    scope — but a LocalBackend write still lands in the LIVE world tree.
  * `MIND_WORLD` pointed at a tmp dir does not redirect it either: the write is
    performed by the DAEMON, which resolves its own world path. No env var set
    in the test process can move it.
  * A retirement tombstone applied once does not hold — a heartbeat newer than
    `retired_at` auto-un-retires the row, so purging without closing this path
    is a treadmill. It recurred at least four times.

So the defence has to stop the CALL, in the test's own subprocess, before the
daemon is ever reached. That is what these tests pin.

BOTH DIRECTIONS ARE PINNED, because the fix is a REFUSAL and the risk is
refusing too much: `test_lease_renewal_cadence.py` legitimately counts tick
invocations, and it must keep working via the documented opt-in.

HERMETIC by the same construction as its sibling: `_stage` (imported, not
copied) relocates PROJECT_ROOT, copies `core/scripts`, and overwrites
`heartbeat-tick.sh` with a recorder, so nothing here can reach a real daemon.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Import the staging helper rather than duplicating it — a forked copy would
# drift from the contract it is meant to reproduce (guard-920: a regression test
# must replicate the literal production shape, not a contract-ideal of it).
from test_lease_renewal_cadence import _stage  # noqa: E402

OPT_IN = "MIND_DIARY_SHARED_TICK_TEST"
# The literal the production notice prints. Asserted against the SUBPROCESS's
# stderr, never against this process's own output.
NOTICE = "shared heartbeat tick SUPPRESSED under pytest"


def _append(root, *, agent="alpha", opt_in=False, content="probe"):
    """Invoke the RELOCATED execution-diary.py exactly as the sibling suite does.

    The only difference from `test_lease_renewal_cadence._append` is that the
    opt-in var is a PARAMETER here — that is the whole axis under test.
    """
    env = dict(os.environ)
    env.update(
        {
            "MIND_AGENT": agent,
            "MIND_AGENT_DIR": str(root / "agents" / agent),
            "PROJECT_ROOT": str(root),
            "STORAGE_BACKEND": "own-cloud",
            "MIND_SID": "",
        }
    )
    if opt_in:
        env[OPT_IN] = "1"
    else:
        # Must be ABSENT, not empty: the production check is a truthiness test,
        # and inheriting a stray value from the ambient environment would make
        # the refusal case silently untested.
        env.pop(OPT_IN, None)
    payload = json.dumps({"entry_type": "observation", "content": content})
    return subprocess.run(
        [sys.executable, str(root / "core" / "scripts" / "execution-diary.py"), "append"],
        input=payload, capture_output=True, text=True, timeout=120,
        env=env, cwd=str(root),
    )


def _ticks(log):
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def test_pytest_current_test_is_actually_set():
    """POSITIVE CONTROL for every test below.

    The refusal keys on PYTEST_CURRENT_TEST reaching the SUBPROCESS. If pytest
    ever stopped exporting it, or a future `_append` stopped inheriting the
    parent environment, the refusal would silently never fire and the two tests
    below would both pass for the wrong reason — the suppression test because
    nothing ticked, the opt-in test because everything did.
    """
    assert os.environ.get("PYTEST_CURRENT_TEST"), \
        "pytest is not exporting PYTEST_CURRENT_TEST — the refusal cannot fire"


def test_shared_tick_is_suppressed_under_pytest(tmp_path):
    """THE DEFECT: without this, the tick reaches the live fleet roster."""
    root, _sess, log = _stage(tmp_path)
    r = _append(root)

    assert r.returncode == 0, f"diary append failed: {r.stderr[-400:]}"
    assert _ticks(log) == 0, (
        f"heartbeat-tick.sh ran {_ticks(log)}x under pytest — the phantom "
        "team-state-shard writer is still reachable from a test. On a box with a "
        "live daemon this writes world/team-state/agents/<fake-agent>.yaml into "
        "the real fleet roster."
    )
    assert NOTICE in r.stderr, (
        "the suppression was silent — a skipped side effect that announces "
        f"nothing trades one invisible behaviour for another. stderr={r.stderr[-400:]!r}"
    )


def test_diary_entry_still_lands_when_the_tick_is_suppressed(tmp_path):
    """The refusal must cost nothing else.

    `_tick_shared_heartbeat_if_due` is fail-open by contract (guard-1562: a diary
    write must never fail because lease renewal had an opinion). Returning early
    is a stronger form of that, and this pins it — a refusal that also swallowed
    the breadcrumb would be a worse bug than the one being fixed.
    """
    root, _sess, log = _stage(tmp_path)
    r = _append(root, content="breadcrumb-survives")

    assert r.returncode == 0, f"diary append failed: {r.stderr[-400:]}"
    assert _ticks(log) == 0
    diary = root / "agents" / "alpha" / "session" / "execution-diary.jsonl"
    assert diary.exists(), "the diary file was not written at all"
    assert "breadcrumb-survives" in diary.read_text(encoding="utf-8"), \
        "the diary entry was lost when the tick was suppressed"


def test_documented_opt_in_still_exercises_the_tick(tmp_path):
    """THE NARROWING MUST NOT GO TOO FAR.

    `test_lease_renewal_cadence.py` exists to prove the lease is renewed between
    iteration boundaries; it counts invocations of this exact subprocess. If the
    refusal had no escape hatch, that entire suite would silently stop testing
    what it names — passing while measuring nothing.
    """
    root, sess, log = _stage(tmp_path)
    r = _append(root, opt_in=True)

    assert r.returncode == 0, f"diary append failed: {r.stderr[-400:]}"
    assert _ticks(log) == 1, (
        f"expected exactly 1 tick with {OPT_IN} set, got {_ticks(log)} — the "
        "opt-in no longer reaches the tick, so the lease-renewal suite is "
        "asserting on a call that never happens."
    )
    assert NOTICE not in r.stderr, \
        "the suppression notice fired even though the opt-in was set"
    assert (sess / "claim-renewal-last").exists(), \
        "the rate-limit stamp was not written on the opt-in path"


def test_refusal_does_not_depend_on_the_rate_limit_stamp(tmp_path):
    """The refusal must be the CHOKEPOINT, not a second copy of the mitigation.

    The pre-existing defence was for each test to pre-touch `claim-renewal-last`
    so the rate limiter returned early. That only ever protected tests that
    remembered to do it — which is exactly why this chokepoint was added. With
    NO stamp present (the condition under which the old mitigation does nothing),
    the tick must still not fire.
    """
    root, sess, log = _stage(tmp_path)
    stamp = sess / "claim-renewal-last"
    assert not stamp.exists(), "fixture precondition: no stamp should exist yet"

    r = _append(root)

    assert r.returncode == 0, f"diary append failed: {r.stderr[-400:]}"
    assert _ticks(log) == 0, (
        "the tick fired with no rate-limit stamp present — the refusal is not "
        "acting as a chokepoint, it is riding on the old per-test mitigation."
    )
