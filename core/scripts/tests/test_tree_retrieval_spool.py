"""Unit tests for _tree_retrieval_spool ().

The module defers the knowledge-tree INDEX retrieval bump to a machine-local
spool drained by the next STRUCTURAL tree write, replacing a whole-object PUT
of the whole index per retrieval. These tests pin the two properties that make
that safe rather than merely cheap:

  * NOTHING IS LOST -- a failed append returns False (so the caller falls back
    to the legacy in-index write for exactly those keys), and `.flushing`
    residue from a drain that died mid-flight stays visible to readers.
  * NOTHING IS DOUBLE-STAMPED -- `apply_pending` never touches
    `data["last_updated"]`, which only the structural writer owns.

The tmp fixture deliberately does NOT reuse the real index basename: the
module needs only `tree_path.parent`, and the store-write guards pattern-match
that basename wherever it appears, working directory notwithstanding.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _tree_retrieval_spool as trs  # noqa: E402


@pytest.fixture()
def idx(tmp_path):
    """A stand-in for the tree index; only its PARENT dir is used."""
    p = tmp_path / "idx.yaml"
    p.write_text("nodes: {}\n", encoding="utf-8")
    return p


# ------------------------------------------------------------------ flag --

def test_spooled_enabled_defaults_false(monkeypatch):
    monkeypatch.delenv(trs.SPOOLED_ENV, raising=False)
    assert trs.spooled_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " On "])
def test_spooled_enabled_accepts_truthy(monkeypatch, val):
    monkeypatch.setenv(trs.SPOOLED_ENV, val)
    assert trs.spooled_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_spooled_enabled_rejects_falsy(monkeypatch, val):
    monkeypatch.setenv(trs.SPOOLED_ENV, val)
    assert trs.spooled_enabled() is False


# ------------------------------------------------------------ record_bump --

def test_record_bump_appends_and_reports_success(idx):
    assert trs.record_bump(idx, "a/b", "2026-09-17") is True
    # Blank lines are expected: every record carries a leading newline so a
    # torn predecessor cannot swallow it. The parser skips them.
    lines = [ln for ln in
             trs.spool_path(idx).read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"key": "a/b", "ts": "2026-09-17", "delta": 1}


def test_record_bump_returns_false_without_raising_on_bad_path(tmp_path):
    """A False return is the caller's signal to fall back for THIS key.

    The failure must never surface as an exception (retrieval would die) nor
    as a silent success (the counter would be lost outright).
    """
    missing = tmp_path / "no-such-dir" / "idx.yaml"
    assert trs.record_bump(missing, "a/b", "2026-09-17") is False


def test_record_bump_rejects_empty_key(idx):
    assert trs.record_bump(idx, "", "2026-09-17") is False
    assert not trs.spool_path(idx).exists()


# ---------------------------------------------------------- pending_deltas --

def test_pending_deltas_sums_and_keeps_max_ts(idx):
    trs.record_bump(idx, "a", "2026-09-15")
    trs.record_bump(idx, "a", "2026-09-17")
    trs.record_bump(idx, "a", "2026-09-16")
    trs.record_bump(idx, "b", "2026-09-14")

    out = trs.pending_deltas(idx)
    assert out["a"] == {"delta": 3, "last_ts": "2026-09-17"}
    assert out["b"] == {"delta": 1, "last_ts": "2026-09-14"}


def test_pending_deltas_skips_a_torn_line_without_losing_the_rest(idx):
    trs.record_bump(idx, "a", "2026-09-17")
    with open(trs.spool_path(idx), "a", encoding="utf-8") as fh:
        fh.write('{"key": "b", "ts": "2026-09-1')      # crashed mid-write
    trs.record_bump(idx, "c", "2026-09-17")

    out = trs.pending_deltas(idx)
    assert set(out) == {"a", "c"}


def test_pending_deltas_sees_flushing_residue(idx):
    """A drain that died between rotate and commit still holds real deltas."""
    trs.record_bump(idx, "a", "2026-09-17")
    assert trs.rotate(idx) is True
    assert not trs.spool_path(idx).exists()
    trs.record_bump(idx, "b", "2026-09-17")

    out = trs.pending_deltas(idx)
    assert set(out) == {"a", "b"}, "residue must stay visible to readers"


def test_pending_deltas_empty_when_nothing_spooled(idx):
    assert trs.pending_deltas(idx) == {}


# ----------------------------------------------------------- apply_pending --

def test_apply_pending_folds_counters_and_stamps_last_retrieved():
    data = {"nodes": {"a": {"retrieval_count": 4}}, "last_updated": "2026-01-01"}
    applied = trs.apply_pending(data, {"a": {"delta": 3, "last_ts": "2026-09-17"}})

    assert applied == 1
    assert data["nodes"]["a"]["retrieval_count"] == 7
    assert data["nodes"]["a"]["last_retrieved"] == "2026-09-17"


def test_apply_pending_treats_a_missing_counter_as_zero():
    data = {"nodes": {"a": {}}}
    trs.apply_pending(data, {"a": {"delta": 2, "last_ts": "2026-09-17"}})
    assert data["nodes"]["a"]["retrieval_count"] == 2


def test_apply_pending_drops_vanished_nodes():
    """PRUNE/RETIRE/MERGE between the bump and the flush. The retrieval was
    already served; the counter is incidental on a node that is gone."""
    data = {"nodes": {"a": {"retrieval_count": 1}}}
    applied = trs.apply_pending(data, {"gone": {"delta": 9, "last_ts": "2026-09-17"}})

    assert applied == 0
    assert data["nodes"] == {"a": {"retrieval_count": 1}}


def test_apply_pending_never_touches_last_updated():
    """The structural writer owns last_updated. Stamping it from a pure
    counter fold would make a counter flush indistinguishable from a real
    content change to every downstream freshness reader."""
    data = {"nodes": {"a": {"retrieval_count": 0}}, "last_updated": "2026-01-01"}
    trs.apply_pending(data, {"a": {"delta": 1, "last_ts": "2026-09-17"}})
    assert data["last_updated"] == "2026-01-01"


def test_apply_pending_is_a_noop_on_empty_deltas():
    data = {"nodes": {"a": {"retrieval_count": 1}}, "last_updated": "2026-01-01"}
    assert trs.apply_pending(data, {}) == 0
    assert data == {"nodes": {"a": {"retrieval_count": 1}},
                    "last_updated": "2026-01-01"}


# ------------------------------------------------------- rotate / commit --

def test_rotate_preserves_earlier_residue_instead_of_overwriting_it(idx):
    trs.record_bump(idx, "a", "2026-09-17")
    trs.rotate(idx)                       # a -> flushing
    trs.record_bump(idx, "b", "2026-09-17")
    trs.rotate(idx)                       # b must APPEND onto a, not replace

    out = trs.pending_deltas(idx)
    assert set(out) == {"a", "b"}, "a dead drain's deltas must survive the next rotate"


def test_rotate_reports_false_when_there_is_nothing_to_drain(idx):
    assert trs.rotate(idx) is False


def test_take_for_flush_then_commit_clears_and_stamps(idx):
    trs.record_bump(idx, "a", "2026-09-17")
    trs.record_bump(idx, "a", "2026-09-17")

    deltas = trs.take_for_flush(idx)
    assert deltas["a"]["delta"] == 2
    # Still readable until commit: the flushing file is the SOLE record of
    # those deltas until the index write lands.
    assert trs.pending_deltas(idx)["a"]["delta"] == 2

    assert trs.commit_flush(idx, "2026-09-17") is True
    assert trs.pending_deltas(idx) == {}
    assert trs.stamp_path(idx).read_text(encoding="utf-8").strip() == "2026-09-17"


def test_take_for_flush_is_empty_when_nothing_spooled(idx):
    assert trs.take_for_flush(idx) == {}


def test_drain_does_not_double_count_a_previously_read_delta(idx):
    """The read-merge (pending_deltas) is non-consuming; only the drain
    consumes. A reader folding deltas must not cause the next structural
    write to skip them, nor to apply them twice."""
    trs.record_bump(idx, "a", "2026-09-17")

    reader_view = {"nodes": {"a": {"retrieval_count": 5}}}
    trs.apply_pending(reader_view, trs.pending_deltas(idx))
    assert reader_view["nodes"]["a"]["retrieval_count"] == 6

    writer_data = {"nodes": {"a": {"retrieval_count": 5}}}
    trs.apply_pending(writer_data, trs.take_for_flush(idx))
    trs.commit_flush(idx, "2026-09-17")

    assert writer_data["nodes"]["a"]["retrieval_count"] == 6
    assert trs.pending_deltas(idx) == {}


def test_apply_pending_never_moves_last_retrieved_backward():
    """`last_retrieved` is a monotonic clock, not a last-write-wins field.

    The index can legally hold a NEWER stamp than the spool: when `record_bump`
    fails for a key, retrieve.py narrows that key to the legacy in-index write
    which stamps `today`, while an older spooled delta for the same key is
    still pending. Folding that delta must not regress the stamp --
    `tree_archive.effective_relevance` takes the MAX of `last_retrieved` and
    `last_relevant_at`-or-`last_updated` as the archival clock, and on a node
    retrieved often but edited rarely `last_retrieved` IS that max, so a
    backward move makes a live node read as stale to the archival sweep.
    """
    data = {"nodes": {"a": {"retrieval_count": 5, "last_retrieved": "2026-09-20"}}}
    trs.apply_pending(data, {"a": {"delta": 1, "last_ts": "2026-09-15"}})

    assert data["nodes"]["a"]["last_retrieved"] == "2026-09-20"
    # The COUNTER still folds -- only the clock is guarded.
    assert data["nodes"]["a"]["retrieval_count"] == 6


def test_apply_pending_still_advances_last_retrieved_when_spool_is_newer():
    """Positive control for the guard above: the ordinary forward case must
    still move. A guard that pinned the stamp unconditionally would pass the
    regression test and silently freeze every node's retrieval clock."""
    data = {"nodes": {"a": {"retrieval_count": 5, "last_retrieved": "2026-09-10"}}}
    trs.apply_pending(data, {"a": {"delta": 1, "last_ts": "2026-09-17"}})

    assert data["nodes"]["a"]["last_retrieved"] == "2026-09-17"
