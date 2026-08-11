""" — events.update_status must REPORT unrecognized override keys.

`overrides` is caller-supplied and the new-record dict is a FIXED allowlist over
it, so before this fix an override key outside {owner, participants,
decomposition, completion_signals} was discarded with no error: validate_record
passed, the append succeeded, and the caller's field was simply gone. Worse than
the handoff.yaml instance that motivated the class (g-115-3385), because this
store is append-only event-sourced and the write is never revisited.

Report, do NOT reject (rb-538 / guard-527): a caller may legitimately pass
provenance the event schema does not persist. So this file pins BOTH directions
— the WARN fires and names the keys, AND it stays silent on an all-recognized
call. The negative control is the load-bearing half: a WARN that fired
unconditionally would satisfy a fires-on-bad-input test forever while telling a
reader nothing (guard-1760).
"""
import io
import os
import sys
from contextlib import redirect_stderr
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("STORAGE_BACKEND", "local")

import events  # noqa: E402

RECOGNIZED = ("owner", "participants", "decomposition", "completion_signals")
PERSISTED_SCHEMA = {
    "event_id", "owner", "participants", "decomposition", "status",
    "completion_signals", "created_at", "recorded_by",
}


@pytest.fixture()
def store(tmp_path):
    p = tmp_path / "events.jsonl"
    events.add_event(
        {
            "event_id": "ev-dropkey",
            "owner": "alpha",
            "participants": [{"role": "owner", "agent": "alpha"}],
            "decomposition": [],
            "status": "proposed",
            "completion_signals": [],
        },
        path=p,
    )
    return p


def _update(store, status, overrides):
    err = io.StringIO()
    with redirect_stderr(err):
        rec = events.update_status("ev-dropkey", status, overrides=overrides,
                                   path=store)
    return rec, err.getvalue()


def test_unrecognized_override_keys_are_reported_and_named(store):
    rec, msg = _update(store, "in-progress",
                       {"owner": "bravo", "provenance_note": "x", "typo_kee": 1})
    assert "unrecognized override key(s)" in msg
    assert "2" in msg
    for k in ("provenance_note", "typo_kee"):
        assert k in msg, "WARN must NAME the dropped key %r, not just count it" % k


def test_unrecognized_keys_are_dropped_not_persisted(store):
    rec, _ = _update(store, "in-progress",
                     {"provenance_note": "x", "typo_kee": 1})
    # Report-not-reject: the write still succeeds and the key still does not
    # enter the event schema. Both halves matter.
    assert "provenance_note" not in rec
    assert "typo_kee" not in rec
    assert set(rec) == PERSISTED_SCHEMA


def test_recognized_overrides_still_apply(store):
    rec, _ = _update(store, "in-progress", {"owner": "bravo"})
    assert rec["owner"] == "bravo"


@pytest.mark.parametrize("key", RECOGNIZED)
def test_no_warn_on_recognized_keys_negative_control(store, key):
    """The half that keeps the WARN meaningful: silent when nothing was dropped."""
    value = {
        "owner": "bravo",
        "participants": [{"role": "reviewer", "agent": "bravo"}],
        "decomposition": ["a"],
        "completion_signals": ["done"],
    }[key]
    _, msg = _update(store, "in-progress", {key: value})
    assert msg.strip() == "", "WARN fired on a recognized key %r: %r" % (key, msg)


def test_no_warn_when_overrides_absent(store):
    _, msg = _update(store, "completed", None)
    assert msg.strip() == ""


@pytest.mark.parametrize("overrides", [
    {1: "x"},                      # int-only  -> ", ".join() raised
    {"provenance": 1, 2: "y"},     # mixed     -> sorted() raised on mixed types
    {"weird": None},               # control: already fine, must stay fine
])
def test_non_string_override_keys_do_not_abort_the_write(store, overrides):
    """A reporting path must never be able to fail the write it reports on.

    Found by fresh-eyes on this very change: `sorted(k for k in overrides ...)`
    plus `", ".join(...)` assume string keys, so a non-string key turned a key
    that was previously silently IGNORED into a TypeError that aborted the
    append to an append-only event store. Strictly worse than the silence the
    check was added to fix. The reference implementation shared the shape and
    was corrected in the same sweep (guard-3088).
    """
    rec, msg = _update(store, "in-progress", overrides)
    assert set(rec) == PERSISTED_SCHEMA
    assert "unrecognized override key(s)" in msg


@pytest.mark.parametrize("key,value", [
    ("status", "completed"),
    ("created_at", "2020-01-01T00:00:00"),
    ("event_id", "ev-other"),
    ("recorded_by", "someone-else"),
])
def test_param_sourced_record_keys_are_reported(store, key, value):
    """The hole the obvious predicate leaves.

    These four ARE keys of the assembled record, so the reference fix's
    `k not in <output>` predicate would call them recognized and stay silent —
    while `overrides` has no effect on any of them (they come from parameters).
    Passing one is always a caller mistake, and always silent without this.
    """
    rec, msg = _update(store, "in-progress", {key: value})
    assert key in msg, "override %r is ignored but was not reported" % key
    assert rec[key] != value, (
        "override %r unexpectedly took effect — this test encodes that it does "
        "NOT; if the contract changed, the WARN is now wrong too" % key
    )
