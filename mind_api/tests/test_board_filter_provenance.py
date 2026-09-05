"""Pins the filter-provenance footer on GET /v1/board/read ( follow-up).

A filtered EMPTY result is indistinguishable from "the record is not there"
unless the reply states the window it actually covered. Measured live: a search
for an 05:07 post against a `--last 60` slice that began 17:52 returned a clean
zero, and the positive control (`grep -c 'msg-'` = 72) PASSED, because a control
that shares the TOOL but not the FILTER cannot detect a filter miss.

Three behaviours are pinned, and all three must hold:
  - a non-empty human read reports the window it actually covered
  - an empty human read still names its filters (so the zero is scoped, not bare)
  - JSON output carries NO footer -- one object per line stays parseable
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from mind_api.src.endpoints import board


class _FakePaths:
    def __init__(self, world: Path):
        self.world = world
        self.agent_name = "alpha"


class _FakeCtx:
    def __init__(self, world: Path, query: dict):
        self.paths = _FakePaths(world)
        self.query = query
        self.headers = {}


TS_FMT = "%Y-%m-%dT%H:%M:%S"


def _seed(tmp_path: Path) -> Path:
    now = datetime.now()
    world = tmp_path / "world"
    (world / "board").mkdir(parents=True)
    rows = [
        {"id": "msg-old", "timestamp": (now - timedelta(days=60)).strftime(TS_FMT),
         "author": "alpha", "type": "finding", "text": "stale", "tags": ["t"]},
        {"id": "msg-recent", "timestamp": (now - timedelta(minutes=5)).strftime(TS_FMT),
         "author": "alpha", "type": "finding", "text": "fresh", "tags": ["t"]},
    ]
    with (world / "board" / "findings.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return world


def _read(world: Path, query: dict):
    resp = board.read(_FakeCtx(world, query))
    return resp.status, resp.body.decode("utf-8")


def test_human_read_reports_the_window_it_covered(tmp_path):
    """The truncating filter must disclose its own realized extent."""
    world = _seed(tmp_path)
    status, body = _read(world, {"channel": "findings", "last": "1"})
    assert status == 200
    assert "msg-recent" in body and "msg-old" not in body, "last=1 should truncate"
    assert "window covered" in body, "footer absent: a wrong-window zero stays silent"
    assert "last=1" in body, "footer must name the filter that truncated"


def test_empty_human_read_names_its_filters(tmp_path):
    """A zero must arrive scoped to the filters that produced it."""
    world = _seed(tmp_path)
    status, body = _read(world, {"channel": "findings", "author": "nobody-at-all"})
    assert status == 200
    assert "0 messages matched" in body
    assert "author=nobody-at-all" in body, "the zero must name the filter behind it"
    assert "not proof the record is absent" in body


def test_json_output_has_no_footer(tmp_path):
    """The JSON branch stays one-object-per-line: a footer would break parsers."""
    world = _seed(tmp_path)
    status, body = _read(world, {"channel": "findings", "json": "1"})
    assert status == 200
    for line in [ln for ln in body.splitlines() if ln.strip()]:
        json.loads(line)  # raises if the footer leaked into JSON
    assert "window covered" not in body and "0 messages matched" not in body
