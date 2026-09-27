"""A day moved by segment retention stays readable (, outcome 3).

Segment retention (core/scripts/board-segments-retain.py) moves the oldest date
segments into <channel>-archive.jsonl. Those days sit AFTER the base file's own
posts, which stop growing at the flip and keep the older earliest stamp. So a
reach condition keyed on the live half's earliest record would never open the
archive for a window that reaches a moved day, and the day would be lost.

The fixture is built so that the pre-change condition CANNOT fire, and it
asserts that first: the base file's earliest post predates the read's cutoff.
The control then removes the archive leg and shows the day disappears, which
proves the day is carried by the archive reach and by nothing else.
"""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

os.environ["STORAGE_BACKEND"] = "local"

from mind_api.src.endpoints import board  # noqa: E402

_SCRIPT = Path(__file__).resolve().parents[2] / "core" / "scripts" / "board-segments-retain.py"
_SPEC = importlib.util.spec_from_file_location("board_segments_retain", _SCRIPT)
retain = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(retain)

CH = "coordination"
TS = "%Y-%m-%dT%H:%M:%S"


class _FakePaths:
    def __init__(self, world: Path):
        self.world = world
        self.agent_name = "alpha"


class _FakeCtx:
    def __init__(self, world: Path, query: dict):
        self.paths = _FakePaths(world)
        self.query = query
        self.headers = {}


def _post(mid, when: datetime):
    return {"id": mid, "author": "bravo", "session_id": "", "timestamp": when.strftime(TS),
            "channel": CH, "type": "status", "text": mid, "reply_to": None, "tags": []}


def _write(path: Path, recs):
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


def _seed(tmp_path: Path):
    now = datetime.now().replace(microsecond=0)
    today = now.date()
    world = tmp_path / "world"
    b = world / "board"
    b.mkdir(parents=True)

    def day(n, hour):
        return datetime.combine(today - timedelta(days=n), datetime.min.time()) + timedelta(hours=hour)

    _write(b / f"{CH}-archive.jsonl", [_post("msg-arch-1", now - timedelta(days=40))])
    # The base file stopped growing at the flip, so its posts predate every segment.
    _write(b / f"{CH}.jsonl", [_post("msg-base-1", now - timedelta(days=25)),
                               _post("msg-base-2", now - timedelta(days=24))])
    moved = {15: ["msg-d15-a", "msg-d15-b"], 14: ["msg-d14-a"]}
    for n, ids in moved.items():
        _write(b / f"{CH}-{(today - timedelta(days=n)).isoformat()}.jsonl",
               [_post(mid, day(n, 10 + i)) for i, mid in enumerate(ids)])
    for n in (2, 0):
        _write(b / f"{CH}-{(today - timedelta(days=n)).isoformat()}.jsonl",
               [_post(f"msg-d{n}", now - timedelta(days=n, minutes=1))])
    return world, now, {mid for ids in moved.values() for mid in ids}


def _read_ids(world: Path, since: str) -> set:
    resp = board.read(_FakeCtx(world, {"channel": CH, "since": since, "json": "1"}))
    return {json.loads(ln)["id"] for ln in resp.body.decode("utf-8").splitlines() if ln.strip()}


def _move(world: Path, tmp_path: Path):
    rc = retain.main(["--board-dir", str(world / "board"), "--batch-days", "0",
                      "--apply", "--archive-dir", str(tmp_path / "cold")])
    assert rc == 0


def test_the_fixture_defeats_the_base_file_keyed_condition(tmp_path):
    world, now, _moved = _seed(tmp_path)
    cutoff = board._parse_since("16d")
    base_earliest = now - timedelta(days=25)
    assert base_earliest < cutoff, "otherwise the old condition would reach anyway"


def test_a_moved_day_reads_the_same_before_and_after_the_move(tmp_path):
    world, _now, moved = _seed(tmp_path)
    before = _read_ids(world, "16d") & moved
    assert before == moved
    _move(world, tmp_path)
    for n in (15, 14):
        day = (datetime.now().date() - timedelta(days=n)).isoformat()
        assert not (world / "board" / f"{CH}-{day}.jsonl").exists()
    assert _read_ids(world, "16d") & moved == moved


def test_control_without_the_archive_leg_the_moved_day_is_gone(tmp_path, monkeypatch):
    world, _now, moved = _seed(tmp_path)
    _move(world, tmp_path)
    monkeypatch.setattr(board, "_read_archive_tail", lambda *a, **k: ([], False, 0))
    assert _read_ids(world, "16d") & moved == set()


def test_a_window_inside_the_segments_still_never_opens_the_archive(tmp_path, monkeypatch):
    world, _now, _moved = _seed(tmp_path)
    _move(world, tmp_path)
    calls = []
    real = board._read_archive_tail
    monkeypatch.setattr(board, "_read_archive_tail",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    ids = _read_ids(world, "1d")
    assert "msg-d0" in ids and calls == []
