"""aspirations-move-goals.py — move goals between aspiration records across stores.

The data repair for goals misfiled by the cross-store id collision: twelve agent goals filed
into the world's unrelated aspiration because ``add-goal asp-002`` resolved the world record
first. The guards that matter: ids renumber into the target family (continuing after its
highest), references among the moved goals follow the map, progress is recomputed on both
records with the daemon's formula, the target is written BEFORE the source, and dry-run
writes nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "aspirations-move-goals.py"


def _run(*args: str) -> tuple[int, dict]:
    env = os.environ.copy()
    env["STORAGE_BACKEND"] = "local"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"raw": proc.stdout, "stderr": proc.stderr}
    return proc.returncode, payload


def _write(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _stores(tmp_path: Path) -> tuple[Path, Path]:
    world = tmp_path / "world.jsonl"
    agent = tmp_path / "agent.jsonl"
    _write(world, [
        {"id": "asp-002", "title": "Operating Rhythm", "goals": [
            {"id": "g-002-01", "title": "Sprint planning", "status": "pending", "recurring": True},
            {"id": "g-002-03", "title": "Research API", "status": "pending",
             "aspiration": "asp-002"},
            {"id": "g-002-04", "title": "Research alternatives", "status": "completed",
             "dependencies": ["g-002-03"], "description": "after g-002-03; see g-002-01"},
        ]},
    ])
    _write(agent, [
        {"id": "asp-004", "title": "Build data infrastructure", "initial_goal_count": 2,
         "goals": [
             {"id": "g-004-01", "title": "first", "status": "completed"},
             {"id": "g-004-11", "title": "pipeline", "status": "completed"},
         ]},
    ])
    return world, agent


def _args(world: Path, agent: Path, *goals: str) -> list[str]:
    out = ["--from-file", str(world), "--from-asp", "asp-002",
           "--to-file", str(agent), "--to-asp", "asp-004"]
    for g in goals:
        out += ["--goal", g]
    return out


def test_dry_run_reports_the_map_and_progress_and_writes_nothing(tmp_path: Path) -> None:
    world, agent = _stores(tmp_path)
    before = (world.read_text(), agent.read_text())
    rc, report = _run(*_args(world, agent, "g-002-03", "g-002-04"))
    assert rc == 0, report
    assert report["applied"] is False
    assert report["id_map"] == {"g-002-03": "g-004-12", "g-002-04": "g-004-13"}
    # Progress with the daemon's formula: recurring goals never count.
    assert report["from"]["progress_after"]["total_goals"] == 0
    assert report["from"]["progress_after"]["recurring_goals"] == 1
    assert report["to"]["progress_after"]["total_goals"] == 4
    assert report["to"]["progress_after"]["completed_goals"] == 3
    assert report["to"]["progress_after"]["fan_out_ratio"] == 2.0
    assert (world.read_text(), agent.read_text()) == before


def test_apply_moves_renumbers_and_rewrites_references(tmp_path: Path) -> None:
    world, agent = _stores(tmp_path)
    scan = tmp_path / "wm.yaml"
    scan.write_text("current: g-002-04\n", encoding="utf-8")
    rc, report = _run(*_args(world, agent, "g-002-03", "g-002-04"), "--scan", str(scan), "--apply")
    assert rc == 0, report
    assert report["applied"] is True and report["source_changed"] == []
    assert report["scan_hits"] == {str(scan): [1]}
    (src,) = _read(world)
    assert [g["id"] for g in src["goals"]] == ["g-002-01"]
    assert src["progress"] == {"completed_goals": 0, "total_goals": 0, "recurring_goals": 1,
                               "fan_out_ratio": None}
    (dst,) = _read(agent)
    ids = [g["id"] for g in dst["goals"]]
    assert ids == ["g-004-01", "g-004-11", "g-004-12", "g-004-13"]
    moved = {g["id"]: g for g in dst["goals"]}
    assert moved["g-004-12"]["aspiration"] == "asp-004"
    assert moved["g-004-13"]["dependencies"] == ["g-004-12"]
    # A reference to a goal that did NOT move () is left alone.
    assert moved["g-004-13"]["description"] == "after g-004-12; see g-002-01"
    assert dst["progress"]["completed_goals"] == 3 and dst["progress"]["total_goals"] == 4


def test_refuses_a_goal_the_source_does_not_hold_and_a_missing_target(tmp_path: Path) -> None:
    world, agent = _stores(tmp_path)
    rc, report = _run(*_args(world, agent, "g-002-09"), "--apply")
    assert rc == 1 and "does not hold" in report["error"]
    rc, report = _run("--from-file", str(world), "--from-asp", "asp-002", "--to-file", str(agent),
                      "--to-asp", "asp-009", "--goal", "g-002-03", "--apply")
    assert rc == 1 and "no record asp-009" in report["error"]
    assert [g["id"] for g in _read(world)[0]["goals"]] == ["g-002-01", "g-002-03", "g-002-04"]


def test_same_file_move_between_two_records(tmp_path: Path) -> None:
    store = tmp_path / "store.jsonl"
    _write(store, [
        {"id": "asp-001", "title": "A", "goals": [{"id": "g-001-01", "title": "x", "status": "pending"}]},
        {"id": "asp-002", "title": "B", "goals": []},
    ])
    rc, report = _run("--from-file", str(store), "--from-asp", "asp-001", "--to-file", str(store),
                      "--to-asp", "asp-002", "--goal", "g-001-01", "--apply")
    assert rc == 0, report
    a, b = _read(store)
    assert a["goals"] == [] and [g["id"] for g in b["goals"]] == ["g-002-01"]
