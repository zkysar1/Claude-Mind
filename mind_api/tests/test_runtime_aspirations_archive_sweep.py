"""POST /v1/aspirations/archive-sweep endpoint tests (PR 9d).

Mirrors cmd_archive_sweep behavior exactly:
  - completed/retired aspirations with no issues → archived
  - recurring-goal recovery (completed/retired + recurring goals → active)
  - unfinished-goal recovery (completed + non-terminal goals → active)
  - corrupted-recurring recovery (active + recurring goals with status=completed)
  - shape-recurring-corrupted recovery (recurring=false but interval_hours+lastAchievedAt)
  - stale-blocker cleanup on remaining goals
  - archive-append preserves existing archive records
  - idempotency (sweep with nothing to archive → 0)
  - recovery-only write (no archive, but recovered → live file updated)
  - source=agent
  - invalid source → 400
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest


def _post(port: int, source: str = "world", agent: str = "alpha"):
    """POST /v1/aspirations/archive-sweep and return parsed JSON."""
    import urllib.request
    url = f"http://127.0.0.1:{port}/v1/aspirations/archive-sweep?source={source}"
    req = urllib.request.Request(
        url, method="POST",
        headers={
            "X-Mind-Agent": agent,
            "X-Runtime-Client": "test",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _post_raw(port: int, source: str = "world", agent: str = "alpha"):
    """POST and return (status_code, body_dict)."""
    import urllib.request
    import urllib.error
    url = f"http://127.0.0.1:{port}/v1/aspirations/archive-sweep?source={source}"
    req = urllib.request.Request(
        url, method="POST",
        headers={
            "X-Mind-Agent": agent,
            "X-Runtime-Client": "test",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def _write_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=True) + "\n")


def _make_asp(asp_id: str, status: str = "active", goals=None) -> Dict[str, Any]:
    return {
        "id": asp_id,
        "title": f"Test {asp_id}",
        "status": status,
        "priority": "LOW",
        "archived": status in ("completed", "retired"),
        "goals": goals or [],
        "progress": {"completed_goals": 0, "total_goals": 0},
    }


def _make_goal(goal_id: str, status: str = "completed", **kwargs) -> Dict[str, Any]:
    g = {"id": goal_id, "title": f"Goal {goal_id}", "status": status}
    g.update(kwargs)
    return g


# ---- Tests ----

def test_empty_sweep(running_daemon):
    """No completed/retired aspirations → archived_count=0."""
    project_root, port = running_daemon
    resp = _post(port)
    assert resp["ok"] is True
    assert resp["archived_count"] == 0
    assert resp["recovered"] == 0


def test_single_completed_archived(running_daemon):
    """One completed aspiration with only terminal goals → archived."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp("asp-001", "completed", goals=[
        _make_goal("g-001-01", "completed"),
    ])
    _write_jsonl(world / "aspirations.jsonl", [asp])

    resp = _post(port)
    assert resp["ok"] is True
    assert resp["archived_count"] == 1

    live = _read_jsonl(world / "aspirations.jsonl")
    assert len(live) == 0

    archive = _read_jsonl(world / "aspirations-archive.jsonl")
    assert len(archive) == 1
    assert archive[0]["id"] == "asp-001"


def test_multi_record_sweep(running_daemon):
    """Two completed + one active → two archived, one remains."""
    project_root, port = running_daemon
    world = project_root / "world"
    items = [
        _make_asp("asp-001", "completed", goals=[
            _make_goal("g-001-01", "completed"),
        ]),
        _make_asp("asp-002", "active", goals=[
            _make_goal("g-002-01", "pending"),
        ]),
        _make_asp("asp-003", "retired"),
    ]
    _write_jsonl(world / "aspirations.jsonl", items)

    resp = _post(port)
    assert resp["archived_count"] == 2

    live = _read_jsonl(world / "aspirations.jsonl")
    assert len(live) == 1
    assert live[0]["id"] == "asp-002"

    archive = _read_jsonl(world / "aspirations-archive.jsonl")
    assert len(archive) == 2


def test_retired_swept(running_daemon):
    """Retired aspiration → archived."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "retired"),
    ])

    resp = _post(port)
    assert resp["archived_count"] == 1

    archive = _read_jsonl(world / "aspirations-archive.jsonl")
    assert len(archive) == 1
    assert archive[0]["status"] == "retired"


def test_archive_append_preserves_existing(running_daemon):
    """Archive file already has records → new ones appended, old preserved."""
    project_root, port = running_daemon
    world = project_root / "world"

    existing = _make_asp("asp-099", "completed")
    _write_jsonl(world / "aspirations-archive.jsonl", [existing])

    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "completed", goals=[
            _make_goal("g-001-01", "completed"),
        ]),
    ])

    resp = _post(port)
    assert resp["archived_count"] == 1

    archive = _read_jsonl(world / "aspirations-archive.jsonl")
    assert len(archive) == 2
    ids = {a["id"] for a in archive}
    assert ids == {"asp-099", "asp-001"}


def test_recurring_goal_recovery(running_daemon):
    """Completed aspiration with recurring goal → recovered to active."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "completed", goals=[
            _make_goal("g-001-01", "completed", recurring=True, interval_hours=24),
        ]),
    ])

    resp = _post(port)
    assert resp["archived_count"] == 0
    assert resp["recovered"] == 1
    assert resp["warnings"] is not None
    assert any("Recovering asp-001" in w for w in resp["warnings"])

    live = _read_jsonl(world / "aspirations.jsonl")
    assert len(live) == 1
    assert live[0]["status"] == "active"
    assert live[0]["archived"] is False
    assert live[0]["goals"][0]["status"] == "pending"


def test_unfinished_goal_recovery(running_daemon):
    """Completed aspiration with a pending (non-terminal) goal → recovered."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "completed", goals=[
            _make_goal("g-001-01", "completed"),
            _make_goal("g-001-02", "pending"),
        ]),
    ])

    resp = _post(port)
    assert resp["archived_count"] == 0
    assert resp["recovered"] == 1
    assert any("unfinished" in w for w in (resp["warnings"] or []))

    live = _read_jsonl(world / "aspirations.jsonl")
    assert live[0]["status"] == "active"


def test_corrupted_recurring_recovery(running_daemon):
    """Active aspiration with recurring goal at status=completed → reset to pending."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed", recurring=True, interval_hours=24),
        ]),
    ])

    resp = _post(port)
    assert resp["archived_count"] == 0
    assert resp["recovered"] == 1

    live = _read_jsonl(world / "aspirations.jsonl")
    assert live[0]["goals"][0]["status"] == "pending"


def test_shape_recurring_corrupted_recovery(running_daemon):
    """Active aspiration, goal with recurring=false but interval_hours+lastAchievedAt
    and status=completed → reset to pending."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       recurring=False, interval_hours=24,
                       lastAchievedAt="2026-05-01T00:00:00"),
        ]),
    ])

    resp = _post(port)
    assert resp["archived_count"] == 0
    assert resp["recovered"] == 1
    assert any("shape-recurring" in w for w in (resp["warnings"] or []))

    live = _read_jsonl(world / "aspirations.jsonl")
    assert live[0]["goals"][0]["status"] == "pending"


def test_stale_blocker_cleanup(running_daemon):
    """After archiving, blocked_by refs to archived goals are cleared."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "completed", goals=[
            _make_goal("g-001-01", "completed"),
        ]),
        _make_asp("asp-002", "active", goals=[
            _make_goal("g-002-01", "pending",
                       blocked_by=["g-001-01"],
                       blocked_since="2026-05-01T00:00:00"),
        ]),
    ])

    resp = _post(port)
    assert resp["archived_count"] == 1

    live = _read_jsonl(world / "aspirations.jsonl")
    assert len(live) == 1
    remaining_goal = live[0]["goals"][0]
    assert remaining_goal["blocked_by"] == []
    assert remaining_goal["blocked_since"] is None


def test_idempotency(running_daemon):
    """Two consecutive sweeps with nothing to archive → both return 0."""
    project_root, port = running_daemon

    resp1 = _post(port)
    assert resp1["archived_count"] == 0
    resp2 = _post(port)
    assert resp2["archived_count"] == 0


def test_recovery_only_write(running_daemon):
    """Nothing to archive but recovery happened → live file updated."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed", recurring=True, interval_hours=24),
        ]),
    ])

    resp = _post(port)
    assert resp["archived_count"] == 0
    assert resp["recovered"] == 1

    live = _read_jsonl(world / "aspirations.jsonl")
    assert live[0]["goals"][0]["status"] == "pending"


def test_source_agent(running_daemon):
    """source=agent reads/writes agent-local aspirations."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _write_jsonl(agent_dir / "aspirations.jsonl", [
        _make_asp("asp-100", "completed", goals=[
            _make_goal("g-100-01", "completed"),
        ]),
    ])

    resp = _post(port, source="agent")
    assert resp["archived_count"] == 1

    live = _read_jsonl(agent_dir / "aspirations.jsonl")
    assert len(live) == 0

    archive = _read_jsonl(agent_dir / "aspirations-archive.jsonl")
    assert len(archive) == 1


def test_invalid_source(running_daemon):
    """source=invalid → 400."""
    _, port = running_daemon
    status, body = _post_raw(port, source="invalid")
    assert status == 400
    assert body["error"] == "invalid_source"
