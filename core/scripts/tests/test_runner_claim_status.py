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
                 claim_fields=None, **overrides):
    """Build a GET /v1/admin/runner-claims body with heartbeat `age` seconds old.

    `claim_fields` merges extra keys into THIS agent's claim dict (as opposed to
    `**overrides`, which patches the top-level body). Needed by the g-306-224
    fingerprint tests, and deliberately unrestricted so a test can inject a key
    the endpoint does NOT emit — that is how the wrapper is proven to read only
    the digest and never a raw token.
    """
    claims = [{"agent": a, "machine_id": "other-box", "agent_state": "RUNNING",
               "heartbeat_at": int(time.time()) - 30} for a in extra_agents]
    if agent is not None:
        claims.append({"agent": agent, "machine_id": machine, "agent_state": state,
                       "heartbeat_at": (int(time.time()) - age) if age is not None else None,
                       **(claim_fields or {})})
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
# THE LOCAL ARM () — the gate keys on CAPABILITY, not backend NAME.
#
# The pin above is about a backend with NO claim store. A local backend now MAY
# have one: the git-ref arm backs the same lease with refs/mind/claim/<env>/<agent>.
# The two sections are not in tension — together they say the gate must ask "did a
# real claim store answer?", which the daemon reports as `claim_store`, rather
# than "is the backend named own-cloud?". A name-keyed predicate refused the git
# backend forever however live its claim was, which is the defect  fixed;
# the docstring at the top of this file anticipated that backend arriving by
# promotion and the predicate could not see it.
# ---------------------------------------------------------------------------

def test_local_backend_with_claim_store_reports_live():
    """Criterion 1: a real verdict naming the holder, NOT rc=4 REFUSE."""
    body = _claims_body(backend="local", claim_store=True,
                        machine="cc-77", age=60)
    rc, out = _run("status", body)
    assert rc == 0, f"live local-backend claim must report LIVE, got rc={rc}: {out!r}"
    assert "LIVE" in out
    assert "cc-77" in out, f"verdict must name the holder machine: {out!r}"


def test_local_backend_with_claim_store_refuses_when_stale():
    """A claim store answering does not make a STALE claim live — the freshness
    branch must still apply once the capability gate is passed."""
    body = _claims_body(backend="local", claim_store=True, age=STALE + 600)
    rc, out = _run("status", body)
    assert rc == 4, f"stale claim must REFUSE even with a claim store: {out!r}"
    assert "STALE" in out


def test_local_backend_with_claim_store_refuses_when_not_running():
    body = _claims_body(backend="local", claim_store=True, state="IDLE")
    rc, out = _run("status", body)
    assert rc == 4
    assert "NOT-RUNNING" in out


def test_local_backend_with_claim_store_refuses_when_agent_absent():
    body = _claims_body(backend="local", claim_store=True, agent=None,
                        extra_agents=("bravo",))
    rc, out = _run("status", body)
    assert rc == 4
    assert "ABSENT" in out


def test_absent_claim_store_field_falls_back_to_legacy_predicate():
    """Backward compatibility, pinned explicitly rather than left implicit.

    A daemon older than the `claim_store` field omits it entirely. The gate must
    then behave EXACTLY as before (`backend == "own-cloud"`), so upgrading the
    wrapper ahead of the daemon cannot start reporting local boxes as alive —
    and, in the other direction, cannot start refusing own-cloud.
    """
    legacy_local = _claims_body(backend="local", age=60)      # no claim_store key
    rc, out = _run("status", legacy_local)
    assert rc == 4, f"legacy local body must REFUSE: {out!r}"

    legacy_owncloud = _claims_body(backend="own-cloud", age=60)  # no claim_store key
    rc, out = _run("status", legacy_owncloud)
    assert rc == 0, f"legacy own-cloud body must stay LIVE: {out!r}"


def test_claim_store_false_refuses_regardless_of_backend_name():
    """Fail-safe direction: an explicit `claim_store: false` refuses even when
    the backend calls itself own-cloud. Capability beats name in BOTH directions,
    so a backend that knows it cannot witness a claim is believed."""
    body = _claims_body(backend="own-cloud", claim_store=False, age=60)
    rc, out = _run("status", body)
    assert rc == 4, f"explicit claim_store:false must REFUSE: {out!r}"
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
    """Per core/scripts/runner-claim.sh, ok:false is a DAEMON ERROR — that file's
    summary block exits 2 on `not r.get("ok")` and its case arm maps that 2 to
    wrapper exit 1 — which the contract keeps distinct from REFUSE.
    """
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


# ---------------------------------------------------------------------------
# The runner-token FINGERPRINT clause on the LIVE line ()
# ---------------------------------------------------------------------------
#
# worker_reducer_liveness must notice a SAME-BOX reducer restart — a re-minted
# runner_token under an UNCHANGED machine_id — which the machine axis
# structurally cannot see. The claim payload deliberately carries a
# non-reversible DIGEST and never the token: runner_token is the
# ConditionExpression bearer credential for heartbeat() and release_runner(), so
# a reader holding it could forge a heartbeat (defeating reclaim_if_stale) or
# release a live claim. This wrapper is the surface that publishes the digest.

def _live_line(out: str) -> str:
    for ln in out.splitlines():
        if "status: LIVE" in ln:
            return ln
    raise AssertionError(f"no LIVE line in wrapper output: {out!r}")


def test_live_line_carries_the_token_fingerprint_from_the_payload():
    rc, out = _run("status", _claims_body(
        age=60, claim_fields={"runner_token_fp": "1f4c0a9b2e6d8035"}))
    assert rc == 0, out
    assert "token-fp 1f4c0a9b2e6d8035" in _live_line(out)


def test_live_line_prints_unknown_when_the_daemon_omits_the_fingerprint():
    """A daemon predating the field emits no runner_token_fp. The wrapper must
    still produce a well-formed LIVE line — and `unknown` must parse back to
    NON-DISCRIMINATING on the consumer side, never to a fingerprint literally
    spelled 'unknown' (which would compare EQUAL across two different reducers
    and read as 'no takeover', the one direction the axis must never fail in)."""
    rc, out = _run("status", _claims_body(age=60))
    assert rc == 0, out
    assert "token-fp unknown" in _live_line(out)


def test_the_wrapper_never_prints_the_raw_token_even_if_the_payload_carries_one():
    """Defence in depth. The endpoint has no raw-token field to send (RunnerClaim
    carries only the digest), but a wrapper that echoed whatever it was handed
    would turn any future upstream slip into a credential in every worker's
    captured stdout and state file. The wrapper must read ONLY the fp key."""
    rc, out = _run("status", _claims_body(
        age=60, claim_fields={"runner_token_fp": "1f4c0a9b2e6d8035",
                              "runner_token": "f47ac10b-58cc-4372-a567-0e02b2c3d479"}))
    assert rc == 0, out
    assert "f47ac10b" not in out
    assert "token-fp 1f4c0a9b2e6d8035" in _live_line(out)


def test_the_real_emitter_output_is_readable_by_the_real_parser():
    """The end-to-end join, and the one that makes the other three more than
    hand-copies (guard-920).

    runner-claim.sh is bash-with-embedded-python and cannot import from
    worker_reducer_liveness, so emitter and parser are linked only by a prose
    format. Both `_parse_machine` and `_parse_token_fp` are driven here against
    the LIVE output of the REAL wrapper block — so a reformat on either side
    fails here instead of silently returning None and killing takeover
    detection.
    """
    sys.path.insert(0, str(_SCRIPTS))
    from worker_reducer_liveness import _parse_machine, _parse_token_fp

    rc, out = _run("status", _claims_body(
        age=60, machine="cc-02",
        claim_fields={"runner_token_fp": "1f4c0a9b2e6d8035"}))
    assert rc == 0, out
    assert _parse_machine(out) == "cc-02"
    assert _parse_token_fp(out) == "1f4c0a9b2e6d8035"

    # ...and the pre-upgrade line must yield None, not the string "unknown".
    _, old = _run("status", _claims_body(age=60, machine="cc-02"))
    assert _parse_machine(old) == "cc-02"
    assert _parse_token_fp(old) is None


# ---------------------------------------------------------------------------
# : python rc=1 (ANY uncaught exception) must be OP-AWARE.
#
# Every test above drives the extracted summary block and asserts the rc IT
# returns. This fix lives one layer out — in the wrapper's `case $pyrc` mapping,
# where rc=1 used to fall into the `*)` catch-all and become "raw echo, exit 0".
# That mapping is correct for the three MUTATING ops (the daemon call succeeded;
# only the summary rendering crashed) and is the fail-OPEN direction for
# `status`, whose exit code IS the answer — a stack trace would be reported to
# the caller as a live runner.
#
# So two things need pinning, and the first is what makes the second more than
# decorative: that the crash is REACHABLE at all (the block really returns 1 on
# a plausible shape), and that the wrapper then routes it by op.
# ---------------------------------------------------------------------------

def _resolved_bash() -> str:
    """The bash binary, resolved the way every framework caller resolves it.

    Never a bare "bash" argv[0]: on win32 CreateProcess searches System32 before
    PATH, so it binds the WSL launcher and blocks forever on a dead LxssManager
    (rb-5255 / the bare-bash-authoring gate).
    """
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    from _runtime_bash import BASH
    return BASH


def _case_block() -> str:
    """Extract the wrapper's live `case $pyrc in ... esac` rc-mapping block.

    Read from the wrapper on every run, exactly as _summary_block() is, so this
    test cannot drift from a stale copy of the mapping.
    """
    src = _WRAPPER.read_text(encoding="utf-8")
    m = re.search(r"^(case \$pyrc in$.*?^esac$)", src, re.S | re.M)
    assert m, "could not locate the `case $pyrc` mapping in runner-claim.sh"
    return m.group(1)


def _run_case(pyrc, op, response='{"raw":true}', summary="summary-text",
              agent="alpha"):
    """Drive the REAL extracted case block with a given python rc and op.

    `agent=None` runs with AGENT ABSENT from the environment. The mapping must
    not depend on it -- AGENT is consumed inside the summary block, never by the
    case arms -- and the goal's own criterion asks for the sibling ops to be
    proven in exactly that shape, matching the existing python-level
    test_sibling_ops_unchanged_without_agent_env.
    """
    import shlex
    script = (
        "pyrc=%d\nOP=%s\nRESPONSE=%s\nSUMMARY=%s\n%s\n"
        % (pyrc, shlex.quote(op), shlex.quote(response),
           shlex.quote(summary), _case_block())
    )
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT", "MIND_AGENT")}
    if agent is not None:
        env["AGENT"] = agent
    p = subprocess.run([_resolved_bash(), "-s"], input=script,
                       capture_output=True, text=True, env=env)
    return p.returncode, (p.stdout + p.stderr)


# MEASURED, not assumed: a probe over 10 response shapes run against this same
# extracted block from core/scripts/runner-claim.sh on 2026-09-06 (hostname
# cc-13, uname -r 6.8.0-138-generic,
# both read verbatim in that session -- a nickname alone has named two different
# machines before). The
# goal named three expected triggers; TWO were FALSE. `claims` arriving as a
# dict and `claims` entries as bare strings do NOT crash — the
# `runner_stale_seconds is None` guard fires first and already refuses at rc=4,
# as do claims-as-string, int entries, a string stale value and a string
# heartbeat age. The one real trigger generalises WIDER than the goal described:
# it is not about `claims` at all, it is TOP-LEVEL NOT A DICT, where `r.get`
# raises before any claims handling is reached.
@pytest.mark.parametrize("body,shape", [
    ('[{"ok":true,"backend":"own-cloud"}]', "top-level JSON array"),
    ('"just a string"', "top-level JSON string"),
    ('42', "top-level JSON number"),
])
def test_toplevel_not_a_dict_reaches_rc1(body, shape):
    """The TRIGGER must be reachable, or the wrapper arm below is decorative.

    Source for every claim here: the summary block of core/scripts/runner-claim.sh,
    which _summary_block() extracts and runs directly below -- so each statement is
    re-checked against that file on every run rather than asserted from memory.
    json.loads() succeeds on any of these, so the unparseable-body path (already
    closed by g-306-118-b) never fires; the AttributeError lands on the very
    first `r.get` in core/scripts/runner-claim.sh. This is the residue that arm
    could not cover.
    """
    rc, out = _run("status", body)
    assert rc == 1, f"expected an uncaught exception (rc=1) for {shape}; got {rc}: {out!r}"
    assert "AttributeError" in out, f"expected the crash to surface: {out!r}"


@pytest.mark.parametrize("body,shape", [
    ('{"ok":true,"backend":"own-cloud","claims":{"alpha":{"state":"RUNNING"}}}',
     "claims as a dict"),
    ('{"ok":true,"backend":"own-cloud","claims":["alpha","bravo"]}',
     "claims entries as bare strings"),
    ('{"ok":true,"backend":"own-cloud","claims":"alpha"}', "claims as a string"),
    ('{"ok":true,"backend":"own-cloud","claims":[1,2,3]}', "claims as ints"),
])
def test_claims_level_malformations_already_refuse_without_crashing(body, shape):
    """BOUNDARY control: these were expected to crash and DO NOT.

    Pinned deliberately. It records which half of the malformed-shape space was
    already safe before the rc=1 arm existed, so a future reader does not re-file
    this ground, and it fails loudly if the freshness guard that catches them is
    ever moved below the claims handling.
    """
    rc, out = _run("status", body)
    assert rc == 4, f"{shape} should already REFUSE at rc=4, got {rc}: {out!r}"
    assert "REFUSE" in out, f"{shape} must refuse explicitly: {out!r}"


def test_wrapper_maps_crash_to_refuse_for_status():
    """`status` is the ASSERTING op: a crashed summary must refuse, never affirm.

    rb-6448 is the governing lesson — for an asserting op over a polymorphic
    backend the question is not what the siblings return, but whether silence
    reads as YES or NO in the caller's hands. g-306-118-b applied exactly this to
    three other fail-open status paths (the shared noop branch, an unparseable
    body, a missing python launcher) and missed this one.
    """
    rc, out = _run_case(1, "status")
    assert rc == 4, f"a crashed status must REFUSE with 4, got {rc}: {out!r}"
    assert "REFUSE" in out, f"the refusal must be diagnosed, not silent: {out!r}"
    assert "NOTHING" in out, f"diagnostic must say the result asserts nothing: {out!r}"


@pytest.mark.parametrize("agent", ["alpha", None], ids=["agent-set", "agent-absent"])
@pytest.mark.parametrize("op", ["acquire", "heartbeat", "release"])
def test_wrapper_keeps_raw_echo_exit0_for_mutating_ops_on_crash(op, agent):
    """The three MUTATING ops stay deliberately unchanged.

    Their daemon call already succeeded and only the summary rendering crashed,
    so degrading to a raw echo with exit 0 remains correct — the mutation
    happened. Pinned so the status fix is not later over-applied to them.
    """
    rc, out = _run_case(1, op, agent=agent)
    label = f"op={op} agent={'set' if agent else 'ABSENT'}"
    assert rc == 0, f"{label} must stay exit 0 on a crashed summary, got {rc}: {out!r}"
    assert "(raw)" in out, f"{label} must still raw-echo the response: {out!r}"


@pytest.mark.parametrize("pyrc,op,want", [
    (0, "status", 0),      # verdict rendered
    (2, "status", 1),      # daemon op-level failure
    (4, "acquire", 4),     # acquire held
    (5, "release", 5),     # release unconfirmed
    (3, "status", 0),      # unenumerated rc still hits the catch-all
])
def test_wrapper_other_rc_mappings_unchanged(pyrc, op, want):
    """Regression: adding the rc=1 arm must not disturb any other mapping."""
    rc, _ = _run_case(pyrc, op)
    assert rc == want, f"pyrc={pyrc} op={op} mapped to {rc}, want {want}"
