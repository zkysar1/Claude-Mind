"""-b: runner-claim.sh `status` must obey its rc CONTRACT.

`status` is the one ASSERTING op among four MUTATING siblings, and that
asymmetry is the whole reason this file exists. For acquire/heartbeat/release a
no-op backend correctly maps to exit 0 ("there was nothing to mutate, so we are
done"). For `status` the same mapping would be a lie: there was nothing to READ,
so "a live runner exists" is a claim the backend cannot support.

THE CRITICAL PIN (the reason the goal called this non-trivial): a non-own-cloud
backend has no cross-machine claim store, so its no-op MUST map to rc=4 REFUSE
and NEVER to rc=0 alive. ZDS runs a git backend and receives this by promotion;
a no-op that read as "alive" would make the cross-box worker look activatable on
a deployment where it explicitly is not. Fail-safe direction is refuse.

Contract under test:
    0 -- a live RUNNING claim for this agent with a fresh heartbeat
    4 -- absent / stale / not-RUNNING / no claim store / unreadable freshness
    2 -- daemon-reported error (the wrapper maps this to its own exit 1)

Like test_runner_claim_release_surface.py, this exercises the wrapper's embedded
Python summary block in isolation (no daemon required) by extracting the heredoc
between the PYEOF markers and feeding it mocked daemon bodies via env -- the same
RESPONSE/OP/AGENT-via-env contract runner-claim.sh itself uses (guard-165).
Extraction reads the LIVE wrapper on every run, so the test cannot drift from a
stale copy of the logic.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1]
_WRAPPER = _SCRIPTS / "runner-claim.sh"
_STARTER = _SCRIPTS / "mind-api-start.sh"

# The live SSOT value at time of writing. Tests construct heartbeats relative to
# whatever the mocked response reports, so this is only a realistic default.
STALE = 3900


def _summary_block() -> str:
    """Extract the embedded `<<'PYEOF' ... PYEOF` summary block from the wrapper."""
    src = _WRAPPER.read_text(encoding="utf-8")
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", src, re.S)
    assert m, "could not locate the PYEOF summary block in runner-claim.sh"
    return m.group(1)


def _run(op: str, response: str, agent: str = "alpha"):
    """Drive the real summary block in the production env shape.

    STORAGE_BACKEND is pinned local (guard-955): this block never touches a
    store, but no test in this tree may run unpinned on an own-cloud box.
    """
    env = {**os.environ, "RESPONSE": response, "OP": op, "AGENT": agent,
           "STORAGE_BACKEND": "local"}
    p = subprocess.run([sys.executable, "-"], input=_summary_block(),
                       capture_output=True, text=True, env=env)
    return p.returncode, (p.stdout + p.stderr)


def _claims_body(*, backend="own-cloud", agent="alpha", state="RUNNING",
                 age=60, stale_after=STALE, machine="cc-99", extra_agents=(),
                 **overrides):
    """Build a GET /v1/admin/runner-claims body with heartbeat `age` seconds old."""
    claims = [{"agent": a, "machine_id": "other-box", "agent_state": "RUNNING",
               "heartbeat_at": int(time.time()) - 30} for a in extra_agents]
    if agent is not None:
        claims.append({"agent": agent, "machine_id": machine, "agent_state": state,
                       "heartbeat_at": (int(time.time()) - age) if age is not None else None})
    body = {"backend": backend, "ok": True, "environment_id": "ayoai-mind",
            "runner_stale_seconds": stale_after, "claims": claims}
    body.update(overrides)
    return json.dumps(body)


# ---------------------------------------------------------------------------
# THE CRITICAL PIN — a backend with no claim store must REFUSE, never affirm.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("backend", ["local", "git", "s3", ""])
def test_non_owncloud_backend_refuses_and_never_reports_alive(backend):
    """rc MUST be 4. This test FAILS if the noop ever maps to 0.

    The real non-own-cloud response is `{ok:true, claims:[]}` -- note `ok:true`
    and an EMPTY list, which is exactly the shape a careless reader treats as
    success.
    """
    body = json.dumps({"backend": backend, "ok": True, "claims": [],
                       "reason": "non-own-cloud backend — no cross-machine DDB claims"})
    rc, out = _run("status", body)
    assert rc != 0, (
        f"backend={backend!r} reported ALIVE (rc=0) on a backend with no claim "
        f"store — this is the fail-OPEN direction the contract forbids: {out!r}")
    assert rc == 4, f"backend={backend!r} -> rc={rc}, want 4 (REFUSE): {out!r}"
    assert "REFUSE" in out


def test_non_owncloud_refuses_even_if_endpoint_later_adds_noop_key():
    """Ordering defense.

    The generic handler further down the block maps `noop:true` -> exit 0. That
    is correct for the mutating ops. Today the runner-claims endpoint happens
    NOT to emit `noop` -- but that is an accident of two endpoints having
    differently-shaped no-op payloads, not a guarantee. If anyone ever aligns
    them, status must not silently start reporting every non-own-cloud box as
    ALIVE. This pins that status is decided BEFORE the generic noop branch.
    """
    body = json.dumps({"backend": "local", "ok": True, "noop": True,
                       "claims": [], "reason": "no claim store"})
    rc, out = _run("status", body)
    assert rc == 4, f"noop-bearing non-own-cloud body must still REFUSE, got rc={rc}: {out!r}"
    assert "REFUSE" in out


# ---------------------------------------------------------------------------
# The live / not-live decision
# ---------------------------------------------------------------------------

def test_live_fresh_running_claim_is_zero():
    rc, out = _run("status", _claims_body(age=60))
    assert rc == 0, f"fresh RUNNING claim must be 0, got {rc}: {out!r}"
    assert "LIVE" in out and "cc-99" in out


def test_absent_agent_refuses():
    """Claims exist for OTHER agents but not this one -- absent, not alive."""
    rc, out = _run("status", _claims_body(agent=None, extra_agents=("zeta", "bravo")))
    assert rc == 4
    assert "ABSENT" in out


def test_stale_heartbeat_refuses():
    rc, out = _run("status", _claims_body(age=STALE + 500))
    assert rc == 4, f"stale heartbeat must refuse, got {rc}: {out!r}"
    assert "STALE" in out


@pytest.mark.parametrize("age,want", [(STALE - 1, 0), (STALE, 0), (STALE + 1, 4)])
def test_staleness_boundary_is_strictly_greater_than(age, want):
    """`age > stale_after` -- at exactly the threshold the claim is still live."""
    rc, out = _run("status", _claims_body(age=age))
    assert rc == want, f"age={age} vs threshold {STALE} -> rc={rc}, want {want}: {out!r}"


@pytest.mark.parametrize("state", ["IDLE", "STOPPED", "", "running"])
def test_non_running_state_refuses(state):
    """A claim row that is not RUNNING is not a live runner.

    'running' lowercase IS accepted (the branch upper()s it) -- included here to
    pin that the comparison is case-insensitive rather than accidentally strict.
    """
    rc, out = _run("status", _claims_body(state=state, age=10))
    if state.upper() == "RUNNING":
        assert rc == 0, f"state={state!r} should normalise to RUNNING: {out!r}"
    else:
        assert rc == 4, f"state={state!r} -> rc={rc}, want 4: {out!r}"
        assert "NOT-RUNNING" in out


# ---------------------------------------------------------------------------
# Unreadable inputs must refuse, not affirm (guard-487: absent != clear)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stale_after", [None, 0, -1, "3900", 3900.5])
def test_unusable_threshold_refuses(stale_after):
    """A daemon that cannot report a usable freshness threshold leaves freshness
    UNREADABLE. Unreadable is not fresh -- refuse. (A pre-change daemon omits the
    field entirely; this is the live shape observed on cc-04 before the recycle.)
    """
    rc, out = _run("status", _claims_body(stale_after=stale_after, age=10))
    assert rc == 4, f"stale_after={stale_after!r} -> rc={rc}, want 4: {out!r}"
    assert "REFUSE" in out and "runner_stale_seconds" in out


@pytest.mark.parametrize("hb", [None, "not-an-int", [], {}])
def test_unreadable_heartbeat_refuses(hb):
    body = json.dumps({"backend": "own-cloud", "ok": True, "runner_stale_seconds": STALE,
                       "environment_id": "ayoai-mind",
                       "claims": [{"agent": "alpha", "machine_id": "cc-99",
                                   "agent_state": "RUNNING", "heartbeat_at": hb}]})
    rc, out = _run("status", body)
    assert rc == 4, f"heartbeat_at={hb!r} -> rc={rc}, want 4: {out!r}"


def test_daemon_error_is_two_not_four():
    """ok:false is a DAEMON ERROR (block exit 2 -> wrapper exit 1), which the
    contract keeps distinct from REFUSE."""
    body = json.dumps({"backend": "own-cloud", "ok": False, "error": "AccessDenied Scan"})
    rc, out = _run("status", body)
    assert rc == 2, f"daemon error must be 2, got {rc}: {out!r}"
    assert "FAILED" in out


def test_unparseable_body_refuses_for_status_but_raw_echoes_for_others():
    """Divergence pinned deliberately.

    Exit 3 is the wrapper's "degrade to a raw echo, exit 0" path. That is fine
    for a mutating op (the call succeeded; only the summary failed) and wrong for
    status, whose exit code IS the answer.
    """
    rc_status, out = _run("status", "not json at all")
    assert rc_status == 4, f"status on unparseable body must refuse, got {rc_status}: {out!r}"
    rc_release, _ = _run("release", "not json at all")
    assert rc_release == 3, f"non-status unparseable must stay 3, got {rc_release}"


# ---------------------------------------------------------------------------
# Regression: the three mutating ops are untouched
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op,body,want", [
    ("release", '{"ok":true,"released":false,"backend":"own-cloud"}', 5),
    ("release", '{"ok":true,"released":true,"backend":"own-cloud"}', 0),
    ("release", '{"ok":true,"noop":true,"backend":"local","reason":"x"}', 0),
    ("acquire", '{"ok":true,"acquired":false,"held":true,"backend":"own-cloud"}', 4),
    ("acquire", '{"ok":true,"acquired":true,"held":false,"backend":"own-cloud"}', 0),
    ("heartbeat", '{"ok":true,"beat":true,"backend":"own-cloud"}', 0),
])
def test_sibling_ops_unchanged_without_agent_env(op, body, want):
    """The new branch reads AGENT, but only under op=='status'. Callers of the
    other three never set it -- proven here by running with AGENT absent, which
    is exactly how test_runner_claim_release_surface.py invokes the block."""
    env = {k: v for k, v in os.environ.items() if k != "AGENT"}
    env.update({"RESPONSE": body, "OP": op, "STORAGE_BACKEND": "local"})
    p = subprocess.run([sys.executable, "-"], input=_summary_block(),
                       capture_output=True, text=True, env=env)
    assert p.returncode == want, (
        f"op={op} regressed to rc={p.returncode} (want {want}): "
        f"{(p.stdout + p.stderr)!r}")


# ---------------------------------------------------------------------------
# The claim-liveness gate's goal-id extraction (fixed alongside this goal)
# ---------------------------------------------------------------------------

def _live_gate_regex() -> str:
    """Read the goal-id ERE out of the LIVE mind-api-start.sh, so this test
    pins the shipped pattern rather than a copy that can drift."""
    src = _STARTER.read_text(encoding="utf-8")
    m = re.search(r"grep -oE '(g-\[0-9\][^']*)'", src)
    assert m, "could not locate the claim-liveness goal-id grep in mind-api-start.sh"
    return m.group(1)


@pytest.mark.parametrize("gid", [
    "g-306-118",      # plain
    "g-115-4661",     # 4-digit goal counter
    "g-306-118-b",    # decomposition child -- the truncation case
    "g-250-03-c",
    "g-115-1234-a",
])
def test_claim_liveness_regex_does_not_truncate_decomposition_children(gid):
    """Regression for a permanent, silent restart wedge.

    `g-[0-9]+-[0-9]+` matched only the PARENT of a decomposition child, so the
    gate liveness-checked 'g-306-118' while the agent was executing
    'g-306-118-b'. A decomposed parent is by definition status='decomposed',
    which the check reports STALE -- so --restart was REFUSED 100% of the time
    for any agent working any decomposition child. It is not caught by any
    fail-open path because the probe SUCCEEDS, just against the wrong goal.

    Runs the real grep in the real production pipeline shape (the wrapper pipes
    a team-state JSON body through `grep -oE ... | head -n1`).
    """
    pattern = _live_gate_regex()
    body = json.dumps({"goal_id": gid, "title": "x", "phase": "4"})
    p = subprocess.run(f"grep -oE {pattern!r} | head -n1", shell=True,
                       input=body, capture_output=True, text=True)
    assert p.stdout.strip() == gid, (
        f"regex {pattern!r} extracted {p.stdout.strip()!r} from {gid!r} — "
        f"truncating a child to its parent re-wedges the restart gate")


def test_claim_liveness_regex_still_takes_one_id_from_a_doubled_body():
    """The 2026-07-18 rt_call double-emit defence must survive the fix."""
    pattern = _live_gate_regex()
    p = subprocess.run(f"grep -oE {pattern!r} | head -n1", shell=True,
                       input="g-306-118-bg-306-118-b", capture_output=True, text=True)
    assert p.stdout.strip() == "g-306-118-b"
