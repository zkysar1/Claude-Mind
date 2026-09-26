""" — experience retrieval_stats bumps are SPOOLED, not store rewrites.

Every experience bump used to rewrite the agent's whole experience.jsonl: one
store version per retrieval call and per utilization-feedback item, measured at
~630 versions/day of a 3.27 MB store (569 same-size counter edits). The two
writers now append one delta line to a machine-local spool, and `flush` folds
the deltas back into the SAME records in one locked write per interval.

These tests pin the pieces a refactor would silently drop: the spool never
syncs, the interval gate holds, the fold is ONE write that sums, stamps and
recomputes, unknown/absent/un-landable cases never write or double-count, and
retrieve.py's call path spools instead of rewriting.
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _experience_stats_spool as es  # noqa: E402


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")


def _write_store(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records),
                    encoding="utf-8")


def _read_store(path):
    return {r["id"]: r for r in
            (json.loads(line) for line in
             path.read_text(encoding="utf-8").splitlines() if line.strip())}


def _fresh_stamp(base):
    (base / es.STAMP_NAME).write_text(str(time.time()))


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "experience.jsonl"
    _write_store(path, [
        {"id": "exp-a", "category": "spool-cat", "retrieval_stats": {
            "retrieval_count": 4, "times_useful": 1, "times_noise": 0,
            "utility_ratio": 0.25, "last_retrieved": "2026-09-01"}},
        {"id": "exp-b", "category": "spool-cat", "retrieval_stats": {
            "retrieval_count": 0, "times_useful": 0, "utility_ratio": 0.0,
            "last_retrieved": None}},
        {"id": "exp-c", "category": "spool-cat"},
    ])
    return path


@pytest.fixture
def count_writes(monkeypatch):
    import _fileops
    calls = []
    real = _fileops.locked_modify_jsonl

    def counting(path, modifier, *a, **k):
        calls.append(str(path))
        return real(path, modifier, *a, **k)
    monkeypatch.setattr(_fileops, "locked_modify_jsonl", counting)
    return calls


# --- the spool never leaves this box -----------------------------------------

def test_spool_names_are_sync_excluded():
    """A synced counter spool would be drained on EVERY box that pulls it,
    multiplying each increment by the fleet size."""
    import owncloud_sync
    for name in es.SYNC_EXCLUDED_NAMES:
        assert name in owncloud_sync._EXCLUDE_NAMES, name
    assert set(es.SYNC_EXCLUDED_NAMES) == {
        es.SPOOL_NAME, es.FLUSHING_NAME, es.STAMP_NAME}
    assert es.FLUSH_LOCK_NAME.endswith(".lock")  # rides the *.lock glob


# --- record ------------------------------------------------------------------

def test_record_appends_one_delta_line(store):
    assert es.record(store, "exp-a", "retrieval_count") is True
    lines = (store.parent / es.SPOOL_NAME).read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert (rec["id"], rec["counter"], rec["delta"]) == (
        "exp-a", "retrieval_count", 1)
    assert len(rec["ts"]) == 19


def test_record_never_raises(tmp_path):
    assert es.record(tmp_path / "absent-dir" / "experience.jsonl",
                     "exp-a", "retrieval_count") is False
    assert es.record(None, "exp-a", "retrieval_count") is False
    assert es.record(tmp_path / "experience.jsonl", "", "retrieval_count") is False


# --- flush gating ------------------------------------------------------------

def test_empty_spool_is_a_noop(store, count_writes):
    assert es.flush(store)["status"] == "empty"
    assert count_writes == []


def test_small_spool_defers_inside_the_interval(store, count_writes):
    _fresh_stamp(store.parent)
    before = store.read_bytes()
    for _ in range(3):
        es.record(store, "exp-a", "retrieval_count")
    out = es.flush(store)
    assert out == {"status": "deferred", "pending_lines": 3}
    assert store.read_bytes() == before and count_writes == []


def test_burst_bypasses_the_interval(store, count_writes):
    _fresh_stamp(store.parent)
    es.record(store, "exp-a", "retrieval_count")
    es.record(store, "exp-b", "retrieval_count")
    assert es.flush(store, burst_records=2)["status"] == "flushed"
    assert len(count_writes) == 1


# --- the fold ----------------------------------------------------------------

def test_fold_sums_stamps_and_recomputes_in_one_write(store, count_writes):
    for _ in range(3):
        es.record(store, "exp-a", "retrieval_count")
    es.record(store, "exp-a", "times_useful")
    es.record(store, "exp-b", "retrieval_count")

    out = es.flush(store, force=True)

    assert out["status"] == "flushed"
    assert (out["records"], out["increments"], out["missing"]) == (2, 5, [])
    assert len(count_writes) == 1, "N bumps must land as ONE store write"
    recs = _read_store(store)
    a = recs["exp-a"]["retrieval_stats"]
    assert (a["retrieval_count"], a["times_useful"]) == (7, 2)
    assert a["utility_ratio"] == round(2 / 7, 4)
    assert a["last_retrieved"] == time.strftime("%Y-%m-%d")
    assert recs["exp-b"]["retrieval_stats"]["retrieval_count"] == 1
    assert not (store.parent / es.SPOOL_NAME).exists()
    assert not (store.parent / es.FLUSHING_NAME).exists()
    assert (store.parent / es.STAMP_NAME).exists()


def test_blockless_record_gets_a_block_only_from_a_retrieval(store, count_writes):
    """Mirrors the daemon writer: a judgement alone never manufactures a
    denominator no retrieval produced."""
    before = store.read_bytes()
    es.record(store, "exp-c", "times_useful")
    out = es.flush(store, force=True)
    assert out["records"] == 0
    assert store.read_bytes() == before, "nothing matched, nothing written"

    es.record(store, "exp-c", "retrieval_count")
    es.flush(store, force=True)
    c = _read_store(store)["exp-c"]["retrieval_stats"]
    # Same shape the legacy bump creates: no invented judgement counter.
    assert c == {"retrieval_count": 1,
                 "last_retrieved": time.strftime("%Y-%m-%d")}


def test_unknown_ids_are_dropped_loudly_without_a_write(store, count_writes,
                                                         capsys):
    before = store.read_bytes()
    es.record(store, "exp-ghost", "retrieval_count")
    out = es.flush(store, force=True)
    assert (out["status"], out["records"], out["missing"]) == (
        "flushed", 0, ["exp-ghost"])
    assert store.read_bytes() == before
    assert "exp-ghost" in capsys.readouterr().err
    assert not (store.parent / es.FLUSHING_NAME).exists()


# --- batches that cannot land ------------------------------------------------

def test_absent_store_retains_the_batch(tmp_path):
    """Absence is indistinguishable from unreadability here (guard-6922), so
    the batch waits rather than being discarded."""
    exp = tmp_path / "experience.jsonl"
    es.record(exp, "exp-a", "retrieval_count")
    assert es.flush(exp, force=True)["status"] == "retained"
    assert (tmp_path / es.FLUSHING_NAME).exists()

    _write_store(exp, [{"id": "exp-a", "retrieval_stats": {
        "retrieval_count": 1, "times_useful": 0}}])
    _fresh_stamp(tmp_path)  # residue must drain regardless of the interval
    out = es.flush(exp)
    assert out["status"] == "flushed" and out["records"] == 1
    assert _read_store(exp)["exp-a"]["retrieval_stats"]["retrieval_count"] == 2


def test_no_claim_drops_the_batch_and_stamps(store, monkeypatch):
    """A box without the runner claim cannot land an agent-dir write; the
    pre-spool writers lost these increments too (g-115-8750). Retaining them
    would grow the spool forever on every worker box."""
    class FakeNoClaim(Exception):
        pass
    import _fileops

    def refuse(*a, **k):
        raise FakeNoClaim("no claim")
    monkeypatch.setattr(es, "_no_claim_types", lambda: (FakeNoClaim,))
    monkeypatch.setattr(_fileops, "locked_modify_jsonl", refuse)
    before = store.read_bytes()
    es.record(store, "exp-a", "retrieval_count")
    es.record(store, "exp-b", "retrieval_count")

    out = es.flush(store, force=True)

    assert out == {"status": "no_claim", "dropped": 2}
    assert store.read_bytes() == before
    assert not (store.parent / es.SPOOL_NAME).exists()
    assert not (store.parent / es.FLUSHING_NAME).exists()
    assert (store.parent / es.STAMP_NAME).exists()


def test_residue_drains_first_and_torn_lines_are_skipped(store, capsys):
    (store.parent / es.FLUSHING_NAME).write_text(
        json.dumps({"id": "exp-a", "counter": "retrieval_count", "delta": 1,
                    "ts": "2026-09-20T10:00:00"}) + "\n"
        + '{"id": "exp-a", "coun\n', encoding="utf-8")
    es.record(store, "exp-b", "retrieval_count")
    _fresh_stamp(store.parent)

    out = es.flush(store)

    assert out["status"] == "flushed" and out["records"] == 2
    recs = _read_store(store)
    assert recs["exp-a"]["retrieval_stats"]["retrieval_count"] == 5
    assert recs["exp-b"]["retrieval_stats"]["retrieval_count"] == 1
    assert "torn" in capsys.readouterr().err


# --- retrieve.py's call path -------------------------------------------------

def _load_retrieve(tmp_path):
    """Load retrieve.py against a tmp world with no bound agent (the same env
    guard test_utilization_spool uses), then point EXP_PATH at a tmp store."""
    orig = {k: os.environ.get(k) for k in ("MIND_WORLD", "MIND_AGENT")}
    world = tmp_path / "world"
    world.mkdir()
    os.environ["MIND_WORLD"] = str(world)
    os.environ.pop("MIND_AGENT", None)
    try:
        spec = importlib.util.spec_from_file_location(
            "retrieve_exp_spool_test", SCRIPTS / "retrieve.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_load_experiences_spools_instead_of_rewriting(tmp_path, store,
                                                      monkeypatch):
    r = _load_retrieve(tmp_path)
    monkeypatch.setattr(r, "EXP_PATH", store)
    _fresh_stamp(store.parent)
    before = store.read_bytes()

    selected = r.load_experiences(["spool-cat"], "medium")

    assert {x["id"] for x in selected} == {"exp-a", "exp-b", "exp-c"}
    assert store.read_bytes() == before, "a retrieval must not rewrite the store"
    spooled = sorted(json.loads(line)["id"] for line in
                     (store.parent / es.SPOOL_NAME).read_text().splitlines())
    assert spooled == ["exp-a", "exp-b", "exp-c"]


def test_failed_spool_append_falls_back_for_that_id_only(tmp_path, store,
                                                         monkeypatch):
    """The legacy RMW is narrowed to the ids the spool refused, so the spooled
    majority is never counted twice."""
    r = _load_retrieve(tmp_path)
    monkeypatch.setattr(r, "EXP_PATH", store)
    _fresh_stamp(store.parent)
    real_record = es.record
    monkeypatch.setattr(
        es, "record",
        lambda p, rid, c, delta=1: False if rid == "exp-b"
        else real_record(p, rid, c, delta))

    r.load_experiences(["spool-cat"], "medium")

    recs = _read_store(store)
    assert recs["exp-b"]["retrieval_stats"]["retrieval_count"] == 1
    assert recs["exp-a"]["retrieval_stats"]["retrieval_count"] == 4, \
        "exp-a was spooled; the fallback must not also bump it"
    es.flush(store, force=True)
    recs = _read_store(store)
    assert recs["exp-a"]["retrieval_stats"]["retrieval_count"] == 5
    assert recs["exp-b"]["retrieval_stats"]["retrieval_count"] == 1
