"""Tests for phantom-goal-audit.py ().

Verifies the ALL-null-provenance phantom signature, the legacy-goal
false-positive exclusion (partial provenance), and the rb-245 schema
verification gate.
"""
import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "phantom_goal_audit", Path(__file__).resolve().parents[1] / "phantom-goal-audit.py")
pga = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pga)


def _write_world(tmp_path, aspirations):
    (tmp_path / "aspirations.jsonl").write_text(
        "\n".join(json.dumps(a) for a in aspirations), encoding="utf-8")
    return str(tmp_path)


def test_phantom_all_null_detected(tmp_path):
    world = _write_world(tmp_path, [
        {"id": "asp-1", "goals": [
            {"id": "g-1-1", "title": "normal", "status": "pending",
             "created_at": "2026-07-19T00:00:00", "filed_by_agent": "bravo", "goal_source": "user"},
            {"id": "g-1-2", "title": "phantom", "status": "pending",
             "created_at": None, "filed_by_agent": None, "goal_source": None},
        ]},
    ])
    r = pga.audit(world_dir=world)
    assert r["schema_verified"] is True
    assert [p["goal_id"] for p in r["phantoms"]] == ["g-1-2"]
    assert r["live_phantoms"] == 1
    assert "phantom_goals_found" in r["flags"]


def test_legacy_partial_provenance_not_phantom(tmp_path):
    world = _write_world(tmp_path, [
        {"id": "asp-1", "goals": [
            {"id": "g-1-1", "title": "normal", "status": "completed",
             "created_at": "2026-07-19T00:00:00", "filed_by_agent": "bravo", "goal_source": "agent-self"},
            # legacy: created_at null but goal_source present -> NOT a phantom
            {"id": "g-1-2", "title": "legacy", "status": "completed",
             "created_at": None, "goal_source": "agent-self"},
        ]},
    ])
    r = pga.audit(world_dir=world)
    assert r["phantoms"] == []
    assert r["legacy_null_created_at"] == 1
    assert r["flags"] == []


def test_rb245_schema_unverified_when_no_created_at(tmp_path):
    # NO goal carries created_at -> field renamed? abort, do NOT flag every goal.
    world = _write_world(tmp_path, [
        {"id": "asp-1", "goals": [
            {"id": "g-1-1", "title": "a", "status": "pending", "goal_source": "user"},
            {"id": "g-1-2", "title": "b", "status": "pending", "filed_by_agent": "x"},
        ]},
    ])
    r = pga.audit(world_dir=world)
    assert r["schema_verified"] is False
    assert "schema_unverified" in r["flags"]
    assert r["phantoms"] == []


def test_empty_world(tmp_path):
    world = _write_world(tmp_path, [])
    r = pga.audit(world_dir=world)
    assert r["scanned"] == 0
    assert r["phantoms"] == []


def test_live_vs_terminal_phantom_severity(tmp_path):
    world = _write_world(tmp_path, [
        {"id": "asp-1", "goals": [
            {"id": "g-1-0", "title": "anchor", "status": "completed",
             "created_at": "2026-07-19T00:00:00", "filed_by_agent": "bravo", "goal_source": "user"},
            {"id": "g-1-1", "title": "live-phantom", "status": "pending",
             "created_at": None, "filed_by_agent": None, "goal_source": None},
            {"id": "g-1-2", "title": "terminal-phantom", "status": "completed",
             "created_at": None, "filed_by_agent": None, "goal_source": None},
        ]},
    ])
    r = pga.audit(world_dir=world)
    assert len(r["phantoms"]) == 2
    assert r["live_phantoms"] == 1  # only the pending one counts as live
