"""test_goal_field_append.py — goal-field-append safety invariants ().

The helper exists because ``aspirations-update-goal.sh <id> <field> <value>``
REPLACES the field, so every annotate-an-existing-record write is a hand-rolled
read-modify-write. Its whole value is in what it REFUSES, so that is what these
tests pin.

The pure helpers are tested DIRECTLY rather than through process exit codes.
A test that can only assert "rc=0 and something printed" is the weak-predicate
shape guard-2460 names — the destructive case it defends against also returns
rc=0 and prints a full record. Calling ``compose`` / ``verify_post`` /
``is_read_projected`` lets the assertions be about the invariant instead.

Tests:
  1. compose — PRE survives verbatim, no leading blank on an empty PRE, the
     sentinel round-trips so the idempotency check can see it.
  2. verify_post — the sig-40 property: a POST that contains the appended text
     and the sentinel but DROPPED the PRE is a FAILURE, not a success. That is
     the case a compare-against-your-own-construction check cannot catch.
  3. is_read_projected — the guard-1251 discriminator. The six-key default
     projection reads as projected; a record carrying an unprojected canary
     does not.
  4. Refusals through main(), with the subprocess layer stubbed: a projected
     read, a non-text (dict) field, and a composed value that opens with a JSON
     bracket. None of these reach a write.
  5. The shell wrapper's argv contract: an unknown flag and a missing
     positional both exit 2 rather than sliding a token into the value slot.

No test here writes to a goal store. The write path was exercised live against
g-115-4717 during implementation; these are the invariants that must not drift.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

from _runtime_bash import bash_cmd  # noqa: E402  (guard-580/581: never bare "bash")


def _load():
    # goal-field-append.py is hyphenated — load via importlib for its symbols.
    spec = importlib.util.spec_from_file_location(
        "goal_field_append", CORE_SCRIPTS / "goal-field-append.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GFA = _load()

WRAPPER = CORE_SCRIPTS / "goal-field-append.sh"


class _Res:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


# ── 1. compose ──────────────────────────────────────────────────────────────

def test_compose_preserves_pre_verbatim():
    pre = "original note\nwith a second line"
    out = GFA.compose(pre, "appended text", "m1")
    assert pre in out, "PRE must survive verbatim — this is the whole point of the helper"
    assert out.startswith(pre)


def test_compose_on_empty_pre_has_no_leading_blank():
    out = GFA.compose("", "first note", "m1")
    assert out.startswith("first note"), "an empty PRE must not produce a leading blank line"


def test_compose_separates_pre_from_text_with_a_blank_line():
    out = GFA.compose("pre", "post", "m1")
    assert "pre\n\npost" in out


def test_compose_sentinel_round_trips():
    # The idempotency check reads the sentinel back out of the STORED value, so
    # sentinel_for and compose must agree on its exact spelling.
    out = GFA.compose("pre", "text", "my-marker")
    assert GFA.sentinel_for("my-marker") in out


def test_compose_sentinel_is_marker_specific():
    out = GFA.compose("pre", "text", "marker-a")
    assert GFA.sentinel_for("marker-b") not in out, "a different marker must not read as already-appended"


# ── 1b. wrapped_marker_refusal — the double-wrap guard () ──────────
#
# The script owns the wrapping, but the convention is only ever VISIBLE in its
# wrapped form (inside a record), so callers reasonably pass the wrapped form
# back. That wrote `[appended:[appended:m]]` at rc=0 and broke the idempotency
# key: a retry with the BARE form finds its own sentinel and refuses, while a
# retry with the malformed form appends again.

def test_an_already_wrapped_marker_is_refused():
    msg = GFA.wrapped_marker_refusal("[appended:g-001-739-residual-bucket]")
    assert msg is not None, "the wrapped form must be refused, not silently double-wrapped"
    assert "g-001-739-residual-bucket" in msg, "the refusal must name the BARE token to use"


def test_a_wrapped_marker_missing_its_closing_bracket_is_still_refused():
    # The prefix is what identifies the shape; a truncated paste is still a paste.
    msg = GFA.wrapped_marker_refusal("[appended:half")
    assert msg is not None
    assert "'half'" in msg


def test_an_ordinary_marker_is_not_refused():
    """ANTI-VACUITY for the two above: the guard must not refuse everything."""
    assert GFA.wrapped_marker_refusal("g-001-847-double-wrap") is None


def test_a_marker_that_merely_CONTAINS_appended_is_not_refused():
    """The must-not-refuse case (guard-1106: test what an exclusion NEWLY excludes).

    A new refusal is only safe if it is narrower than it looks. These markers all
    carry the word — one even carries the full sentinel — but not as the PREFIX,
    so none of them is the paste this guard exists to catch.
    """
    for ok in (
        "appended-note",                     # starts with the word, not the sentinel
        "g-001-847-appended-twice",          # contains it mid-token
        "note-[appended:inner]",             # contains the sentinel, but not at position 0
        "[appendedx:m]",                     # near-miss prefix
    ):
        assert GFA.wrapped_marker_refusal(ok) is None, f"{ok!r} must NOT be refused"


def test_the_refusal_prefix_is_the_one_sentinel_for_uses():
    """The two must not drift: a refusal keyed on a different literal than the
    wrapper writes would refuse the wrong shape and miss the real one."""
    assert GFA.sentinel_for("m").startswith(GFA.SENTINEL_PREFIX)
    assert GFA.wrapped_marker_refusal(GFA.sentinel_for("m")) is not None


# ── 2. verify_post — the sig-40 property ────────────────────────────────────

def test_verify_post_clean_write_has_no_problems():
    pre = "original"
    post = GFA.compose(pre, "new", "m1")
    assert GFA.verify_post(pre, post, GFA.sentinel_for("m1")) == []


def test_verify_post_catches_a_dropped_pre():
    # The destructive case: the write landed, the sentinel is present, the text
    # is present — and the original content is GONE. Comparing POST against the
    # string this script built would call that a success.
    pre = "original content that must survive"
    post = "new text\n" + GFA.sentinel_for("m1")
    problems = GFA.verify_post(pre, post, GFA.sentinel_for("m1"))
    assert any("PRE content did NOT survive" in p for p in problems)


def test_verify_post_catches_a_missing_sentinel():
    pre = "original"
    problems = GFA.verify_post(pre, pre + "\n\nnew", GFA.sentinel_for("m1"))
    assert any("sentinel absent" in p for p in problems)


def test_verify_post_catches_a_non_text_post():
    problems = GFA.verify_post("original", {"a": 1}, GFA.sentinel_for("m1"))
    assert len(problems) == 1
    assert "not text" in problems[0]


def test_verify_post_catches_a_field_that_did_not_grow():
    pre = "original content"
    problems = GFA.verify_post(pre, pre, GFA.sentinel_for("m1"))
    assert any("length did not grow" in p for p in problems)


# ── 3. is_read_projected — the guard-1251 discriminator ─────────────────────

def test_default_six_key_projection_reads_as_projected():
    row = {"asp_id": "asp-115", "category": "x", "goal_id": "g-1", "source": "world",
           "status": "pending", "title": "t"}
    assert GFA.is_read_projected(row) is True


def test_record_with_an_unprojected_canary_is_not_projected():
    row = {"goal_id": "g-1", "status": "pending", "priority": "MEDIUM",
           "description": "d", "outcome_note": "note"}
    assert GFA.is_read_projected(row) is False


def test_extra_keys_without_a_canary_still_read_as_projected():
    # Wider than the default projection but carrying none of the fields a full
    # read always produces — not proof the read was unprojected.
    row = {"goal_id": "g-1", "status": "pending", "unrelated": 1}
    assert GFA.is_read_projected(row) is True


def test_non_dict_reads_as_projected():
    assert GFA.is_read_projected(None) is True
    assert GFA.is_read_projected("g-1") is True


# ── 4. Refusals through main(), subprocess layer stubbed ────────────────────

def _stub_read(monkeypatch, row):
    """Make the read subprocess return exactly `row`, and fail any write."""
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        joined = " ".join(str(a) for a in argv)
        if "aspirations-query.sh" in joined:
            return _Res(stdout=json.dumps([row]))
        raise AssertionError(f"a write was attempted but should have been refused: {joined}")

    monkeypatch.setattr(GFA, "_run", fake_run)
    return calls


def test_projected_read_is_refused_before_composing(monkeypatch, capsys):
    _stub_read(monkeypatch, {"asp_id": "asp-115", "category": "x", "goal_id": "g-1",
                             "source": "world", "status": "pending", "title": "t"})
    with pytest.raises(SystemExit) as exc:
        GFA.main(["g-1", "outcome_note", "m1", "text"])
    assert exc.value.code == GFA.RC_READ_UNSAFE
    assert "PROJECTED" in capsys.readouterr().err


def test_non_text_field_is_refused(monkeypatch, capsys):
    _stub_read(monkeypatch, {"goal_id": "g-1", "priority": "MEDIUM",
                             "verification": {"outcomes": [], "checks": []}})
    with pytest.raises(SystemExit) as exc:
        GFA.main(["g-1", "verification", "m1", "text"])
    assert exc.value.code == GFA.RC_FIELD_SHAPE
    err = capsys.readouterr().err
    assert "not text" in err and "guard-2444" in err


def test_composed_value_opening_with_a_json_bracket_is_refused(monkeypatch, capsys):
    # aspirations-update-goal.sh parse_value JSON-decodes any value starting
    # with { or [, so it would be stored as an object rather than as our text.
    _stub_read(monkeypatch, {"goal_id": "g-1", "priority": "MEDIUM", "outcome_note": None})
    with pytest.raises(SystemExit) as exc:
        GFA.main(["g-1", "outcome_note", "m1", '{"looks": "like json"}'])
    assert exc.value.code == GFA.RC_VALUE_SHAPE


def test_empty_text_is_refused_before_any_read(monkeypatch):
    def fake_run(argv, **kw):
        raise AssertionError("nothing should be read for an empty append")

    monkeypatch.setattr(GFA, "_run", fake_run)
    with pytest.raises(SystemExit) as exc:
        GFA.main(["g-1", "outcome_note", "m1", "\n\n"])
    assert exc.value.code == GFA.RC_VALUE_SHAPE


def test_matching_marker_is_a_no_op(monkeypatch, capsys):
    pre = "already annotated\n" + GFA.sentinel_for("m1")
    _stub_read(monkeypatch, {"goal_id": "g-1", "priority": "MEDIUM", "outcome_note": pre})
    # No write stub is provided — _stub_read raises on any write attempt, so
    # reaching the wrapper at all fails this test.
    assert GFA.main(["g-1", "outcome_note", "m1", "text"]) == GFA.RC_OK
    out = json.loads(capsys.readouterr().out)
    assert out["changed"] is False


# ── 5. The shell wrapper's argv contract ────────────────────────────────────

def _run_wrapper(*args):
    return subprocess.run(
        bash_cmd(WRAPPER, *args),
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        env={**os.environ, "STORAGE_BACKEND": "local"},
    )


def test_unknown_flag_is_refused_with_exit_2():
    # The failure this helper exists to prevent: on the hand-rolled sibling
    # wrappers an unknown flag is DROPPED and the next token is promoted into
    # the value slot (guard-1047 / guard-2460). _argv_strict refuses instead.
    res = _run_wrapper("--append", "g-1", "outcome_note", "m1", "text")
    assert res.returncode == 2


def test_missing_positionals_exit_2():
    res = _run_wrapper("g-1", "outcome_note")
    assert res.returncode == 2


# ── 6. The concurrent-append race () ──────────────────────────────
#
# POSITIVE CONTROL. Every test below was written against the PRE-fix code and
# watched go red before the fix existed — a concurrency test authored after the
# fix proves nothing about the race, because the interleaving it claims to
# reproduce may never have been reachable.
#
# The race: read_goal() and the write are two separate subprocess round-trips
# with nothing serializing the span. A and B both read PRE, both compose
# PRE+own-text, and B's write clobbers A's. Both pass verify_post(), because
# each writer's own sentinel is present, its own PRE survived, and the length
# grew — so neither ever learns the other's text is gone.

def _concurrent_store(monkeypatch, *, pre, peer_text, field="outcome_note"):
    """Fake store whose value mutates BETWEEN this process's read and its write.

    The first query returns a snapshot of `pre`; the peer's append is applied to
    the store immediately after that snapshot is handed back. That ordering IS
    the race — every later query (including the fix's pre-write re-read) sees
    the peer's value, exactly as a real concurrent writer would leave it.

    `peer_text=None` disables the interleaving, giving the uncontended control.
    """
    store = {"goal_id": "g-1", "priority": "MEDIUM", field: pre}
    state = {"queries": 0, "writes": []}

    def fake_run(argv, **kw):
        joined = " ".join(str(a) for a in argv)
        if "aspirations-query.sh" in joined:
            state["queries"] += 1
            snapshot = json.dumps([dict(store)])
            if state["queries"] == 1 and peer_text is not None:
                store[field] = (store[field] + "\n\n" if store[field] else "") + peer_text
            return _Res(stdout=snapshot)
        if "aspirations-update-goal.sh" in joined:
            # The composed value travels on STDIN, never argv (). Reading it
            # from kw["input"] is not a cosmetic harness update to match the caller —
            # the three asserts below ARE the pin for that contract. Through argv the
            # value was capped by Windows CreateProcess at ~32,767 chars for the whole
            # command line (guard-5634), so any field past that became permanently
            # unwritable from half the fleet (WinError 206). A regression to a
            # positional value must fail HERE, cheaply, rather than on a Windows box at
            # 32k — and it would otherwise pass silently, because the fake would happily
            # store argv[-1] whatever it holds (guard-920: replicate the literal
            # production arg shape, not the contract-ideal one).
            assert "--value-stdin" in argv, "the write must declare --value-stdin"
            written = kw.get("input")
            assert written is not None, "the composed value must arrive on stdin"
            assert written not in argv, "the value must NOT appear in argv at all"
            state["writes"].append(written)
            store[field] = written
            return _Res(stdout=json.dumps(dict(store)))
        raise AssertionError(f"unexpected subprocess: {joined}")

    monkeypatch.setattr(GFA, "_run", fake_run)
    return store, state


def test_peer_append_is_not_clobbered(monkeypatch):
    """THE control. Asserts data CONSERVATION and nothing else.

    Deliberately makes no claim about the exit code or about which branch ran:
    a fix that conserved the peer's text some other way would satisfy this too.
    The predicate is the defect itself — "is the other writer's text still
    there" — which is what rc=0-and-a-printed-record cannot tell you
    (guard-2460).
    """
    store, _ = _concurrent_store(monkeypatch, pre="ORIGINAL", peer_text="PEER-TEXT-B")
    try:
        GFA.main(["g-1", "outcome_note", "mA", "MY-TEXT-A"])
    except SystemExit:
        pass
    assert "PEER-TEXT-B" in store["outcome_note"], (
        "the peer's concurrent append was silently clobbered — this is the "
        "g-115-5638 lost update"
    )


def test_concurrent_modification_is_refused_loudly(monkeypatch, capsys):
    store, state = _concurrent_store(monkeypatch, pre="ORIGINAL", peer_text="PEER-TEXT-B")
    with pytest.raises(SystemExit) as exc:
        GFA.main(["g-1", "outcome_note", "mA", "MY-TEXT-A"])
    assert exc.value.code == GFA.RC_CONCURRENT_MODIFICATION
    assert state["writes"] == [], "a detected conflict must write NOTHING"
    err = capsys.readouterr().err
    assert "NOTHING WAS WRITTEN" in err, "the refusal must say no data was lost"


def test_uncontended_append_still_writes(monkeypatch):
    """No false positive: with no peer, the write proceeds exactly as before."""
    store, state = _concurrent_store(monkeypatch, pre="ORIGINAL", peer_text=None)
    rc = GFA.main(["g-1", "outcome_note", "mA", "MY-TEXT-A"])
    assert rc == GFA.RC_OK
    assert len(state["writes"]) == 1
    assert "ORIGINAL" in store["outcome_note"] and "MY-TEXT-A" in store["outcome_note"]


def test_concurrent_run_of_the_same_marker_is_idempotent_not_a_conflict(monkeypatch):
    """A peer running OUR marker is a completed duplicate, not a lost update."""
    store, state = _concurrent_store(
        monkeypatch, pre="ORIGINAL",
        peer_text="MY-TEXT-A\n" + GFA.sentinel_for("mA"))
    rc = GFA.main(["g-1", "outcome_note", "mA", "MY-TEXT-A"])
    assert rc == GFA.RC_OK
    assert state["writes"] == [], "the work already landed — writing again would duplicate it"


# ── 7. cas_conflict, the pure compare half ──────────────────────────────────

def test_cas_conflict_none_when_unchanged():
    assert GFA.cas_conflict("same", "same") is None


def test_cas_conflict_reports_a_peer_append():
    msg = GFA.cas_conflict("ORIGINAL", "ORIGINAL\n\nPEER")
    assert msg is not None and "appended" in msg


def test_cas_conflict_distinguishes_a_rewrite_from_an_append():
    """A rewrite is a different hazard: a retry would land on unreviewed text."""
    msg = GFA.cas_conflict("ORIGINAL", "COMPLETELY DIFFERENT")
    assert msg is not None and "REWRITTEN" in msg


def test_cas_conflict_reports_creation_from_empty():
    msg = GFA.cas_conflict("", "created by a peer")
    assert msg is not None and "empty at read time" in msg


def test_cas_conflict_rejects_a_non_text_reread():
    assert "not text" in GFA.cas_conflict("ORIGINAL", {"nested": 1})


# ── 6. : echo/rc false alarms cleared by the authoritative store read ──

def _stateful_run(state, echo_on_write, write_rc=0):
    """A fake _run whose reads always reflect state['stored'] and whose write
    lands `composed` in the store while returning a caller-supplied ECHO/rc."""
    def fake_run(argv, input=None, **kw):
        joined = " ".join(str(a) for a in argv)
        if "aspirations-query.sh" in joined:
            return _Res(stdout=json.dumps([{"goal_id": "g-1", "priority": "MEDIUM",
                                            "outcome_note": state["stored"]}]))
        if "aspirations-update-goal.sh" in joined:
            state["stored"] = state["composed"]        # the write LANDS soundly
            return _Res(stdout=echo_on_write, returncode=write_rc)
        raise AssertionError(f"unexpected call: {joined}")
    return fake_run


def test_echo_missing_pre_is_cleared_by_authoritative_read(monkeypatch, capsys):
    """The wrapper stdout ECHO can omit PRE while the store is written correctly
    (the measured rc=7 signature: sentinel present, length grew, PRE absent).
    verify_post against the echo alone would falsely die 'field was overwritten';
    the fix re-verifies against an independent store read and SUCCEEDS."""
    pre = "orig\n" + GFA.sentinel_for("m0")
    marker, text = "m1", "the appended text"
    sentinel = GFA.sentinel_for(marker)
    composed = GFA.compose(pre, text, marker)
    misleading_echo = json.dumps({"goal_id": "g-1",
                                  "outcome_note": "X" * 200 + "\n\n" + text + "\n" + sentinel})
    # sanity: this echo trips the OLD verify (PRE not a substring, but grew + sentinel)
    assert GFA.verify_post(pre, "X" * 200 + "\n\n" + text + "\n" + sentinel, sentinel) == \
        ["PRE content did NOT survive the write — the field was overwritten"]
    state = {"stored": pre, "composed": composed}
    monkeypatch.setattr(GFA, "_run", _stateful_run(state, misleading_echo))
    assert GFA.main(["g-1", "outcome_note", marker, text]) == GFA.RC_OK
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["changed"] is True


def test_wrapper_rc_nonzero_but_write_landed_recovers(monkeypatch, capsys):
    """The wrapper can return rc!=0 from its OWN output parsing after the field
    is already written (the measured rc=6). The fix confirms landing via an
    independent read and SUCCEEDS instead of dying RC_WRITE_FAILED."""
    pre = "orig\n" + GFA.sentinel_for("m0")
    marker, text = "m1", "text with a dict literal {'a': 1}"
    composed = GFA.compose(pre, text, marker)
    state = {"stored": pre, "composed": composed}
    monkeypatch.setattr(GFA, "_run",
                        _stateful_run(state, "not-json JSONDecodeError noise", write_rc=1))
    assert GFA.main(["g-1", "outcome_note", marker, text]) == GFA.RC_OK


def test_genuine_loss_dies_without_prescribing_a_whole_store_restore(monkeypatch, capsys):
    """A REAL rewrite (store shows the marker but PRE is gone) must still fail —
    but the message must NOT prescribe history.py restore (which rewrites the
    whole shared store in place and destroys concurrent Bodies' work)."""
    pre = "orig content\n" + GFA.sentinel_for("m0")
    marker, text = "m1", "appended"
    sentinel = GFA.sentinel_for(marker)
    rewritten = "TOTALLY DIFFERENT HEAD\n\n" + text + "\n" + sentinel   # marker present, PRE gone
    bad_echo = json.dumps({"goal_id": "g-1", "outcome_note": rewritten})
    state = {"stored": pre, "composed": rewritten}
    monkeypatch.setattr(GFA, "_run", _stateful_run(state, bad_echo))
    with pytest.raises(SystemExit) as exc:
        GFA.main(["g-1", "outcome_note", marker, text])
    assert exc.value.code == GFA.RC_VERIFY_FAILED
    err = capsys.readouterr().err
    assert "history.py restore" in err and "DO NOT run history.py restore" in err
    assert "Recover the PRE value from" not in err


# ── 7. The rotation is announced to the CALLER, not only to the field ────────
#
# rotate_oversize REBINDS `pre`, so every length reported below it is measured
# against the REDUCED value. Reporting that as a bare `pre_len` is honest about
# a base the caller never saw, and it has twice sent an agent hunting for data
# loss that never happened: a 77 KB "discrepancy" chased against a hand pre-read
# (bravo, cc-05) and a full window spent building a recovery directory for
# 287 KB never at risk (alpha, cc-04), both 2026-09-22. The notice written INTO
# the value serves whoever reads the goal later; this key serves whoever just
# wrote it and is reconciling lengths right now.

import _paths as _paths_for_sink


def _no_rotation(goal_id, field, source, pre):
    return pre


def test_a_rotation_is_reported_to_the_caller(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(_paths_for_sink, "WORLD_DIR", str(tmp_path))
    pre = ("orig block that the rotation will cut\n" + GFA.sentinel_for("m0"))
    reduced = "kept tail\n" + GFA.sentinel_for("m0")
    marker, text = "m1", "the appended text"
    composed = GFA.compose(reduced, text, marker)
    state = {"stored": pre, "composed": composed}

    def fake_rotate(g, f, s, p):
        # The real rotate_oversize is ITS OWN STORE WRITE ("ROTATION IS ITS OWN
        # WRITE, DELIBERATELY NOT PART OF THE APPEND", goal-field-append.py:259).
        # The stub must land the reduced value too, or the CAS guard below
        # correctly refuses the append against a base that never changed.
        state["stored"] = reduced
        return reduced

    monkeypatch.setattr(GFA, "rotate_oversize", fake_rotate)

    def run(argv, input=None, **kw):
        joined = " ".join(str(a) for a in argv)
        if "aspirations-query.sh" in joined:
            return _Res(stdout=json.dumps([{"goal_id": "g-1", "priority": "MEDIUM",
                                            "outcome_note": state["stored"]}]))
        if "aspirations-update-goal.sh" in joined:
            state["stored"] = state["composed"]
            return _Res(stdout=json.dumps({"goal_id": "g-1",
                                           "outcome_note": state["composed"]}))
        raise AssertionError("unexpected call: %s" % joined)

    monkeypatch.setattr(GFA, "_run", run)
    assert GFA.main(["g-1", "outcome_note", marker, text]) == GFA.RC_OK
    out = json.loads(capsys.readouterr().out)
    assert "rotated" in out, (
        "a rotation cut %d bytes and the caller was told nothing — this is the "
        "exact shape that cost two agents a window" % (len(pre) - len(reduced)))
    r = out["rotated"]
    assert r["pre_len_before_rotation"] == len(pre)
    assert r["moved_bytes"] == len(pre) - len(reduced)
    assert r["archive"].endswith("g-1.outcome_note.md")
    # and pre_len itself still describes the base the append actually ran on
    assert out["pre_len"] == len(reduced)


def test_no_rotation_means_no_rotated_key(monkeypatch, capsys, tmp_path):
    """Absence must stay meaningful — a key present on every write says nothing."""
    monkeypatch.setattr(_paths_for_sink, "WORLD_DIR", str(tmp_path))
    monkeypatch.setattr(GFA, "rotate_oversize", _no_rotation)
    pre = "orig\n" + GFA.sentinel_for("m0")
    marker, text = "m1", "the appended text"
    composed = GFA.compose(pre, text, marker)
    state = {"stored": pre, "composed": composed}
    monkeypatch.setattr(GFA, "_run", _stateful_run(
        state, json.dumps({"goal_id": "g-1", "outcome_note": composed})))
    assert GFA.main(["g-1", "outcome_note", marker, text]) == GFA.RC_OK
    out = json.loads(capsys.readouterr().out)
    assert "rotated" not in out


# ── 8. The rotation's own read->write window () ───────────────────
#
# rotate_oversize re-reads the field (its CAS) and then replaces it through a
# SEPARATE subprocess. A peer append committed between that re-read and the
# store write is overwritten by the reduced value -- and NO post-write check can
# see it, because the clobbered store holds EXACTLY the value the rotation meant
# to write: the notice is present, the newest block is present, the block count
# equals len(kept). That is why the goal's preferred remedy (compare the landed
# block count) cannot catch this race; only a compare evaluated INSIDE the
# store's own lock can refuse the write. The fake below models the daemon's
# X-Mind-Expect-Field-Sha256 precondition: a sent hash that no longer matches
# the stored value is refused (409, nothing written), and a matched one is
# confirmed on stderr the way aspirations-update-goal.sh re-emits it.

import hashlib


def _sha(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _blocks(n, size=90):
    """n sentinel-terminated blocks, oldest first, joined the way compose() joins."""
    return "\n\n".join(("block-%d " % i) + ("x" * size) + "\n" + GFA.sentinel_for("m%d" % i)
                       for i in range(n))


def _small_rotation_bounds(monkeypatch, tmp_path):
    monkeypatch.setattr(_paths_for_sink, "WORLD_DIR", str(tmp_path))
    monkeypatch.setattr(GFA, "ROTATE_DISABLED", False)
    monkeypatch.setattr(GFA, "ROTATE_AT_BYTES", 200)
    monkeypatch.setattr(GFA, "ROTATE_KEEP_BYTES", 150)


def _rotation_store(monkeypatch, *, pre, peer_block, confirm=True, resent=False):
    """Fake store + daemon for rotate_oversize called directly.

    Query #1 is the rotation's CAS re-read; the peer's append is committed to the
    store IMMEDIATELY AFTER that snapshot is handed back -- the window this goal
    names. `peer_block=None` gives the uncontended control; `confirm=False`
    models a daemon that predates the precondition header and ignores it.
    `resent=True` models the transport re-sending the request (rt_call does so
    after a stale-daemon recycle or a timeout, keeping only the LAST reply): the
    first copy lands, and the reply is the second copy's refusal.
    """
    store = {"value": pre}
    state = {"queries": 0, "writes": [], "refused": 0, "expect_sent": []}

    def fake_run(argv, input=None, **kw):
        args = [str(a) for a in argv]
        joined = " ".join(args)
        if "aspirations-query.sh" in joined:
            state["queries"] += 1
            snapshot = json.dumps([{"goal_id": "g-1", "priority": "MEDIUM",
                                    "outcome_note": store["value"]}])
            if state["queries"] == 1 and peer_block is not None:
                store["value"] = store["value"] + "\n\n" + peer_block
            return _Res(stdout=snapshot)
        if "aspirations-update-goal.sh" in joined:
            expect = args[args.index("--expect-sha256") + 1] if "--expect-sha256" in args else None
            state["expect_sent"].append(expect)
            if resent:
                state["writes"].append(input)
                store["value"] = input
                state["refused"] += 1
                return _Res(returncode=1, stderr=json.dumps(
                    {"error": "field_precondition_failed", "message": "NOTHING WAS WRITTEN"}))
            if confirm and expect is not None and expect != _sha(store["value"]):
                state["refused"] += 1
                return _Res(returncode=1, stderr=json.dumps(
                    {"error": "field_precondition_failed", "message": "NOTHING WAS WRITTEN"}))
            state["writes"].append(input)
            store["value"] = input
            echo = ("[update-goal] precondition_checked field-sha256=%s\n" % expect
                    if (confirm and expect is not None) else "")
            return _Res(stdout=json.dumps({"goal_id": "g-1", "outcome_note": input}),
                        stderr=echo)
        raise AssertionError("unexpected call: %s" % joined)

    monkeypatch.setattr(GFA, "_run", fake_run)
    return store, state


def test_rotation_does_not_clobber_a_peer_append_in_its_write_window(monkeypatch, tmp_path):
    """THE regression. Asserts data CONSERVATION: the peer's block survives.

    Fails against the pre-fix rotation, which wrote the reduced value
    unconditionally over the peer's commit and then returned it as success.
    """
    _small_rotation_bounds(monkeypatch, tmp_path)
    pre = _blocks(6)
    peer = "PEER-BLOCK-B\n" + GFA.sentinel_for("peer")
    store, state = _rotation_store(monkeypatch, pre=pre, peer_block=peer)
    out = GFA.rotate_oversize("g-1", "outcome_note", "world", pre)
    assert "PEER-BLOCK-B" in store["value"], (
        "the peer's append landed inside the rotation's read->write window and was "
        "silently replaced by the reduced value -- the g-115-10535 lost update")
    assert out == pre, "a refused rotation must hand back the ORIGINAL value, whole"
    assert state["writes"] == [], "a refused conditional write must write NOTHING"


def test_rotation_sends_the_hash_of_the_value_it_composed_from(monkeypatch, tmp_path):
    """The precondition is only as good as its operand: it must be sha256(pre),
    the exact text `reduced` was built from -- not the reduced value, and not a
    later re-read."""
    _small_rotation_bounds(monkeypatch, tmp_path)
    pre = _blocks(6)
    _, state = _rotation_store(monkeypatch, pre=pre, peer_block=None)
    GFA.rotate_oversize("g-1", "outcome_note", "world", pre)
    assert state["expect_sent"] == [_sha(pre)]


def test_uncontended_rotation_still_rotates(monkeypatch, tmp_path):
    """No false positive: with no peer, the conditional write lands and the
    rotation returns the reduced value, exactly as before the fix."""
    _small_rotation_bounds(monkeypatch, tmp_path)
    pre = _blocks(6)
    store, state = _rotation_store(monkeypatch, pre=pre, peer_block=None)
    out = GFA.rotate_oversize("g-1", "outcome_note", "world", pre)
    assert len(state["writes"]) == 1 and state["refused"] == 0
    assert out == store["value"] and out != pre
    assert out.startswith(GFA.ROTATE_NOTICE_HEAD)
    assert "block-5 " in out and "block-0 " not in out


def test_unconfirmed_precondition_is_announced_not_trusted(monkeypatch, tmp_path, capsys):
    """A daemon that predates the header accepts the write and IGNORES the
    precondition at rc=0 (guard-5505). The rotation must not treat that write as
    protected: it keeps the landed value (refusing it would false-alarm every
    rotation during a rollout) and says loudly that the window was unguarded."""
    _small_rotation_bounds(monkeypatch, tmp_path)
    pre = _blocks(6)
    store, state = _rotation_store(monkeypatch, pre=pre, peer_block=None, confirm=False)
    out = GFA.rotate_oversize("g-1", "outcome_note", "world", pre)
    assert out == store["value"] and out != pre
    err = capsys.readouterr().err
    assert "precondition" in err and "NOT confirmed" in err, err


def test_rotation_that_landed_behind_a_refused_resend_is_kept(monkeypatch, tmp_path, capsys):
    """The precondition made the write non-idempotent. A re-sent copy of a
    rotation that already LANDED meets its own write, is refused 409, and the
    wrapper exits 1 -- measured on the fixture daemon: first send 200, identical
    re-send 409 "NOTHING WAS WRITTEN" over a store holding exactly that value.
    Handing back `pre` then sent main()'s CAS to report a rewrite nobody made
    (RC 9, "NOTHING WAS WRITTEN") over a rotation that stands. A refusal is not
    proof that nothing landed (guard-7050): confirm against the store."""
    _small_rotation_bounds(monkeypatch, tmp_path)
    pre = _blocks(6)
    store, state = _rotation_store(monkeypatch, pre=pre, peer_block=None, resent=True)
    out = GFA.rotate_oversize("g-1", "outcome_note", "world", pre)
    assert len(state["writes"]) == 1 and state["refused"] == 1
    assert out == store["value"] and out != pre, (
        "the rotation landed; handing back the original reports it undone")
    err = capsys.readouterr().err
    assert "re-sent" in err, err


# ── 9. Description retention polarity () ─────────────────────────
#
# The measured incident (2026-09-22T01:39:29, , guard-7281): an
# ordinary description append ran keep-newest rotation and archived the 30
# OLDEST blocks — evicting the unit-lease STEP 0 (which carried a
# [hoisted:...] pin marker the rotation ignored), the PULL-FIRST step, and the
# alert ROUTING TABLE, leaving only the later lesson blocks that commented on
# them. The archive was intact; the loss was the READ SURFACE of the executable
# head, which no integrity check can see.
#
# The policy these tests pin: for field == "description" only, a block the
# author pinned (a [hoisted:...] or [pin:...] marker) is non-evictable, the
# rest rotates keep-newest, and when no block is pinned the OLDEST block is
# pinned by default. Pinning is on the MARKER the author writes, not on an
# ordinal — the executable head is a RUN of blocks and its length is not
# knowable from position alone. When the pinned head alone exceeds the keep
# bound, rotation fails open: the statement surviving beats the field bounded.
# Every test below asserts a property that FAILS against the pre-fix
# keep-newest rotation — a test that passes on both is not a regression pin.

def _bounds(monkeypatch, tmp_path, at, keep):
    monkeypatch.setattr(_paths_for_sink, "WORLD_DIR", str(tmp_path))
    monkeypatch.setattr(GFA, "ROTATE_DISABLED", False)
    monkeypatch.setattr(GFA, "ROTATE_AT_BYTES", at)
    monkeypatch.setattr(GFA, "ROTATE_KEEP_BYTES", keep)


def _description_rotation_store(monkeypatch, *, pre):
    """Fake store + daemon for rotate_oversize on a DESCRIPTION field.

    Mirrors section 8's _rotation_store, but the field is `description` — the
    key the g-115-10533 retention branch is gated on. The precondition is
    enforced the way the daemon enforces it (hash mismatch -> 409, nothing
    written); no peer interleaving here, because section 8 already pins the
    window and the description path shares that CAS code.
    """
    store = {"value": pre}
    state = {"queries": 0, "writes": [], "refused": 0}

    def fake_run(argv, input=None, **kw):
        args = [str(a) for a in argv]
        joined = " ".join(args)
        if "aspirations-query.sh" in joined:
            state["queries"] += 1
            # Echo the value under BOTH text-field keys: rotate_oversize
            # re-reads the field it is rotating (the CAS fresh-read) and a
            # description-keyed-only fake would fail that read for a
            # progress_note rotation and return pre unchanged.
            return _Res(stdout=json.dumps([{"goal_id": "g-1", "priority": "MEDIUM",
                                            "description": store["value"],
                                            "progress_note": store["value"]}]))
        if "aspirations-update-goal.sh" in joined:
            expect = args[args.index("--expect-sha256") + 1] if "--expect-sha256" in args else None
            if expect is not None and expect != _sha(store["value"]):
                state["refused"] += 1
                return _Res(returncode=1, stderr=json.dumps(
                    {"error": "field_precondition_failed", "message": "NOTHING WAS WRITTEN"}))
            state["writes"].append(input)
            store["value"] = input
            return _Res(stdout=json.dumps({"goal_id": "g-1", "description": input}),
                        stderr="[update-goal] precondition_checked field-sha256=%s\n" % expect)
        raise AssertionError("unexpected call: %s" % joined)

    monkeypatch.setattr(GFA, "_run", fake_run)
    return store, state


def _desc_block(body, marker=None, pin=None):
    """One compose()-shaped block: body, optional pin-marker line, sentinel."""
    text = body
    if pin:
        text += "\n" + pin
    return text + "\n" + GFA.sentinel_for(marker)


def test_pinned_head_run_survives_a_description_rotation(monkeypatch, tmp_path):
    """THE regression. The author-pinned blocks (a RUN, not one) are never the
    blocks that get archived; the oldest UNPINNED blocks are what rotation
    moves. Fails against the pre-fix rotation, which kept the newest blocks and
    cut exactly the pinned head (the measured g-115-817 incident)."""
    step0 = _desc_block(
        "STEP 0 — TAKE THE UNIT LEASE FIRST, BEFORE STEP 1 AND BEFORE READING "
        "THE CURSOR: bash core/scripts/unit-claim.sh acquire g-115-817 run-X",
        marker="g817-run464-step0", pin="[hoisted:g817-run464-step0-unit-lease]")
    pull_first = _desc_block(
        "STEP 1 — PULL-FIRST: bash world/scripts/inbox-watch-pull.sh check "
        "--json --lane alerts; if verdict=NEW run the full sweep",
        marker="g817-run464-step1", pin="[pin:g817-run464-step1-pull-first]")
    # SEVEN lessons, not three: with only three, the cut (2 x 135 B = 270 B)
    # is smaller than the rotation NOTICE itself (~480 B, it embeds the full
    # sink path), so `out` came out LARGER than `pre` — the fix cut, but the
    # field grew. The fixture must cut more than the notice costs; that is
    # what the size assertion below is really checking.
    lessons = [_desc_block("LESSON (%s) — a run note appended to the "
                           "description, %s" % (c, "x" * 60), marker="lesson-%s" % c)
               for c in "UVWXYAB"]
    pre = "\n\n".join([step0, pull_first] + lessons)
    _bounds(monkeypatch, tmp_path, at=500, keep=450)
    store, state = _description_rotation_store(monkeypatch, pre=pre)
    out = GFA.rotate_oversize("g-1", "description", "world", pre)
    assert len(state["writes"]) == 1 and state["refused"] == 0, \
        "the description is oversize and nothing is pinned against rotation; it must rotate"
    assert out == store["value"] and out != pre
    assert out.startswith(GFA.ROTATE_NOTICE_HEAD)
    # (a) the executable head survives — BOTH pinned blocks, not just the first:
    # a first-block pin saved STEP 0 in the incident and still lost Step 1.
    assert "TAKE THE UNIT LEASE FIRST" in out, \
        "STEP 0 (the pinned task statement) was archived — the g-115-817 incident"
    assert "PULL-FIRST" in out, \
        "the second pinned block (a run of blocks, not one) was archived"
    assert "[hoisted:g817-run464-step0-unit-lease]" in out
    # (b) the field is still bounded: the oldest UNPINNED lessons went to the sink.
    sink = tmp_path / "audit-reports" / "goal-note-archive" / "g-1.description.md"
    assert sink.exists()
    archived = sink.read_text(encoding="utf-8")
    assert "LESSON (U)" in archived, "rotation must still cut something — a bare exemption is not the fix"
    assert "LESSON (U)" not in out
    assert len(out.encode("utf-8")) < len(pre.encode("utf-8"))


def test_unmarked_description_never_archives_its_first_block(monkeypatch, tmp_path):
    """The goal's literal check: a description carrying NO pin markers still
    keeps its first block (the original goal statement) — keep-newest alone
    would have cut it. The default floor is strictly safer than the old
    polarity, never worse."""
    blocks = [_desc_block("BLOCK-%d %s" % (i, "y" * 90), marker="m%d" % i) for i in range(4)]
    pre = "\n\n".join(blocks)
    _bounds(monkeypatch, tmp_path, at=400, keep=300)
    store, state = _description_rotation_store(monkeypatch, pre=pre)
    out = GFA.rotate_oversize("g-1", "description", "world", pre)
    assert out != pre and out.startswith(GFA.ROTATE_NOTICE_HEAD), "must rotate when oversize"
    assert "BLOCK-0 " in out, \
        "the FIRST block of an unmarked description (the task statement) was archived"
    assert "BLOCK-1 " not in out, "with the head pinned, rotation cuts from the REST"
    assert "BLOCK-3 " in out, "the newest block is still kept (keep-newest over the rest)"


def test_first_block_never_archived_even_when_only_a_later_block_is_marked(
        monkeypatch, tmp_path):
    """The goal's check, in the shape the measured  record actually
    took: an UNMARKED first block, a MARKED block further down. A
    marker-OR-default implementation (pin the marked set, else block 0) would
    pin only the marked block and let keep-newest archive block 0 — the
    original statement — which is exactly what this test exists to forbid."""
    first = _desc_block("ORIGINAL STATEMENT: what this goal is for, " + "s" * 70, marker="orig")
    marked = _desc_block("PROTOCOL " + "p" * 70, marker="proto", pin="[pin:proto]")
    lessons = [_desc_block("LESSON-%d " % i + "l" * 70, marker="m%d" % i) for i in range(4)]
    pre = "\n\n".join([first, marked] + lessons)
    _bounds(monkeypatch, tmp_path, at=400, keep=350)
    store, state = _description_rotation_store(monkeypatch, pre=pre)
    out = GFA.rotate_oversize("g-1", "description", "world", pre)
    assert out != pre and out.startswith(GFA.ROTATE_NOTICE_HEAD), \
        "the description is oversize; a rotation must actually fire (no vacuous pass)"
    assert "ORIGINAL STATEMENT: what this goal is for" in out, \
        "the FIRST block was archived although no block was marked with a pin"
    assert "[pin:proto]" in out, "the marked block is pinned too — the run, not one block"
    # and rotation still cut something: the oldest UNPINNED lessons went to the sink
    sink = tmp_path / "audit-reports" / "goal-note-archive" / "g-1.description.md"
    assert sink.exists()
    archived = sink.read_text(encoding="utf-8")
    assert "LESSON-0" in archived and "LESSON-0" not in out


def test_pinned_head_oversizing_the_bound_fails_open(monkeypatch, tmp_path):
    """When the pinned head ALONE exceeds the keep bound, no bound can shrink
    the field without cutting the statement: rotation must cut nothing (the
    fail-open the module docstring names). (a) the statement survives beats
    (b) the field is bounded; the field is then a curation job, not a reason
    to lose the task definition."""
    pinned = _desc_block("STEP 0 " + "z" * 380, marker="step0",
                         pin="[hoisted:step0]")
    rest = _desc_block("a lesson " + "q" * 100, marker="lesson")
    pre = pinned + "\n\n" + rest
    _bounds(monkeypatch, tmp_path, at=300, keep=200)
    store, state = _description_rotation_store(monkeypatch, pre=pre)
    out = GFA.rotate_oversize("g-1", "description", "world", pre)
    assert out == pre, "the pinned head alone exceeds the bound; the field must be returned whole"
    assert state["writes"] == [] and state["refused"] == 0
    assert not (tmp_path / "audit-reports" / "goal-note-archive" / "g-1.description.md").exists(), \
        "fail-open happens before any archive write — the sink must not be created"


def test_pin_markers_do_not_apply_outside_description(monkeypatch, tmp_path):
    """ANTI-VACUITY: the retention branch is field-SPECIFIC. A [hoisted:...]
    marker in a progress_note does not make its block non-evictable — the
    note's keep-newest polarity is right there, and a blanket 'markers are
    always pinned' implementation would fail this test and stop the notes
    aging out."""
    marked_old = _desc_block("OLD NOTE " + "n" * 60, marker="old",
                             pin="[hoisted:old-note]")
    new_a = _desc_block("note A " + "a" * 90, marker="new-a")
    new_b = _desc_block("note B " + "b" * 90, marker="new-b")
    pre = "\n\n".join([marked_old, new_a, new_b])
    _bounds(monkeypatch, tmp_path, at=250, keep=200)
    store, state = _description_rotation_store(monkeypatch, pre=pre)
    out = GFA.rotate_oversize("g-1", "progress_note", "world", pre)
    assert out != pre and out.startswith(GFA.ROTATE_NOTICE_HEAD), "the note must still rotate"
    assert "OLD NOTE " not in out, \
        "the marker is description policy, not note policy — the oldest note still ages out"
    assert "note B " in out, "the newest note is kept (keep-newest is unchanged)"
    assert "note A " not in out, \
        "keep-newest keeps only as much as the bound allows — the note still ages out"


def test_description_rotation_notice_names_the_kept_head(monkeypatch, tmp_path):
    """guard-6105 provenance: a reader of the reduced field must be able to
    tell what happened without knowing this code exists. A description
    rotation keeps the pinned head, not just the newest blocks, so the notice
    must say 'oldest non-pinned' and 'pinned head was kept' — claiming it cut
    'the oldest block(s)' would be false once a pinned head was deliberately
    left in place."""
    step0 = _desc_block("STEP 0 " + "s" * 90, marker="step0", pin="[pin:step0]")
    lessons = [_desc_block("LESSON-%d " % i + "l" * 80, marker="m%d" % i) for i in range(4)]
    pre = "\n\n".join([step0] + lessons)
    _bounds(monkeypatch, tmp_path, at=400, keep=350)
    store, state = _description_rotation_store(monkeypatch, pre=pre)
    out = GFA.rotate_oversize("g-1", "description", "world", pre)
    assert out != pre
    assert "oldest non-pinned block(s)" in out, \
        "the notice must not claim it cut the oldest blocks when a pinned head was kept"
    assert "pinned (non-evictable) head was kept" in out
    # and the progress_note wording is unchanged (the branch is description-only)
    assert "oldest block(s)" not in out or "non-pinned" in out


def test_pinned_description_indices_marker_set_plus_unconditional_block0_floor():
    """The pure helper: pinning is the UNION of the author-marked set and an
    unconditional block-0 floor. The marker pins the executable head wherever
    it sits (a run, not one block); block 0 is pinned on top of it — the
    goal's absolute check — so a later marked block can never re-arm
    keep-newest over an unmarked original statement. When block 0 is itself
    marked it is pinned once, not twice; empty input pins nothing."""
    marked = [_desc_block("a " + "1" * 50, marker="a"),
              _desc_block("b " + "2" * 50, marker="b", pin="[pin:b]"),
              _desc_block("c " + "3" * 50, marker="c", pin="[hoisted:c]")]
    assert GFA.pinned_description_indices(marked) == [0, 1, 2], \
        "the block-0 floor must ride on top of the marked set, not replace it"
    unmarked = [_desc_block("x", marker="x"), _desc_block("y", marker="y")]
    assert GFA.pinned_description_indices(unmarked) == [0]
    assert GFA.pinned_description_indices([]) == []
    # block 0 marked: the floor and the marker coincide — one pin, not two
    first_marked = [_desc_block("a " + "1" * 50, marker="a", pin="[pin:a]"),
                    _desc_block("b " + "2" * 50, marker="b", pin="[pin:b]")]
    assert GFA.pinned_description_indices(first_marked) == [0, 1]
    # the marker must be the PIN marker: an ordinary [appended:] sentinel is not one
    plain = [_desc_block("body with the word hoisted mid-text", marker="m")]
    assert GFA.pinned_description_indices(plain) == [0]
