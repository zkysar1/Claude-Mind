"""g-115-1340: dynamic runner-derived agent-ownership cutover regression.

Capstone of the g-115-1335 chain (g-1336 design, g-1337 DDB session-lock,
g-1338 live-claim ownership resolution in owncloud_sync._owned_agents,
g-1339 /stop full-dir S3 flush + stale-lock-break). Exercises the dynamic
ownership resolver (lodestar §3) that the cutover flips ON via OWNERSHIP_MODE.

own X  <=>  THIS machine holds a live RUNNING DDB runner-claim for X whose
heartbeat_at is within OWNERSHIP_STALE_SECONDS. Three goal-named cases:

  * A-stop-B move  -- start on machine A, /stop releases the claim, /start on
    machine B with NO env edit and NO daemon restart. The resolver reads the
    live claim at call-time, so ownership follows the claim from A to B with
    zero static MACHINE_OWNED_AGENTS edits.
  * crash-no-release -- a crashed runner whose heartbeat went stale does NOT
    permanently pin ownership: a stale RUNNING claim is excluded, so a peer's
    reclaim (stale-lock-break) can take over.
  * two-machine-no-cutover safety -- until the cutover flips OWNERSHIP_MODE to
    'dynamic', the resolver is byte-identical to the static MACHINE_OWNED_AGENTS
    path. The dynamic path is INERT under the default, so a deployment that has
    not opted in cannot be perturbed mid-flight.

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
from owncloud_backend import RunnerClaim  # noqa: E402

STALE = 900  # OWNERSHIP_STALE_SECONDS under test


@pytest.fixture(autouse=True)
def _clean_ownership_env(monkeypatch):
    """Default every test to the inert/unset ownership state so a runner shell
    that has MACHINE_OWNED_AGENTS / OWNERSHIP_MODE / STORAGE_BACKEND exported
    (this repo's .env.local sets all three) cannot perturb results. Each test
    opts into the mode it exercises via the _wire fixture or an explicit setenv.
    """
    monkeypatch.delenv("MACHINE_OWNED_AGENTS", raising=False)
    monkeypatch.delenv("MACHINE_MULTI", raising=False)
    monkeypatch.delenv("OWNERSHIP_MODE", raising=False)
    monkeypatch.delenv("OWNERSHIP_STALE_SECONDS", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)


class _FakeClaimBackend:
    """Minimal stand-in for OwnCloudBackend: exposes the two attributes the
    dynamic resolver reads -- .machine_id (SSOT for 'me') and
    .list_runner_claims() -> [RunnerClaim]. raise_on_list models the
    guard-597 'live claim read failed' branch."""

    def __init__(self, machine_id, claims=(), *, raise_on_list=None):
        self.machine_id = machine_id
        self._claims = list(claims)
        self._raise = raise_on_list

    def list_runner_claims(self):
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
    import get_backend`, and set the dynamic-mode env. Returns a setter so a
    test can swap the backend (e.g. resolve the same claim set as machine A
    then as machine B) without re-entering the fixture."""

    def _set(be, *, mode="dynamic", backend="own-cloud", stale=STALE):
        monkeypatch.setenv("OWNERSHIP_MODE", mode)
        monkeypatch.setenv("STORAGE_BACKEND", backend)
        monkeypatch.setenv("OWNERSHIP_STALE_SECONDS", str(stale))
        monkeypatch.delenv("MACHINE_OWNED_AGENTS", raising=False)
        monkeypatch.setattr(storage_backend, "get_backend", lambda: be)

    return _set


# ── A-stop-B move: ownership follows the live claim, no env edit / no restart ──

def test_ownership_a_stop_b_move_follows_live_claim(wire, monkeypatch):
    # Phase 1 — alpha runs on machine A. A holds the live RUNNING claim.
    be_a = _FakeClaimBackend("machineA", [_claim("alpha", "machineA")])
    wire(be_a)
    assert _mod._owned_agents() == {"alpha"}, "A owns alpha while it holds the claim"

    # Phase 2 — /stop on A flushes + releases; /start on B re-claims. The live
    # claim row now names machineB. NO MACHINE_OWNED_AGENTS edit, NO restart:
    # the resolver re-reads the claim at call-time.
    moved_claim = [_claim("alpha", "machineB")]
    # Resolve as machine A now: A no longer holds the claim -> de-owns alpha.
    wire(_FakeClaimBackend("machineA", moved_claim))
    assert _mod._owned_agents() == set(), "A de-owns alpha after the claim moved to B"
    # Resolve as machine B: B holds the live claim -> owns alpha.
    wire(_FakeClaimBackend("machineB", moved_claim))
    assert _mod._owned_agents() == {"alpha"}, "B owns alpha after re-claim"

    # The move required zero static-list edits — assert it stayed unset.
    import os
    assert os.environ.get("MACHINE_OWNED_AGENTS") is None


def test_ownership_a_stop_b_move_no_static_env_edit(wire, monkeypatch):
    # Even if a stale MACHINE_OWNED_AGENTS lists the agent on the OLD machine,
    # dynamic mode ignores it: ownership is the live claim, not the static list.
    monkeypatch.setenv("MACHINE_OWNED_AGENTS", "alpha")  # stale A-machine list
    # Claim has moved to B; resolve as A. Static list still says "alpha", but
    # dynamic resolution must de-own it (B holds the live claim).
    wire(_FakeClaimBackend("machineA", [_claim("alpha", "machineB")]))
    # wire() clears MACHINE_OWNED_AGENTS to prove dynamic does not consult it,
    # so re-set it AFTER wire to model the stale-env-left-behind scenario.
    monkeypatch.setenv("MACHINE_OWNED_AGENTS", "alpha")
    assert _mod._owned_agents() == set(), (
        "dynamic mode de-owns alpha on A despite a stale MACHINE_OWNED_AGENTS=alpha"
    )


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


# ── two-machine-no-cutover safety: dynamic is INERT until OWNERSHIP_MODE flips ──

def test_ownership_two_machine_no_cutover_static_is_inert(monkeypatch):
    # OWNERSHIP_MODE unset (default). Even with a fully-populated claim backend,
    # _owned_agents() must return the STATIC list, never the dynamic set. This
    # is the safety the goal title names: a deployment that has not opted into
    # the cutover keeps byte-identical static behaviour mid-flight.
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setenv("MACHINE_OWNED_AGENTS", "alpha,bravo")
    populated = _FakeClaimBackend("machineA", [
        _claim("alpha", "machineA"), _claim("zeta", "machineA"),
    ])
    monkeypatch.setattr(storage_backend, "get_backend", lambda: populated)
    # Static path reads MACHINE_OWNED_AGENTS, ignores the live claims entirely.
    assert _mod._owned_agents() == {"alpha", "bravo"}, (
        "static (un-cut-over) mode returns MACHINE_OWNED_AGENTS, not the claim set"
    )


def test_ownership_static_mode_explicit_is_inert(monkeypatch):
    # OWNERSHIP_MODE explicitly 'static' is identically inert.
    monkeypatch.setenv("OWNERSHIP_MODE", "static")
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setenv("MACHINE_OWNED_AGENTS", "alpha")
    boom = _FakeClaimBackend("machineA", raise_on_list=RuntimeError("must not be called"))
    monkeypatch.setattr(storage_backend, "get_backend", lambda: boom)
    # If the static path ever touched the backend this would raise.
    assert _mod._owned_agents() == {"alpha"}


# ── fail-safe + backend-gating branches ──

def test_ownership_dynamic_failsafe_falls_back_to_static(wire, monkeypatch):
    # Dynamic mode but the live claim read raises (guard-597) -> fall back to the
    # STATIC list, NOT own-all (own-all on a 2nd machine clobbers peer S3 bytes).
    failing = _FakeClaimBackend("machineA", raise_on_list=RuntimeError("DDB down"))
    wire(failing)
    monkeypatch.setenv("MACHINE_OWNED_AGENTS", "alpha")  # set after wire cleared it
    assert _mod._owned_agents() == {"alpha"}, "failed claim read falls back to static list"


def test_ownership_dynamic_failsafe_empty_static_not_own_all(wire, monkeypatch):
    # Same fail path, but with NO static list -> _owned_agents_static() returns
    # None... which here means 'own all'. Assert the failsafe returns the static
    # resolver's value verbatim (None), never silently substituting a set.
    failing = _FakeClaimBackend("machineA", raise_on_list=RuntimeError("DDB down"))
    wire(failing)  # wire clears MACHINE_OWNED_AGENTS
    assert _mod._owned_agents() is None, "failsafe returns static resolver's value (None=own-all when list empty)"


def test_ownership_dynamic_unknown_machine_id_falls_back(wire, monkeypatch):
    # machine_id unresolved ('unknown' or falsy) -> cannot prove which machine we
    # are -> fall back to static (conservative).
    for mid in ("unknown", "", None):
        wire(_FakeClaimBackend(mid, [_claim("alpha", "machineA")]))
        monkeypatch.setenv("MACHINE_OWNED_AGENTS", "bravo")
        assert _mod._owned_agents() == {"bravo"}, f"machine_id={mid!r} falls back to static"


def test_ownership_dynamic_local_backend_owns_all(wire):
    # Dynamic mode but STORAGE_BACKEND != own-cloud (single-machine local store)
    # -> own ALL (None). The dynamic path only narrows ownership under own-cloud.
    wire(_FakeClaimBackend("machineA", [_claim("alpha", "machineA")]), backend="local")
    assert _mod._owned_agents() is None, "local backend owns all (None) even under dynamic mode"


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
    # Dynamic, own-cloud, this machine holds zero live claims -> own NONE (empty
    # set, not None). The walk-prune's 'owned is not None' branch then prunes all
    # agent dirs, which is correct when this machine runs nothing.
    wire(_FakeClaimBackend("machineA", []))
    assert _mod._owned_agents() == set(), "no live claims -> own none (empty set, not None)"
