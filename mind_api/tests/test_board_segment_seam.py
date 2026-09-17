"""Regression pins for the board reader-side PATH-LIST SEAM ().

`GET /v1/board/read` used to resolve a channel to ONE hardcoded filename. That
is safe only while the writer also writes exactly one file — and the whole point
of the segmentation work this goal precedes is that it will not. `_gate_log.py`
states the ordering constraint verbatim: the reader seam lands BEFORE any writer
change, because "a false all-clear is the worst available failure direction".
On the board the blast radius is the fleet's own claim path — a reader that
misses a partner's claim double-executes.

There is exactly ONE read implementation to pin. `core/scripts/board.py` has no
`read` subcommand and `board-read.sh` is daemon-only under the 2026-05-14
cutover (see test_board_archive_reach.py's scope note), so this endpoint IS the
CLI path, and pinning it here covers every consumer that reaches a channel
through `board-read.sh`.

THREE PROPERTIES, and the third is the one nothing else could catch: a footer
that computes "window covered X .. Y" from oldest/newest asserts coverage ACROSS
a hole, so enumerating paths is not enough — the seam has to REPORT
discontinuity. Unit-level pins for the seam itself (ordering, sibling-store
exclusion, the eviction mutation control) live in
core/scripts/tests/test_board_paths.py; this file pins the WIRING.
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
CH = "coordination"


def _msg(mid: str, ts: datetime, text: str):
    return {
        "id": mid, "author": "bravo", "session_id": "",
        "timestamp": ts.strftime(TS_FMT), "channel": CH, "type": "status",
        "text": text, "reply_to": None, "tags": [],
    }


def _write(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _body(resp):
    return resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)


def test_a_date_segment_is_read_not_silently_dropped(tmp_path):
    """The defect the seam exists to prevent, in its simplest form.

    Before the seam, a post living in `coordination-2026-xx-xx.jsonl` was
    invisible to every reader — at HTTP 200, with a footer asserting the window
    was covered.
    """
    now = datetime.now().replace(microsecond=0)
    world = tmp_path / "world"
    board_dir = world / "board"
    _write(board_dir / f"{CH}.jsonl",
           [_msg("msg-live-001", now - timedelta(hours=2), "live")])
    seg_day = (now - timedelta(days=1)).date()
    _write(board_dir / f"{CH}-{seg_day.isoformat()}.jsonl",
           [_msg("msg-seg-001", now - timedelta(days=1), "in a date segment")])

    resp = board.read(_FakeCtx(world, {"channel": CH, "since": "7d"}))
    text = _body(resp)

    assert "msg-seg-001" in text, text[-600:]
    assert "msg-live-001" in text          # positive control: live still read
    assert "2 message(s)" in text, text[-400:]


def test_sibling_receipt_store_is_not_read_as_posts(tmp_path):
    """`<channel>-reads.jsonl` is the READ-RECEIPT store backing --unread-only.

    It sits in the same directory and matches a loose `<channel>-*.jsonl` glob,
    so a seam that globbed loosely would inject receipts into the message list.
    Measured present in the live board on this box (2026-09-17): coordination,
    findings, decisions, general and reasoning each carry one.
    """
    now = datetime.now().replace(microsecond=0)
    world = tmp_path / "world"
    board_dir = world / "board"
    _write(board_dir / f"{CH}.jsonl",
           [_msg("msg-live-001", now - timedelta(hours=2), "live")])
    _write(board_dir / f"{CH}-reads.jsonl",
           [{"id": "RECEIPT-NOT-A-POST", "agent": "alpha",
             "timestamp": now.strftime(TS_FMT)}])

    text = _body(board.read(_FakeCtx(world, {"channel": CH, "since": "7d"})))

    assert "RECEIPT-NOT-A-POST" not in text, text[-600:]
    assert "1 message(s)" in text, text[-400:]


def test_footer_reports_discontinuity_instead_of_asserting_coverage(tmp_path):
    """A hole INSIDE the covered span must be stated, not papered over.

    "window covered X .. Y" is computed from oldest/newest, so without this the
    reply asserts coverage across days it holds nothing for — the same defect as
    a missing segment, seen from the reporting side.
    """
    now = datetime.now().replace(microsecond=0)
    world = tmp_path / "world"
    board_dir = world / "board"
    _write(board_dir / f"{CH}.jsonl", [
        _msg("msg-old", now - timedelta(days=5), "old"),
        _msg("msg-new", now - timedelta(hours=1), "new"),
    ])

    text = _body(board.read(_FakeCtx(world, {"channel": CH, "since": "30d"})))

    assert "DISCONTINUOUS" in text, text[-600:]
    assert "NOT fully covered" in text, text[-600:]


def test_footer_is_unchanged_on_a_continuous_read(tmp_path):
    """The healthy path must gain NO noise — otherwise the note stops being read.

    This is the control for the test above: same endpoint, same shape, one day
    apart instead of five, and the seam must say nothing.
    """
    now = datetime.now().replace(microsecond=0)
    world = tmp_path / "world"
    _write(world / "board" / f"{CH}.jsonl", [
        _msg("msg-a", now - timedelta(days=1), "a"),
        _msg("msg-b", now - timedelta(hours=1), "b"),
    ])

    text = _body(board.read(_FakeCtx(world, {"channel": CH, "since": "30d"})))

    assert "DISCONTINUOUS" not in text, text[-400:]
    assert "evicted mid-read" not in text, text[-400:]
    assert "window covered" in text, text[-400:]


def test_a_segment_evicted_mid_read_reaches_the_footer(tmp_path, monkeypatch):
    """Eviction is REPORTED, at the endpoint, not swallowed into a short window.

    `jsonl_cache.get()` returns [] for a missing file, so routing segments
    through the cache would make an evicted segment read as an empty one — a
    silently short window with a footer claiming coverage. The endpoint routes
    segments through `_board_paths.read_paths` for exactly this reason.

    Driving it by monkeypatching the enumerator is deliberate: the real race is
    "the path existed at enumeration and was gone at open", and handing the
    endpoint a path that is already gone reproduces the state the open sees
    without needing to win a race inside the test.
    """
    now = datetime.now().replace(microsecond=0)
    world = tmp_path / "world"
    board_dir = world / "board"
    live = board_dir / f"{CH}.jsonl"
    _write(live, [_msg("msg-live-001", now - timedelta(hours=2), "live")])
    ghost = board_dir / f"{CH}-{(now - timedelta(days=1)).date().isoformat()}.jsonl"

    real = board._board_paths.channel_paths

    def _with_ghost(board_dir_arg, channel, include_archive=True):
        paths = real(board_dir_arg, channel, include_archive=include_archive)
        return list(paths) + [ghost]          # enumerated, then gone

    monkeypatch.setattr(board._board_paths, "channel_paths", _with_ghost)

    text = _body(board.read(_FakeCtx(world, {"channel": CH, "since": "7d"})))

    assert "evicted mid-read" in text, text[-600:]
    assert "NOT fully covered" in text, text[-600:]
    # The surviving live post is still served — an eviction shortens the window,
    # it must not fail the read.
    assert "msg-live-001" in text
