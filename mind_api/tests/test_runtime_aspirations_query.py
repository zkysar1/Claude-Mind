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


# ---------------------------------------------------------------------------
# Vacuous goal_field filters are refused, not answered ()
#
# Before this, an unknown/misspelled/output-only field name returned `[]` with
# HTTP 200 — byte-identical to a genuine empty AND sharing its exit code, so a
# caller had no discriminator at all. Three false zeros in one investigation
# (2026-08-10), then a fourth by a different agent in a different lane the next
# day, all against goals that existed.
# ---------------------------------------------------------------------------

def test_query_unknown_goal_field_rejected(seeded_with_goals):
    """A field name no goal record carries is unanswerable, not empty."""
    _, port = seeded_with_goals
    try:
        _get(port, "/v1/aspirations/query",
             {"goal_field_name": "zzz_not_a_field", "goal_field_value": "x"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "unknown_goal_field"
        # Must name the offending key AND list real ones, so the caller can
        # self-correct without reading the source (mirrors invalid_goal_status).
        # The full valid-key list is asserted, not a sample: the omitted key is
        # exactly the one the caller was reaching for (guard-1941).
        assert "zzz_not_a_field" in err["detail"]
        for valid in ("id", "title", "status", "category"):
            assert valid in err["detail"], f"valid key {valid!r} missing from detail"
    else:
        raise AssertionError("expected 400 for a field name no record carries")


def test_query_unknown_field_with_none_value_does_not_match_everything(seeded_with_goals):
    """The over-broad half, and the reason the refusal is not gated on `not results`.

    `_goal_matches` compares str(goal.get(field)) != value, so an ABSENT key
    stringifies to "None" and the literal value "None" matched EVERY goal. That
    returned the whole store with HTTP 200 — a wrong answer that looks like a
    thorough one. A result-size-gated refusal would sail straight past it.
    """
    _, port = seeded_with_goals
    try:
        _get(port, "/v1/aspirations/query",
             {"goal_field_name": "zzz_not_a_field", "goal_field_value": "None"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read().decode("utf-8"))["error"] == "unknown_goal_field"
    else:
        raise AssertionError(
            "expected 400 — an absent key with value 'None' must not match every goal")


def test_query_goal_id_alias_resolves_to_id(seeded_with_goals):
    """`goal_id` is what every projection EMITS, so callers key on it ().

    Matching reads the raw record, where the key is `id`. Measured twice by two
    agents a day apart: `--goal-field goal_id <id>` returned 0 rows for a goal
    that exists.
    """
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_field_name": "goal_id", "goal_field_value": "g-001-01"})
    data = json.loads(body)
    assert [r["goal_id"] for r in data] == ["g-001-01"]

    # The alias must agree with the un-aliased spelling, not merely be non-empty.
    _, body_id = _get(port, "/v1/aspirations/query",
                      {"goal_field_name": "id", "goal_field_value": "g-001-01"})
    assert json.loads(body_id) == data


def test_query_valid_field_with_no_match_still_returns_empty(seeded_with_goals):
    """OVER-REFUSAL GUARD — the failure mode this change could introduce.

    `category` is carried by every seeded goal, so a value that matches none of
    them is a genuine empty result and MUST stay HTTP 200 + []. If this ever
    starts raising, the refusal has widened from 'key absent everywhere' to
    'nothing matched' — trading a silent false zero for a loud false failure,
    which is not an improvement.
    """
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_field_name": "category",
                    "goal_field_value": "no-such-category"})
    assert json.loads(body) == []


def test_query_projection_only_key_gets_a_specific_hint(seeded_with_goals):
    """`asp_id` is visible in output but attached at projection time.

    Refused like any absent key, with an extra sentence — the generic message
    would send a caller hunting for a key they can plainly see in the response.
    """
    _, port = seeded_with_goals
    try:
        _get(port, "/v1/aspirations/query",
             {"goal_field_name": "asp_id", "goal_field_value": "asp-001"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        detail = json.loads(e.read().decode("utf-8"))["detail"]
        assert "projection" in detail
    else:
        raise AssertionError("expected 400 for a projection-only key")


def test_query_unknown_field_against_goalless_store_is_empty_not_400(running_daemon):
    """ZERO-GOALS GUARD — a store with no goal records cannot validate a field name.

    Uses `running_daemon` rather than `seeded_with_goals` precisely because the
    conftest seeds aspirations whose `goals` arrays are EMPTY. That store exists
    (so the 404 no_aspiration_store branch does not fire) while carrying zero goal
    records — the one state in which EVERY field name is unseen. Without the
    guard the refusal fires on all of them, so a fresh world answers every
    filtered query with a 400 and "no goals yet" is reported as a caller error.

    The empty array is the honest answer here, and it is the same answer the
    endpoint gave before the refusal existed — this pin is what keeps the loud
    half of g-115-5752 from widening into the quiet half's territory.
    """
    _, port = running_daemon
    status, body = _get(port, "/v1/aspirations/query",
                        {"goal_field_name": "zzz_not_a_field",
                         "goal_field_value": "x"})
    assert status == 200
    assert json.loads(body) == []
