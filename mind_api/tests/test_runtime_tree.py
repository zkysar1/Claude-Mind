"""GET /v1/tree/find-node — daemon endpoint tests.

Tests the daemon tree read endpoint directly (daemon-only, no CLI fallback).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


def _get(port: int, path: str, query: dict, *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}"
    req = urllib.request.Request(url)
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_tree_find_returns_matching_node(running_daemon):
    _, port = running_daemon
    status, body = _get(
        port, "/v1/tree/find-node",
        {"text": "alpha"},
        agent="alpha",
    )
    assert status == 200
    data = json.loads(body)
    assert isinstance(data, list)
    keys = [n["key"] for n in data]
    assert "alpha-test-node" in keys


def test_tree_find_leaf_only_excludes_interior(running_daemon):
    _, port = running_daemon
    # beta-other-node has children=[alpha-test-node] so it's NOT a leaf.
    # alpha-test-node has children=[] so it IS a leaf.
    status, body = _get(
        port, "/v1/tree/find-node",
        {"text": "node", "leaf_only": "1", "top": "5"},
        agent="alpha",
    )
    assert status == 200
    data = json.loads(body)
    keys = {n["key"] for n in data}
    assert "alpha-test-node" in keys
    assert "beta-other-node" not in keys


def test_tree_find_missing_text_400(running_daemon):
    _, port = running_daemon
    try:
        _get(port, "/v1/tree/find-node", {}, agent="alpha")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "missing_param"
    else:
        raise AssertionError("expected 400 when text is missing")


def test_tree_find_invalid_top_400(running_daemon):
    _, port = running_daemon
    try:
        _get(port, "/v1/tree/find-node",
             {"text": "alpha", "top": "notanumber"},
             agent="alpha")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "invalid_param"
    else:
        raise AssertionError("expected 400 when top is non-integer")
