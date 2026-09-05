"""Tests for history-field.py — the field-granular historical reader ().

Fixtures here are synthetic in-memory strings; nothing reads a live store.

Pins the two contracts that make the tool safe to build a recovery on:

  1. It finds a goal NESTED inside an aspiration line. The aspirations store is
     one line per ASPIRATION carrying a goals[] array, so a top-level-only scan
     returns a clean not-found for every goal in the store the tool exists for —
     a false absence that reads exactly like "nothing was lost".
  2. ABSENT and EMPTY are different exit codes. A caller that cannot separate
     "the snapshot carried no such field" (4) from "the field was empty" (0) will
     read one as the other; that is the rb-245 class, and here it decides whether
     a recovery happens at all.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts (this file is core/scripts/tests/)


def _load():
    spec = importlib.util.spec_from_file_location(
        "history_field_mod", SCRIPTS / "history-field.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def _asp_line(asp_id, goals):
    return json.dumps({"id": asp_id, "goals": goals})


def test_finds_goal_nested_under_aspiration():
    text = "\n".join([
        _asp_line("asp-115", [
            {"id": "g-1", "progress_note": "alpha text"},
            {"id": "g-2", "progress_note": "beta text"},
        ]),
        _asp_line("asp-326", [{"id": "g-3", "progress_note": "gamma"}]),
    ])
    rec, where = MOD.find_record(text, "g-2")
    assert rec is not None, "nested goal must be found — top-level-only is a false absence"
    assert rec["progress_note"] == "beta text"
    assert "asp-115" in where


def test_finds_top_level_record():
    text = json.dumps({"id": "rb-9", "content": "flat store"})
    rec, where = MOD.find_record(text, "rb-9")
    assert rec is not None and where == "top-level"
    assert rec["content"] == "flat store"


def test_missing_record_returns_none():
    text = _asp_line("asp-1", [{"id": "g-1"}])
    rec, where = MOD.find_record(text, "g-nope")
    assert rec is None and where is None


def test_unparseable_line_does_not_abort_the_scan():
    """A banner or comment line must not hide records on later lines."""
    text = "\n".join([
        "this is not structured data",
        "# a comment",
        _asp_line("asp-1", [{"id": "g-7", "outcome_note": "found me"}]),
    ])
    rec, _ = MOD.find_record(text, "g-7")
    assert rec is not None and rec["outcome_note"] == "found me"


def test_absent_and_empty_are_distinct_exit_codes():
    """The whole point: 4 = no such key, 0 = key present but empty."""
    assert MOD.EXIT_NO_FIELD != MOD.EXIT_OK
    assert MOD.EXIT_NO_RECORD != MOD.EXIT_NO_FIELD
    assert (MOD.EXIT_OK, MOD.EXIT_NO_RECORD, MOD.EXIT_NO_FIELD) == (0, 3, 4)


def test_empty_string_field_is_present_not_absent():
    text = _asp_line("asp-1", [{"id": "g-1", "progress_note": ""}])
    rec, _ = MOD.find_record(text, "g-1")
    assert "progress_note" in rec, "an empty value must still register as PRESENT"
    assert rec["progress_note"] == ""


def test_wrapper_help_runs():
    """The .sh wrapper must exec python3 (CLAUDE.md) and reach argparse."""
    r = subprocess.run(
        [str(SCRIPTS / "history-field.sh"), "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "--field" in r.stdout


def _run_main(monkeypatch, capsys, snapshot_text, argv):
    """Drive main() end-to-end with the history layer stubbed out.

    The four names main() reaches into `history` for are module-level in this
    module's namespace, so patching them here exercises the REAL branch logic
    without needing a live .history tree.
    """
    monkeypatch.setattr(MOD, "resolve_target", lambda f: Path("/nonexistent/store.jsonl"))
    monkeypatch.setattr(MOD, "_find_snapshot_by_name", lambda t, v: Path("/nonexistent/snap"))
    monkeypatch.setattr(MOD, "_find_history_snapshots", lambda t: ["snap"])
    monkeypatch.setattr(MOD, "_read_snapshot_text", lambda p, t: snapshot_text)
    monkeypatch.setattr(sys, "argv", ["history-field.py"] + argv)
    rc = MOD.main()
    return rc, capsys.readouterr()


def _argv(field):
    return ["store.jsonl", "snap", "--goal", "g-1", "--field", field]


def test_null_field_exits_5_and_writes_nothing_to_stdout(monkeypatch, capsys):
    """A null value is NOT the four-character text "null".

    stdout feeds `goal-field-append.sh --value-file`, so emitting json.dumps(None)
    would append the WORD "null" into a narrative field — a write nobody asked
    for, which the next clobber audit then reads as content. guard-1753: a reader
    must distinguish "could not resolve" from "genuinely empty".
    """
    text = _asp_line("asp-1", [{"id": "g-1", "progress_note": None}])
    rc, cap = _run_main(monkeypatch, capsys, text, _argv("progress_note"))
    assert rc == MOD.EXIT_NULL_FIELD == 5
    assert cap.out == "", "null must never reach stdout"
    assert "NULL" in cap.err


def test_null_empty_and_absent_are_told_apart_only_by_exit_code(monkeypatch, capsys):
    """All three write 0 bytes to stdout, so the exit code carries the whole answer."""
    cases = [
        ({"id": "g-1", "progress_note": None}, MOD.EXIT_NULL_FIELD),
        ({"id": "g-1", "progress_note": ""}, MOD.EXIT_OK),
        ({"id": "g-1", "outcome_note": "elsewhere"}, MOD.EXIT_NO_FIELD),
    ]
    seen = []
    for goal, expected in cases:
        rc, cap = _run_main(monkeypatch, capsys, _asp_line("asp-1", [goal]), _argv("progress_note"))
        assert rc == expected, f"{goal} expected exit {expected}, got {rc}"
        assert cap.out == "", "all three states emit 0 stdout bytes — that is the point"
        seen.append(rc)
    assert len(set(seen)) == 3, "collapsing any two of these re-creates the rb-245 class"


def test_reader_does_not_invoke_the_destructive_restore_cli():
    """guard-4165 / guard-5651: the restore CLI OVERWRITES the live store.

    This reader must never reach it. `_history_store.restore()` RETURNS bytes and
    is a different function; the check is that neither file shells out to the
    CLI's restore subcommand.
    """
    for name in ("history-field.py", "history-field.sh"):
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "history.py restore" not in src
