"""POST /v1/aspirations/meta-update endpoint tests (PR 52).

Mirrors cmd_meta_update behavior exactly:
  - single field update on existing meta file
  - multi-field update (batch body)
  - missing file → default meta created then updated
  - dotted field names rejected (400)
  - empty body rejected (400)
  - non-object body rejected (400)
  - source=agent routes to agent dir
  - invalid source → 400
  - nested JSON value (object/array) round-trips correctly
  - integer and boolean type coercion round-trips correctly
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _post(port: int, body: dict, source: str = "world", agent: str = "alpha"):
    """POST /v1/aspirations/meta-update and return parsed JSON."""
    import urllib.request
    url = f"http://127.0.0.1:{port}/v1/aspirations/meta-update?source={source}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, method="POST", data=data,
        headers={
            "Content-Type": "application/json",
            "X-Mind-Agent": agent,
            "X-Runtime-Client": "test",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _post_raw(port: int, body, source: str = "world", agent: str = "alpha"):
    """POST and return (status_code, body_dict)."""
    import urllib.request
    import urllib.error
    url = f"http://127.0.0.1:{port}/v1/aspirations/meta-update?source={source}"
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
    elif isinstance(body, str):
        data = body.encode()
    else:
        data = body
    req = urllib.request.Request(
        url, method="POST", data=data,
        headers={
            "Content-Type": "application/json",
            "X-Mind-Agent": agent,
            "X-Runtime-Client": "test",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_single_field_update(running_daemon):
    """Update one field on an existing meta file."""
    project_root, port = running_daemon
    meta_path = project_root / "world" / "aspirations-meta.json"
    meta_path.write_text(
        json.dumps({"last_updated": None, "session_count": 3}),
        encoding="utf-8",
    )
    resp = _post(port, {"session_count": 5})
    assert resp["ok"] is True
    assert resp["data"]["session_count"] == 5
    # Original field preserved.
    assert resp["data"]["last_updated"] is None
    # Verify on-disk state.
    disk = json.loads(meta_path.read_text(encoding="utf-8"))
    assert disk["session_count"] == 5


def test_multi_field_update(running_daemon):
    """Batch update: multiple fields in one request."""
    project_root, port = running_daemon
    meta_path = project_root / "world" / "aspirations-meta.json"
    meta_path.write_text(json.dumps({"session_count": 0}), encoding="utf-8")
    resp = _post(port, {"session_count": 10, "last_updated": "2026-05-14T12:00:00"})
    assert resp["ok"] is True
    assert resp["data"]["session_count"] == 10
    assert resp["data"]["last_updated"] == "2026-05-14T12:00:00"


def test_missing_file_creates_default(running_daemon):
    """When aspirations-meta.json does not exist, create with defaults + update."""
    project_root, port = running_daemon
    meta_path = project_root / "world" / "aspirations-meta.json"
    if meta_path.exists():
        meta_path.unlink()
    resp = _post(port, {"session_count": 1})
    assert resp["ok"] is True
    data = resp["data"]
    # Defaults filled in.
    assert data["last_updated"] is None
    assert data["last_evolution"] is None
    assert data["readiness_gates"] == {}
    # Our update applied on top.
    assert data["session_count"] == 1
    # File now exists.
    assert meta_path.exists()


def test_dotted_field_rejected(running_daemon):
    """Dotted field names are rejected with 400."""
    _, port = running_daemon
    code, body = _post_raw(port, {"readiness_gates.gate_a": True})
    assert code == 400
    assert body["error"] == "dotted_field_rejected"


def test_empty_body_rejected(running_daemon):
    """Empty object body is rejected with 400."""
    _, port = running_daemon
    code, body = _post_raw(port, {})
    assert code == 400
    assert body["error"] == "empty_body"


def test_non_object_body_rejected(running_daemon):
    """Array or scalar body is rejected with 400."""
    _, port = running_daemon
    code, body = _post_raw(port, [1, 2, 3])
    assert code == 400
    assert body["error"] == "invalid_body"


def test_source_agent(running_daemon):
    """source=agent writes to agent dir, not world dir."""
    project_root, port = running_daemon
    agent_meta = project_root / "agents" / "alpha" / "aspirations-meta.json"
    if agent_meta.exists():
        agent_meta.unlink()
    resp = _post(port, {"session_count": 42}, source="agent")
    assert resp["ok"] is True
    assert resp["data"]["session_count"] == 42
    # Written to agent dir.
    assert agent_meta.exists()
    disk = json.loads(agent_meta.read_text(encoding="utf-8"))
    assert disk["session_count"] == 42


def test_invalid_source(running_daemon):
    """Invalid source parameter returns 400."""
    _, port = running_daemon
    code, body = _post_raw(port, {"x": 1}, source="bad")
    assert code == 400
    assert body["error"] == "invalid_source"


def test_nested_json_value(running_daemon):
    """Nested JSON objects and arrays round-trip correctly."""
    project_root, port = running_daemon
    meta_path = project_root / "world" / "aspirations-meta.json"
    meta_path.write_text(json.dumps({"session_count": 0}), encoding="utf-8")
    nested = {"gate_a": True, "gate_b": False}
    resp = _post(port, {"readiness_gates": nested})
    assert resp["ok"] is True
    assert resp["data"]["readiness_gates"] == nested


def test_integer_boolean_roundtrip(running_daemon):
    """Integer and boolean values are preserved through JSON round-trip."""
    project_root, port = running_daemon
    meta_path = project_root / "world" / "aspirations-meta.json"
    meta_path.write_text(json.dumps({"session_count": 0}), encoding="utf-8")
    resp = _post(port, {"session_count": 99, "flag": True, "label": "hello"})
    assert resp["ok"] is True
    assert resp["data"]["session_count"] == 99
    assert resp["data"]["flag"] is True
    assert resp["data"]["label"] == "hello"
