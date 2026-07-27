"""X-Mind-Tenant header plumbing (R4, PR-pre-4).

Verifies that ctx.tenant is populated from the request header and defaults
to "default" when the header is absent.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from mind_api.src import lifecycle
from mind_api.src.server import Response, RequestContext, _Handler


def _start_daemon_with_tenant_echo(project_root: Path):
    """Start a minimal daemon that echoes ctx.tenant on GET /v1/test/tenant."""

    def echo_tenant(ctx: RequestContext) -> Response:
        return Response.json({"tenant": ctx.tenant})

    from mind_api.src.server import Server
    server = Server(project_root=project_root, port=0)
    routes = server.routes
    routes[("GET", "/v1/test/tenant")] = echo_tenant

    handler_cls = type(
        "_TenantTestHandler", (_Handler,), {
            "routes": routes,
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


def test_tenant_from_header(project_root):
    """When X-Mind-Tenant is sent, ctx.tenant carries that value."""
    httpd, port = _start_daemon_with_tenant_echo(project_root)
    try:
        status, body = _get(port, "/v1/test/tenant", headers={
            "X-Mind-Agent": "alpha",
            "X-Mind-Tenant": "acme-corp",
        })
        assert status == 200
        assert body["tenant"] == "acme-corp"
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)


def test_tenant_defaults_to_default(project_root):
    """When X-Mind-Tenant is absent, ctx.tenant is 'default'."""
    httpd, port = _start_daemon_with_tenant_echo(project_root)
    try:
        status, body = _get(port, "/v1/test/tenant", headers={
            "X-Mind-Agent": "alpha",
        })
        assert status == 200
        assert body["tenant"] == "default"
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)


# --- T-c: multi-tenant app-authz gate () --------------------------
# The authz/activation is GATED behind MIND_MULTI_TENANT (default OFF). The OFF
# path (acme-corp accepted + propagated) is covered by test_tenant_from_header
# above; these cover the ON path + an explicit OFF-inert regression.

def test_multi_tenant_authz_rejects_mismatch(project_root, monkeypatch):
    """GIVEN multi-tenant ON + the daemon authed for 'pearl', WHEN a request
    carries X-Mind-Tenant: vinheim, THEN it is rejected 403 before any handler
    (brief section 8 app-authz criterion)."""
    import urllib.error
    monkeypatch.setenv("MIND_MULTI_TENANT", "1")
    monkeypatch.setenv("MIND_CUSTOMER", "pearl")
    httpd, port = _start_daemon_with_tenant_echo(project_root)
    try:
        try:
            _get(port, "/v1/test/tenant", headers={
                "X-Mind-Agent": "alpha",
                "X-Mind-Tenant": "vinheim",
            })
            assert False, "expected HTTP 403 for tenant mismatch"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)


def test_multi_tenant_authz_accepts_match(project_root, monkeypatch):
    """GIVEN multi-tenant ON + the daemon authed for 'pearl', WHEN the request
    carries the matching X-Mind-Tenant: pearl, THEN it is accepted (200) and
    ctx.tenant carries 'pearl'."""
    monkeypatch.setenv("MIND_MULTI_TENANT", "1")
    monkeypatch.setenv("MIND_CUSTOMER", "pearl")
    httpd, port = _start_daemon_with_tenant_echo(project_root)
    try:
        status, body = _get(port, "/v1/test/tenant", headers={
            "X-Mind-Agent": "alpha",
            "X-Mind-Tenant": "pearl",
        })
        assert status == 200
        assert body["tenant"] == "pearl"
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)


def test_multi_tenant_disabled_gate_is_inert(project_root, monkeypatch):
    """REGRESSION: with multi-tenant OFF (the default), a non-default tenant
    header is accepted + propagated (the R4 seam) even when MIND_CUSTOMER is set
    — the authz gate is inert unless MIND_MULTI_TENANT is explicitly enabled, so
    the single-tenant deployment and the seam test keep working."""
    monkeypatch.delenv("MIND_MULTI_TENANT", raising=False)
    monkeypatch.setenv("MIND_CUSTOMER", "pearl")  # set but inert while MT off
    httpd, port = _start_daemon_with_tenant_echo(project_root)
    try:
        status, body = _get(port, "/v1/test/tenant", headers={
            "X-Mind-Agent": "alpha",
            "X-Mind-Tenant": "vinheim",
        })
        assert status == 200
        assert body["tenant"] == "vinheim"
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)
