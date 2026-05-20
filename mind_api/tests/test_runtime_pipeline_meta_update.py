"""Tests for POST /v1/pipeline/meta-update (PR 57).

Covers: basic field set, value type coercion, dotted-field rejection,
missing-field param, auto last_updated stamp, file-creation from scratch,
JSON (not JSONL) round-trip, overwrite semantics.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(port: int, path: str, query: dict = None, body: bytes = b"",
          *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query) if query else ""
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post_expect_error(port: int, path: str, query: dict = None,
                       body: bytes = b"", *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query) if query else ""
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _read_meta(world: Path) -> dict:
    meta_path = world / "pipeline-meta.json"
    return json.loads(meta_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPipelineMetaUpdate:
    """POST /v1/pipeline/meta-update endpoint tests."""

    def test_basic_string_field(self, running_daemon):
        root, port = running_daemon
        status, body = _post(port, "/v1/pipeline/meta-update",
                             query={"field": "custom_note", "value": "hello"})
        assert status == 200
        resp = json.loads(body)
        assert resp["ok"] is True
        assert resp["data"]["custom_note"] == "hello"
        assert resp["data"]["last_updated"] is not None

        # Verify on-disk JSON.
        on_disk = _read_meta(root / "world")
        assert on_disk["custom_note"] == "hello"

    def test_integer_coercion(self, running_daemon):
        root, port = running_daemon
        status, body = _post(port, "/v1/pipeline/meta-update",
                             query={"field": "count", "value": "42"})
        assert status == 200
        resp = json.loads(body)
        assert resp["data"]["count"] == 42

    def test_boolean_coercion(self, running_daemon):
        root, port = running_daemon
        status, body = _post(port, "/v1/pipeline/meta-update",
                             query={"field": "enabled", "value": "true"})
        assert status == 200
        resp = json.loads(body)
        assert resp["data"]["enabled"] is True

    def test_json_object_coercion(self, running_daemon):
        root, port = running_daemon
        val = '{"a":1,"b":2}'
        status, body = _post(port, "/v1/pipeline/meta-update",
                             query={"field": "nested", "value": val})
        assert status == 200
        resp = json.loads(body)
        assert resp["data"]["nested"] == {"a": 1, "b": 2}

    def test_null_coercion(self, running_daemon):
        root, port = running_daemon
        status, body = _post(port, "/v1/pipeline/meta-update",
                             query={"field": "cleared", "value": "null"})
        assert status == 200
        resp = json.loads(body)
        assert resp["data"]["cleared"] is None

    def test_dotted_field_rejected(self, running_daemon):
        root, port = running_daemon
        status, body = _post_expect_error(port, "/v1/pipeline/meta-update",
                                          query={"field": "a.b", "value": "x"})
        assert status == 400
        resp = json.loads(body)
        assert resp["error"] == "dotted_field_rejected"

    def test_missing_field_param(self, running_daemon):
        root, port = running_daemon
        status, body = _post_expect_error(port, "/v1/pipeline/meta-update",
                                          query={"value": "hello"})
        assert status == 400
        resp = json.loads(body)
        assert resp["error"] == "missing_param"

    def test_creates_file_when_absent(self, running_daemon):
        root, port = running_daemon
        meta_path = root / "world" / "pipeline-meta.json"
        if meta_path.exists():
            meta_path.unlink()

        status, body = _post(port, "/v1/pipeline/meta-update",
                             query={"field": "fresh_field", "value": "created"})
        assert status == 200
        resp = json.loads(body)
        data = resp["data"]
        assert data["fresh_field"] == "created"
        assert "stage_counts" in data
        assert "accuracy" in data

    def test_overwrites_existing_field(self, running_daemon):
        root, port = running_daemon
        # Set once.
        _post(port, "/v1/pipeline/meta-update",
              query={"field": "version", "value": "1"})
        # Overwrite.
        status, body = _post(port, "/v1/pipeline/meta-update",
                             query={"field": "version", "value": "2"})
        assert status == 200
        resp = json.loads(body)
        assert resp["data"]["version"] == 2

    def test_last_updated_stamped(self, running_daemon):
        root, port = running_daemon
        status, body = _post(port, "/v1/pipeline/meta-update",
                             query={"field": "x", "value": "y"})
        assert status == 200
        resp = json.loads(body)
        lu = resp["data"]["last_updated"]
        assert len(lu) == 10  # YYYY-MM-DD
        assert "-" in lu
