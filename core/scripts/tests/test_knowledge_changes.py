"""knowledge_changes.py: the member's list of knowledge changes, read off the spool lanes.

The fixture tests write records straight into the lanes. The round-trip tests run the
REAL drain (``inbound_drain.drain_environment`` and ``requeue_stale``, with only the
knowledge applier stubbed) and read back what it left, so the reader and the producer
cannot drift apart without one of them failing here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
from pathlib import Path

import pytest

import knowledge_changes as kc

DRAIN_PATH = Path(__file__).resolve().parents[1] / "inbound_drain.py"


def _load_drain():
    spec = importlib.util.spec_from_file_location("inbound_drain_for_knowledge_changes",
                                                  DRAIN_PATH)
    assert spec and spec.loader, f"cannot load {DRAIN_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


drain = _load_drain()

AT = "2026-10-01T11:30:05+00:00"
ROW_KEYS = {"id", "op", "handle", "state", "queued_at", "result", "finished_at",
            "new_handle", "undo_until", "text", "text_truncated"}


def _id(i: int) -> str:
    return f"20261001T1122{i:02d}000000-{i:032x}"


def _name(i: int, suffix: str = "") -> str:
    return f"{_id(i)}{suffix}.json"


def _knowledge(op="edit", text="Widgets are green.", handle="0123456789abcdef", **extra):
    record = {"kind": "knowledge", "environmentKey": "env-secret-key",
              "accountId": "acct-secret", "queued_at": "2026-10-01T11:22:08+00:00",
              "source": "PutAyoEnvironmentDirectives", "handle": handle, "op": op}
    if op == "edit":
        record.update(text=text, base="ab" * 32)
    record.update(extra)
    return record


def _verb(i: int) -> dict:
    return {"kind": "verb", "environmentKey": "env-secret-key", "accountId": "acct-secret",
            "queued_at": "2026-10-01T11:22:08+00:00", "handle": f"h-{i}",
            "verb": "prioritize", "value": "1"}


def _put(env: Path, lane: str, name: str, record) -> Path:
    path = env / lane / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record if isinstance(record, str) else json.dumps(record),
                    encoding="utf-8")
    return path


def _changes(env: Path, **kw) -> list[dict]:
    return kc.list_changes(env, **kw)["changes"]


# --- states and outcomes ------------------------------------------------------


def test_each_lane_is_a_state(tmp_path):
    _put(tmp_path, "inbound", _name(1), _knowledge())
    _put(tmp_path, "processing", _name(2), _knowledge())
    _put(tmp_path, "processed", _name(3), _knowledge(
        outcome={"result": "applied", "at": AT, "new_handle": "fedcba9876543210"}))
    # A new handle names what an APPLIED edit created; on a refusal it is no one's.
    _put(tmp_path, "rejected", _name(4), _knowledge(
        outcome={"result": "view_stale", "at": AT, "new_handle": "0f0f0f0f0f0f0f0f"}))

    rows = {row["id"]: row for row in _changes(tmp_path)}

    assert [rows[_id(i)]["state"] for i in (1, 2, 3, 4)] == [
        "queued", "processing", "applied", "refused"]
    applied, refused = rows[_id(3)], rows[_id(4)]
    assert (applied["result"], applied["finished_at"], applied["new_handle"]) == (
        "applied", AT, "fedcba9876543210")
    assert (refused["result"], refused["finished_at"]) == ("view_stale", AT)
    assert "new_handle" not in refused
    assert rows[_id(1)]["queued_at"] == "2026-10-01T11:22:08+00:00"
    assert (rows[_id(1)]["op"], rows[_id(1)]["handle"]) == ("edit", "0123456789abcdef")


def test_a_forget_row_carries_the_end_of_its_undo_window(tmp_path):
    until = "2026-10-31T11:30:05+00:00"
    _put(tmp_path, "processed", _name(1), _knowledge(
        op="forget", outcome={"result": "applied", "at": AT, "undo_until": until}))
    _put(tmp_path, "processed", _name(2), _knowledge(
        op="undo", outcome={"result": "applied", "at": AT}))
    # A window says how long an APPLIED forget stays undoable; a refusal kept nothing.
    _put(tmp_path, "rejected", _name(3), _knowledge(
        op="forget", outcome={"result": "not_restorable", "at": AT, "undo_until": until}))
    # Claimed or queued: the drain has not finished it, whatever the record says.
    _put(tmp_path, "processing", _name(4), _knowledge(
        op="forget", outcome={"result": "applied", "at": AT, "undo_until": until}))

    rows = {row["id"]: row for row in _changes(tmp_path)}

    assert rows[_id(1)]["undo_until"] == until
    assert [i for i in (2, 3, 4) if "undo_until" in rows[_id(i)]] == []


def test_an_outcome_outside_the_finished_lanes_does_not_count(tmp_path):
    # Left claimed after the outcome was written (the move out failed), or requeued
    # from there: the drain has not finished it, whatever the record says.
    finished = {"result": "applied", "at": AT, "new_handle": "fedcba9876543210"}
    _put(tmp_path, "processing", _name(1), _knowledge(outcome=finished))
    _put(tmp_path, "inbound", _name(2), _knowledge(outcome=finished))

    rows = _changes(tmp_path)

    assert [(r["state"], r["result"], r["finished_at"], "new_handle" in r) for r in rows] == [
        ("queued", None, None, False), ("processing", None, None, False)]


def test_a_record_finished_before_outcomes_were_written_has_no_result(tmp_path):
    _put(tmp_path, "processed", _name(1), _knowledge())
    _put(tmp_path, "rejected", _name(2), _knowledge())

    rows = _changes(tmp_path)

    assert [(r["state"], r["result"], r["finished_at"]) for r in rows] == [
        ("refused", None, None), ("applied", None, None)]


# --- one instruction, one row ---------------------------------------------------


def test_a_requeue_duplicate_does_not_read_as_refused(tmp_path):
    # --requeue-stale sets a claimed copy aside in rejected/, with no outcome, while
    # the original stays queued: one instruction, still pending.
    _put(tmp_path, "inbound", _name(1), _knowledge())
    duplicate = _put(tmp_path, "rejected", _name(1, ".1788847706970"), _knowledge())

    rows = _changes(tmp_path)
    assert [(r["id"], r["state"]) for r in rows] == [(_id(1), "queued")]

    # Once the original is applied the same instruction reads applied, still once.
    (tmp_path / "inbound" / _name(1)).unlink()
    _put(tmp_path, "processed", _name(1), _knowledge(outcome={"result": "applied", "at": AT}))
    rows = _changes(tmp_path)
    assert [(r["id"], r["state"], r["result"]) for r in rows] == [(_id(1), "applied", "applied")]
    assert duplicate.is_file()


def test_a_refusal_with_its_outcome_outranks_a_queued_copy(tmp_path):
    _put(tmp_path, "inbound", _name(1), _knowledge())
    _put(tmp_path, "rejected", _name(1), _knowledge(outcome={"result": "view_stale", "at": AT}))

    assert [(r["state"], r["result"]) for r in _changes(tmp_path)] == [("refused", "view_stale")]


# --- what is read, and what is returned ---------------------------------------


def test_other_kinds_residue_and_unread_lanes_are_not_changes(tmp_path):
    _put(tmp_path, "inbound", _name(1), _verb(1))
    _put(tmp_path, "inbound", _name(2), {**_verb(2), "kind": "directive", "text": "hi"})
    _put(tmp_path, "inbound", ".tmp-x1y2z3.json", _knowledge())
    _put(tmp_path, "inbound", "notes.txt", _knowledge())
    _put(tmp_path, "inbound", _name(3), "{not json")
    _put(tmp_path, "inbound", _name(4), "[1, 2]")
    (tmp_path / "inbound" / _name(5)).mkdir()
    _put(tmp_path, "quarantine", _name(6), _knowledge())
    _put(tmp_path, "failed", _name(7), _knowledge())
    _put(tmp_path, "inbound", _name(8), _knowledge())

    assert [r["id"] for r in _changes(tmp_path)] == [_id(8)]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_or_a_link_in_a_lane_is_not_a_record(tmp_path):
    # Opening a FIFO blocks until a writer appears. Named like the newest record, one
    # hung the read before it returned a single row (fresh-eyes, 2026-10-01).
    _put(tmp_path, "inbound", _name(1), _knowledge())
    fifo = tmp_path / "inbound" / _name(9)
    os.mkfifo(fifo)
    elsewhere = _put(tmp_path, "elsewhere", _name(8), _knowledge())
    (tmp_path / "inbound" / _name(8)).symlink_to(elsewhere)

    done: dict = {}
    reader = threading.Thread(target=lambda: done.update(result=kc.list_changes(tmp_path)),
                              daemon=True)
    reader.start()
    reader.join(10)
    try:
        assert not reader.is_alive(), "list_changes blocked opening a FIFO"
    finally:
        if reader.is_alive():  # release the blocked open so the run can finish
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            reader.join(10)
    assert [r["id"] for r in done["result"]["changes"]] == [_id(1)]


def test_private_fields_are_never_returned(tmp_path):
    record = _knowledge(note="internal note", outcome={
        "result": "applied", "at": AT, "detail": "applier detail", "item_id": "guard-1"})
    assert {"accountId", "environmentKey", "base", "source"} <= record.keys()
    _put(tmp_path, "processed", _name(1), record)

    (row,) = _changes(tmp_path)

    assert set(row) <= ROW_KEYS
    returned = json.dumps(row)
    for private in ("acct-secret", "env-secret-key", "ab" * 32,
                    "PutAyoEnvironmentDirectives", "internal note", "applier detail",
                    "guard-1"):
        assert private not in returned
    assert (row["op"], row["handle"], row["result"]) == ("edit", "0123456789abcdef", "applied")


def test_strings_that_reach_a_member_are_shape_checked(tmp_path):
    _put(tmp_path, "rejected", _name(1), _knowledge(
        op="edit<script>", handle="../../etc", queued_at="yesterday",
        outcome={"result": "<b>no</b>", "at": "soon"}))
    _put(tmp_path, "processed", _name(2), _knowledge(
        outcome={"result": "Applied!", "at": AT, "new_handle": "a/b"}))
    _put(tmp_path, "processed", _name(3), _knowledge(
        op="forget", outcome={"result": "applied", "at": AT, "undo_until": "<b>soon</b>"}))

    rows = {row["id"]: row for row in _changes(tmp_path)}

    bad = rows[_id(1)]
    assert (bad["op"], bad["handle"], bad["queued_at"], bad["result"], bad["finished_at"]) == (
        None, None, None, None, None)
    assert rows[_id(2)]["result"] is None
    assert "new_handle" not in rows[_id(2)]
    assert "undo_until" not in rows[_id(3)]


def test_an_edit_text_is_capped(tmp_path):
    _put(tmp_path, "inbound", _name(1), _knowledge(text="x" * (kc.TEXT_CAP + 100)))
    _put(tmp_path, "inbound", _name(2), _knowledge(text="Widgets are blue."))
    _put(tmp_path, "inbound", _name(3), _knowledge(op="forget"))

    rows = {row["id"]: row for row in _changes(tmp_path)}

    assert (len(rows[_id(1)]["text"]), rows[_id(1)]["text_truncated"]) == (kc.TEXT_CAP, True)
    assert (rows[_id(2)]["text"], rows[_id(2)]["text_truncated"]) == ("Widgets are blue.", False)
    assert "text" not in rows[_id(3)] and "text_truncated" not in rows[_id(3)]


# --- order and bounds --------------------------------------------------------------


def test_newest_first_and_the_limit(tmp_path):
    for i in range(1, 6):
        _put(tmp_path, "inbound", _name(i), _knowledge())

    every = kc.list_changes(tmp_path)
    assert [r["id"] for r in every["changes"]] == [_id(i) for i in (5, 4, 3, 2, 1)]
    assert every["truncated"] is False

    two = kc.list_changes(tmp_path, limit=2)
    assert ([r["id"] for r in two["changes"]], two["truncated"]) == ([_id(5), _id(4)], True)
    assert kc.list_changes(tmp_path, limit=5)["truncated"] is False


def test_the_limit_is_clamped(tmp_path):
    for i in range(kc.MAX_LIMIT + 1):
        _put(tmp_path, "inbound", f"20261001T112208{i:06d}-{i:032x}.json", _knowledge())

    most = kc.list_changes(tmp_path, limit=10 ** 6)
    assert (len(most["changes"]), most["truncated"]) == (kc.MAX_LIMIT, True)
    assert len(_changes(tmp_path, limit=0)) == 1


def test_the_read_is_bounded_by_files_opened_not_rows_returned(tmp_path):
    # Verbs and directives share the lanes, and a record is parsed to learn its kind.
    _put(tmp_path, "inbound", _name(0), _knowledge())
    for i in range(1, 11):
        _put(tmp_path, "inbound", _name(i), _verb(i))

    assert kc.list_changes(tmp_path, max_reads=3) == {"changes": [], "truncated": True}
    assert [r["id"] for r in _changes(tmp_path)] == [_id(0)]


def test_an_environment_with_no_spool_yet_has_no_changes(tmp_path):
    assert kc.list_changes(tmp_path / "never-written") == {"changes": [], "truncated": False}


# --- against the real drain ----------------------------------------------------------


def test_lane_names_are_the_drains():
    assert (kc.INBOUND, kc.PROCESSING, kc.PROCESSED, kc.REJECTED) == (
        drain.INBOUND, drain.PROCESSING, drain.PROCESSED, drain.REJECTED)
    assert kc.KNOWLEDGE_KIND in drain.KNOWN_KINDS


class _Applier:
    """Stands in for knowledge-edit-apply.py: an exit code and a report per handle."""

    def __init__(self, by_handle):
        self.by_handle = by_handle

    def main(self, argv, report=None):
        rc, reported = self.by_handle[argv[argv.index("--handle") + 1]]
        if isinstance(rc, Exception):
            raise rc
        if report is not None:
            report.update(reported)
        return rc


@pytest.fixture
def real_drain(tmp_path, monkeypatch):
    # The destination fence refuses any spool outside this box's world, which a tmp
    # spool always is; its own suite covers it in both directions.
    monkeypatch.setattr(drain, "_destination_fence", lambda spool_root: None)
    # A forget or an undo is only handed a retention directory that is outside the world.
    monkeypatch.setattr(drain, "_own_world_root", lambda: tmp_path / "world-beside-the-spool")
    monkeypatch.setenv("KNOWLEDGE_HANDLE_SECRET", "handle-secret-for-tests")
    monkeypatch.setenv("ENVIRONMENT_ID", "env-under-test")
    env = tmp_path / "env-under-test"
    (env / "inbound").mkdir(parents=True)

    def run(by_handle):
        monkeypatch.setattr(drain, "_load_knowledge_applier", lambda: _Applier(by_handle))
        return drain.drain_environment(env, apply=True, source="world", asp_id="asp-1",
                                       max_records=0, tmp_age_min=60)

    return env, run


def test_what_the_real_drain_leaves_reads_back(real_drain):
    env, run = real_drain
    _put(env, "inbound", _name(1), _knowledge(handle="a" * 16))
    _put(env, "inbound", _name(2), _knowledge(handle="b" * 16))
    _put(env, "inbound", _name(3), _knowledge(handle="c" * 16))
    _put(env, "inbound", _name(4), _knowledge(op="forget", handle="d" * 16))
    _put(env, "inbound", _name(5), _knowledge(op="undo", handle="e" * 16))

    run({"a" * 16: (0, {"handle": "fedcba9876543210"}),
         "b" * 16: (3, {"refused": "view_stale"}),
         "c" * 16: (RuntimeError("applier crashed"), {}),
         "d" * 16: (0, {"forgotten": True, "undo_until": "2026-10-31T11:22:09+00:00"}),
         "e" * 16: (0, {"restored": True})})
    rows = {row["id"]: row for row in _changes(env)}

    applied = rows[_id(1)]
    assert (applied["state"], applied["result"], applied["new_handle"]) == (
        "applied", "applied", "fedcba9876543210")
    assert applied["finished_at"] is not None
    assert (rows[_id(2)]["state"], rows[_id(2)]["result"]) == ("refused", "view_stale")
    assert (rows[_id(3)]["state"], rows[_id(3)]["result"]) == ("processing", None)
    forgotten = rows[_id(4)]
    assert (forgotten["state"], forgotten["op"], forgotten["undo_until"]) == (
        "applied", "forget", "2026-10-31T11:22:09+00:00")
    restored = rows[_id(5)]
    assert (restored["state"], restored["op"]) == ("applied", "undo")
    assert "undo_until" not in restored
    # The lanes this reader skips stay empty under the real drain.
    assert not (env / "failed").exists() and not (env / "quarantine").exists()


def test_a_real_requeue_duplicate_reads_as_one_queued_change(real_drain):
    env, run = real_drain
    _put(env, "inbound", _name(1), _knowledge(handle="c" * 16))
    run({"c" * 16: (RuntimeError("applier crashed"), {})})
    claimed = env / "processing" / _name(1)
    assert claimed.is_file()
    _put(env, "inbound", _name(1), _knowledge(handle="c" * 16))
    stale = time.time() - 600
    os.utime(claimed, (stale, stale))

    res = drain.requeue_stale(env, apply=True, age_min=1)

    assert res["duplicates"] == 1 and (env / "rejected" / _name(1)).is_file()
    assert [(r["id"], r["state"]) for r in _changes(env)] == [(_id(1), "queued")]
