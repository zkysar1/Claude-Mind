"""POST /v1/aspirations/rehome-goal — the B4 move-on-touch primitive ().

goal-intake-management.md §5-6 (I6): a candidate touched in the legacy inbox
moves to its lane under its SAME id. Pinned here, against an in-process daemon:

  * the adopted copy is byte-identical to the original except for the four
    rehome stamps (rehomed_from / rehomed_at / rehome_reason / last_modified) —
    id, provenance and blocked_by all survive;
  * the copy left behind is a merge-surviving POINTER (superseded, recurring
    false, last_modified bumped, prior note kept under a REHOMED head line);
  * a later update-goal lands on the MOVED copy, not the pointer, although the
    source aspiration precedes the target in file order (aspirations_write
    _find_goal prefers the non-superseded copy);
  * re-calls are idempotent; bad targets, unknown goals and dry runs write
    nothing.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SEEDED = "2026-01-01T00:00:00"
REHOME_STAMPS = {"rehomed_from", "rehomed_at", "rehome_reason", "last_modified"}

ORIGINAL = {
    "id": "g-115-07",
    "title": "Investigate: a laneless discovery",
    "description": "body text",
    "status": "candidate",
    "recurring": False,
    "priority": "MEDIUM",
    "created": "2026-09-01T10:00:00",
    "origin_signal": "investigate:auto-filer",
    "discovered_by": "agent-b",
    "blocked_by": ["g-300-01"],
    "work_class": "framework",
    "outcome_note": "earlier note",
    "last_modified": SEEDED,
}


def _post(port, path, query, body=None):
    url = f"http://127.0.0.1:{port}{path}?{urllib.parse.urlencode(query)}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", "alpha")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _seed(world: Path):
    asps = [
        {"id": "asp-115", "title": "Recurring infrastructure monitoring", "status": "active",
         "priority": "MEDIUM", "archived": False,
         "goals": [dict(ORIGINAL), {"id": "g-115-08", "title": "stays", "status": "pending",
                                    "recurring": False}]},
        {"id": "asp-300", "title": "A lane", "status": "active", "priority": "MEDIUM",
         "archived": False, "goals": [{"id": "g-300-01", "title": "blocker",
                                       "status": "pending", "recurring": False}]},
        {"id": "asp-302", "title": "Closed lane", "status": "completed", "priority": "LOW",
         "archived": False, "goals": []},
    ]
    (world / "aspirations.jsonl").write_text(
        "".join(json.dumps(a, ensure_ascii=True) + "\n" for a in asps), encoding="utf-8")


def _copies(world: Path, goal_id: str):
    out = []
    for line in (world / "aspirations.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            asp = json.loads(line)
            out += [(asp["id"], g) for g in asp.get("goals", []) if g.get("id") == goal_id]
    return out


def _rehome(port, **query):
    return _post(port, "/v1/aspirations/rehome-goal", {"source": "world", **query})


def test_adopted_copy_is_byte_identical_and_the_pointer_survives_merge(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    _seed(world)
    status, body = _rehome(port, goal_id="g-115-07", to_asp="asp-300", reason="move-on-touch")
    assert status == 200, body
    assert body["adopted_new"] is True and body["from_asp"] == "asp-115"

    copies = dict(_copies(world, "g-115-07"))
    adopted, pointer = copies["asp-300"], copies["asp-115"]
    assert {k: v for k, v in adopted.items() if k not in REHOME_STAMPS} == \
        {k: v for k, v in ORIGINAL.items() if k != "last_modified"}
    for key in ("id", "created", "origin_signal", "discovered_by", "blocked_by"):
        assert json.dumps(adopted[key], sort_keys=True) == json.dumps(ORIGINAL[key], sort_keys=True)
    assert adopted["rehomed_from"] == "asp-115" and adopted["rehome_reason"] == "move-on-touch"

    assert pointer["status"] == "superseded" and pointer["recurring"] is False
    assert pointer["rehomed_to"] == "asp-300" and pointer["superseded_by_goal"] == "g-115-07"
    assert pointer["last_modified"] > SEEDED  # guard-6913: the pointer must out-date a stale peer
    assert pointer["outcome_note"].startswith("REHOMED ")
    assert pointer["outcome_note"].endswith("\n\nearlier note")  # prior note kept (I5)


def test_a_later_update_lands_on_the_moved_copy_not_the_pointer(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    _seed(world)
    assert _rehome(port, goal_id="g-115-07", to_asp="asp-300")[0] == 200
    status, body = _post(port, "/v1/aspirations/update-goal",
                         {"id": "g-115-07", "field": "progress_note", "source": "world"},
                         body="written after the move")
    assert status == 200, body
    assert body["aspiration_id"] == "asp-300"
    copies = dict(_copies(world, "g-115-07"))
    assert copies["asp-300"]["progress_note"] == "written after the move"
    assert "progress_note" not in copies["asp-115"]


def test_rehome_is_idempotent_and_refusals_write_nothing(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    store = world / "aspirations.jsonl"
    _seed(world)
    seeded = store.read_bytes()
    assert _rehome(port, goal_id="g-115-07", to_asp="asp-300", dry_run="true")[0] == 200
    assert store.read_bytes() == seeded                                   # dry run
    for bad in ("asp-115", "asp-302", "asp-999"):                         # self, completed, absent
        assert _rehome(port, goal_id="g-115-07", to_asp=bad)[0] == 400
    assert _rehome(port, goal_id="g-404-01", to_asp="asp-300")[0] == 404
    assert store.read_bytes() == seeded

    assert _rehome(port, goal_id="g-115-07", to_asp="asp-300")[0] == 200
    moved = store.read_bytes()
    status, body = _rehome(port, goal_id="g-115-07", to_asp="asp-300")
    assert status == 200 and body.get("already_rehomed") is True
    assert store.read_bytes() == moved
