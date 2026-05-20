"""Daemon aspirations-complete-by endpoint tests (PR 9c).

Covers:
  - Happy path: complete a non-recurring goal with agent attribution
  - Recurring goal cycles back to pending with updated tracking fields
  - Missing goal_id returns 400
  - Unknown goal returns 404
  - Agent name from query param vs X-Mind-Agent header fallback
  - --key-finding cross-writes to team-state.yaml recent_completions[]
  - --key-finding failure is non-fatal (warnings surfaced, goal still closes)
  - Stale blockers cleared from remaining aspirations
  - Streak reset on overdue recurring goal
  - completed_at + completed_date stamped on non-recurring completion
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import yaml


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


def _make_asp_with_pending_goal(asp_id="asp-001"):
    return {
        "id": asp_id,
        "title": "Test",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "goals": [
            {"id": f"g-{asp_id[4:]}-01", "title": "Goal to complete",
             "status": "in-progress", "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }


def _make_asp_with_recurring_goal(asp_id="asp-001"):
    return {
        "id": asp_id,
        "title": "Test recurring",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "goals": [
            {"id": f"g-{asp_id[4:]}-01", "title": "Recurring goal",
             "status": "in-progress", "recurring": True,
             "interval_hours": 24, "achievedCount": 3,
             "currentStreak": 2, "longestStreak": 5,
             # Relative, not a fixed date: the endpoint resets currentStreak
             # to 1 when elapsed > 2x interval_hours (here 48h). A hardcoded
             # past date silently crosses that boundary as wall-clock
             # advances, flipping this on-time-cycle fixture into the
             # overdue-reset path (the original "2026-05-13T10:00:00" began
             # failing test_complete_by_recurring_cycle once it aged >48h).
             "lastAchievedAt": (datetime.now() - timedelta(hours=1)).isoformat(),
             "windowStreak": 2, "longestWindowStreak": 4},
        ],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 1},
    }


# ---- Tests ----

def test_complete_by_happy_path(running_daemon):
    """Non-recurring goal -> completed with agent attribution."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_with_pending_goal()
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/complete-by",
                         {"goal_id": "g-001-01", "source": "world",
                          "agent_name": "bravo"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["ok"] is True
    goal = resp["goal"]
    assert goal["status"] == "completed"
    assert goal["completed_by"] == "bravo"
    assert "completed_at" in goal
    assert "completed_date" in goal
    # claimed_by/claimed_at should be cleared
    assert "claimed_by" not in goal
    assert "claimed_at" not in goal


def test_complete_by_recurring_cycle(running_daemon):
    """Recurring goal cycles back to pending with updated tracking."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_with_recurring_goal()
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/complete-by",
                         {"goal_id": "g-001-01", "agent_name": "alpha"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    goal = resp["goal"]
    assert goal["status"] == "pending"
    assert goal["completed_by"] == "alpha"
    assert goal["achievedCount"] == 4
    assert goal["currentStreak"] == 3
    assert "lastAchievedAt" in goal
    # claimed_by/claimed_at cleared on recurring cycle
    assert "claimed_by" not in goal
    assert "claimed_at" not in goal


def test_complete_by_missing_goal_id(running_daemon):
    """No goal_id -> 400."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/complete-by", {})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "missing_goal_id"


def test_complete_by_goal_not_found(running_daemon):
    """Unknown goal_id -> 404."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/complete-by",
                         {"goal_id": "g-999-01"})
    assert status == 404
    resp = json.loads(body)
    assert resp["error"] == "goal_not_found"


def test_complete_by_agent_from_header(running_daemon):
    """Agent defaults to X-Mind-Agent when agent_name not in query."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_with_pending_goal()
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/complete-by",
                         {"goal_id": "g-001-01"}, agent="zeta")
    assert status == 200
    goal = json.loads(body)["goal"]
    assert goal["completed_by"] == "zeta"


def test_complete_by_agent_query_overrides_header(running_daemon):
    """Explicit agent_name in query takes precedence over header."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_with_pending_goal()
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/complete-by",
                         {"goal_id": "g-001-01", "agent_name": "bravo"},
                         agent="alpha")
    assert status == 200
    goal = json.loads(body)["goal"]
    assert goal["completed_by"] == "bravo"


def test_complete_by_key_finding_cross_write(running_daemon):
    """--key-finding appends to team-state.yaml recent_completions[]."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_with_pending_goal()
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/complete-by",
                         {"goal_id": "g-001-01", "agent_name": "alpha",
                          "key_finding": "Discovered X improves Y"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["ok"] is True
    # warnings should be None (no team-state failure)
    assert resp.get("warnings") is None

    # Verify team-state.yaml was updated
    ts_path = world / "team-state.yaml"
    with open(ts_path, "r", encoding="utf-8") as f:
        ts = yaml.safe_load(f)
    completions = ts.get("recent_completions", [])
    assert len(completions) == 1
    assert completions[0]["goal_id"] == "g-001-01"
    assert completions[0]["completed_by"] == "alpha"
    assert completions[0]["key_finding"] == "Discovered X improves Y"
    assert "completed_at" in completions[0]


def test_complete_by_no_key_finding_skips_team_state(running_daemon):
    """Without --key-finding, team-state is not touched."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_with_pending_goal()
    _seed_aspiration(world, asp)

    # Read team-state before
    ts_path = world / "team-state.yaml"
    with open(ts_path, "r", encoding="utf-8") as f:
        ts_before = yaml.safe_load(f)

    status, _ = _post(port, "/v1/aspirations/complete-by",
                       {"goal_id": "g-001-01", "agent_name": "alpha"})
    assert status == 200

    # recent_completions should be unchanged
    with open(ts_path, "r", encoding="utf-8") as f:
        ts_after = yaml.safe_load(f)
    assert ts_after.get("recent_completions") == ts_before.get("recent_completions")


def test_complete_by_clears_stale_blockers(running_daemon):
    """Completing a goal clears blocked_by refs in other aspirations."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp1 = _make_asp_with_pending_goal("asp-001")
    asp2 = {
        "id": "asp-002", "title": "Other", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-002-01", "title": "Blocked", "status": "blocked",
             "recurring": False, "blocked_by": ["g-001-01"],
             "blocked_since": "2026-01-01"},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_two_aspirations(world, asp1, asp2)

    status, _ = _post(port, "/v1/aspirations/complete-by",
                       {"goal_id": "g-001-01", "agent_name": "alpha"})
    assert status == 200

    live = _read_jsonl(world / "aspirations.jsonl")
    asp2_live = [a for a in live if a["id"] == "asp-002"][0]
    blocked_goal = asp2_live["goals"][0]
    assert blocked_goal["blocked_by"] == []
    assert blocked_goal["blocked_since"] is None


def test_complete_by_recurring_streak_reset(running_daemon):
    """Overdue recurring goal (>2x interval) resets streak to 1."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_with_recurring_goal()
    # Set lastAchievedAt far in the past (>48h ago for 24h interval)
    asp["goals"][0]["lastAchievedAt"] = "2026-05-10T00:00:00"
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/complete-by",
                         {"goal_id": "g-001-01", "agent_name": "alpha"})
    assert status == 200
    goal = json.loads(body)["goal"]
    assert goal["currentStreak"] == 1  # reset due to >2x interval
    assert goal["achievedCount"] == 4


def test_complete_by_progress_recomputed(running_daemon):
    """Progress is recomputed after goal completion."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Multi-goal", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "To complete", "status": "in-progress",
             "recurring": False},
            {"id": "g-001-02", "title": "Already done", "status": "completed",
             "recurring": False},
        ],
        "progress": {"completed_goals": 1, "total_goals": 2, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    status, _ = _post(port, "/v1/aspirations/complete-by",
                       {"goal_id": "g-001-01", "agent_name": "alpha"})
    assert status == 200

    live = _read_jsonl(world / "aspirations.jsonl")
    assert live[0]["progress"]["completed_goals"] == 2
    assert live[0]["progress"]["total_goals"] == 2
