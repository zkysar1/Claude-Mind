"""RequestContext lazy path resolution ().

On a pre-init deployment (no world configured, no local-paths.conf, no
.mind-data/) AgentPathResolver.resolve() raises RuntimeError. _serve used to
resolve EAGERLY for every request, which 500'd every route — including
/v1/admin/health, whose docstring contract is "Always succeeds when the
daemon is up" — and broke the spawn-confirmation probe in
mind-api-start.sh:66 (`curl -f` fails on 500, so /start read a live daemon
as never having come up; measured on a zc-03 /opt/coach-mind clone,
2026-08-21).

RequestContext now defers resolution to the first ctx.paths ACCESS via
paths_factory. These units pin that contract:
  - construction with a raising factory does not raise (never resolves)
  - the health handler serves without ever touching ctx.paths
  - first access invokes the factory exactly once (cached thereafter)
  - the loud RuntimeError still surfaces at access time (loud-at-use — the
    _paths.py hard-cut design intent, preserved per endpoint)
  - eager `paths=<obj>` construction remains supported (fixture back-compat)
"""
from __future__ import annotations

import json

import pytest

from mind_api.src.server import RequestContext


def _ctx(**kw):
    defaults = dict(
        method="GET", path="/x", query={}, body=b"",
        paths=None, pid=1, port=2, headers={}, tenant="default",
    )
    defaults.update(kw)
    return RequestContext(**defaults)


def test_construction_never_resolves():
    calls = []

    def factory():
        calls.append(1)
        raise RuntimeError("agent_paths: WORLD_PATH unresolved (test)")

    _ctx(paths_factory=factory)
    assert calls == [], "constructing a ctx must not touch the resolver"


def test_paths_access_raises_loud_when_unresolvable():
    def factory():
        raise RuntimeError("agent_paths: WORLD_PATH unresolved (test)")

    ctx = _ctx(paths_factory=factory)
    with pytest.raises(RuntimeError, match="WORLD_PATH unresolved"):
        _ = ctx.paths


def test_paths_access_resolves_once_and_caches():
    calls = []
    sentinel = object()

    def factory():
        calls.append(1)
        return sentinel

    ctx = _ctx(paths_factory=factory)
    assert ctx.paths is sentinel
    assert ctx.paths is sentinel
    assert calls == [1], "factory must run exactly once (cached)"


def test_eager_paths_back_compat():
    sentinel = object()
    ctx = _ctx(paths=sentinel)
    assert ctx.paths is sentinel


def test_health_serves_with_unresolvable_paths():
    """The health handler must never touch ctx.paths — the pre-init
    spawn-confirmation contract this whole change exists for."""
    from mind_api.src.endpoints.health import health

    def factory():
        raise RuntimeError("agent_paths: WORLD_PATH unresolved (test)")

    ctx = _ctx(path="/v1/admin/health", pid=42, port=4242,
               paths_factory=factory)
    resp = health(ctx)
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["ok"] is True
    assert body["pid"] == 42
