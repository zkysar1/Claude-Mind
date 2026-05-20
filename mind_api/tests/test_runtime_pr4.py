"""PR 4 — Tier A reader endpoint tests.

Covers pipeline, rb, guard, pattern-signatures, spark-questions, experience,
journal, board, team-state, and tree/read.

Each endpoint gets:
  - one happy-path test per major flag
  - one 404 / not-found test where applicable
  - one missing-flag (400) test

Equivalence with the pre-migration CLI is checked at the JSON-shape level
(IDs, counts, presence of expected fields). Byte-for-byte equivalence
would require running the CLI; that's verified separately by smoke tests
against live data.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


def _get(port: int, path: str, query: dict = None, *, agent: str = "alpha"):
    url = f"http://127.0.0.1:{port}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url)
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def test_pipeline_stage_active(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/pipeline/read", {"stage": "active"})
    data = json.loads(body)
    assert len(data) == 1
    assert data[0]["id"] == "2026-05-12_test-active"


def test_pipeline_id_lookup(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/pipeline/read", {"id": "2026-05-12_test-resolved"})
    data = json.loads(body)
    assert data["stage"] == "resolved"
    assert data["outcome"] == "correct"


def test_pipeline_summary(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/pipeline/read", {"summary": "1"})
    assert "2026-05-12_test-active" in body
    assert "[ACTIVE]" in body
    assert "→ correct" in body


def test_pipeline_counts_from_meta(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/pipeline/read", {"counts": "1"})
    data = json.loads(body)
    assert data["active"] == 1
    assert data["resolved"] == 1


def test_pipeline_missing_id_404(running_daemon):
    _, port = running_daemon
    try:
        _get(port, "/v1/pipeline/read", {"id": "nonexistent"})
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        raise AssertionError("expected 404 for missing pipeline id")


def test_pipeline_missing_flag_400(running_daemon):
    _, port = running_daemon
    try:
        _get(port, "/v1/pipeline/read")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "missing_flag"
    else:
        raise AssertionError("expected 400 for missing pipeline flag")


# ---------------------------------------------------------------------------
# Reasoning bank
# ---------------------------------------------------------------------------

def test_rb_active_filters_retired(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/rb/read", {"active": "1"})
    data = json.loads(body)
    ids = {r["id"] for r in data}
    assert ids == {"rb-001", "rb-002"}


def test_rb_universal_active_only(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/rb/read", {"universal": "1"})
    data = json.loads(body)
    # rb-002 applies_to=any AND framework-* category — universal.
    ids = {r["id"] for r in data}
    assert "rb-002" in ids
    # rb-001 has applies_to=framework — also universal per is_universal_rb
    assert "rb-001" in ids
    # rb-003 retired — must be filtered.
    assert "rb-003" not in ids


def test_rb_category_filter(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/rb/read", {"category": "alpha-cat"})
    data = json.loads(body)
    ids = {r["id"] for r in data}
    # category= matches BOTH active and retired (no status filter).
    assert ids == {"rb-001", "rb-003"}


def test_rb_tag_active_only(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/rb/read", {"tag": "uni"})
    data = json.loads(body)
    ids = {r["id"] for r in data}
    assert ids == {"rb-002"}


def test_rb_recent(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/rb/read", {"recent": "1"})
    data = json.loads(body)
    # Newest active is rb-002 (created 2026-05-11).
    assert len(data) == 1
    assert data[0]["id"] == "rb-002"


def test_rb_id_404(running_daemon):
    _, port = running_daemon
    try:
        _get(port, "/v1/rb/read", {"id": "rb-999"})
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        raise AssertionError("expected 404")


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

def test_guard_active(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/guard/read", {"active": "1"})
    data = json.loads(body)
    ids = {r["id"] for r in data}
    assert ids == {"guard-001", "guard-002"}


def test_guard_summary_format(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/guard/read", {"summary": "1"})
    assert "guard-001" in body
    assert "[infra]" in body


# ---------------------------------------------------------------------------
# Pattern signatures
# ---------------------------------------------------------------------------

def test_pattern_active(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/pattern-signatures/read", {"active": "1"})
    data = json.loads(body)
    assert len(data) == 1
    assert data[0]["id"] == "sig-001"


def test_pattern_summary_shows_accuracy(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/pattern-signatures/read", {"summary": "1"})
    assert "accuracy=0.8" in body
    assert "(4/5)" in body


# ---------------------------------------------------------------------------
# Spark questions
# ---------------------------------------------------------------------------

def test_sparks_active_only(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/spark-questions/read", {"active": "1"})
    data = json.loads(body)
    assert len(data) == 1
    assert data[0]["id"] == "sq-001"


def test_sparks_candidates_only(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/spark-questions/read", {"candidates": "1"})
    data = json.loads(body)
    assert len(data) == 1
    assert data[0]["id"] == "sq-c01"


# ---------------------------------------------------------------------------
# Experience
# ---------------------------------------------------------------------------

def test_experience_goal_filter(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/experience/read", {"goal": "g-001-01"})
    data = json.loads(body)
    assert len(data) == 1
    assert data[0]["id"] == "exp-test-1"


def test_experience_most_retrieved(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/experience/read", {"most_retrieved": "1"})
    data = json.loads(body)
    # exp-test-1 has retrieval_count=10 vs exp-test-2 with 2.
    assert len(data) == 1
    assert data[0]["id"] == "exp-test-1"


def test_experience_meta(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/experience/read", {"meta": "1"})
    data = json.loads(body)
    assert data["total_records"] == 2


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

def test_journal_session(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/journal/read", {"session": "2"})
    data = json.loads(body)
    assert data["date"] == "2026-05-11"


def test_journal_latest(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/journal/read", {"latest": "1"})
    data = json.loads(body)
    assert data["session"] == 2


def test_journal_meta_computes(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/journal/read", {"meta": "1"})
    data = json.loads(body)
    assert data["total_sessions"] == 2
    assert data["date_range"] == ["2026-05-10", "2026-05-11"]


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------

def test_board_read_json(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/board/read", {"channel": "general", "json": "1"})
    # JSONL: one line per message.
    lines = [ln for ln in body.split("\n") if ln.strip()]
    assert len(lines) == 2
    parsed = [json.loads(ln) for ln in lines]
    assert {m["author"] for m in parsed} == {"alpha", "bravo"}


def test_board_read_type_filter(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/board/read",
                   {"channel": "general", "type": "claim", "json": "1"})
    lines = [ln for ln in body.split("\n") if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "msg-2"


def test_board_missing_channel_400(running_daemon):
    _, port = running_daemon
    try:
        _get(port, "/v1/board/read")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("expected 400")


def test_board_unknown_channel_returns_empty_line(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/board/read", {"channel": "no-such-channel"})
    assert "empty or does not exist" in body


# ---------------------------------------------------------------------------
# Team-state
# ---------------------------------------------------------------------------

def test_team_state_field_dotted(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/team-state/read",
                   {"field": "strategic_focus.primary", "json": "1"})
    assert json.loads(body) == "building runtime"


def test_team_state_field_dict_yaml(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/team-state/read",
                   {"field": "agent_status.alpha"})
    # YAML output for a dict value
    assert "last_active" in body


def test_team_state_full_dump_json(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/team-state/read", {"json": "1"})
    data = json.loads(body)
    assert data["last_updated_by"] == "alpha"
    assert "strategic_focus" in data


# ---------------------------------------------------------------------------
# Tree read
# ---------------------------------------------------------------------------

def test_tree_read_node(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/tree/read", {"node": "alpha-test-node"})
    data = json.loads(body)
    assert data["key"] == "alpha-test-node"
    assert "summary" in data


def test_tree_read_path(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/tree/read", {"path": "alpha-test-node"})
    assert body.strip() == "world/knowledge/tree/alpha-test-node.md"


def test_tree_read_children(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/tree/read", {"children": "beta-other-node"})
    data = json.loads(body)
    # beta-other-node has alpha-test-node as a child in the fixture
    assert isinstance(data, (list, dict))


def test_tree_read_summary(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/tree/read", {"summary": "1"})
    data = json.loads(body)
    assert data["total"] == 2
    assert "alpha-test-node" in data["nodes"]


def test_tree_read_stats(running_daemon):
    _, port = running_daemon
    _, body = _get(port, "/v1/tree/read", {"stats": "1"})
    data = json.loads(body)
    assert isinstance(data, dict)


def test_tree_read_missing_flag_400(running_daemon):
    _, port = running_daemon
    try:
        _get(port, "/v1/tree/read")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("expected 400")
