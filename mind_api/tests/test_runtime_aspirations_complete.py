"""Daemon aspirations-complete endpoint tests (PR 9a).

Covers:
  - Happy path: complete an aspiration with all goals terminal
  - Recurring-goals guard blocks without force
  - Unfinished-goals guard blocks without force
  - force=true bypasses both guards
  - Intent-satisfaction pathway (validation + supersession)
  - Maturity warning surfaced in warnings[]
  - Archive-before-remove: aspiration appears in archive after complete
  - Stale blockers cleared from remaining aspirations
  - 404 on missing aspiration
  - Missing asp_id returns 400
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
    """Overwrite aspirations.jsonl with a single aspiration dict."""
    path = world / "aspirations.jsonl"
    path.write_text(json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")


def _seed_two_aspirations(world: Path, asp1, asp2):
    """Overwrite aspirations.jsonl with two aspiration dicts."""
    path = world / "aspirations.jsonl"
    lines = [json.dumps(a, ensure_ascii=True) for a in [asp1, asp2]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_asp_all_completed(asp_id="asp-001"):
    """Create an aspiration with all goals in terminal status."""
    return {
        "id": asp_id,
        "title": "Test complete",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "scope": "sprint",
        "goals": [
            {"id": f"g-{asp_id[4:]}-01", "title": "Done 1", "status": "completed",
             "recurring": False},
            {"id": f"g-{asp_id[4:]}-02", "title": "Done 2", "status": "skipped",
             "recurring": False},
        ],
        "progress": {"completed_goals": 1, "total_goals": 2, "recurring_goals": 0},
    }


# ---- Tests ----

def test_complete_happy_path(running_daemon):
    """Complete an aspiration with all goals terminal -> 200, archived."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_all_completed()
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/complete",
                         {"asp_id": "asp-001", "source": "world"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["ok"] is True
    completed_asp = resp["aspiration"]
    assert completed_asp["status"] == "completed"
    assert completed_asp["archived"] is True
    assert "completed_at" in completed_asp

    # Live file should be empty (aspiration removed)
    live = _read_jsonl(world / "aspirations.jsonl")
    assert len(live) == 0

    # Archive should contain the completed aspiration
    archive = _read_jsonl(world / "aspirations-archive.jsonl")
    assert len(archive) == 1
    assert archive[0]["id"] == "asp-001"
    assert archive[0]["status"] == "completed"


def test_complete_recurring_goals_blocked(running_daemon):
    """Aspiration with recurring goals -> 400 without force."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Has recurring", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Recurring", "status": "pending",
             "recurring": True, "interval_hours": 24},
        ],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 1},
    }
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/complete",
                         {"asp_id": "asp-001"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "recurring_goals_present"


def test_complete_unfinished_goals_blocked(running_daemon):
    """Aspiration with non-terminal goals -> 400 without force."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Has pending", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Still pending", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/complete",
                         {"asp_id": "asp-001"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "unfinished_goals_present"


def test_complete_force_bypasses_guards(running_daemon):
    """force=true skips the unfinished guard; the live recurring goal is
    RE-HOMED into the live container rather than archived (g-357-31 — an
    archived recurring goal is unreachable by the selector, so force may
    never strand one; with no live container the archive is refused)."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Force complete", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Recurring", "status": "pending",
             "recurring": True, "interval_hours": 24},
            {"id": "g-001-02", "title": "Pending", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 1},
    }
    container = {
        "id": "asp-002", "title": "Recurring home", "status": "active",
        "priority": "LOW", "archived": False, "recurring_home": True,
        "goals": [],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 0},
    }
    _seed_two_aspirations(world, asp, container)

    status, body = _post(port, "/v1/aspirations/complete",
                         {"asp_id": "asp-001", "force": "true"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["aspiration"]["status"] == "completed"
    live = [json.loads(l) for l in (world / "aspirations.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    home = next(a for a in live if a["id"] == "asp-002")
    assert [g["id"] for g in home["goals"]] == ["g-001-01"], home["goals"]


def test_complete_not_found(running_daemon):
    """Unknown asp_id -> 404."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/complete",
                         {"asp_id": "asp-999"})
    assert status == 404
    resp = json.loads(body)
    assert resp["error"] == "aspiration_not_found"


def test_complete_missing_asp_id(running_daemon):
    """No asp_id -> 400."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/complete", {})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "missing_asp_id"


def test_complete_maturity_warning(running_daemon):
    """Completing a project-scope asp with 0 sessions -> maturity warning."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_all_completed()
    asp["scope"] = "project"
    asp["sessions_active"] = 0
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/complete",
                         {"asp_id": "asp-001"})
    assert status == 200
    resp = json.loads(body)
    warnings = resp.get("warnings") or []
    assert any("MATURITY WARNING" in w for w in warnings)


def test_complete_clears_stale_blockers(running_daemon):
    """After completing asp-001, blocked_by refs in asp-002 are cleaned."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp1 = _make_asp_all_completed("asp-001")
    asp2 = {
        "id": "asp-002", "title": "Other", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-002-01", "title": "Blocked one", "status": "blocked",
             "recurring": False, "blocked_by": ["g-001-01"],
             "blocked_since": "2026-01-01"},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_two_aspirations(world, asp1, asp2)

    status, body = _post(port, "/v1/aspirations/complete",
                         {"asp_id": "asp-001"})
    assert status == 200, f"Expected 200, got {status}: {body}"

    # asp-002 should have blocked_by cleared
    live = _read_jsonl(world / "aspirations.jsonl")
    assert len(live) == 1
    assert live[0]["id"] == "asp-002"
    goal = live[0]["goals"][0]
    assert goal["blocked_by"] == []
    assert goal["blocked_since"] is None


def test_complete_archive_written_before_live(running_daemon):
    """Archive file gets the aspiration even when live is emptied."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_all_completed()
    _seed_aspiration(world, asp)

    status, _ = _post(port, "/v1/aspirations/complete",
                      {"asp_id": "asp-001"})
    assert status == 200

    archive = _read_jsonl(world / "aspirations-archive.jsonl")
    assert len(archive) >= 1
    assert archive[-1]["id"] == "asp-001"
    assert archive[-1]["archived"] is True


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


def test_complete_intent_satisfied_happy(running_daemon):
    """Intent-satisfaction pathway: valid block -> completes with supersession."""
    project_root, port = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Intent test", "status": "active",
        "priority": "LOW", "archived": False, "scope": "sprint",
        "motivation": "Build a comprehensive testing framework for daemon endpoints",
        "goals": [
            {"id": "g-001-01", "title": "Done", "status": "completed",
             "recurring": False,
             "verification": {"outcomes": ["tests pass"]}},
            {"id": "g-001-02", "title": "Done2", "status": "completed",
             "recurring": False,
             "verification": {"outcomes": ["more tests"]}},
            {"id": "g-001-03", "title": "Leftover", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 2, "total_goals": 3, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    intent_block = {
        "evidence_goal_ids": ["g-001-01", "g-001-02"],
        "rationale": "The comprehensive testing framework is fully built and validated with daemon endpoint tests",
        "superseded_goal_ids": ["g-001-03"],
    }
    status, body = _post(
        port, "/v1/aspirations/complete",
        {"asp_id": "asp-001", "intent_satisfied": "true"},
        body=json.dumps(intent_block),
    )
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    completed_asp = resp["aspiration"]
    assert completed_asp["status"] == "completed"
    # Superseded goal should have been transitioned
    g3 = [g for g in completed_asp["goals"] if g["id"] == "g-001-03"][0]
    assert g3["status"] == "superseded"
    assert "intent_satisfaction" in completed_asp
    assert "claimed_at" in completed_asp["intent_satisfaction"]


def test_complete_intent_satisfied_validation_fails(running_daemon):
    """Intent-satisfaction with bad rationale -> 400."""
    project_root, port = running_daemon
    _ensure_intent_config(project_root)
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Intent fail", "status": "active",
        "priority": "LOW", "archived": False, "scope": "sprint",
        "motivation": "Build testing framework",
        "goals": [
            {"id": "g-001-01", "title": "Done", "status": "completed",
             "recurring": False,
             "verification": {"outcomes": ["tests pass"]}},
            {"id": "g-001-02", "title": "Done2", "status": "completed",
             "recurring": False,
             "verification": {"outcomes": ["more tests"]}},
        ],
        "progress": {"completed_goals": 2, "total_goals": 2, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    intent_block = {
        "evidence_goal_ids": ["g-001-01", "g-001-02"],
        "rationale": "too short",  # < 40 chars
        "superseded_goal_ids": [],
    }
    status, body = _post(
        port, "/v1/aspirations/complete",
        {"asp_id": "asp-001", "intent_satisfied": "true"},
        body=json.dumps(intent_block),
    )
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "intent_validation_failed"
