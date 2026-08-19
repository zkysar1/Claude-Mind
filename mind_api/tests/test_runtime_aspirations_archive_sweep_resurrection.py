"""archive-sweep RESURRECTION RECONCILE + single-record archive UPSERT
(goal-completion audit, 2026-08-16).

The class: merge_aspirations is a union by aspiration id, so an aspiration
removed from the live file by retire / complete / archive_sweep is re-added
PRISTINE the next time a box that still holds it merges a stale live file.
Measured 9 of 29 live aspirations also present in the archive; 8 were
resurrected retirements (7 cross-world asp-xw-* stubs the 2026-08-10 sprint
had retired as duplicates of native goals, back as `pending` with no
outcome_note and no last_modified).

Pins:
  - a pristine resurrected copy of a RETIRED aspiration is re-dispositioned
    from the archive (goal status + outcome_note restored) and archived again
    by the same sweep, replacing (not doubling) its archive row
  - a live copy that carries POST-archive work (a goal the archive never
    saw / a claimed goal / a goal modified after the archive stamp) is left
    live; only the stale duplicates inside it are re-dispositioned
  - a live copy whose goals the archive holds NON-terminal is untouched
    (nothing to re-apply)
  - the retire endpoint upserts: re-retiring a resurrected copy leaves ONE
    archive row for that id
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest


def _post(port: int, path: str, agent: str = "alpha", body=None):
    import urllib.request
    import urllib.error
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, method="POST", data=data,
        headers={"X-Mind-Agent": agent, "X-Runtime-Client": "test",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _sweep(port: int):
    code, body = _post(port, "/v1/aspirations/archive-sweep?source=world")
    assert code == 200, body
    return body


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def _write_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=True) + "\n")


def _asp(asp_id: str, status: str, goals, **extra) -> Dict[str, Any]:
    rec = {
        "id": asp_id, "title": f"Test {asp_id}", "status": status,
        "priority": "LOW", "archived": status in ("completed", "retired"),
        "goals": goals,
        "progress": {"completed_goals": 0, "total_goals": len(goals)},
    }
    rec.update(extra)
    return rec


def _goal(goal_id: str, status: str, **kw) -> Dict[str, Any]:
    g = {"id": goal_id, "title": f"Goal {goal_id}", "status": status}
    g.update(kw)
    return g


# ---------------------------------------------------------------------------

def test_pristine_resurrected_retirement_is_redispositioned_and_rearchived(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    # The archive already records the retirement (sprint dispositioned the
    # goal to skipped with a note); the live file carries the PRISTINE copy.
    _write_jsonl(world / "aspirations-archive.jsonl", [
        _asp("asp-xw-20260805T172222", "retired",
             [_goal("g-xw-20260805T172222-01", "skipped",
                    outcome_note="Cross-world stub, duplicate of g-115-5073 - sprint-2026-08-10")],
             retired_at="2026-08-10"),
    ])
    _write_jsonl(world / "aspirations.jsonl", [
        _asp("asp-xw-20260805T172222", "active",
             [_goal("g-xw-20260805T172222-01", "pending")]),
        _asp("asp-002", "active", [_goal("g-002-01", "pending")]),
    ])

    resp = _sweep(port)
    assert resp["resurrected_reconciled"] == ["g-xw-20260805T172222-01"]
    assert resp["archived_count"] == 1
    assert resp["deduped_replaced"] == 1, "must REPLACE the archive row, not append a second"
    assert any("RESURRECTION RECONCILE" in w for w in (resp["warnings"] or []))

    live = _read_jsonl(world / "aspirations.jsonl")
    assert [a["id"] for a in live] == ["asp-002"], "the zombie is gone from live; the real one stays"

    archive = _read_jsonl(world / "aspirations-archive.jsonl")
    rows = [a for a in archive if a["id"] == "asp-xw-20260805T172222"]
    assert len(rows) == 1, "exactly one archive row per id"
    g = rows[0]["goals"][0]
    assert g["status"] == "skipped"
    assert g["outcome_note"].startswith("Cross-world stub, duplicate of g-115-5073")
    assert g["resurrection_reconciled_at"]
    assert rows[0]["status"] == "retired" and rows[0]["retired_at"] == "2026-08-10"


def test_post_archive_work_keeps_the_aspiration_live(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations-archive.jsonl", [
        _asp("asp-328", "completed",
             [_goal("g-328-01", "completed"), _goal("g-328-02", "skipped",
                                                    outcome_note="dropped")],
             completed_at="2026-07-12"),
    ])
    _write_jsonl(world / "aspirations.jsonl", [
        _asp("asp-328", "active", [
            # stale resurrected duplicate of an archived-terminal goal
            _goal("g-328-02", "pending"),
            # a goal the archive never saw = legitimate post-archive work
            _goal("g-328-36", "pending", created_at="2026-07-15T23:00:57"),
        ]),
    ])

    resp = _sweep(port)
    assert resp["resurrected_reconciled"] == ["g-328-02"]
    assert resp["archived_count"] == 0

    live = _read_jsonl(world / "aspirations.jsonl")
    assert len(live) == 1 and live[0]["id"] == "asp-328"
    assert live[0]["status"] == "active", "post-archive work => stays live"
    by_id = {g["id"]: g for g in live[0]["goals"]}
    assert by_id["g-328-02"]["status"] == "skipped"
    assert by_id["g-328-02"]["outcome_note"] == "dropped"
    assert by_id["g-328-36"]["status"] == "pending", "new work untouched"
    assert any("kept live" in w for w in (resp["warnings"] or []))


@pytest.mark.parametrize("live_extra", [
    {"claimed_by": "echo", "claimed_at": "2026-08-15T10:00:00"},
    {"last_modified": "2026-08-12T00:00:00"},
])
def test_a_live_hand_or_a_newer_edit_is_never_overruled(running_daemon, live_extra):
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations-archive.jsonl", [
        _asp("asp-240", "retired",
             [_goal("g-240-101", "skipped", outcome_note="transplanted")],
             retired_at="2026-08-10"),
    ])
    _write_jsonl(world / "aspirations.jsonl", [
        _asp("asp-240", "active", [_goal("g-240-101", "pending", **live_extra)]),
    ])

    resp = _sweep(port)
    assert resp["resurrected_reconciled"] == []
    assert resp["archived_count"] == 0
    live = _read_jsonl(world / "aspirations.jsonl")
    assert live[0]["goals"][0]["status"] == "pending"
    assert live[0]["status"] == "active"


def test_archive_copy_with_open_goal_reapplies_nothing(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    # archive holds the goal NON-terminal (legacy pre- shape) —
    # there is no disposition to re-apply, so the sweep must not invent one
    _write_jsonl(world / "aspirations-archive.jsonl", [
        _asp("asp-007", "retired", [_goal("g-007-01", "pending")], retired_at="2026-08-01"),
    ])
    _write_jsonl(world / "aspirations.jsonl", [
        _asp("asp-007", "active", [_goal("g-007-01", "pending")]),
    ])
    resp = _sweep(port)
    assert resp["resurrected_reconciled"] == []
    assert resp["archived_count"] == 0
    assert _read_jsonl(world / "aspirations.jsonl")[0]["goals"][0]["status"] == "pending"


def test_rearchiving_a_smaller_resurrected_snapshot_keeps_the_archive_rows_goals(running_daemon):
    """The archive row is the LAST home of a terminal goal record. A resurrected
    live copy is a stale snapshot that can carry FEWER goals than the row it
    supersedes (asp-240: 2 live vs 7 archived, 2026-08-16) — replace-by-id must
    UNION goals, never drop the ones only the archive holds."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations-archive.jsonl", [
        _asp("asp-240", "retired", [
            _goal("g-240-101", "skipped", outcome_note="transplanted"),
            _goal("g-240-102", "skipped", outcome_note="false-positive auto-file"),
            _goal("g-240-104", "completed"),
        ], retired_at="2026-08-10"),
    ])
    _write_jsonl(world / "aspirations.jsonl", [
        _asp("asp-240", "active", [_goal("g-240-101", "pending")]),
    ])
    resp = _sweep(port)
    assert resp["archived_count"] == 1 and resp["deduped_replaced"] == 1
    rows = [a for a in _read_jsonl(world / "aspirations-archive.jsonl") if a["id"] == "asp-240"]
    assert len(rows) == 1
    by_id = {g["id"]: g for g in rows[0]["goals"]}
    assert set(by_id) == {"g-240-101", "g-240-102", "g-240-104"}, "archive-only goals survive the replace"
    assert by_id["g-240-101"]["status"] == "skipped" and by_id["g-240-101"]["resurrection_reconciled_at"]
    assert by_id["g-240-102"]["outcome_note"] == "false-positive auto-file"
    assert by_id["g-240-104"]["status"] == "completed"


def test_retire_endpoint_upserts_a_resurrected_copy(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations-archive.jsonl", [
        _asp("asp-xw-20260731T042940", "retired",
             [_goal("g-xw-20260731T042940-01", "skipped", outcome_note="sprint")],
             retired_at="2026-08-10"),
    ])
    _write_jsonl(world / "aspirations.jsonl", [
        _asp("asp-xw-20260731T042940", "active",
             [_goal("g-xw-20260731T042940-01", "pending")]),
    ])
    code, body = _post(port, "/v1/aspirations/retire?asp_id=asp-xw-20260731T042940&source=world")
    assert code == 200, body

    archive = _read_jsonl(world / "aspirations-archive.jsonl")
    rows = [a for a in archive if a["id"] == "asp-xw-20260731T042940"]
    assert len(rows) == 1, f"retire must upsert, not append: {len(rows)} rows"
    assert rows[0]["status"] == "retired"
    assert rows[0]["goals"][0]["status"] == "skipped", "open goal auto-dispositioned on retire (g-115-2860)"
    assert _read_jsonl(world / "aspirations.jsonl") == []
