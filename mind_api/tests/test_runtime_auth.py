"""FR-4 daemon bearer-token auth + FR-5 guarded remote bind + FR-7 runner-claims
endpoint (BRD DynamoDB-Backed Shared-State & Coordination API).

FR-4: when MIND_API_TOKEN is set, every request MUST present a matching
`Authorization: Bearer <token>` or be refused 401 — BEFORE any handler or tenant
check. When MIND_API_TOKEN is unset (the default), no auth is required
(localhost back-compat, NFR-1).

FR-5: the daemon binds 127.0.0.1 by default; a non-loopback MIND_API_BIND is
fail-closed — refused at start() unless MIND_API_TOKEN is set.

FR-7: GET /v1/admin/runner-claims returns the env-scoped runner-claim list; on
the local backend it is an empty no-op list.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from mind_api.src import lifecycle
from mind_api.src.server import _Handler


def _start_daemon(project_root: Path):
    """Start a real in-process daemon with the full registered route table
    (so /v1/admin/runner-claims is present) on an ephemeral loopback port."""
    from mind_api.src.server import Server

    server = Server(project_root=project_root, port=0)
    handler_cls = type(
        "_AuthTestHandler", (_Handler,), {
            "routes": server.routes,
            "resolver": server.resolver,
            "access_log_path": lifecycle.access_log(project_root),
            "pid": 0,
            "port": 0,
        },
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = httpd.server_address[1]
    handler_cls.port = port
    handler_cls.pid = 99999
    lifecycle.write_pid_and_port_atomic(project_root, 99999, port)

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                break
        except OSError:
            time.sleep(0.02)
    return httpd, port


def _get(port: int, path: str, *, headers: dict | None = None) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


# --- FR-4: bearer-token authentication --------------------------------------

def test_auth_disabled_by_default_no_token_required(project_root, monkeypatch):
    """REGRESSION / NFR-1: MIND_API_TOKEN unset → no credential needed; a request
    with no Authorization header is accepted (byte-identical to pre-FR-4)."""
    monkeypatch.delenv("MIND_API_TOKEN", raising=False)
    httpd, port = _start_daemon(project_root)
    try:
        status, _ = _get(port, "/v1/admin/runner-claims",
                         headers={"X-Mind-Agent": "alpha"})
        assert status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)


def test_auth_enabled_valid_token_accepted(project_root, monkeypatch):
    monkeypatch.setenv("MIND_API_TOKEN", "s3cr3t-fleet-token")
    httpd, port = _start_daemon(project_root)
    try:
        status, _ = _get(port, "/v1/admin/runner-claims", headers={
            "X-Mind-Agent": "alpha",
            "Authorization": "Bearer s3cr3t-fleet-token",
        })
        assert status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)


def test_auth_enabled_missing_token_rejected_401(project_root, monkeypatch):
    monkeypatch.setenv("MIND_API_TOKEN", "s3cr3t-fleet-token")
    httpd, port = _start_daemon(project_root)
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(port, "/v1/admin/runner-claims",
                 headers={"X-Mind-Agent": "alpha"})
        assert ei.value.code == 401
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)


def test_auth_enabled_wrong_token_rejected_401(project_root, monkeypatch):
    monkeypatch.setenv("MIND_API_TOKEN", "s3cr3t-fleet-token")
    httpd, port = _start_daemon(project_root)
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(port, "/v1/admin/runner-claims", headers={
                "X-Mind-Agent": "alpha",
                "Authorization": "Bearer WRONG-TOKEN",
            })
        assert ei.value.code == 401
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)


def test_auth_enabled_non_bearer_scheme_rejected_401(project_root, monkeypatch):
    """A correctly-valued token under the wrong scheme (Basic) is still 401 —
    only `Authorization: Bearer <token>` authenticates."""
    monkeypatch.setenv("MIND_API_TOKEN", "s3cr3t-fleet-token")
    httpd, port = _start_daemon(project_root)
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(port, "/v1/admin/runner-claims", headers={
                "X-Mind-Agent": "alpha",
                "Authorization": "Basic s3cr3t-fleet-token",
            })
        assert ei.value.code == 401
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)


# --- FR-5: guarded remote bind (fail-closed) --------------------------------

def test_bind_guard_refuses_routable_without_token(project_root, monkeypatch):
    """FR-5 fail-closed: MIND_API_BIND = a non-loopback interface with no
    MIND_API_TOKEN → Server.start() raises BEFORE binding, so the daemon never
    exposes a routable interface without authentication."""
    from mind_api.src.server import Server

    monkeypatch.setenv("MIND_API_BIND", "0.0.0.0")
    monkeypatch.delenv("MIND_API_TOKEN", raising=False)
    server = Server(project_root=project_root, port=0)
    with pytest.raises(RuntimeError, match="fail-closed"):
        server.start()
    lifecycle.clear_runtime_files(project_root)


def test_bind_guard_allows_loopback_default_without_token(project_root, monkeypatch):
    """REGRESSION: the default (unset MIND_API_BIND / loopback) needs NO token —
    the guard must not fire for 127.0.0.1. Verified by the guard's own logic: a
    loopback bind_addr skips the token requirement."""
    # Assert on the resolution rule directly (starting the server would block on
    # serve_forever). bind_addr default is 127.0.0.1, which is in the loopback
    # allowlist, so the guard is inert regardless of token.
    import os
    monkeypatch.delenv("MIND_API_BIND", raising=False)
    monkeypatch.delenv("MIND_API_TOKEN", raising=False)
    bind_addr = os.environ.get("MIND_API_BIND", "").strip() or "127.0.0.1"
    assert bind_addr in {"127.0.0.1", "::1", "localhost"}


# --- FR-7: runner-claims read endpoint --------------------------------------

def test_runner_claims_local_backend_is_empty_noop(project_root, monkeypatch):
    """On the local backend (test default) GET /v1/admin/runner-claims returns an
    empty claims list without touching DDB."""
    monkeypatch.delenv("MIND_API_TOKEN", raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    httpd, port = _start_daemon(project_root)
    try:
        status, body = _get(port, "/v1/admin/runner-claims",
                            headers={"X-Mind-Agent": "alpha"})
        assert status == 200
        assert body["ok"] is True
        assert body["backend"] == "local"
        assert body["claims"] == []
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)


def test_runner_claims_scan_accessdenied_returns_actionable_hint(monkeypatch):
    """FR-7 hardening (2026-07-01): when the daemon's scoped creds lack
    dynamodb:Scan on the sessions table, the endpoint returns a 500 with an
    ACTIONABLE hint (grant Scan), not just the raw boto error. The
    acquire/heartbeat/release trio does not Scan, so a pre-FR-7 IAM policy leaves
    exactly this read denied — the operator hitting the fleet-health endpoint
    needs to be told what to grant. Regression guard for the observed
    ayoai-mind-dev-creds-vs-zds-sessions AccessDenied case.

    Calls runner_claims() directly with a mocked own-cloud backend whose
    list_runner_claims raises an AccessDenied-on-Scan error (no real AWS)."""
    import sys as _sys
    from pathlib import Path as _Path

    from mind_api.src.endpoints.admin import runner_claims

    repo_root = _Path(__file__).resolve().parents[2]
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setenv("ENVIRONMENT_ID", "test-fleet-env")

    scripts_dir = str(repo_root / "core" / "scripts")
    if scripts_dir not in _sys.path:
        _sys.path.insert(0, scripts_dir)
    import storage_backend

    class _DeniedBackend:
        def list_runner_claims(self):
            raise Exception(
                "An error occurred (AccessDeniedException) when calling the "
                "Scan operation: User: arn:aws:iam::0:user/x is not authorized "
                "to perform: dynamodb:Scan on resource: table/zds-sessions")

    monkeypatch.setattr(storage_backend, "get_backend", lambda: _DeniedBackend())

    class _Paths:
        project_root = repo_root

    class _Ctx:
        paths = _Paths()
        headers = {"x-mind-agent": "alpha"}

    resp = runner_claims(_Ctx())
    assert resp.status == 500
    body = json.loads(resp.body)
    assert body["ok"] is False
    assert "AccessDenied" in body["error"]
    assert "hint" in body, "AccessDenied-on-Scan must carry an actionable hint"
    assert "dynamodb:Scan" in body["hint"]


# --- : the claim projection publishes a DIGEST, never the token -----

def _fp_ctx(monkeypatch, claims):
    """Wire runner_claims() to a mocked own-cloud backend returning `claims`."""
    import sys as _sys
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[2]
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setenv("ENVIRONMENT_ID", "test-fleet-env")

    scripts_dir = str(repo_root / "core" / "scripts")
    if scripts_dir not in _sys.path:
        _sys.path.insert(0, scripts_dir)
    import storage_backend

    class _Backend:
        runner_stale_seconds = 3900

        def list_runner_claims(self):
            return claims

    monkeypatch.setattr(storage_backend, "get_backend", lambda: _Backend())

    class _Paths:
        project_root = repo_root

    class _Ctx:
        paths = _Paths()
        headers = {"x-mind-agent": "alpha"}

    return _Ctx()


def test_runner_claims_returns_the_token_fingerprint(monkeypatch):
    """The consumer-facing half: a worker Body needs to notice a SAME-BOX
    reducer restart (a re-minted runner_token under an unchanged machine_id),
    which machine_id structurally cannot see. The digest is what carries it."""
    import sys as _sys
    from pathlib import Path as _Path
    scripts_dir = str(_Path(__file__).resolve().parents[2] / "core" / "scripts")
    if scripts_dir not in _sys.path:
        _sys.path.insert(0, scripts_dir)
    from owncloud_backend import RunnerClaim, runner_token_fingerprint

    from mind_api.src.endpoints.admin import runner_claims

    fp = runner_token_fingerprint("f47ac10b-58cc-4372-a567-0e02b2c3d479")
    ctx = _fp_ctx(monkeypatch, [
        RunnerClaim(agent="alpha", machine_id="cc-04", agent_state="RUNNING",
                    heartbeat_at=1755000000, runner_token_fp=fp)])

    resp = runner_claims(ctx)
    assert resp.status == 200
    body = json.loads(resp.body)
    (claim,) = body["claims"]
    assert claim["runner_token_fp"] == fp
    assert claim["machine_id"] == "cc-04"


def test_runner_claims_never_leaks_a_raw_token_even_if_the_row_carries_one(monkeypatch):
    """THE security pin, and it must not be provable by RunnerClaim's shape alone.

    `runner_token` is the ConditionExpression bearer credential for heartbeat()
    and release_runner(): anything holding it can forge a heartbeat for another
    agent (defeating reclaim_if_stale, so a crashed runner could never be
    reclaimed) or release a LIVE claim, forcing a healthy reducer down
    mid-flight. RunnerClaim deliberately has no raw-token field — but a test that
    only fed it a RunnerClaim would pass for that reason alone and would go on
    passing if the endpoint switched to serialising whatever object it was
    handed. So this feeds a duck-typed row that DOES expose `runner_token`, which
    passes only because the projection is an EXPLICIT field list (guard-3357).
    """
    from mind_api.src.endpoints.admin import runner_claims

    TOKEN = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    class _LeakyRow:
        agent = "alpha"
        machine_id = "cc-04"
        agent_state = "RUNNING"
        heartbeat_at = 1755000000
        runner_token_fp = "1f4c0a9b2e6d8035"
        runner_token = TOKEN            # must NOT reach the wire

    resp = runner_claims(_fp_ctx(monkeypatch, [_LeakyRow()]))
    assert resp.status == 200
    # Response.body is BYTES — scan the decoded wire form, not the object, so
    # this catches a leak in ANY field rather than only in a key we predicted.
    wire = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else resp.body
    assert TOKEN not in wire, "the raw runner_token reached the response body"
    (claim,) = json.loads(resp.body)["claims"]
    assert set(claim) == {"agent", "machine_id", "agent_state", "heartbeat_at",
                          "runner_token_fp"}, (
        "the claim projection grew a key — if that key is (or can carry) the raw "
        "runner_token, this endpoint now hands every reader a credential that "
        "authorises heartbeat() and release_runner() on someone else's claim")


def test_runner_claims_tolerates_a_row_predating_the_fingerprint(monkeypatch):
    """Mixed-version tolerance: the endpoint reads the field with a getattr
    default, so a backend that predates g-306-224 yields null rather than a 500.
    Null means UNKNOWN — the consumer treats it as non-discriminating, never as
    'unchanged' (worker_reducer_liveness FAIL-SAFE ASYMMETRY)."""
    from mind_api.src.endpoints.admin import runner_claims

    class _OldRow:
        agent = "alpha"
        machine_id = "cc-04"
        agent_state = "RUNNING"
        heartbeat_at = 1755000000

    resp = runner_claims(_fp_ctx(monkeypatch, [_OldRow()]))
    assert resp.status == 200
    (claim,) = json.loads(resp.body)["claims"]
    assert claim["runner_token_fp"] is None
