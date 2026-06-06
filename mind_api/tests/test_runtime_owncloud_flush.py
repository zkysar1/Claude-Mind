"""POST /v1/admin/owncloud-flush — the /stop graceful-handler continuity flush.

Phase 3 of the session-continuity redesign (2026-06-02). The endpoint forces an
immediate own-cloud mirror sweep so a machine-move right after a clean /stop
cannot strand the session's last continuity writes locally. Tested end-to-end
against the in-process daemon (conftest pins STORAGE_BACKEND=local for the
session; the own-cloud-path tests flip it via monkeypatch and inject fake
owncloud_sync / storage_backend modules so NO real S3 call is ever made).

Key invariant under test: the endpoint invokes owncloud_sync.sweep() with the
EXACT same arguments the periodic sweep thread uses
(__main__._start_owncloud_sync_thread) — only_root=None, dry_run=False,
use_manifest=True, full=False — so there is one sweep code path, no drift.
"""
from __future__ import annotations

import json
import sys
import types
import urllib.error
import urllib.request


def _post(port: int, path: str, *, agent: str = "", data: bytes = b"") -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=data, method="POST")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_owncloud_flush_local_backend_noop(running_daemon):
    """Under the local backend (the default) the flush is a clean no-op: the
    local files ARE the store, so there is nothing to mirror."""
    _, port = running_daemon
    status, body = _post(port, "/v1/admin/owncloud-flush")
    assert status == 200
    assert body["backend"] == "local"
    assert body["flushed"] is False
    assert "reason" in body


def test_owncloud_flush_own_cloud_invokes_sweep_with_periodic_args(running_daemon, monkeypatch):
    """Under own-cloud the flush calls owncloud_sync.sweep() with the SAME args
    as the periodic thread (SSOT) and reports its stats."""
    _, port = running_daemon

    calls = []

    fake_sync = types.ModuleType("owncloud_sync")

    def fake_sweep(be, *, only_root, dry_run, use_manifest, full):
        calls.append({"only_root": only_root, "dry_run": dry_run,
                      "use_manifest": use_manifest, "full": full})
        return {"pushed": 3, "scanned": 10, "in_sync": 7,
                "skipped_unchanged": 2, "conflicts": 0, "errors": 0}

    fake_sync.sweep = fake_sweep  # type: ignore[attr-defined]

    fake_backend_mod = types.ModuleType("storage_backend")
    sentinel_backend = object()
    fake_backend_mod.get_backend = lambda: sentinel_backend  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "owncloud_sync", fake_sync)
    monkeypatch.setitem(sys.modules, "storage_backend", fake_backend_mod)
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")

    status, body = _post(port, "/v1/admin/owncloud-flush", agent="alpha")
    assert status == 200
    assert body["backend"] == "own-cloud"
    assert body["flushed"] is True
    assert body["pushed"] == 3
    assert body["in_sync"] == 7
    assert body["scanned"] == 10
    assert body["errors"] == 0
    # SSOT: identical to mind_api/src/__main__._start_owncloud_sync_thread.
    assert calls == [{"only_root": None, "dry_run": False,
                      "use_manifest": True, "full": False}]
    assert calls[0]["only_root"] is None  # full coverage: world + meta + agents


def test_owncloud_flush_sweep_error_returns_500(running_daemon, monkeypatch):
    """A sweep that raises is reported as a 500 with the reason, NOT an
    unhandled stack — so /stop D6.7 can warn-and-proceed (the periodic sweep
    still running in this daemon retries)."""
    _, port = running_daemon

    fake_sync = types.ModuleType("owncloud_sync")

    def boom(be, **kwargs):
        raise RuntimeError("S3 transient")

    fake_sync.sweep = boom  # type: ignore[attr-defined]
    fake_backend_mod = types.ModuleType("storage_backend")
    fake_backend_mod.get_backend = lambda: object()  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "owncloud_sync", fake_sync)
    monkeypatch.setitem(sys.modules, "storage_backend", fake_backend_mod)
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")

    try:
        _post(port, "/v1/admin/owncloud-flush", agent="alpha")
    except urllib.error.HTTPError as e:
        assert e.code == 500
        body = json.loads(e.read().decode("utf-8"))
        assert body["flushed"] is False
        assert "sweep failed" in body["error"]
    else:
        raise AssertionError("expected HTTP 500 on sweep failure")


# --- POST /v1/admin/owncloud-pull (the /start continuity pull) --------------
def test_owncloud_pull_local_backend_noop(running_daemon):
    _, port = running_daemon
    status, body = _post(port, "/v1/admin/owncloud-pull?agent=alpha")
    assert status == 200
    assert body["backend"] == "local"
    assert body["ok"] is False
    assert "reason" in body


def test_owncloud_pull_missing_agent_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/admin/owncloud-pull")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        body = json.loads(e.read().decode("utf-8"))
        assert body["ok"] is False and "agent" in body["error"]
    else:
        raise AssertionError("expected HTTP 400 when agent query missing")


def test_owncloud_pull_own_cloud_invokes_pull_continuity(running_daemon, monkeypatch):
    _, port = running_daemon
    calls = []

    fake_sync = types.ModuleType("owncloud_sync")

    def fake_pull(be, agent, *, dry_run=False):
        calls.append((agent, dry_run))
        return {"agent": agent, "scanned": 16, "pulled": 2, "in_sync": 11,
                "would_pull": 0, "s3_absent": 3, "local_ahead_skipped": 0,
                "multipart_deferred": 0, "errors": 0,
                "pulled_files": ["handoff.yaml", "working-memory.yaml"]}

    fake_sync.pull_continuity = fake_pull  # type: ignore[attr-defined]
    fake_backend_mod = types.ModuleType("storage_backend")
    fake_backend_mod.get_backend = lambda: object()  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "owncloud_sync", fake_sync)
    monkeypatch.setitem(sys.modules, "storage_backend", fake_backend_mod)
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")

    status, body = _post(port, "/v1/admin/owncloud-pull?agent=alpha", agent="alpha")
    assert status == 200
    assert body["ok"] is True
    assert body["backend"] == "own-cloud"
    assert body["agent"] == "alpha"
    assert body["pulled"] == 2
    assert body["pulled_files"] == ["handoff.yaml", "working-memory.yaml"]
    assert calls == [("alpha", False)]


def test_owncloud_pull_error_returns_500(running_daemon, monkeypatch):
    _, port = running_daemon

    fake_sync = types.ModuleType("owncloud_sync")

    def boom(be, agent, *, dry_run=False):
        raise RuntimeError("S3 down")

    fake_sync.pull_continuity = boom  # type: ignore[attr-defined]
    fake_backend_mod = types.ModuleType("storage_backend")
    fake_backend_mod.get_backend = lambda: object()  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "owncloud_sync", fake_sync)
    monkeypatch.setitem(sys.modules, "storage_backend", fake_backend_mod)
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")

    try:
        _post(port, "/v1/admin/owncloud-pull?agent=alpha", agent="alpha")
    except urllib.error.HTTPError as e:
        assert e.code == 500
        body = json.loads(e.read().decode("utf-8"))
        assert body["ok"] is False and "pull failed" in body["error"]
    else:
        raise AssertionError("expected HTTP 500 on pull failure")


def test_owncloud_pull_fail_closed_manifest_returns_200_ok_false(running_daemon, monkeypatch):
    """pull_continuity fail-closes (untrustworthy manifest / no agents root) by
    RETURNING stats with an 'error' key rather than raising. The endpoint must
    surface that as HTTP 200 + ok=False (not a 500) so the wrapper reports a
    no-pull rather than a server crash."""
    _, port = running_daemon

    fake_sync = types.ModuleType("owncloud_sync")

    def fake_pull(be, agent, *, dry_run=False):
        return {"agent": agent, "scanned": 0, "pulled": 0,
                "error": "session-manifest untrustworthy — pulled nothing (fail-closed)"}

    fake_sync.pull_continuity = fake_pull  # type: ignore[attr-defined]
    fake_backend_mod = types.ModuleType("storage_backend")
    fake_backend_mod.get_backend = lambda: object()  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "owncloud_sync", fake_sync)
    monkeypatch.setitem(sys.modules, "storage_backend", fake_backend_mod)
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")

    status, body = _post(port, "/v1/admin/owncloud-pull?agent=alpha", agent="alpha")
    assert status == 200          # fail-closed is NOT a server error
    assert body["ok"] is False
    assert "error" in body and "untrustworthy" in body["error"]
    assert body["pulled"] == 0
