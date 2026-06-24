"""GET /v1/admin/ready — readiness probe with a store round-trip ().

Two layers:
  - End-to-end over the real HTTP listener for the LOCAL branch (the conftest
    pins STORAGE_BACKEND=local for the whole session, so the running_daemon
    fixture exercises the skip-local path).
  - Direct handler / helper unit tests for the OWN-CLOUD branches (ok, store
    error, probe timeout, backend-init failure) using fakes + monkeypatch, since
    the own-cloud path must never touch real S3.
"""
from __future__ import annotations

import json
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# storage_backend lives in core/scripts; make it importable + patchable so the
# handler's `from storage_backend import get_backend` resolves the patched attr.
sys.path.insert(0, str(REPO_ROOT / "core" / "scripts"))

from mind_api.src.endpoints import ready as ready_module  # noqa: E402


def _get(port: int, path: str) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


# --- ctx + backend fakes ----------------------------------------------------

def _ctx():
    """Minimal RequestContext stand-in: the handler only reads
    ctx.paths.project_root (own-cloud branch)."""
    paths = types.SimpleNamespace(project_root=REPO_ROOT)
    return types.SimpleNamespace(paths=paths)


class _FakeS3:
    def __init__(self, *, raises: Exception | None = None, sleep_s: float = 0.0):
        self._raises = raises
        self._sleep_s = sleep_s

    def head_bucket(self, Bucket):  # noqa: N803 —  kwarg name
        if self._sleep_s:
            import time
            time.sleep(self._sleep_s)
        if self._raises is not None:
            raise self._raises
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


class _FakeDdb:
    def __init__(self, *, raises: Exception | None = None, sleep_s: float = 0.0):
        self._raises = raises
        self._sleep_s = sleep_s

    def get_item(self, TableName, Key):  # noqa: N803 —  kwarg names
        if self._sleep_s:
            import time
            time.sleep(self._sleep_s)
        if self._raises is not None:
            raise self._raises
        return {}  # empty Item — the sentinel probe key is never written


class _FakeBackend:
    name = "own-cloud"

    def __init__(self, *, raises=None, sleep_s=0.0, ddb_raises=None, ddb_sleep_s=0.0):
        # `raises`/`sleep_s` drive the S3 leg (back-compat with existing tests);
        # `ddb_raises`/`ddb_sleep_s` drive the DDB leg ().
        self.bucket = "test-bucket"
        self.sessions_table = "test-sessions"
        self.s3 = _FakeS3(raises=raises, sleep_s=sleep_s)
        self.ddb = _FakeDdb(raises=ddb_raises, sleep_s=ddb_sleep_s)


# --- end-to-end (local branch) ---------------------------------------------

def test_ready_local_skips_store(running_daemon):
    # conftest pins STORAGE_BACKEND=local for the session.
    _, port = running_daemon
    status, body = _get(port, "/v1/admin/ready")
    assert status == 200
    assert body["ready"] is True
    assert body["store_check"] == "skipped-local"
    assert body["backend"] in ("local", "")


# --- _probe_store helper ----------------------------------------------------

def test_probe_store_ok():
    status, detail = ready_module._probe_store(_FakeBackend())
    assert status == "ok"
    assert detail == ""


def test_probe_store_s3_error_surfaces_exception():
    status, detail = ready_module._probe_store(
        _FakeBackend(raises=RuntimeError("403 Forbidden"))
    )
    assert status == "error"
    assert detail.startswith("s3:")  # leg is named ( dual-leg probe)
    assert "RuntimeError" in detail
    assert "403" in detail


def test_probe_store_ddb_error_surfaces_exception():
    # S3 leg ok, DDB leg raises — the probe must fail and name the ddb leg.
    status, detail = ready_module._probe_store(
        _FakeBackend(ddb_raises=RuntimeError("ExpiredTokenException"))
    )
    assert status == "error"
    assert detail.startswith("ddb:")
    assert "ExpiredTokenException" in detail


def test_probe_store_timeout(monkeypatch):
    # Tighten the bound so a 0.3s probe trips the timeout deterministically.
    monkeypatch.setattr(ready_module, "_PROBE_TIMEOUT_S", 0.05)
    status, detail = ready_module._probe_store(_FakeBackend(sleep_s=0.3))
    assert status == "timeout"
    assert "exceeded" in detail


def test_probe_store_ddb_timeout(monkeypatch):
    # A hang in the DDB leg (S3 ok) must also trip the single shared join-timeout.
    monkeypatch.setattr(ready_module, "_PROBE_TIMEOUT_S", 0.05)
    status, detail = ready_module._probe_store(_FakeBackend(ddb_sleep_s=0.3))
    assert status == "timeout"
    assert "exceeded" in detail


# --- ready() handler, own-cloud branch -------------------------------------

def _patch_get_backend(monkeypatch, backend=None, raises=None):
    import storage_backend
    if raises is not None:
        def _gb():
            raise raises
    else:
        def _gb():
            return backend
    monkeypatch.setattr(storage_backend, "get_backend", _gb)


def test_ready_owncloud_ok(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    _patch_get_backend(monkeypatch, backend=_FakeBackend())
    resp = ready_module.ready(_ctx())
    body = json.loads(resp.body.decode("utf-8"))
    assert resp.status == 200
    assert body["ready"] is True
    assert body["store_check"] == "ok"
    assert "probe_ms" in body


def test_ready_owncloud_store_error_is_503(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    _patch_get_backend(
        monkeypatch, backend=_FakeBackend(raises=RuntimeError("403 Forbidden"))
    )
    resp = ready_module.ready(_ctx())
    body = json.loads(resp.body.decode("utf-8"))
    assert resp.status == 503
    assert body["ready"] is False
    assert body["store_check"] == "error"
    assert "403" in body["error"]


def test_ready_owncloud_ddb_error_is_503(monkeypatch):
    # S3 reachable but the DDB leg fails (e.g. divergent IAM / stale token on the
    # sessions table) — /ready must still 503 ( dual-leg coverage).
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    _patch_get_backend(
        monkeypatch,
        backend=_FakeBackend(ddb_raises=RuntimeError("ResourceNotFoundException")),
    )
    resp = ready_module.ready(_ctx())
    body = json.loads(resp.body.decode("utf-8"))
    assert resp.status == 503
    assert body["ready"] is False
    assert body["store_check"] == "error"
    assert "ResourceNotFoundException" in body["error"]
    assert "ddb" in body["error"]


def test_ready_owncloud_backend_init_failure_is_503(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    _patch_get_backend(
        monkeypatch, raises=RuntimeError("MIND_AWS_* are not set")
    )
    resp = ready_module.ready(_ctx())
    body = json.loads(resp.body.decode("utf-8"))
    assert resp.status == 503
    assert body["ready"] is False
    assert body["store_check"] == "backend-init-failed"
    assert "MIND_AWS" in body["error"]


def test_ready_owncloud_timeout_is_503(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setattr(ready_module, "_PROBE_TIMEOUT_S", 0.05)
    _patch_get_backend(monkeypatch, backend=_FakeBackend(sleep_s=0.3))
    resp = ready_module.ready(_ctx())
    body = json.loads(resp.body.decode("utf-8"))
    assert resp.status == 503
    assert body["ready"] is False
    assert body["store_check"] == "timeout"
