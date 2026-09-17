"""aspirations-move-goals.py — move goals between aspiration records across stores.

The data repair for goals misfiled by the cross-store id collision: twelve agent goals filed
into the world's unrelated aspiration because ``add-goal asp-002`` resolved the world record
first. The guards that matter: ids renumber into the target family (continuing after its
highest), references among the moved goals follow the map, progress is recomputed on both
records with the daemon's formula, the target is written BEFORE the source, and dry-run
writes nothing. The source copy is TOMBSTONED, never removed: the live store is union-merged
across boxes with no deletion semantics (guard-1072), so a popped goal is resurrected by any
peer still holding it (measured 2026-09-17, 24 of 24 moved goals back as duplicates).
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
    # The two moved non-recurring goals stay in the source as superseded tombstones,
    # and the daemon's formula counts abandoned goals in total_goals.
    assert report["from"]["progress_after"]["total_goals"] == 2
    assert report["from"]["progress_after"]["completed_goals"] == 0
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
    assert [g["id"] for g in src["goals"]] == ["g-002-01", "g-002-03", "g-002-04"]
    stones = {g["id"]: g for g in src["goals"][1:]}
    assert stones["g-002-03"]["status"] == "superseded" and stones["g-002-04"]["status"] == "superseded"
    assert stones["g-002-03"]["moved_as"] == "g-004-12" and stones["g-002-04"]["moved_as"] == "g-004-13"
    assert stones["g-002-03"]["moved_to"] == "asp-004" and stones["g-002-03"]["recurring"] is False
    assert stones["g-002-03"]["moved_at"] == stones["g-002-03"]["last_modified"]
    assert "g-004-12" in stones["g-002-03"]["outcome_note"] and "guard-1072" in stones["g-002-03"]["outcome_note"]
    # The untouched goal is byte-identical; the tombstones are the only source change.
    assert src["goals"][0] == {"id": "g-002-01", "title": "Sprint planning", "status": "pending", "recurring": True}
    assert src["progress"] == {"completed_goals": 0, "total_goals": 2, "recurring_goals": 1,
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
    assert [(g["id"], g["status"], g.get("moved_as")) for g in a["goals"]] == [("g-001-01", "superseded", "g-002-01")]
    assert [(g["id"], g["status"]) for g in b["goals"]] == [("g-002-01", "pending")]


def test_tombstone_survives_the_cross_box_union_merge(tmp_path: Path) -> None:
    """The property the tombstone exists for: merged against a peer's STALE pre-move
    copy (still pending, older last_modified), the source record stays superseded.
    A popped record has no side to win from — the union simply keeps the stale copy."""
    sys.path.insert(0, str(SCRIPT.parent))
    from coordination_merge import _merge_goals  # noqa: E402

    world, agent = _stores(tmp_path)
    stale = [json.loads(json.dumps(g)) for g in _read(world)[0]["goals"]]
    for g in stale:
        g.setdefault("last_modified", "2026-09-16T00:00:00")
    # A peer copy that was never edited carries NO last_modified at all — the common
    # shape for a freshly filed idea; the merge sorts a missing stamp oldest.
    unstamped = [{k: v for k, v in g.items() if k != "last_modified"} for g in stale]
    rc, _report = _run(*_args(world, agent, "g-002-03"), "--apply")
    assert rc == 0
    tombstoned = _read(world)[0]["goals"]
    for peer in (stale, unstamped):
        for a_side, b_side in ((peer, tombstoned), (tombstoned, peer)):
            merged = {g["id"]: g for g in _merge_goals(a_side, b_side, "002")}
            assert merged["g-002-03"]["status"] == "superseded", merged["g-002-03"]
            assert merged["g-002-03"]["moved_as"] == "g-004-12"
            assert merged["g-002-04"]["status"] == "completed"  # untouched goal unaffected
    # Contrast: the pre-fix behaviour. A side that simply LACKS the goal loses it back.
    without = [g for g in tombstoned if g["id"] != "g-002-03"]
    merged = {g["id"]: g for g in _merge_goals(without, stale, "002")}
    assert merged["g-002-03"]["status"] == "pending"  # resurrected — exactly what the tombstone prevents


def test_refuses_a_claimed_or_in_progress_goal(tmp_path: Path) -> None:
    world, agent = _stores(tmp_path)
    recs = _read(world)
    recs[0]["goals"][1]["claimed_by"] = "alpha"
    _write(world, recs)
    rc, report = _run(*_args(world, agent, "g-002-03", "g-002-04"), "--apply")
    assert rc == 1 and "in flight" in report["error"] and "g-002-03" in report["error"]
    assert _read(world) == recs                      # nothing written on either side
    assert [g["id"] for g in _read(agent)[0]["goals"]] == ["g-004-01", "g-004-11"]


def test_target_ids_skip_the_evicted_seqs(tmp_path: Path) -> None:
    """An evicted seq is allocated forever: a goal re-minted onto one would be dropped
    by the next merge as a resurrection (g-115-2430). The daemon's allocator counts
    evicted ids toward max+1; so must the mover."""
    world, agent = _stores(tmp_path)
    recs = _read(agent)
    recs[0]["archived_census"] = {"evicted_ids": {"completed": ["g-004-12", "g-004-20"],
                                                  "skipped": ["g-004-13"]}}
    _write(agent, recs)
    rc, report = _run(*_args(world, agent, "g-002-03", "g-002-04"), "--apply")
    assert rc == 0, report
    assert report["id_map"] == {"g-002-03": "g-004-21", "g-002-04": "g-004-22"}
    assert [g["id"] for g in _read(agent)[0]["goals"]] == ["g-004-01", "g-004-11", "g-004-21", "g-004-22"]


def test_refuses_to_move_a_tombstone_again(tmp_path: Path) -> None:
    world, agent = _stores(tmp_path)
    rc, _ = _run(*_args(world, agent, "g-002-03"), "--apply")
    assert rc == 0
    rc, report = _run(*_args(world, agent, "g-002-03"), "--apply")      # second move of the tombstone
    assert rc == 1 and "already moved" in report["error"] and "g-002-03->g-004-12" in report["error"]
    assert [g["id"] for g in _read(agent)[0]["goals"]] == ["g-004-01", "g-004-11", "g-004-12"]  # no third copy
