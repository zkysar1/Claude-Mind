"""aspirations-renumber.py — renumber one aspiration and its goal-id family, in place.

The data repair for the cross-store id collision (agent-local asp-002 minted while the world
already held asp-002). The guards that matter: only the TARGET record is rewritten (a
sibling record's reference to the same token may mean the colliding twin — reported, never
touched); dry-run writes nothing; a --to that already exists anywhere refuses.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "aspirations-renumber.py"


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


def _store(tmp_path: Path) -> Path:
    records = [
        {"id": "asp-001", "title": "Other", "goals": [
            {"id": "g-001-01", "title": "unrelated", "blocked_by": ["g-002-02"]}]},
        {"id": "asp-002", "title": "Build data infrastructure", "aspiration": "asp-002",
         "goals": [
             {"id": "g-002-01", "title": "first", "aspiration": "asp-002"},
             {"id": "g-002-02", "title": "second", "depends_on": ["g-002-01"],
              "description": "after g-002-01 lands; see exp-g-002-01-notes"},
             "g-002-03",
         ],
         "origin_signal": "asp-002 seeded"},
    ]
    path = tmp_path / "aspirations.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_dry_run_reports_and_writes_nothing(tmp_path: Path) -> None:
    path = _store(tmp_path)
    before = path.read_text(encoding="utf-8")
    rc, report = _run("--file", str(path), "--from", "asp-002", "--to", "asp-004")
    assert rc == 0, report
    assert report["applied"] is False
    assert path.read_text(encoding="utf-8") == before
    assert report["goal_ids"] == {"g-002-01": "g-004-01", "g-002-02": "g-004-02"}
    # The sibling record's reference to  is reported, not rewritten.
    assert report["other_record_refs"] == {"asp-001": 1}


def test_apply_rewrites_every_token_inside_the_target_record_only(tmp_path: Path) -> None:
    path = _store(tmp_path)
    scan = tmp_path / "working-memory.yaml"
    scan.write_text("current_goal: g-002-02\nother: g-009-01\n", encoding="utf-8")
    rc, report = _run("--file", str(path), "--from", "asp-002", "--to", "asp-004",
                      "--scan", str(scan), "--apply")
    assert rc == 0, report
    assert report["applied"] is True
    assert report["scan_hits"] == {str(scan): [1]}
    other, target = _read(path)
    assert target["id"] == "asp-004" and target["aspiration"] == "asp-004"
    assert [g["id"] if isinstance(g, dict) else g for g in target["goals"]] == [
        "g-004-01", "g-004-02", "g-004-03"]
    assert target["goals"][0]["aspiration"] == "asp-004"
    assert target["goals"][1]["depends_on"] == ["g-004-01"]
    assert target["goals"][1]["description"] == "after g-004-01 lands; see exp-g-004-01-notes"
    assert target["origin_signal"] == "asp-004 seeded"
    # Untouched: the other record, its id, and its (ambiguous) reference.
    assert other["id"] == "asp-001" and other["goals"][0]["blocked_by"] == ["g-002-02"]
    # A wider id (asp-0020) never matches the asp-002 token.
    assert "asp-002" not in json.dumps(target)


def test_drop_stray_goal_records_removes_only_duplicates_of_inner_goals(tmp_path: Path) -> None:
    # The hand-written-store shape: goal records sitting at the top level beside the real
    # aspiration, duplicating its inner goals with stale statuses. A stray with no inner
    # twin is never discarded.
    records = [
        {"id": "g-002-01", "aspiration": "asp-002", "title": "first", "status": "pending"},
        {"id": "g-002-09", "aspiration": "asp-002", "title": "only copy", "status": "pending"},
        {"id": "asp-002", "title": "Target", "goals": [
            {"id": "g-002-01", "title": "first", "status": "completed"}]},
        {"id": "asp-003", "title": "Other", "goals": []},
    ]
    path = tmp_path / "aspirations.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    rc, report = _run("--file", str(path), "--from", "asp-002", "--to", "asp-004")
    assert rc == 0 and report["dropped_stray_goal_records"] == []
    assert report["kept_stray_goal_records"] == ["g-002-01", "g-002-09"]
    rc, report = _run("--file", str(path), "--from", "asp-002", "--to", "asp-004",
                      "--drop-stray-goal-records", "--apply")
    assert rc == 0, report
    assert report["dropped_stray_goal_records"] == ["g-002-01"]
    assert report["kept_stray_goal_records"] == ["g-002-09"]
    ids = [r["id"] for r in _read(path)]
    assert ids == ["g-002-09", "asp-004", "asp-003"]
    target = _read(path)[1]
    assert target["goals"][0] == {"id": "g-004-01", "title": "first", "status": "completed"}


def test_refuses_when_the_target_id_already_exists(tmp_path: Path) -> None:
    path = _store(tmp_path)
    rc, report = _run("--file", str(path), "--from", "asp-002", "--to", "asp-001", "--apply")
    assert rc == 1 and "already exists" in report["error"]
    sibling = tmp_path / "world.jsonl"
    sibling.write_text(json.dumps({"id": "asp-004", "goals": []}) + "\n", encoding="utf-8")
    rc, report = _run("--file", str(path), "--from", "asp-002", "--to", "asp-004",
                      "--sibling", str(sibling), "--apply")
    assert rc == 1 and "already exists there" in report["error"]
    assert [r["id"] for r in _read(path)] == ["asp-001", "asp-002"]


def test_refuses_a_missing_or_malformed_id(tmp_path: Path) -> None:
    path = _store(tmp_path)
    rc, report = _run("--file", str(path), "--from", "asp-009", "--to", "asp-010")
    assert rc == 1 and "0 records" in report["error"]
    rc, report = _run("--file", str(path), "--from", "asp-2", "--to", "asp-010")
    assert rc == 2
