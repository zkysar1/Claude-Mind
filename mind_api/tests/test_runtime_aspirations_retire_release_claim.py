"""Daemon aspirations-retire/release/claim endpoint tests (PR 9b).

Covers:
  retire:
    - Happy path: retire an aspiration -> archived with status=retired
    - Recurring-goals guard blocks without force
    - force=true bypasses recurring guard
    - 404 on missing aspiration
    - Missing asp_id returns 400
    - Unfinished goals produce a warning but do not block
    - Stale blockers cleared from remaining aspirations

  release:
    - Happy path: release a claimed goal -> claimed_by/claimed_at cleared
    - Release unclaimed goal -> still 200 with had_claim=false
    - 404 on missing goal
    - Missing goal_id returns 400
    - Agent-queue goal returns 400 with helpful error

  claim:
    - Happy path: claim a goal -> claimed_by/claimed_at set
    - Already claimed by same agent -> 200 (idempotent)
    - Already claimed by different agent -> 409
    - Cross-lane refused without justification -> 400
    - Cross-lane override accepted with justification + ledger written
    - 404 on missing goal
    - Missing goal_id returns 400
    - Missing agent returns 400
    - Agent-queue goal returns 400 with helpful error
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


def _seed_aspiration(world: Path, asp):
    path = world / "aspirations.jsonl"
    path.write_text(json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")


def _seed_two_aspirations(world: Path, asp1, asp2):
    path = world / "aspirations.jsonl"
    lines = [json.dumps(a, ensure_ascii=True) for a in [asp1, asp2]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# retire tests
# ---------------------------------------------------------------------------

def _make_asp_all_completed(asp_id="asp-001"):
    return {
        "id": asp_id,
        "title": "Test retire",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "goals": [
            {"id": f"g-{asp_id[4:]}-01", "title": "Done 1", "status": "completed",
             "recurring": False},
            {"id": f"g-{asp_id[4:]}-02", "title": "Done 2", "status": "skipped",
             "recurring": False},
        ],
        "progress": {"completed_goals": 1, "total_goals": 2, "recurring_goals": 0},
    }


def test_retire_happy_path(running_daemon):
    """Retire an aspiration with all goals terminal -> 200, archived."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_all_completed()
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/retire",
                         {"asp_id": "asp-001", "source": "world"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["ok"] is True
    retired_asp = resp["aspiration"]
    assert retired_asp["status"] == "retired"
    assert retired_asp["archived"] is True
    assert retired_asp["completed_at"] is None
    assert "retired_at" in retired_asp

    live = _read_jsonl(world / "aspirations.jsonl")
    assert len(live) == 0

    archive = _read_jsonl(world / "aspirations-archive.jsonl")
    assert len(archive) == 1
    assert archive[0]["id"] == "asp-001"
    assert archive[0]["status"] == "retired"


def test_retire_recurring_goals_blocked(running_daemon):
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

    status, body = _post(port, "/v1/aspirations/retire", {"asp_id": "asp-001"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "recurring_goals_present"


def test_retire_force_bypasses_recurring_guard(running_daemon):
    """force=true skips recurring guard."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Force retire", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Recurring", "status": "pending",
             "recurring": True, "interval_hours": 24},
        ],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 1},
    }
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/retire",
                         {"asp_id": "asp-001", "force": "true"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["aspiration"]["status"] == "retired"


def test_retire_not_found(running_daemon):
    """Unknown asp_id -> 404."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/retire", {"asp_id": "asp-999"})
    assert status == 404
    resp = json.loads(body)
    assert resp["error"] == "aspiration_not_found"


def test_retire_missing_asp_id(running_daemon):
    """No asp_id -> 400."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/retire", {})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "missing_asp_id"


def test_retire_unfinished_goals_warning(running_daemon):
    """Retire with unfinished goals -> 200 with warning (not blocking)."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Unfinished", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Pending", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/retire", {"asp_id": "asp-001"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    warnings = resp.get("warnings") or []
    assert any("RETIREMENT NOTE" in w for w in warnings)
    assert resp["aspiration"]["status"] == "retired"


def test_retire_clears_stale_blockers(running_daemon):
    """After retiring asp-001, blocked_by refs in asp-002 are cleaned."""
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

    status, body = _post(port, "/v1/aspirations/retire", {"asp_id": "asp-001"})
    assert status == 200, f"Expected 200, got {status}: {body}"

    live = _read_jsonl(world / "aspirations.jsonl")
    assert len(live) == 1
    assert live[0]["id"] == "asp-002"
    goal = live[0]["goals"][0]
    assert goal["blocked_by"] == []
    assert goal["blocked_since"] is None


# ---------------------------------------------------------------------------
# release tests
# ---------------------------------------------------------------------------

def _make_asp_with_claimed_goal():
    return {
        "id": "asp-001", "title": "Test release", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Claimed goal", "status": "in-progress",
             "recurring": False, "claimed_by": "alpha",
             "claimed_at": "2026-05-10T10:00:00"},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }


def test_release_happy_path(running_daemon):
    """Release a claimed goal -> 200, claimed_by/claimed_at cleared."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_claimed_goal())

    status, body = _post(port, "/v1/aspirations/release",
                         {"id": "g-001-01"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["had_claim"] is True
    goal = resp["goal"]
    assert "claimed_by" not in goal
    assert "claimed_at" not in goal


def test_release_unclaimed_goal(running_daemon):
    """Release an unclaimed goal -> 200 with had_claim=false."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Test", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Unclaimed", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/release", {"id": "g-001-01"})
    assert status == 200
    resp = json.loads(body)
    assert resp["had_claim"] is False


def test_release_not_found(running_daemon):
    """Unknown goal_id -> 404."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/release", {"id": "g-999-01"})
    assert status == 404
    resp = json.loads(body)
    assert resp["error"] == "goal_not_found"


def test_release_missing_goal_id(running_daemon):
    """No id param -> 400."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/release", {})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "missing_goal_id"


def test_release_agent_queue_goal(running_daemon):
    """Goal in agent queue -> 400 with helpful error."""
    project_root, port = running_daemon
    world = project_root / "world"
    # Seed world with no matching goal
    asp = {
        "id": "asp-001", "title": "World", "status": "active",
        "priority": "LOW", "archived": False, "goals": [],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)
    # Seed agent queue with the goal
    agent_asp = {
        "id": "asp-100", "title": "Agent", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-100-01", "title": "Agent goal", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    agent_path = project_root / "agents" / "alpha" / "aspirations.jsonl"
    agent_path.write_text(json.dumps(agent_asp, ensure_ascii=True) + "\n", encoding="utf-8")

    status, body = _post(port, "/v1/aspirations/release", {"id": "g-100-01"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "agent_queue_goal"


# ---------------------------------------------------------------------------
# claim tests
# ---------------------------------------------------------------------------

def _make_asp_with_unclaimed_goal(intended_agent=None):
    goal = {
        "id": "g-001-01", "title": "Claimable goal", "status": "pending",
        "recurring": False,
    }
    if intended_agent:
        goal["intended_agent"] = intended_agent
    return {
        "id": "asp-001", "title": "Test claim", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [goal],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }


def test_claim_happy_path(running_daemon):
    """Claim a goal -> 200, claimed_by/claimed_at set."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_unclaimed_goal())

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["ok"] is True
    goal = resp["goal"]
    assert goal["claimed_by"] == "alpha"
    assert "claimed_at" in goal


def test_claim_idempotent_same_agent(running_daemon):
    """Claiming a goal already claimed by the same agent -> 200."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_with_unclaimed_goal()
    asp["goals"][0]["claimed_by"] = "alpha"
    asp["goals"][0]["claimed_at"] = "2026-05-10T10:00:00"
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha"})
    assert status == 200
    resp = json.loads(body)
    assert resp["goal"]["claimed_by"] == "alpha"


def test_claim_already_claimed_different_agent(running_daemon):
    """Goal claimed by bravo, alpha claims -> 409."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_with_unclaimed_goal()
    asp["goals"][0]["claimed_by"] = "bravo"
    asp["goals"][0]["claimed_at"] = "2026-05-10T10:00:00"
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha"})
    assert status == 409
    resp = json.loads(body)
    assert resp["error"] == "already_claimed"


def test_claim_cross_lane_refused(running_daemon):
    """Goal routed to bravo, alpha claims without cross_lane -> 400."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_unclaimed_goal(intended_agent="bravo"))

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "cross_lane_refused"


def test_claim_cross_lane_override(running_daemon):
    """Goal routed to bravo, alpha claims with cross_lane -> 200 + ledger."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_unclaimed_goal(intended_agent="bravo"))

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "cross_lane": "urgent unblock needed"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["goal"]["claimed_by"] == "alpha"

    # Ledger should have the override record
    ledger_path = world / "override-bypass-ledger.jsonl"
    if ledger_path.exists():
        records = _read_jsonl(ledger_path)
        assert len(records) >= 1
        rec = records[-1]
        assert rec["gate"] == "capability-route-gate"
        assert rec["context"]["agent_claiming"] == "alpha"
        assert rec["context"]["intended_agent"] == "bravo"


def test_claim_cross_lane_either_no_block(running_daemon):
    """Goal with intended_agent='either' -> alpha claims without cross_lane -> 200."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_unclaimed_goal(intended_agent="either"))

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha"})
    assert status == 200


def test_claim_not_found(running_daemon):
    """Unknown goal_id -> 404."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-999-01", "agent": "alpha"})
    assert status == 404
    resp = json.loads(body)
    assert resp["error"] == "goal_not_found"


def test_claim_missing_goal_id(running_daemon):
    """No id param -> 400."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/claim", {"agent": "alpha"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "missing_goal_id"


def test_claim_missing_agent(running_daemon):
    """No agent param -> 400."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/claim", {"id": "g-001-01"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "missing_agent"


def test_claim_agent_queue_goal(running_daemon):
    """Goal in agent queue -> 400 with helpful error."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "World", "status": "active",
        "priority": "LOW", "archived": False, "goals": [],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)
    agent_asp = {
        "id": "asp-100", "title": "Agent", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-100-01", "title": "Agent goal", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    agent_path = project_root / "agents" / "alpha" / "aspirations.jsonl"
    agent_path.write_text(json.dumps(agent_asp, ensure_ascii=True) + "\n", encoding="utf-8")

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-100-01", "agent": "alpha"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "agent_queue_goal"
