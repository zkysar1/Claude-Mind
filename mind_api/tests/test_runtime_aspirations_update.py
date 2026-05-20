"""Daemon aspirations-update endpoint tests (PR 54).

Covers:
  - Single-field update (title)
  - Multi-field update (title + scope)
  - Status transition (active -> paused)
  - source=agent queue
  - Boolean round-trip (archived)
  - Integer round-trip (numeric field)
  - Validation: invalid status
  - Validation: invalid priority
  - Validation: invalid scope
  - Validation: invalid coordination_mode
  - Validation: archived must be bool
  - Validation: dotted field rejected
  - 404 on missing aspiration
  - Empty body rejected
  - Non-object body rejected
  - Missing asp_id rejected
  - Invalid asp_id format rejected
  - Invalid source rejected
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest


def _post(port, path, query, body=None, *, agent="alpha", headers=None):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    data = body if isinstance(body, bytes) else (body.encode("utf-8") if body else None)
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _read_jsonl(path: Path):
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestUpdateAspirationHappy:
    def test_single_field(self, running_daemon):
        root, port = running_daemon
        body = json.dumps({"title": "Updated Title"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"}, body)
        assert status == 200
        resp = json.loads(text)
        assert resp["ok"] is True
        assert resp["aspiration"]["title"] == "Updated Title"

        # Verify persisted
        items = _read_jsonl(root / "world" / "aspirations.jsonl")
        asp = next(a for a in items if a["id"] == "asp-001")
        assert asp["title"] == "Updated Title"

    def test_multi_field(self, running_daemon):
        root, port = running_daemon
        body = json.dumps({"title": "Multi", "scope": "project"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"}, body)
        assert status == 200
        resp = json.loads(text)
        assert resp["aspiration"]["title"] == "Multi"
        assert resp["aspiration"]["scope"] == "project"

    def test_status_transition(self, running_daemon):
        root, port = running_daemon
        body = json.dumps({"status": "paused"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"}, body)
        assert status == 200
        resp = json.loads(text)
        assert resp["aspiration"]["status"] == "paused"

    def test_source_agent(self, running_daemon):
        root, port = running_daemon
        body = json.dumps({"title": "Agent Updated"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-100", "source": "agent"}, body)
        assert status == 200
        resp = json.loads(text)
        assert resp["aspiration"]["title"] == "Agent Updated"

        # Verify persisted in agent queue
        items = _read_jsonl(root / "agents" / "alpha" / "aspirations.jsonl")
        asp = next(a for a in items if a["id"] == "asp-100")
        assert asp["title"] == "Agent Updated"

    def test_boolean_round_trip(self, running_daemon):
        root, port = running_daemon
        body = json.dumps({"archived": True})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"}, body)
        assert status == 200
        resp = json.loads(text)
        assert resp["aspiration"]["archived"] is True

    def test_integer_round_trip(self, running_daemon):
        root, port = running_daemon
        body = json.dumps({"chronic_friction": 3})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"}, body)
        assert status == 200
        resp = json.loads(text)
        assert resp["aspiration"]["chronic_friction"] == 3


# ---------------------------------------------------------------------------
# Validation rejection tests
# ---------------------------------------------------------------------------


class TestUpdateAspirationValidation:
    def test_invalid_status(self, running_daemon):
        _, port = running_daemon
        body = json.dumps({"status": "bogus"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"}, body)
        assert status == 400
        assert "invalid_status" in text

    def test_invalid_priority(self, running_daemon):
        _, port = running_daemon
        body = json.dumps({"priority": "ULTRA"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"}, body)
        assert status == 400
        assert "invalid_priority" in text

    def test_invalid_scope(self, running_daemon):
        _, port = running_daemon
        body = json.dumps({"scope": "galaxy"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"}, body)
        assert status == 400
        assert "invalid_scope" in text

    def test_invalid_coordination_mode(self, running_daemon):
        _, port = running_daemon
        body = json.dumps({"coordination_mode": "chaotic"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"}, body)
        assert status == 400
        assert "invalid_coordination_mode" in text

    def test_archived_must_be_bool(self, running_daemon):
        _, port = running_daemon
        body = json.dumps({"archived": "yes"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"}, body)
        assert status == 400
        assert "invalid_archived" in text

    def test_dotted_field_rejected(self, running_daemon):
        _, port = running_daemon
        body = json.dumps({"progress.total": 5})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"}, body)
        assert status == 400
        assert "dotted_field_rejected" in text


# ---------------------------------------------------------------------------
# Error tests
# ---------------------------------------------------------------------------


class TestUpdateAspirationErrors:
    def test_missing_aspiration(self, running_daemon):
        _, port = running_daemon
        body = json.dumps({"title": "Nope"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-999", "source": "world"}, body)
        assert status == 404
        assert "aspiration_not_found" in text

    def test_empty_body(self, running_daemon):
        _, port = running_daemon
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"}, b"{}")
        assert status == 400
        assert "empty_body" in text

    def test_non_object_body(self, running_daemon):
        _, port = running_daemon
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "world"},
                             json.dumps(["not", "an", "object"]))
        assert status == 400
        assert "invalid_body" in text

    def test_missing_asp_id(self, running_daemon):
        _, port = running_daemon
        body = json.dumps({"title": "X"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"source": "world"}, body)
        assert status == 400
        assert "missing_asp_id" in text

    def test_invalid_asp_id_format(self, running_daemon):
        _, port = running_daemon
        body = json.dumps({"title": "X"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "bad-id", "source": "world"}, body)
        assert status == 400
        assert "invalid_asp_id" in text

    def test_invalid_source(self, running_daemon):
        _, port = running_daemon
        body = json.dumps({"title": "X"})
        status, text = _post(port, "/v1/aspirations/update",
                             {"asp_id": "asp-001", "source": "mars"}, body)
        assert status == 400
        assert "invalid_source" in text
