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


# ------------------------------------------------------------ drain door --
# : `flush_into_index` is the drain every box reaches. `write_tree` is
# not that door: its only live callers are reducer-side maintenance scripts.

import os  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import yaml  # noqa: E402


def _index(tmp_path, nodes):
    p = tmp_path / "idx.yaml"
    p.write_text(yaml.safe_dump({"nodes": nodes}), encoding="utf-8")
    return p


def _counts(p):
    nodes = yaml.safe_load(p.read_text(encoding="utf-8"))["nodes"]
    return {k: n.get("retrieval_count", 0) for k, n in nodes.items()}


def _spool(p, *keys, ts="2026-10-02"):
    for k in keys:
        assert trs.record_bump(p, k, ts)


def test_flush_on_an_empty_spool_is_a_noop(tmp_path):
    p = _index(tmp_path, {"a": {"retrieval_count": 1}})
    before = p.read_bytes()
    assert trs.flush_into_index(p)["status"] == "empty"
    assert p.read_bytes() == before
    assert not trs.stamp_path(p).exists()


def test_flush_folds_the_spool_in_one_index_write_and_clears_it(tmp_path, monkeypatch):
    import _fileops
    p = _index(tmp_path, {"a": {"retrieval_count": 4, "last_retrieved": "2026-09-01"},
                          "b": {"retrieval_count": 0}})
    _spool(p, "a", "a", "b")
    real = _fileops.locked_modify_yaml
    calls = []

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(_fileops, "locked_modify_yaml", counting)
    out = trs.flush_into_index(p)
    assert out["status"] == "flushed" and out["keys"] == 2 and out["applied"] == 2
    assert len(calls) == 1, "one whole-index write per drain, however many keys"
    assert _counts(p) == {"a": 6, "b": 1}
    nodes = yaml.safe_load(p.read_text(encoding="utf-8"))["nodes"]
    assert nodes["a"]["last_retrieved"] == "2026-10-02"
    assert not trs.spool_path(p).exists() and not trs.flushing_path(p).exists()
    assert trs.stamp_path(p).exists()
    assert trs.pending_deltas(p) == {}
    assert not (tmp_path / trs.FLUSH_LOCK_BASENAME).exists(), "flush lock leaked"


def test_flush_defers_inside_the_interval_and_keeps_the_lag_readable(tmp_path):
    p = _index(tmp_path, {"a": {"retrieval_count": 0}})
    _spool(p, "a")
    assert trs.flush_into_index(p)["status"] == "flushed"   # no stamp yet: drains
    _spool(p, "a", "a")
    before = p.read_bytes()
    assert trs.flush_into_index(p)["status"] == "deferred"
    assert p.read_bytes() == before, "a deferred drain must not write the index"
    assert trs.pending_deltas(p)["a"]["delta"] == 2


def test_flush_drains_once_the_interval_has_elapsed(tmp_path):
    p = _index(tmp_path, {"a": {"retrieval_count": 0}})
    _spool(p, "a")
    assert trs.flush_into_index(p)["status"] == "flushed"
    _spool(p, "a", "a")
    later = time.time() + trs.DRAIN_INTERVAL_SECONDS + 1
    assert trs.flush_into_index(p, now=later)["status"] == "flushed"
    assert _counts(p)["a"] == 3


def test_a_stamp_dated_in_the_future_does_not_wedge_the_drain(tmp_path):
    p = _index(tmp_path, {"a": {"retrieval_count": 0}})
    _spool(p, "a")
    assert trs.flush_into_index(p)["status"] == "flushed"
    _spool(p, "a")
    # clock fault: the stamp is a day ahead of "now"
    assert trs.flush_into_index(p, now=time.time() - 86400)["status"] == "flushed"
    assert _counts(p)["a"] == 2


def test_force_drains_inside_the_interval(tmp_path):
    p = _index(tmp_path, {"a": {"retrieval_count": 0}})
    _spool(p, "a")
    assert trs.flush_into_index(p)["status"] == "flushed"
    _spool(p, "a")
    assert trs.flush_into_index(p, force=True)["status"] == "flushed"
    assert _counts(p)["a"] == 2


def test_crash_residue_drains_inside_the_interval_and_counts_once(tmp_path):
    p = _index(tmp_path, {"a": {"retrieval_count": 0}})
    _spool(p, "a", "a")
    assert trs.flush_into_index(p)["status"] == "flushed"   # fresh stamp
    _spool(p, "a")
    assert trs.take_for_flush(p)                            # a drain that died before commit
    assert trs.flushing_path(p).exists()
    out = trs.flush_into_index(p)                           # inside the interval
    assert out["status"] == "flushed"
    assert _counts(p)["a"] == 3, "2 landed + 1 residue, each exactly once"
    assert not trs.flushing_path(p).exists()


def test_flush_reports_busy_while_another_drain_holds_the_lock(tmp_path):
    from storage_backend import LocalBackend
    p = _index(tmp_path, {"a": {"retrieval_count": 0}})
    _spool(p, "a")
    lock_path = tmp_path / trs.FLUSH_LOCK_BASENAME
    lb = LocalBackend()
    lb.acquire_lock(lock_path, timeout=1, stale_seconds=120)
    try:
        assert trs.flush_into_index(p)["status"] == "busy"
        assert trs.pending_deltas(p)["a"]["delta"] == 1, "a busy drain loses nothing"
    finally:
        lb.release_lock(lock_path)
    assert trs.flush_into_index(p)["status"] == "flushed"


def test_a_write_that_raises_loses_nothing_and_the_retry_counts_once(tmp_path, monkeypatch):
    import _fileops
    p = _index(tmp_path, {"a": {"retrieval_count": 0}})
    _spool(p, "a", "a")
    real = _fileops.locked_modify_yaml

    def modifier_runs_then_the_write_fails(path, fn, *a, **k):
        fn({"nodes": {"a": {}}})      # the batch has been taken ...
        raise OSError("disk full")    # ... and the index write never lands

    monkeypatch.setattr(_fileops, "locked_modify_yaml", modifier_runs_then_the_write_fails)
    assert trs.flush_into_index(p)["status"] == "error"
    assert trs.pending_deltas(p)["a"]["delta"] == 2, "the batch must stay readable"
    monkeypatch.setattr(_fileops, "locked_modify_yaml", real)
    assert trs.flush_into_index(p)["status"] == "flushed"
    assert _counts(p)["a"] == 2, "applied exactly once across the failure and the retry"


def test_the_modifier_takes_the_batch_once_when_the_write_is_retried(tmp_path, monkeypatch):
    """locked_modify_yaml re-runs its modifier on an own-cloud If-Match conflict."""
    import _fileops
    p = _index(tmp_path, {"a": {"retrieval_count": 0}})
    _spool(p, "a", "a")
    real = _fileops.locked_modify_yaml

    def run_the_modifier_twice(path, fn, *a, **k):
        fn({"nodes": {"a": {"retrieval_count": 100}}})      # attempt 1: conflicted, discarded
        return real(path, fn, *a, **k)                       # attempt 2 on the fresh read

    monkeypatch.setattr(_fileops, "locked_modify_yaml", run_the_modifier_twice)
    assert trs.flush_into_index(p)["status"] == "flushed"
    assert _counts(p)["a"] == 2, "the second pass must fold the same batch, not an empty one"


def test_deltas_for_vanished_nodes_are_dropped_without_writing_the_index(tmp_path):
    p = _index(tmp_path, {"a": {"retrieval_count": 5}})
    long_ago = time.time() - 3600
    os.utime(p, (long_ago, long_ago))   # a rewrite moves this even when the bytes match
    _spool(p, "gone")
    before = p.read_bytes()
    out = trs.flush_into_index(p)
    assert out["status"] == "flushed" and out["applied"] == 0
    assert p.read_bytes() == before
    assert abs(p.stat().st_mtime - long_ago) < 1, "nothing applied means nothing to write"
    assert trs.pending_deltas(p) == {}


def test_flush_applies_on_top_of_a_peer_write_made_after_the_bump(tmp_path):
    p = _index(tmp_path, {"a": {"retrieval_count": 1}})
    _spool(p, "a")
    # a peer's drain lands in the shared index between this box's bump and its flush
    p.write_text(yaml.safe_dump({"nodes": {"a": {"retrieval_count": 10}}}),
                 encoding="utf-8")
    assert trs.flush_into_index(p)["status"] == "flushed"
    assert _counts(p)["a"] == 11


def test_two_racing_flushers_apply_the_batch_once(tmp_path):
    p = _index(tmp_path, {"a": {"retrieval_count": 0}})
    _spool(p, "a", "a", "a")
    barrier = threading.Barrier(2)
    statuses = []

    def run():
        barrier.wait()
        statuses.append(trs.flush_into_index(p)["status"])

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _counts(p)["a"] == 3, "statuses=%r" % (statuses,)
    assert statuses.count("flushed") == 1, statuses


def test_every_name_the_lane_creates_is_machine_local_to_the_sync_layer():
    """The spool sits in a SYNCED dir; a synced counter spool is drained by every box."""
    import owncloud_sync as o
    for name in trs.SYNC_EXCLUDED_NAMES + (trs.FLUSH_LOCK_BASENAME,):
        assert o._is_machine_local(name, "world"), "%s would sync" % name
    # controls, so a classifier that says True for everything cannot pass this
    assert o._is_machine_local("gate-firings.spool.jsonl", "world")
    assert not o._is_machine_local("some-shared-note.md", "world")
