"""GET /v1/wm/read — daemon endpoint tests.

Tests the daemon working-memory read endpoint (daemon-only, no CLI fallback).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import yaml


def _get(port: int, path: str, query: dict, *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url)
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_wm_read_slot_json(running_daemon):
    _, port = running_daemon
    status, body = _get(
        port, "/v1/wm/read",
        {"slot": "active_strategy", "json": "1"},
        agent="alpha",
    )
    assert status == 200
    assert json.loads(body) == "depth-first"


def test_wm_read_nested_slot_json(running_daemon):
    _, port = running_daemon
    status, body = _get(
        port, "/v1/wm/read",
        {"slot": "active_context.summary", "json": "1"},
        agent="alpha",
    )
    assert status == 200
    assert json.loads(body) == "currently testing the runtime"


def test_wm_read_top_level_key_json(running_daemon):
    _, port = running_daemon
    status, body = _get(
        port, "/v1/wm/read",
        {"slot": "encoding_queue", "json": "1"},
        agent="alpha",
    )
    assert status == 200
    assert json.loads(body) == ["item-1", "item-2"]


def test_wm_read_missing_slot_returns_null(running_daemon):
    _, port = running_daemon
    status, body = _get(
        port, "/v1/wm/read",
        {"slot": "no_such_slot", "json": "1"},
        agent="alpha",
    )
    assert status == 200
    assert json.loads(body) is None


def test_wm_read_full_dump_json(running_daemon):
    _, port = running_daemon
    status, body = _get(
        port, "/v1/wm/read",
        {"json": "1"},
        agent="alpha",
    )
    assert status == 200
    data = json.loads(body)
    assert data["session_id"] == "test-sid-001"
    assert "active_context" in data["slots"]


def test_wm_read_yaml_default(running_daemon):
    """No json=1 flag → YAML output, parseable by yaml.safe_load."""
    _, port = running_daemon
    status, body = _get(
        port, "/v1/wm/read",
        {"slot": "active_context"},
        agent="alpha",
    )
    assert status == 200
    parsed = yaml.safe_load(body)
    assert parsed["summary"] == "currently testing the runtime"


