"""Reducer-only stamp guard on team-state-in-flight.sh (write-side twin of -d).

in_flight is keyed by AGENT NAME (one row per mind) while bodies are per-SID, so
an unconditional stamp lets a worker Body's claim clobber the reducer's live row
— and nothing self-heals it (heartbeat-tick never refreshes in_flight). The guard
must SKIP (exit 0, stderr note, NO daemon call) whenever the caller cannot prove
it is the reducer: running-session-id absent (cross-box worker box), mismatched
(same-box worker), or MIND_SID unset (hook miss — fail toward no-clobber).

"NO daemon call" stayed true after g-306-160 added the body-keyed visibility
row to this same skip branch, but it is no longer free: it now depends on that
row being gated on the --agent having a local agent dir. FAKE_AGENT has none,
so nothing is written. Without that gate this file created a real
`zz-inflight-guard-test` shard in the SHARED team-state on every suite run —
and neither assertion below could see it, because a body-row write prints no
stdout and `in_flight` itself is still untouched. If you add another write to
the skip branch, re-derive this invariant rather than assuming it survived.

The match→stamp branch is deliberately NOT exercised here: it needs a live
running-session-id equal to the caller's SID, which on a worker box is the exact
state CW-pre forbids, and fabricating one in the live repo would make this box
reducer-shaped. Production exercises it on the reducer's next claim. The skip
branches are the ones whose failure clobbers (board msg-20260803-122452-alpha-5159).

Run: STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_team_state_in_flight_guard.py -q
"""
import os
import subprocess
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))
from _runtime_bash import bash_cmd  # noqa: E402

SCRIPT = (CORE_SCRIPTS / "team-state-in-flight.sh").as_posix()
SID = "11111111-1111-4111-8111-111111111111"
# Nonexistent agent: rsid is absent by construction, and if the guard were ever
# inverted the stray stamp would land on a garbage row, never a live agent's.
FAKE_AGENT = "zz-inflight-guard-test"


def _run(agent, sid):
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    if sid is None:
        env.pop("MIND_SID", None)
    else:
        env["MIND_SID"] = sid
    return subprocess.run(
        bash_cmd(SCRIPT, "--agent", agent, "--goal-id", "PROBE-NO-WRITE",
                 "--title", "probe", "--phase", "0"),
        capture_output=True, text=True, env=env, timeout=120)


def test_absent_rsid_skips_without_daemon_call():
    r = _run(FAKE_AGENT, SID)
    assert r.returncode == 0, r.stderr
    assert "SKIP stamp" in r.stderr and "rsid=absent" in r.stderr
    # The daemon success line must never print — the guard exits before rt_call.
    assert "in_flight set" not in r.stdout


def test_unset_sid_skips_without_daemon_call():
    r = _run(FAKE_AGENT, None)
    assert r.returncode == 0, r.stderr
    assert "SKIP stamp" in r.stderr and "sid=unset" in r.stderr
    assert "in_flight set" not in r.stdout
