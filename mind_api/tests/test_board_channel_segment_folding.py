"""The channel ENUMERATOR must not report a date segment as its own channel ().

Both enumerators took a file stem as a channel name over a bare `*.jsonl` glob:
`core/scripts/board.py::cmd_channels` and
`mind_api/src/endpoints/board_write.py::channels`. Once a segmented writer creates
`findings-2026-09-17.jsonl`, each reported it as a channel BESIDE `findings`, so the
per-row counts stopped showing real depth.

THE DECISION RECORDED HERE IS **FOLD**, NOT HIDE. Hiding removes the spurious ROW and
leaves the parent's count understating the channel by exactly the hidden segment's
messages -- a visible wrong row traded for an invisible wrong number, which is strictly
harder to detect. Folding makes `count` the channel's TRUE total, and it agrees with
what `_board_paths.channel_paths()` already defines a channel to BE (archive + live +
segments). The enumerator disagreeing with the reader's own definition of a channel IS
the defect; folding removes it.

SCOPE IS SEGMENTS ONLY. `-archive.jsonl` keeps its own row (that surface is g-358-129's),
and it stays separate BY CONSTRUCTION rather than by a second rule: `archive_name()` is
`<channel>-archive.jsonl`, "archive" is not a date, so `segment_parent` returns None for
it. `test_an_archive_is_not_folded` is the control that pins exactly that -- without it, a
fix that folded on any `<base>-<suffix>` split would pass every other test here.

CONTROL DISCIPLINE. `test_control_the_raw_stem_enumeration_yields_the_extra_row`
reproduces the PRE-FIX behaviour on the SAME fixture (the bare glob + `.stem`) and asserts
the extra row appears. If it ever goes green, the fixture stopped discriminating and every
other assertion in this file proves nothing (guard-2435: declaring a test a control does
not make it one).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "core" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _board_paths as bp  # noqa: E402
from mind_api.src.endpoints import board_write  # noqa: E402


class _FakePaths:
    def __init__(self, world: Path):
        self.world = world
        self.agent_name = "alpha"


class _FakeCtx:
    def __init__(self, world: Path):
        self.paths = _FakePaths(world)
        self.query = {}
        self.headers = {}


def _post(mid: str, ts: str, channel: str):
    return {
        "id": mid, "author": "alpha", "session_id": "",
        "timestamp": ts, "channel": channel, "type": "status",
        "text": "x", "reply_to": None, "tags": [],
    }


def _write(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=True) + "\n")


@pytest.fixture()
def board(tmp_path):
    """A live channel file, one date segment of it, and an unrelated channel."""
    d = tmp_path / "board"
    _write(d / "findings.jsonl", [
        _post("m1", "2026-09-15T01:00:00", "findings"),
        _post("m2", "2026-09-15T02:00:00", "findings"),
    ])
    _write(d / "findings-2026-09-17.jsonl", [
        _post("m3", "2026-09-17T03:00:00", "findings"),
        _post("m4", "2026-09-17T04:00:00", "findings"),
        _post("m5", "2026-09-17T05:00:00", "findings"),
    ])
    _write(d / "general.jsonl", [_post("m6", "2026-09-16T01:00:00", "general")])
    return d


# --- the helper, reused rather than re-derived -------------------------------

def test_segment_parent_inverts_is_segment():
    assert bp.segment_parent("findings-2026-09-17.jsonl") == "findings"
    assert bp.segment_parent("findings.jsonl") is None
    assert bp.segment_parent("general.jsonl") is None


def test_segment_parent_agrees_with_is_segment():
    """The inverse must never disagree with the forward predicate."""
    name = "coordination-2026-01-02.jsonl"
    parent = bp.segment_parent(name)
    assert parent == "coordination"
    assert bp.is_segment(parent, name) is True


def test_an_archive_is_not_folded():
    """CONTROL for over-folding — the scope fence.

    `-archive.jsonl` is NOT a date segment and must keep its own row. A fix that
    folded on any `<base>-<suffix>` split would pass every other test in this file
    and silently swallow the archive row.
    """
    assert bp.segment_parent(bp.archive_name("findings")) is None
    assert bp.segment_parent("coordination-archive-archive.jsonl") is None


def test_a_hyphenated_channel_is_not_mistaken_for_a_segment():
    assert bp.segment_parent("npc-behavior.jsonl") is None
    assert bp.segment_parent("findings-draft.jsonl") is None


# --- the daemon enumerator (the LIVE path) -----------------------------------

def _daemon_rows(board_dir: Path):
    world = board_dir.parent
    resp = board_write.channels(_FakeCtx(world))
    payload = json.loads(resp.body.decode("utf-8") if isinstance(resp.body, bytes) else resp.body)
    assert payload["ok"] is True
    return {row["name"]: row for row in payload["channels"]}


def test_daemon_does_not_emit_a_row_for_a_segment(board):
    rows = _daemon_rows(board)
    assert "findings-2026-09-17" not in rows, (
        "the date segment is still enumerated as a channel of its own")
    assert set(rows) == {"findings", "general"}


def test_daemon_folds_the_segment_count_into_the_parent(board):
    """FOLD, not hide: the count must be the channel's true total (2 live + 3 segment)."""
    rows = _daemon_rows(board)
    assert rows["findings"]["count"] == 5, (
        "count shows only the live file — the segment was hidden, not folded, so the "
        "row understates the channel's real depth")
    assert rows["general"]["count"] == 1


def test_daemon_last_timestamp_spans_the_segment(board):
    """The newest post lives in the SEGMENT, so a live-file-only read reports a stale ts."""
    rows = _daemon_rows(board)
    assert rows["findings"]["last_timestamp"] == "2026-09-17T05:00:00"


# --- the CLI twin (docstring claims parity; both move together) --------------

def test_cli_twin_matches_the_daemon(board, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "board_under_test", _SCRIPTS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod, "BOARD_DIR", board, raising=False)
    monkeypatch.setattr(mod, "require_board", lambda: None, raising=False)
    mod.cmd_channels(type("A", (), {})())
    out = capsys.readouterr().out

    assert "findings-2026-09-17" not in out, (
        "CLI twin still lists the segment as its own channel — the docstring claims "
        "parity with the daemon, so they drift")
    assert "findings" in out and "general" in out


# --- the control -------------------------------------------------------------

def test_control_the_raw_stem_enumeration_yields_the_extra_row(board):
    """POSITIVE CONTROL — the pre-fix behaviour on the SAME fixture.

    If this goes green the fixture stopped discriminating and nothing above proves
    anything. It asserts the defect, not the fix.
    """
    raw = sorted(p.stem for p in board.glob("*.jsonl"))
    assert "findings-2026-09-17" in raw
    assert len(raw) == 3, "the fixture no longer contains the extra row to remove"
