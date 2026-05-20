"""POST /v1/aspirations/recover-recurring endpoint tests (PR 53).

Mirrors cmd_recover_recurring behavior exactly:
  - no recovery needed (all clean) → recovered=0
  - Case 1: recurring=true + status=completed → reset to pending
  - Case 2: shape-recurring corrupted (recurring=false + interval_hours +
    lastAchievedAt + status=completed) → reset to pending
  - Case 3: recurring goal pointing to archived hypothesis → retired
  - combined: Case 1 + Case 2 in single sweep
  - skips terminal aspirations (completed/retired)
  - progress recomputed on affected aspirations
  - idempotency (two sweeps, second recovers 0)
  - source=agent reads/writes agent-local aspirations
  - invalid source → 400
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest


def _post(port: int, source: str = "world", agent: str = "alpha"):
    """POST /v1/aspirations/recover-recurring and return parsed JSON."""
    import urllib.request
    url = (f"http://127.0.0.1:{port}/v1/aspirations/recover-recurring"
           f"?source={source}")
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
    url = (f"http://127.0.0.1:{port}/v1/aspirations/recover-recurring"
           f"?source={source}")
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

def test_no_recovery_needed(running_daemon):
    """All aspirations clean, no corrupted recurring goals → recovered=0."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "pending"),
        ]),
    ])

    resp = _post(port)

    assert resp["recovered"] == 0
    assert resp["goals"] == []


def test_case1_recurring_completed(running_daemon):
    """Case 1: recurring=true + status=completed → reset to pending."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       recurring=True, interval_hours=24),
        ]),
    ])

    resp = _post(port)

    assert resp["recovered"] == 1
    assert resp["goals"][0]["pattern"] == "recurring-completed"
    assert resp["goals"][0]["goal"] == "g-001-01"

    live = _read_jsonl(world / "aspirations.jsonl")
    assert live[0]["goals"][0]["status"] == "pending"


def test_case2_shape_recurring_corrupted(running_daemon):
    """Case 2: recurring=false + interval_hours + lastAchievedAt +
    status=completed → reset to pending."""
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

    assert resp["recovered"] == 1
    assert resp["goals"][0]["pattern"] == "shape-recurring"

    live = _read_jsonl(world / "aspirations.jsonl")
    assert live[0]["goals"][0]["status"] == "pending"


def test_case3_hypothesis_archived(running_daemon):
    """Case 3: recurring goal pointing to archived hypothesis → retired."""
    project_root, port = running_daemon
    world = project_root / "world"

    # Write a pipeline record with stage=archived.
    _write_jsonl(world / "pipeline.jsonl", [
        {"id": "2026-01-01_test-hyp", "stage": "archived",
         "title": "Test hypothesis"},
    ])

    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "pending",
                       recurring=True, interval_hours=48,
                       hypothesis_id="2026-01-01_test-hyp"),
        ]),
    ])

    resp = _post(port)

    assert resp["recovered"] == 1
    assert resp["goals"][0]["pattern"] == "hypothesis-archived"
    assert resp["goals"][0]["hypothesis_id"] == "2026-01-01_test-hyp"

    live = _read_jsonl(world / "aspirations.jsonl")
    goal = live[0]["goals"][0]
    assert goal["status"] == "completed"
    assert goal["recurring"] is False
    assert "hypothesis 2026-01-01_test-hyp stage=archived" in goal["outcome_note"]
    assert goal.get("completed_at") is not None


def test_case3_from_pipeline_archive(running_daemon):
    """Case 3: archived hypothesis in pipeline-archive.jsonl (cold store)."""
    project_root, port = running_daemon
    world = project_root / "world"

    # Write to pipeline-archive.jsonl (cold archive) instead of pipeline.jsonl.
    _write_jsonl(world / "pipeline-archive.jsonl", [
        {"id": "2026-02-01_cold-hyp", "stage": "archived",
         "title": "Cold hypothesis"},
    ])

    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "pending",
                       recurring=True, interval_hours=48,
                       hypothesis_id="2026-02-01_cold-hyp"),
        ]),
    ])

    resp = _post(port)

    assert resp["recovered"] == 1
    assert resp["goals"][0]["pattern"] == "hypothesis-archived"


def test_combined_case1_and_case2(running_daemon):
    """Both Case 1 and Case 2 recoveries in a single sweep."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       recurring=True, interval_hours=24),
            _make_goal("g-001-02", "completed",
                       recurring=False, interval_hours=48,
                       lastAchievedAt="2026-05-01T00:00:00"),
        ]),
    ])

    resp = _post(port)

    assert resp["recovered"] == 2

    patterns = {g["pattern"] for g in resp["goals"]}
    assert patterns == {"recurring-completed", "shape-recurring"}

    live = _read_jsonl(world / "aspirations.jsonl")
    for g in live[0]["goals"]:
        assert g["status"] == "pending"


def test_skips_terminal_aspirations(running_daemon):
    """Completed/retired aspirations are skipped — archive-sweep owns those."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "completed", goals=[
            _make_goal("g-001-01", "completed",
                       recurring=True, interval_hours=24),
        ]),
        _make_asp("asp-002", "retired", goals=[
            _make_goal("g-002-01", "completed",
                       recurring=True, interval_hours=24),
        ]),
    ])

    resp = _post(port)

    assert resp["recovered"] == 0

    # Aspirations unchanged.
    live = _read_jsonl(world / "aspirations.jsonl")
    assert live[0]["goals"][0]["status"] == "completed"
    assert live[1]["goals"][0]["status"] == "completed"


def test_progress_recomputed(running_daemon):
    """Progress is recomputed on affected aspirations."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       recurring=True, interval_hours=24),
            _make_goal("g-001-02", "completed"),
        ]),
    ])

    resp = _post(port)
    assert resp["recovered"] == 1

    live = _read_jsonl(world / "aspirations.jsonl")
    progress = live[0]["progress"]
    #  is recurring (excluded from counts),  is completed non-recurring.
    assert progress["completed_goals"] == 1
    assert progress["total_goals"] == 1
    assert progress["recurring_goals"] == 1


def test_idempotency(running_daemon):
    """Two consecutive sweeps — second recovers nothing."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       recurring=True, interval_hours=24),
        ]),
    ])

    resp1 = _post(port)
    assert resp1["recovered"] == 1

    resp2 = _post(port)
    assert resp2["recovered"] == 0


def test_source_agent(running_daemon):
    """source=agent reads/writes agent-local aspirations."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _write_jsonl(agent_dir / "aspirations.jsonl", [
        _make_asp("asp-100", "active", goals=[
            _make_goal("g-100-01", "completed",
                       recurring=True, interval_hours=24),
        ]),
    ])

    resp = _post(port, source="agent")

    assert resp["recovered"] == 1

    live = _read_jsonl(agent_dir / "aspirations.jsonl")
    assert live[0]["goals"][0]["status"] == "pending"


def test_invalid_source(running_daemon):
    """source=invalid → 400."""
    _, port = running_daemon
    status, body = _post_raw(port, source="invalid")
    assert status == 400
    assert body["error"] == "invalid_source"
