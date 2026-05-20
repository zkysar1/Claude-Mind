"""Daemon aspirations-complete-intent endpoint tests (PR 51).

Covers:
  - Happy path: intent-satisfaction completes an aspiration
  - Superseded goals transitioned correctly
  - Validation: rationale too short -> 400
  - Validation: evidence goal not completed -> 400
  - Validation: evidence goals insufficient cardinality -> 400
  - Validation: superseded goal is recurring -> 400
  - Validation: cross-contamination (goal in both lists) -> 400
  - Validation: remaining unfinished after supersession -> 400
  - Recurring goals present -> 400
  - Missing body -> 400
  - Missing asp_id -> 400
  - 404 on missing aspiration
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
    if not path.exists():
        return items
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def _seed_aspiration(world: Path, asp, *, source="world"):
    path = world / "aspirations.jsonl"
    path.write_text(json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")


def _ensure_intent_config(project_root: Path):
    """Seed core/config/aspirations.yaml with intent_satisfaction block."""
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "aspirations.yaml"
    if not cfg_path.exists():
        cfg_path.write_text(
            "intent_satisfaction:\n"
            "  min_evidence_by_scope:\n"
            "    sprint: 2\n"
            "    project: 3\n"
            "    initiative: 5\n",
            encoding="utf-8",
        )


def _make_intent_asp(asp_id="asp-001", *, extra_pending=True):
    """Aspiration with 2 completed goals, 1 pending (for supersession)."""
    goals = [
        {"id": f"g-{asp_id[4:]}-01", "title": "Done 1", "status": "completed",
         "recurring": False, "verification": {"outcomes": ["tests pass"]}},
        {"id": f"g-{asp_id[4:]}-02", "title": "Done 2", "status": "completed",
         "recurring": False, "verification": {"outcomes": ["more tests"]}},
    ]
    if extra_pending:
        goals.append(
            {"id": f"g-{asp_id[4:]}-03", "title": "Leftover", "status": "pending",
             "recurring": False})
    return {
        "id": asp_id,
        "title": "Intent test",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "scope": "sprint",
        "motivation": "Build a comprehensive testing framework for daemon endpoints",
        "goals": goals,
        "progress": {"completed_goals": 2,
                     "total_goals": len(goals),
                     "recurring_goals": 0},
    }


def _valid_intent_block(asp_id="asp-001"):
    return {
        "evidence_goal_ids": [f"g-{asp_id[4:]}-01", f"g-{asp_id[4:]}-02"],
        "rationale": "The comprehensive testing framework is fully built and validated with daemon endpoint tests",
        "superseded_goal_ids": [f"g-{asp_id[4:]}-03"],
    }


# ---- Tests ----

def test_complete_intent_happy_path(running_daemon):
    """Intent-satisfaction completes aspiration, supersedes pending goals."""
    project_root, port = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    _seed_aspiration(world, _make_intent_asp())

    status, body = _post(
        port, "/v1/aspirations/complete-intent",
        {"asp_id": "asp-001"},
        body=json.dumps(_valid_intent_block()),
    )
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["ok"] is True
    asp = resp["aspiration"]
    assert asp["status"] == "completed"
    assert asp["archived"] is True
    assert "completed_at" in asp
    assert "intent_satisfaction" in asp
    assert "claimed_at" in asp["intent_satisfaction"]

    # Live file should be empty
    live = _read_jsonl(world / "aspirations.jsonl")
    assert len(live) == 0

    # Archive should contain the completed aspiration
    archive = _read_jsonl(world / "aspirations-archive.jsonl")
    assert len(archive) == 1
    assert archive[0]["id"] == "asp-001"


def test_complete_intent_superseded_goals_transitioned(running_daemon):
    """Superseded goals get status=superseded."""
    project_root, port = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    _seed_aspiration(world, _make_intent_asp())

    status, body = _post(
        port, "/v1/aspirations/complete-intent",
        {"asp_id": "asp-001"},
        body=json.dumps(_valid_intent_block()),
    )
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    g3 = [g for g in resp["aspiration"]["goals"] if g["id"] == "g-001-03"][0]
    assert g3["status"] == "superseded"
    assert g3["superseded_by_aspiration"] == "asp-001"


def test_complete_intent_rationale_too_short(running_daemon):
    """Rationale < 40 chars -> 400 intent_validation_failed."""
    project_root, port = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    _seed_aspiration(world, _make_intent_asp())

    block = _valid_intent_block()
    block["rationale"] = "too short"

    status, body = _post(
        port, "/v1/aspirations/complete-intent",
        {"asp_id": "asp-001"},
        body=json.dumps(block),
    )
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "intent_validation_failed"
    assert "rationale too short" in resp["detail"]


def test_complete_intent_evidence_not_completed(running_daemon):
    """Evidence goal that is not completed -> 400."""
    project_root, port = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    asp = _make_intent_asp()
    # Make first goal pending instead of completed
    asp["goals"][0]["status"] = "pending"
    _seed_aspiration(world, asp)

    status, body = _post(
        port, "/v1/aspirations/complete-intent",
        {"asp_id": "asp-001"},
        body=json.dumps(_valid_intent_block()),
    )
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "intent_validation_failed"
    assert "must be completed" in resp["detail"]


def test_complete_intent_insufficient_evidence(running_daemon):
    """Too few evidence goals for scope -> 400."""
    project_root, port = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    asp = _make_intent_asp()
    _seed_aspiration(world, asp)

    block = _valid_intent_block()
    block["evidence_goal_ids"] = [""]  # Only 1, sprint requires 2

    status, body = _post(
        port, "/v1/aspirations/complete-intent",
        {"asp_id": "asp-001"},
        body=json.dumps(block),
    )
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "intent_validation_failed"
    assert "evidence_goal_ids" in resp["detail"]


def test_complete_intent_superseded_recurring_rejected(running_daemon):
    """Cannot supersede a recurring goal -> 400.

    Note: when a goal is marked recurring=True, the recurring-goals guard
    fires BEFORE intent validation (recurring goals must not be archived).
    So the error is recurring_goals_present, not intent_validation_failed.
    """
    project_root, port = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    asp = _make_intent_asp()
    asp["goals"][2]["recurring"] = True
    asp["goals"][2]["interval_hours"] = 24
    _seed_aspiration(world, asp)

    status, body = _post(
        port, "/v1/aspirations/complete-intent",
        {"asp_id": "asp-001"},
        body=json.dumps(_valid_intent_block()),
    )
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "recurring_goals_present"
    assert "recurring" in resp["detail"]


def test_complete_intent_cross_contamination(running_daemon):
    """Goal in both evidence and superseded lists -> 400."""
    project_root, port = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    asp = _make_intent_asp()
    _seed_aspiration(world, asp)

    block = _valid_intent_block()
    block["superseded_goal_ids"] = [""]  #  is also in evidence

    status, body = _post(
        port, "/v1/aspirations/complete-intent",
        {"asp_id": "asp-001"},
        body=json.dumps(block),
    )
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "intent_validation_failed"
    assert "both evidence and superseded" in resp["detail"]


def test_complete_intent_remaining_unfinished(running_daemon):
    """Goals left unfinished and not superseded -> 400."""
    project_root, port = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    asp = _make_intent_asp()
    _seed_aspiration(world, asp)

    block = _valid_intent_block()
    block["superseded_goal_ids"] = []  #  is pending but not superseded

    status, body = _post(
        port, "/v1/aspirations/complete-intent",
        {"asp_id": "asp-001"},
        body=json.dumps(block),
    )
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "intent_validation_failed"
    assert "still be unfinished" in resp["detail"]


def test_complete_intent_recurring_goals_blocked(running_daemon):
    """Aspiration with recurring goals -> 400."""
    project_root, port = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Has recurring", "status": "active",
        "priority": "LOW", "archived": False, "scope": "sprint",
        "motivation": "Build a comprehensive testing framework for daemon endpoints",
        "goals": [
            {"id": "g-001-01", "title": "Recurring", "status": "pending",
             "recurring": True, "interval_hours": 24},
        ],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 1},
    }
    _seed_aspiration(world, asp)

    block = {
        "evidence_goal_ids": [],
        "rationale": "The comprehensive testing framework is fully built and validated",
        "superseded_goal_ids": [],
    }
    status, body = _post(
        port, "/v1/aspirations/complete-intent",
        {"asp_id": "asp-001"},
        body=json.dumps(block),
    )
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "recurring_goals_present"


def test_complete_intent_missing_body(running_daemon):
    """No body -> 400."""
    project_root, port = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    _seed_aspiration(world, _make_intent_asp())

    status, body = _post(
        port, "/v1/aspirations/complete-intent",
        {"asp_id": "asp-001"},
    )
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "invalid_body"


def test_complete_intent_missing_asp_id(running_daemon):
    """No asp_id -> 400."""
    _, port = running_daemon
    status, body = _post(
        port, "/v1/aspirations/complete-intent", {},
        body=json.dumps({"evidence_goal_ids": [], "rationale": "x" * 50,
                         "superseded_goal_ids": []}),
    )
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "missing_asp_id"


def test_complete_intent_not_found(running_daemon):
    """Unknown asp_id -> 404."""
    project_root, port = running_daemon
    _ensure_intent_config(project_root)

    status, body = _post(
        port, "/v1/aspirations/complete-intent",
        {"asp_id": "asp-999"},
        body=json.dumps(_valid_intent_block("asp-999")),
    )
    assert status == 404
    resp = json.loads(body)
    assert resp["error"] == "aspiration_not_found"
