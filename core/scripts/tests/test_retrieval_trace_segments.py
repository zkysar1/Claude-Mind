"""Retrieval-trace date segments: the shared predicate and the merge registration.

g-358-220 outcome 2. The legacy `world/retrieval-trace.jsonl` is registered to
`merge_append_only_jsonl`; before this change a date segment
`retrieval-trace-YYYY-MM-DD.jsonl` resolved to None, so a segmented writer would
have produced a handler-less store that write-freezes under concurrent cross-box
appends (guard-1055). These tests pin that a segment now INHERITS the parent's
registration (g-358-183 shape) through the one predicate in `_retrieval_trace`,
and that same-stem siblings do not.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

# parents[1] IS core/scripts (see test_telemetry_merge_registration.py for why
# one hop and no re-descent).
SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _retrieval_trace as rt  # noqa: E402
import coordination_merge as cm  # noqa: E402

SEG = "retrieval-trace-2026-09-27.jsonl"


def test_segment_name_matches_the_predicate():
    name = rt.segment_name(dt.date(2026, 9, 27))
    assert name == SEG
    assert rt.is_segment(name)
    assert rt.segment_parent(name) == rt.LEGACY_STORE_NAME == "retrieval-trace.jsonl"


def test_segment_name_defaults_to_today():
    assert rt.segment_name() == rt.segment_name(dt.datetime.now().date())


@pytest.mark.parametrize("name", [
    "retrieval-trace.jsonl",                     # the legacy file is not a segment
    "retrieval-trace-archive.jsonl",
    "retrieval-trace-2026-09-27.jsonl.bak",
    "retrieval-trace-2026-09-27.jsonl.gz",
    "retrieval-trace-2026-09-27.spool.jsonl",
    "retrieval-trace-2026-9-27.jsonl",
    "xretrieval-trace-2026-09-27.jsonl",
    "",
    None,
])
def test_same_stem_siblings_are_not_segments(name):
    assert not rt.is_segment(name)
    assert rt.segment_parent(name) is None


@pytest.mark.parametrize("path", [
    f"world/{SEG}",
    f"/opt/deploy/.mind-data/world/{SEG}",
    f"C:\\deploy\\world\\{SEG}",
])
def test_segment_resolves_to_the_parents_handler(path):
    h = cm.merge_handler_for(path)
    assert h is cm._HANDLERS["retrieval-trace.jsonl"]
    assert h is cm.merge_append_only_jsonl


@pytest.mark.parametrize("path", [
    "world/retrieval-trace-archive.jsonl",
    f"world/{SEG}.bak",
    "world/retrieval-trace-2026-09-27.spool.jsonl",
    "world/some-other-ledger-2026-09-27.jsonl",
])
def test_siblings_stay_unregistered(path):
    assert cm.merge_handler_for(path) is None


def test_segment_follows_the_parent_registration(monkeypatch):
    """Discriminating control: the segment's answer IS the parent's registration.

    A hardcoded `return merge_append_only_jsonl` would pass the resolution test
    above and fail both halves of this one.
    """
    sentinel = lambda a, b: a  # noqa: E731
    monkeypatch.setitem(cm._HANDLERS, "retrieval-trace.jsonl", sentinel)
    assert cm.merge_handler_for(f"world/{SEG}") is sentinel
    monkeypatch.delitem(cm._HANDLERS, "retrieval-trace.jsonl")
    assert cm.merge_handler_for(f"world/{SEG}") is None


def _lines(*recs):
    return "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in recs).encode()


def test_segment_merge_is_union_and_byte_commutative():
    shared = {"ts": "2026-09-27T01:00:00", "agent": "a", "goal_id": "g-1"}
    left = {"ts": "2026-09-27T01:00:05", "agent": "a", "goal_id": "g-2"}
    right = {"ts": "2026-09-27T01:00:07", "agent": "b", "goal_id": "g-3"}
    h = cm.merge_handler_for(f"world/{SEG}")
    ab = h(_lines(shared, left), _lines(shared, right))
    ba = h(_lines(shared, right), _lines(shared, left))
    assert ab == ba  # byte-identical, not merely set-equal (guard-4641)
    rows = [json.loads(l) for l in ab.decode().splitlines() if l.strip()]
    assert sorted(r["goal_id"] for r in rows) == ["g-1", "g-2", "g-3"]


def _touch(p: Path, text="{}\n"):
    p.write_text(text, encoding="utf-8")
    return p


def test_trace_paths_legacy_first_then_segments_in_date_order(tmp_path):
    later = _touch(tmp_path / "retrieval-trace-2026-09-28.jsonl")
    legacy = _touch(tmp_path / "retrieval-trace.jsonl")
    earlier = _touch(tmp_path / "retrieval-trace-2026-09-27.jsonl")
    _touch(tmp_path / "retrieval-trace-archive.jsonl")
    _touch(tmp_path / "retrieval-trace-2026-09-27.jsonl.bak")
    (tmp_path / "retrieval-trace-2026-09-29.jsonl").mkdir()  # not a file
    assert rt.trace_paths(tmp_path) == [legacy, earlier, later]


def test_trace_paths_without_legacy(tmp_path):
    seg = _touch(tmp_path / SEG)
    assert rt.trace_paths(str(tmp_path)) == [seg]


def test_trace_paths_unresolved_world_dir_says_so(capsys):
    assert rt.trace_paths(None) == []
    assert "world_dir unresolved" in capsys.readouterr().err
