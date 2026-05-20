"""PR 6 — /v1/aspirations/query daemon endpoint tests.

The endpoint mirrors `aspirations.py query` cross-queue: reads world AND agent
aspirations, returns flat list of {goal_id, asp_id, source, title, status}
across all matching goals. AND semantics across filters.

The conftest seeds aspirations with empty goals arrays — we overwrite them
in a fixture per test, then exercise the filters.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest


def _get(port: int, path: str, query: dict, *, agent: str = "alpha") -> tuple[int, str]:
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}"
    req = urllib.request.Request(url)
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


@pytest.fixture
def seeded_with_goals(running_daemon):
    """Overwrite the conftest aspirations with versions that have goals.

    World asp-001 → 2 goals (1 pending, 1 completed).
    Agent asp-100 → 3 goals (2 pending, 1 blocked; one with tags=['urgent']).
    """
    project_root, port = running_daemon
    world_asp = project_root / "world" / "aspirations.jsonl"
    agent_asp = project_root / "agents" / "alpha" / "aspirations.jsonl"

    world_record = {
        "id": "asp-001",
        "title": "World aspiration",
        "status": "active",
        "priority": "MEDIUM",
        "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Build encoding pipeline",
             "status": "pending", "category": "framework-architecture",
             "tags": ["routine"]},
            {"id": "g-001-02", "title": "Audit token usage",
             "status": "completed", "category": "observability",
             "tags": []},
        ],
        "progress": {"completed_goals": 1, "total_goals": 2},
    }
    agent_record = {
        "id": "asp-100",
        "title": "Agent aspiration",
        "status": "active",
        "priority": "HIGH",
        "archived": False,
        "goals": [
            {"id": "g-100-01", "title": "Encode session insights",
             "status": "pending", "category": "framework-architecture",
             "tags": ["urgent", "encoding"]},
            {"id": "g-100-02", "title": "Refactor encoder",
             "status": "pending", "category": "framework-architecture",
             "tags": []},
            {"id": "g-100-03", "title": "Investigate flaky test",
             "status": "blocked", "category": "infra",
             "tags": ["urgent"]},
        ],
        "progress": {"completed_goals": 0, "total_goals": 3},
    }
    world_asp.write_text(json.dumps(world_record) + "\n", encoding="utf-8")
    agent_asp.write_text(json.dumps(agent_record) + "\n", encoding="utf-8")
    return project_root, port


# ---------------------------------------------------------------------------
# Filter: goal_status
# ---------------------------------------------------------------------------

def test_query_by_goal_status_single(seeded_with_goals):
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query", {"goal_status": "pending"})
    data = json.loads(body)
    ids = sorted(r["goal_id"] for r in data)
    assert ids == ["g-001-01", "g-100-01", "g-100-02"]


def test_query_by_goal_status_comma_list(seeded_with_goals):
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_status": "pending,blocked"})
    data = json.loads(body)
    ids = sorted(r["goal_id"] for r in data)
    assert ids == ["g-001-01", "g-100-01", "g-100-02", "g-100-03"]


def test_query_includes_source_field(seeded_with_goals):
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query", {"goal_status": "completed"})
    data = json.loads(body)
    assert len(data) == 1
    assert data[0]["source"] == "world"
    assert data[0]["asp_id"] == "asp-001"


def test_query_invalid_status_rejected(seeded_with_goals):
    _, port = seeded_with_goals
    try:
        _get(port, "/v1/aspirations/query", {"goal_status": "nonsense"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        body = e.read().decode("utf-8")
        err = json.loads(body)
        assert err["error"] == "invalid_goal_status"
        # Message format must list valid statuses so the user can self-correct.
        assert "nonsense" in err["detail"]
        assert "pending" in err["detail"]
    else:
        raise AssertionError("expected 400 for invalid goal_status")


# ---------------------------------------------------------------------------
# Filter: goal_field (paired name/value)
# ---------------------------------------------------------------------------

def test_query_by_goal_field_scalar(seeded_with_goals):
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_field_name": "category",
                    "goal_field_value": "framework-architecture"})
    data = json.loads(body)
    ids = sorted(r["goal_id"] for r in data)
    assert ids == ["g-001-01", "g-100-01", "g-100-02"]


def test_query_by_goal_field_list_contains(seeded_with_goals):
    """List-valued fields use 'contains' semantics — cmd_query line 935."""
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_field_name": "tags", "goal_field_value": "urgent"})
    data = json.loads(body)
    ids = sorted(r["goal_id"] for r in data)
    assert ids == ["g-100-01", "g-100-03"]


def test_query_goal_field_name_only_rejected(seeded_with_goals):
    """Half-pair → 400. The CLI's argparse nargs=2 enforces this; the daemon
    must too, otherwise the wrapper's PASSTHROUGH could silently send a
    value-less filter."""
    _, port = seeded_with_goals
    try:
        _get(port, "/v1/aspirations/query", {"goal_field_name": "category"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "invalid_goal_field"
    else:
        raise AssertionError("expected 400 for half-paired goal_field")


# ---------------------------------------------------------------------------
# Filter: title_contains (case-insensitive substring)
# ---------------------------------------------------------------------------

def test_query_title_contains_case_insensitive(seeded_with_goals):
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query", {"title_contains": "ENCOD"})
    data = json.loads(body)
    ids = sorted(r["goal_id"] for r in data)
    assert ids == ["g-001-01", "g-100-01", "g-100-02"]


# ---------------------------------------------------------------------------
# AND semantics across filters
# ---------------------------------------------------------------------------

def test_query_and_semantics(seeded_with_goals):
    """Multiple filters narrow the result set — all must match."""
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_status": "pending",
                    "goal_field_name": "category",
                    "goal_field_value": "framework-architecture",
                    "title_contains": "session"})
    data = json.loads(body)
    ids = [r["goal_id"] for r in data]
    assert ids == ["g-100-01"]


# ---------------------------------------------------------------------------
# Missing-filter 400
# ---------------------------------------------------------------------------

def test_query_no_filter_400(seeded_with_goals):
    _, port = seeded_with_goals
    try:
        _get(port, "/v1/aspirations/query", {})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "missing_filter"
    else:
        raise AssertionError("expected 400 when no filter provided")


# ---------------------------------------------------------------------------
# Empty result returns []
# ---------------------------------------------------------------------------

def test_query_empty_result(seeded_with_goals):
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"title_contains": "nonexistent-substring"})
    assert json.loads(body) == []
