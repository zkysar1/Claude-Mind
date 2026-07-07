"""Unconditional live-claim agent-ownership resolution (owncloud_sync._owned_agents).

History: the g-115-1335 chain built dynamic runner-derived ownership behind an
OWNERSHIP_MODE cutover flag (g-1336 design, g-1337 DDB session-lock, g-1338
live-claim resolution, g-1339 /stop full-dir flush + stale-lock-break, g-1340
cutover). The flag — and the static MACHINE_OWNED_AGENTS / runner-token
fallbacks — were REMOVED (g-115-1737, 2026-07-02): single-runner ownership is now
UNCONDITIONAL and keyed on STORAGE_BACKEND alone. This file regresses the
live-claim resolver in its now-only form.

own X  <=>  THIS machine holds a live RUNNING DDB runner-claim for X whose
heartbeat_at is within OWNERSHIP_STALE_SECONDS. Resolution:

  * own-cloud backend -> the live-claim set (this machine's fresh RUNNING claims).
  * A-stop-B move  -- start on machine A, /stop releases the claim, /start on
    machine B with NO env edit and NO daemon restart: the resolver reads the live
    claim at call-time, so ownership follows the claim from A to B.
  * crash-no-release -- a crashed runner whose heartbeat went stale does NOT
    permanently pin ownership: a stale RUNNING claim is excluded.
  * local backend -> own ALL (None); no cross-machine contention.
  * DDB failure / unknown machine_id -> own NONE (empty set), NEVER own-all: a
    machine that cannot prove it holds the claim must not push a peer's cached
    dir over the peer's newer S3 bytes. (This closes the latent own-all-on-failure
    hole the removed static fallback had when MACHINE_OWNED_AGENTS was unset.)

Pure unit test: a fake claim backend (no DDB, no moto) is wired through
storage_backend.get_backend, which _owned_agents() late-imports.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import owncloud_sync as _mod  # noqa: E402 — module under test
import storage_backend  # noqa: E402 — monkeypatch target for get_backend
from owncloud_backend import RunnerClaim, OwnCloudPermissionError  # noqa: E402

STALE = 900  # OWNERSHIP_STALE_SECONDS under test


@pytest.fixture(autouse=True)
def _clean_ownership_env(monkeypatch):
    """Default every test to an unset ownership env so a runner shell that
    exports STORAGE_BACKEND / MACHINE_MULTI / OWNERSHIP_STALE_SECONDS (this
    repo's .env.local sets some of them) cannot perturb results. Each test opts
    into the backend it exercises via the _wire fixture or an explicit setenv."""
    monkeypatch.delenv("MACHINE_MULTI", raising=False)
    monkeypatch.delenv("OWNERSHIP_STALE_SECONDS", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)


class _FakeClaimBackend:
    """Minimal stand-in for OwnCloudBackend: exposes the two attributes the
    resolver reads -- .machine_id (SSOT for 'me') and .list_runner_claims() ->
    [RunnerClaim]. raise_on_list models the 'live claim read failed' branch."""

    def __init__(self, machine_id, claims=(), *, raise_on_list=None):
        self.machine_id = machine_id
        self._claims = list(claims)
        self._raise = raise_on_list
        self.calls = 0

    def list_runner_claims(self):
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return list(self._claims)


def _claim(agent, machine_id, *, state="RUNNING", age_s=0):
    """A RunnerClaim heartbeated `age_s` seconds ago (age_s=0 => fresh now)."""
    return RunnerClaim(
        agent=agent,
        machine_id=machine_id,
        agent_state=state,
        heartbeat_at=int(time.time()) - age_s,
    )


@pytest.fixture
def wire(monkeypatch):
    """Wire a fake backend into _owned_agents()'s late `from storage_backend
    import get_backend`, and set the own-cloud env. Returns a setter so a test
    can swap the backend (resolve the same claim set as machine A then as
    machine B) without re-entering the fixture."""

    def _set(be, *, backend="own-cloud", stale=STALE):
        monkeypatch.setenv("STORAGE_BACKEND", backend)
        monkeypatch.setenv("OWNERSHIP_STALE_SECONDS", str(stale))
        monkeypatch.setattr(storage_backend, "get_backend", lambda: be)

    return _set


# ── A-stop-B move: ownership follows the live claim, no env edit / no restart ──

def test_ownership_a_stop_b_move_follows_live_claim(wire):
    # Phase 1 — alpha runs on machine A. A holds the live RUNNING claim.
    be_a = _FakeClaimBackend("machineA", [_claim("alpha", "machineA")])
    wire(be_a)
    assert _mod._owned_agents() == {"alpha"}, "A owns alpha while it holds the claim"

    # Phase 2 — /stop on A flushes + releases; /start on B re-claims. The live
    # claim row now names machineB. NO env edit, NO restart: the resolver
    # re-reads the claim at call-time.
    moved_claim = [_claim("alpha", "machineB")]
    # Resolve as machine A now: A no longer holds the claim -> de-owns alpha.
    wire(_FakeClaimBackend("machineA", moved_claim))
    assert _mod._owned_agents() == set(), "A de-owns alpha after the claim moved to B"
    # Resolve as machine B: B holds the live claim -> owns alpha.
    wire(_FakeClaimBackend("machineB", moved_claim))
    assert _mod._owned_agents() == {"alpha"}, "B owns alpha after re-claim"


# ── crash-no-release: a stale RUNNING claim does not pin ownership ──

def test_ownership_crash_no_release_stale_claim_excluded(wire):
    # machineA crashed: its RUNNING claim is still in DDB but heartbeat_at is
    # older than the stale window. Resolving as A must NOT own alpha.
    crashed = _FakeClaimBackend("machineA", [_claim("alpha", "machineA", age_s=STALE + 60)])
    wire(crashed)
    assert _mod._owned_agents() == set(), (
        "a crashed runner's stale RUNNING claim does not pin ownership"
    )


def test_ownership_crash_then_peer_reclaim_owns(wire):
    # After A's crash, B breaks the stale lock and re-claims. Both rows can
    # transiently coexist (A stale-RUNNING, B fresh-RUNNING). Resolving as B
    # owns alpha; resolving as A owns nothing.
    claims = [
        _claim("alpha", "machineA", age_s=STALE + 60),  # crashed, stale
        _claim("alpha", "machineB", age_s=0),           # reclaimed, fresh
    ]
    wire(_FakeClaimBackend("machineB", claims))
    assert _mod._owned_agents() == {"alpha"}, "B owns alpha after stale-lock-break reclaim"
    wire(_FakeClaimBackend("machineA", claims))
    assert _mod._owned_agents() == set(), "crashed A does not re-own alpha"


def test_ownership_stale_boundary_is_strict(wire):
    # heartbeat exactly at the stale boundary is NOT live (resolver uses strict
    # `< stale`); one second fresher IS live.
    wire(_FakeClaimBackend("machineA", [_claim("alpha", "machineA", age_s=STALE)]))
    assert _mod._owned_agents() == set(), "claim at the exact stale boundary is excluded"
    wire(_FakeClaimBackend("machineA", [_claim("alpha", "machineA", age_s=STALE - 5)]))
    assert _mod._owned_agents() == {"alpha"}, "claim just inside the window is owned"


# ── fail-safe + backend-gating branches ──

def test_ownership_ddb_failure_owns_none(wire):
    # own-cloud but the live claim read raises -> own NONE (empty set), NEVER
    # own-all (own-all on a 2nd machine clobbers peer S3 bytes) and NEVER a stale
    # static list (that env var no longer exists). Closes the latent
    # own-all-on-failure hole the removed static fallback had.
    failing = _FakeClaimBackend("machineA", raise_on_list=RuntimeError("DDB down"))
    wire(failing)
    assert _mod._owned_agents() == set(), "failed claim read -> own none, never own-all"


def test_ownership_ddb_failure_is_empty_set_not_none(wire):
    # Explicitly: the failure fallback is the EMPTY SET, not None. None would mean
    # own-all (the walk-prune's `owned is not None` branch would skip pruning),
    # re-opening the clobber hole. Must be set().
    failing = _FakeClaimBackend("machineA", raise_on_list=RuntimeError("DDB down"))
    wire(failing)
    result = _mod._owned_agents()
    assert result == set() and result is not None, (
        "fallback is the empty set, never None/own-all"
    )


def test_ownership_permission_gap_fails_loud(wire):
    # : a PERSISTENT IAM/permission gap (OwnCloudPermissionError from
    # list_runner_claims' DDB Scan — e.g. the daemon's creds lack dynamodb:Scan)
    # must NOT conservative-degrade to own-none the way a transient error does.
    # A permission gap silently owning no dirs is the 2026-07-04 fleet-wedge
    # (): the fleet synced nothing for days because AccessDenied looked
    # identical to "owns no agent dirs". It must fail LOUD (re-raise) so the gap
    # surfaces for remediation. Contrast test_ownership_ddb_failure_owns_none
    # (transient RuntimeError -> own none, unchanged).
    failing = _FakeClaimBackend(
        "machineA", raise_on_list=OwnCloudPermissionError("dynamodb:Scan denied"))
    wire(failing)
    with pytest.raises(OwnCloudPermissionError):
        _mod._owned_agents()


def test_ownership_unknown_machine_id_owns_none(wire):
    # machine_id unresolved ('unknown' or falsy) -> cannot prove which machine we
    # are -> own NONE (empty set), never own-all.
    for mid in ("unknown", "", None):
        wire(_FakeClaimBackend(mid, [_claim("alpha", "machineA")]))
        assert _mod._owned_agents() == set(), f"machine_id={mid!r} -> own none"


def test_ownership_local_backend_owns_all(wire):
    # STORAGE_BACKEND != own-cloud (single-machine local store) -> own ALL (None).
    # The claim narrowing only applies under own-cloud.
    wire(_FakeClaimBackend("machineA", [_claim("alpha", "machineA")]), backend="local")
    assert _mod._owned_agents() is None, "local backend owns all (None)"


# ── claim-filter predicate: only my-machine, RUNNING, fresh claims count ──

def test_ownership_peer_running_claim_not_owned_by_me(wire):
    # A peer's fresh RUNNING claim (machine_id=B) is NOT in my (machineA) set.
    wire(_FakeClaimBackend("machineA", [
        _claim("alpha", "machineA"),   # mine
        _claim("bravo", "machineB"),   # peer's -> excluded
    ]))
    assert _mod._owned_agents() == {"alpha"}, "peer-machine claims are not owned by me"


def test_ownership_idle_claim_on_my_machine_not_owned(wire):
    # An IDLE row on my machine (agent_state != RUNNING) is NOT owned: ownership
    # tracks the live RUNNING runner, not a parked IDLE binding.
    wire(_FakeClaimBackend("machineA", [
        _claim("alpha", "machineA", state="RUNNING"),
        _claim("bravo", "machineA", state="IDLE"),
    ]))
    assert _mod._owned_agents() == {"alpha"}, "IDLE claims on my machine are not owned"


def test_ownership_no_claims_owns_none(wire):
    # own-cloud, this machine holds zero live claims -> own NONE (empty set, not
    # None). The walk-prune's 'owned is not None' branch then prunes all agent
    # dirs, which is correct when this machine runs nothing.
    wire(_FakeClaimBackend("machineA", []))
    assert _mod._owned_agents() == set(), "no live claims -> own none (empty set, not None)"


def test_ownership_none_machine_id_claim_excluded(wire):
    # A claim row with machine_id=None (a create-only IDLE row) must be EXCLUDED,
    # not crash: None == "machineA" is False.
    be = _FakeClaimBackend("machineA", [
        RunnerClaim(agent="alpha", machine_id=None, agent_state="RUNNING",
                    heartbeat_at=int(time.time())),
    ])
    wire(be)
    assert _mod._owned_agents() == set(), "a None-machine_id claim is excluded, not a crash"


def test_ownership_resolves_once_per_call(wire):
    # Cost control: exactly one list_runner_claims() per _owned_agents() call —
    # never a per-file DDB read.
    be = _FakeClaimBackend("machineA", [_claim("alpha", "machineA")])
    wire(be)
    _mod._owned_agents()
    assert be.calls == 1
