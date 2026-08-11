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
