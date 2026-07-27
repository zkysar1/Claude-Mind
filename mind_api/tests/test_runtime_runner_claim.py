"""POST /v1/admin/runner-{acquire,heartbeat,release} — the DDB runner-claim
lifecycle endpoints (lodestar dynamic-ownership design §4).

The cross-machine half of single-runner enforcement: /start acquires a DDB
session-lock, each iteration refreshes its heartbeat, /stop releases it after
the final flush. Tested end-to-end against the in-process daemon (conftest pins
STORAGE_BACKEND=local for the session; the own-cloud-path tests flip it via
monkeypatch and inject fake storage_backend / owncloud_backend modules so NO
real DynamoDB call is ever made).

Backend correctness (the conditional IDLE->RUNNING CAS, idempotent release,
env-scoped enumeration) is covered separately by
core/scripts/tests/test_owncloud_backend.py — these tests assert the ENDPOINT
contract: param validation, the non-own-cloud no-op short-circuit, the
held=true normal-200 answer, and 500-on-error (callers fail open).

File basename starts with `test_` so domain-leak-check.sh skips it (agent names
here are test fixtures, not a domain leak).
"""
from __future__ import annotations

import json
import sys
import time
import types
import urllib.error
import urllib.request


def _post(port: int, path: str, *, agent: str = "") -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=b"", method="POST")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class _RunnerHeld(Exception):
    """Stand-in for owncloud_backend.RunnerHeld so the endpoint's
    `from owncloud_backend import RunnerHeld` and the mock backend's raise refer
    to the SAME class (else `except RunnerHeld` would not catch it)."""


class _Backend:
    """Mock OwnCloudBackend — records calls, drives each method's outcome."""
    def __init__(self, *, acquire_exc=None, heartbeat_exc=None,
                 release_ret=True, release_exc=None,
                 reclaim_ret=False, reclaim_exc=None, acquire_exc_then_ok=False,
                 runner_state=None):
        self._acquire_exc = acquire_exc
        self._heartbeat_exc = heartbeat_exc
        self._release_ret = release_ret
        self._release_exc = release_exc
        # §5 stale-lock-break (): reclaim_if_stale outcome + the
        # acquire-retry path the acquire endpoint runs on RunnerHeld.
        self._reclaim_ret = reclaim_ret
        self._reclaim_exc = reclaim_exc
        self._acquire_exc_then_ok = acquire_exc_then_ok
        # Previous-holder row for the stale-break diagnostics (2026-07-07):
        # the endpoint reads get_runner_state BEFORE reclaiming so a broken
        # claim reports prev_machine_id + heartbeat age. None = row unreadable.
        self._runner_state = runner_state
        self._acquire_n = 0
        self.calls: list = []

    def acquire_runner(self, agent, token):
        self.calls.append(("acquire", agent, token))
        self._acquire_n += 1
        if self._acquire_exc is not None:
            # acquire_exc_then_ok models a stale claim that reclaim broke between
            # attempt 1 (raises) and attempt 2 (succeeds).
            if self._acquire_exc_then_ok and self._acquire_n > 1:
                return True
            raise self._acquire_exc
        return True

    def reclaim_if_stale(self, agent):
        self.calls.append(("reclaim", agent))
        if self._reclaim_exc is not None:
            raise self._reclaim_exc
        return self._reclaim_ret

    def get_runner_state(self, agent):
        self.calls.append(("runner_state", agent))
        return self._runner_state

    def heartbeat(self, agent, token):
        self.calls.append(("heartbeat", agent, token))
        if self._heartbeat_exc:
            raise self._heartbeat_exc

    def release_runner(self, agent, token):
        self.calls.append(("release", agent, token))
        if self._release_exc:
            raise self._release_exc
        return self._release_ret


def _inject(monkeypatch, backend):
    """Pin own-cloud backend + inject fake storage_backend/owncloud_backend so
    the endpoints resolve get_backend()/RunnerHeld to our test doubles."""
    fake_sb = types.ModuleType("storage_backend")
    fake_sb.get_backend = lambda: backend  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "storage_backend", fake_sb)
    fake_ocb = types.ModuleType("owncloud_backend")
    fake_ocb.RunnerHeld = _RunnerHeld  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "owncloud_backend", fake_ocb)
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")


# ── acquire ─────────────────────────────────────────────────────────────────
def test_acquire_local_backend_noop(running_daemon):
    """Under the local backend (default) acquire is a clean no-op — single
    machine, no cross-machine DDB claim to take."""
    _, port = running_daemon
    status, body = _post(port, "/v1/admin/runner-acquire?agent=alpha&token=tokA")
    assert status == 200
    assert body["backend"] == "local"
    assert body["noop"] is True
    assert "reason" in body


def test_acquire_missing_agent_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/admin/runner-acquire?token=tokA")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        body = json.loads(e.read().decode("utf-8"))
        assert body["ok"] is False and "agent" in body["error"]
    else:
        raise AssertionError("expected HTTP 400 when agent query missing")


def test_acquire_missing_token_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/admin/runner-acquire?agent=alpha")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        body = json.loads(e.read().decode("utf-8"))
        assert body["ok"] is False and "token" in body["error"]
    else:
        raise AssertionError("expected HTTP 400 when token query missing")


def test_acquire_own_cloud_success(running_daemon, monkeypatch):
    _, port = running_daemon
    be = _Backend()
    _inject(monkeypatch, be)
    status, body = _post(port, "/v1/admin/runner-acquire?agent=alpha&token=tokA",
                         agent="alpha")
    assert status == 200
    assert body["backend"] == "own-cloud"
    assert body["ok"] is True
    assert body["acquired"] is True
    assert body["held"] is False
    assert be.calls == [("acquire", "alpha", "tokA")]


def test_acquire_own_cloud_held_is_200_not_error(running_daemon, monkeypatch):
    """RunnerHeld (a peer owns a live claim) is a NORMAL 200 answer with
    held=true — not a 500. The /start gate reads held=true and refuses."""
    _, port = running_daemon
    be = _Backend(acquire_exc=_RunnerHeld("alpha already RUNNING"))
    _inject(monkeypatch, be)
    status, body = _post(port, "/v1/admin/runner-acquire?agent=alpha&token=tokB",
                         agent="alpha")
    assert status == 200
    assert body["ok"] is True
    assert body["acquired"] is False
    assert body["held"] is True


def test_acquire_own_cloud_error_500(running_daemon, monkeypatch):
    _, port = running_daemon
    be = _Backend(acquire_exc=RuntimeError("DDB throttled"))
    _inject(monkeypatch, be)
    try:
        _post(port, "/v1/admin/runner-acquire?agent=alpha&token=tokA", agent="alpha")
    except urllib.error.HTTPError as e:
        assert e.code == 500
        body = json.loads(e.read().decode("utf-8"))
        assert body["ok"] is False and "acquire failed" in body["error"]
    else:
        raise AssertionError("expected HTTP 500 on acquire failure")


# ── acquire §5 stale-lock-break: reclaim-on-held () ────────────────
def test_acquire_reclaims_stale_peer_then_succeeds(running_daemon, monkeypatch):
    """On RunnerHeld the endpoint reclaims a CRASHED peer's stale claim and
    retries acquire ONCE — a crash-no-release can never PIN ownership (§5)."""
    _, port = running_daemon
    be = _Backend(acquire_exc=_RunnerHeld("alpha RUNNING (crashed peer)"),
                  reclaim_ret=True, acquire_exc_then_ok=True)
    _inject(monkeypatch, be)
    status, body = _post(port, "/v1/admin/runner-acquire?agent=alpha&token=tokB",
                         agent="alpha")
    assert status == 200
    assert body["ok"] is True
    assert body["acquired"] is True
    assert body["held"] is False
    assert body["reclaimed_stale"] is True
    # runner_state row is None here (unreadable) -> the endpoint's best-effort
    # guard keeps the diagnostics keys ABSENT rather than emitting nulls.
    assert "prev_machine_id" not in body
    assert "prev_heartbeat_age_seconds" not in body
    # acquire(held) -> runner_state(prev capture) -> reclaim(True) -> acquire(ok)
    assert be.calls == [("acquire", "alpha", "tokB"),
                        ("runner_state", "alpha"),
                        ("reclaim", "alpha"),
                        ("acquire", "alpha", "tokB")]


def test_acquire_stale_break_reports_previous_holder(running_daemon, monkeypatch):
    """2026-07-07 bravo dual-runner follow-through: a stale-break acquire must
    report WHO it broke and HOW stale the claim was (prev_machine_id +
    prev_heartbeat_age_seconds), so /start can never again narrate a
    stale-break as 'no live peer was detected'."""
    _, port = running_daemon
    hb_age = 1320  # the incident gap: a 22-minute max-effort turn
    be = _Backend(acquire_exc=_RunnerHeld("bravo RUNNING (stale peer)"),
                  reclaim_ret=True, acquire_exc_then_ok=True,
                  runner_state={"machine_id": "cc-05",
                                "heartbeat_at": str(int(time.time()) - hb_age)})
    _inject(monkeypatch, be)
    status, body = _post(port, "/v1/admin/runner-acquire?agent=bravo&token=tokB",
                         agent="bravo")
    assert status == 200
    assert body["reclaimed_stale"] is True
    assert body["prev_machine_id"] == "cc-05"
    assert hb_age - 10 <= body["prev_heartbeat_age_seconds"] <= hb_age + 60


def test_acquire_live_peer_not_reclaimed_stays_held(running_daemon, monkeypatch):
    """A genuinely-LIVE peer (fresh heartbeat) is NOT reclaimed: reclaim_if_stale
    returns False, so the endpoint answers held=true with NO retry — only a
    crashed peer is broken, never a live one."""
    _, port = running_daemon
    be = _Backend(acquire_exc=_RunnerHeld("alpha RUNNING (live peer)"),
                  reclaim_ret=False)
    _inject(monkeypatch, be)
    status, body = _post(port, "/v1/admin/runner-acquire?agent=alpha&token=tokB",
                         agent="alpha")
    assert status == 200
    assert body["ok"] is True
    assert body["acquired"] is False
    assert body["held"] is True
    assert "reclaimed_stale" not in body
    # acquire(held) -> runner_state(prev capture) -> reclaim(False) -> NO retry
    assert be.calls == [("acquire", "alpha", "tokB"),
                        ("runner_state", "alpha"),
                        ("reclaim", "alpha")]


def test_acquire_reclaim_then_retry_races_back_to_held(running_daemon, monkeypatch):
    """Race: reclaim succeeds but another machine acquires between our reclaim and
    our retry, so the retry RAISES RunnerHeld again — the endpoint answers
    held=true (NOT a 500, NOT a double-acquire)."""
    _, port = running_daemon
    be = _Backend(acquire_exc=_RunnerHeld("raced"),
                  reclaim_ret=True, acquire_exc_then_ok=False)  # retry still raises
    _inject(monkeypatch, be)
    status, body = _post(port, "/v1/admin/runner-acquire?agent=alpha&token=tokB",
                         agent="alpha")
    assert status == 200
    assert body["ok"] is True
    assert body["acquired"] is False
    assert body["held"] is True
    # acquire(held) -> runner_state -> reclaim(True) -> acquire(held again, raced) -> held
    assert be.calls == [("acquire", "alpha", "tokB"),
                        ("runner_state", "alpha"),
                        ("reclaim", "alpha"),
                        ("acquire", "alpha", "tokB")]


def test_acquire_reclaim_error_returns_500(running_daemon, monkeypatch):
    """A reclaim/retry that raises a NON-RunnerHeld error (DDB fault) surfaces as
    500 so the /start gate fails open — never a silent wrong-ownership answer."""
    _, port = running_daemon
    be = _Backend(acquire_exc=_RunnerHeld("held"),
                  reclaim_exc=RuntimeError("DDB throttled on reclaim"))
    _inject(monkeypatch, be)
    try:
        _post(port, "/v1/admin/runner-acquire?agent=alpha&token=tokB", agent="alpha")
    except urllib.error.HTTPError as e:
        assert e.code == 500
        body = json.loads(e.read().decode("utf-8"))
        assert body["ok"] is False and "reclaim-retry failed" in body["error"]
    else:
        raise AssertionError("expected HTTP 500 on reclaim failure")


# ── heartbeat ─────────────────────────────────────────────────────────────────
def test_heartbeat_local_backend_noop(running_daemon):
    _, port = running_daemon
    status, body = _post(port, "/v1/admin/runner-heartbeat?agent=alpha&token=tokA")
    assert status == 200
    assert body["backend"] == "local"
    assert body["noop"] is True


def test_heartbeat_own_cloud_success(running_daemon, monkeypatch):
    _, port = running_daemon
    be = _Backend()
    _inject(monkeypatch, be)
    status, body = _post(port, "/v1/admin/runner-heartbeat?agent=alpha&token=tokA",
                         agent="alpha")
    assert status == 200
    assert body["ok"] is True
    assert body["beat"] is True
    assert be.calls == [("heartbeat", "alpha", "tokA")]


def test_heartbeat_own_cloud_token_mismatch_500(running_daemon, monkeypatch):
    """A reclaimed runner's heartbeat (token no longer matches) raises in the
    backend; the endpoint surfaces 500 so heartbeat-tick.sh fails open."""
    _, port = running_daemon
    be = _Backend(heartbeat_exc=RuntimeError("ConditionalCheckFailed"))
    _inject(monkeypatch, be)
    try:
        _post(port, "/v1/admin/runner-heartbeat?agent=alpha&token=stale", agent="alpha")
    except urllib.error.HTTPError as e:
        assert e.code == 500
        body = json.loads(e.read().decode("utf-8"))
        assert body["ok"] is False and "heartbeat failed" in body["error"]
    else:
        raise AssertionError("expected HTTP 500 on heartbeat failure")


# ── release ─────────────────────────────────────────────────────────────────
def test_release_local_backend_noop(running_daemon):
    _, port = running_daemon
    status, body = _post(port, "/v1/admin/runner-release?agent=alpha&token=tokA")
    assert status == 200
    assert body["backend"] == "local"
    assert body["noop"] is True


def test_release_own_cloud_transitioned(running_daemon, monkeypatch):
    _, port = running_daemon
    be = _Backend(release_ret=True)
    _inject(monkeypatch, be)
    status, body = _post(port, "/v1/admin/runner-release?agent=alpha&token=tokA",
                         agent="alpha")
    assert status == 200
    assert body["ok"] is True
    assert body["released"] is True
    assert be.calls == [("release", "alpha", "tokA")]


def test_release_own_cloud_idempotent_already_idle(running_daemon, monkeypatch):
    """release_runner returns False (already reclaimed/idle) — a NORMAL 200, not
    an error: /stop must always succeed even if the claim was already gone."""
    _, port = running_daemon
    be = _Backend(release_ret=False)
    _inject(monkeypatch, be)
    status, body = _post(port, "/v1/admin/runner-release?agent=alpha&token=tokA",
                         agent="alpha")
    assert status == 200
    assert body["ok"] is True
    assert body["released"] is False


def test_release_own_cloud_error_500(running_daemon, monkeypatch):
    _, port = running_daemon
    be = _Backend(release_exc=RuntimeError("DDB down"))
    _inject(monkeypatch, be)
    try:
        _post(port, "/v1/admin/runner-release?agent=alpha&token=tokA", agent="alpha")
    except urllib.error.HTTPError as e:
        assert e.code == 500
        body = json.loads(e.read().decode("utf-8"))
        assert body["ok"] is False and "release failed" in body["error"]
    else:
        raise AssertionError("expected HTTP 500 on release failure")
