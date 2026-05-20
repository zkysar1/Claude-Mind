"""GET /v1/admin/health returns ok and an integer uptime in seconds.

Tests the request lifecycle end-to-end against a real HTTP listener (the
conftest fixture spawns a Server-bound ThreadingHTTPServer on a free port).
"""
from __future__ import annotations

import json
import urllib.request


def _get(port: int, path: str, *, agent: str = "") -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url)
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_health_ok(running_daemon):
    _, port = running_daemon
    status, body = _get(port, "/v1/admin/health")
    assert status == 200
    assert body["ok"] is True
    assert isinstance(body["uptime_s"], (int, float))
    assert body["uptime_s"] >= 0.0
    assert isinstance(body["version"], str)
    # git_head_sha is None when not in a git repo (test fixtures), or a 40-char
    # hex string when the daemon is running inside a git checkout. Both shapes
    # are valid — wrappers treat None as "no comparison possible."
    assert "git_head_sha" in body
    sha = body["git_head_sha"]
    assert sha is None or (isinstance(sha, str) and len(sha) == 40)


def test_unknown_route_404(running_daemon):
    _, port = running_daemon
    try:
        _get(port, "/nope")
    except urllib.error.HTTPError as e:
        assert e.code == 404
        body = json.loads(e.read().decode("utf-8"))
        assert body["error"] == "not_found"
    else:
        raise AssertionError("expected 404")


