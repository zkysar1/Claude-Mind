#!/usr/bin/env python3
"""Tests for the append-ledger git merge driver ().

Covers the goal's contract for the core/config readings ledgers:
  1. two boxes appending a reading between pushes merge cleanly, ours then theirs
  2. nothing is deduped or interleaved when the two blocks share lines (the
     merge=union failure, measured in production at merge 10069ea4c0)
  3. edits and deletions merge as a 3-way merge does; an edited or deleted tail
     is not resurrected by the other side's append
  4. both sides rewriting the same text is still a REAL conflict (exit 1, markers)
Plus a live `git merge` through the registered wrapper (guard-1290).
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
_DRIVER = _SCRIPTS / "git-merge-append-ledger.py"

_spec = importlib.util.spec_from_file_location("git_merge_append_ledger", _DRIVER)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["git_merge_append_ledger"] = _mod
_spec.loader.exec_module(_mod)

merge_append = _mod.merge_append
main = _mod.main

_HAS_GIT = subprocess.run(["git", "--version"], capture_output=True).returncode == 0
needs_git = pytest.mark.skipif(not _HAS_GIT, reason="git not available")

BASE = "# Readings\n\nAppend new readings below.\n\n## 2026-09-15 — alpha\n\nold reading\n"
# Two readings that SHARE lines, the shape union garbles: a code fence, a rule
# and a formulaic heading appear in both blocks.
OURS_BLOCK = "\n## 2026-09-16 — bravo\n\n```\nprobe a\n```\n\n### Net\n\nnothing\n\n---\n"
THEIRS_BLOCK = "\n## 2026-09-16T18:5x — zeta\n\n```\nprobe b\n```\n\n### Net\n\nnothing\n\n---\n"


# --- 1 + 2: concurrent appends ------------------------------------------------

def test_concurrent_appends_are_ours_then_theirs_verbatim():
    merged, unresolved = merge_append(BASE, BASE + OURS_BLOCK, BASE + THEIRS_BLOCK)
    assert unresolved == 0
    assert merged == BASE + OURS_BLOCK + THEIRS_BLOCK


def _union(base, ours, theirs, tmp_path):
    for name, text in (("o", ours), ("b", base), ("t", theirs)):
        (tmp_path / name).write_text(text, encoding="utf-8")
    return subprocess.run(
        ["git", "merge-file", "--union", "-p",
         str(tmp_path / "o"), str(tmp_path / "b"), str(tmp_path / "t")],
        capture_output=True, text=True).stdout


@needs_git
def test_shared_lines_are_not_deduped_where_union_dedupes(tmp_path):
    """The motivating defect. The fixture must discriminate: skip, never pass
    vacuously, if this git's union stops garbling it."""
    ours, theirs = BASE + OURS_BLOCK, BASE + THEIRS_BLOCK
    exact = BASE + OURS_BLOCK + THEIRS_BLOCK
    if _union(BASE, ours, theirs, tmp_path) == exact:
        pytest.skip("this git's merge=union no longer garbles the fixture")
    merged, unresolved = merge_append(BASE, ours, theirs)
    assert unresolved == 0
    assert merged == exact
    assert merged.count("```") == 4 and merged.count("### Net") == 2


def test_identical_appends_are_kept_once():
    merged, unresolved = merge_append(BASE, BASE + OURS_BLOCK, BASE + OURS_BLOCK)
    assert (merged, unresolved) == (BASE + OURS_BLOCK, 0)


def test_append_already_contained_in_the_other_append_is_kept_once():
    more = OURS_BLOCK + THEIRS_BLOCK
    merged, unresolved = merge_append(BASE, BASE + more, BASE + OURS_BLOCK)
    assert (merged, unresolved) == (BASE + more, 0)


def test_one_side_unchanged_takes_the_other_side():
    assert merge_append(BASE, BASE, BASE + THEIRS_BLOCK) == (BASE + THEIRS_BLOCK, 0)
    assert merge_append(BASE, BASE + OURS_BLOCK, BASE) == (BASE + OURS_BLOCK, 0)


def test_base_without_trailing_newline_still_appends_cleanly():
    base = "row 1\nrow 2"
    merged, unresolved = merge_append(base, base + "\nrow A\n", base + "\nrow B\n")
    assert (merged, unresolved) == ("row 1\nrow 2\nrow A\nrow B\n", 0)


def test_editing_a_final_line_without_newline_is_not_an_append():
    base = "row 1\nrow 2"
    merged, unresolved = merge_append(base, "row 1\nrow 2 edited\n", "row 1\nrow 2 other\n")
    assert unresolved == 1
    assert "<<<<<<< ours" in merged and ">>>>>>> theirs" in merged


# --- 3: edits and deletions ---------------------------------------------------

def test_tail_edit_is_not_resurrected_by_the_other_sides_append():
    base = "a\nb\nc\n"
    merged, unresolved = merge_append(base, "a\nb\nC edited\n", base + "new\n")
    assert (merged, unresolved) == ("a\nb\nC edited\nnew\n", 0)


def test_tail_deletion_is_honored_beside_an_append():
    base = "a\nb\nc\n"
    merged, unresolved = merge_append(base, base + "new\n", "a\nb\n")
    assert (merged, unresolved) == ("a\nb\nnew\n", 0)


@needs_git
def test_separate_mid_file_edits_and_both_appends_all_survive():
    base = "h\nm1\nm2\nm3\nm4\nend\n"
    ours = "h\nM1\nm2\nm3\nm4\nend\nours reading\n"
    theirs = "h\nm1\nm2\nm3\nM4\nend\ntheirs reading\n"
    merged, unresolved = merge_append(base, ours, theirs)
    assert (merged, unresolved) == ("h\nM1\nm2\nm3\nM4\nend\nours reading\ntheirs reading\n", 0)


@needs_git
def test_marker_like_lines_inside_readings_are_plain_text():
    """A reading may quote a standard conflict marker; the internal diff3 pass
    uses wide markers so the quote is never parsed as structure."""
    # Neither side extends the whole base (each edits a line >=2 lines apart,
    # guard-2556), so this runs the diff3 path: the tail hunk's ours section
    # carries the quoted markers, and theirs rewrote the tail it appended to.
    base = "h\nm1\nk1\nk2\nk3\nend\n"
    quote = "<<<<<<< ours\nquoted\n=======\n>>>>>>> theirs\n"
    ours = "h\nM1\nk1\nk2\nk3\nend\n" + quote
    theirs = "h\nm1\nk1\nk2\nk3\nEND\nplain\n"
    merged, unresolved = merge_append(base, ours, theirs)
    assert unresolved == 0
    assert merged == "h\nM1\nk1\nk2\nk3\nEND\nplain\n" + quote


@needs_git
def test_block_appended_by_both_sides_is_kept_once_beside_a_rewrite():
    """One side also rewrote a line, so this is not the pure-append shortcut;
    the identical appended block must still land once."""
    base = "h\nm1\nk1\nk2\nend\n"
    merged, unresolved = merge_append(base, "h\nM1\nk1\nk2\nend\nX\n", base + "X\n")
    assert (merged, unresolved) == ("h\nM1\nk1\nk2\nend\nX\n", 0)


@needs_git
def test_rewrite_beside_shared_line_appends_keeps_both_blocks_whole():
    """Pins --diff3. One side also edited the preamble, so this is the diff3 path,
    and the two appended readings share lines. Measured on git 2.43.0: with
    --zdiff3 instead, merge-file moves those shared lines out of the hunk and the
    merge exits clean with a fence lost, the union garble again (g-115-10127)."""
    edited = BASE.replace("Append new readings below.", "Append new readings BELOW.")
    merged, unresolved = merge_append(BASE, edited + OURS_BLOCK, BASE + THEIRS_BLOCK)
    assert (merged, unresolved) == (edited + OURS_BLOCK + THEIRS_BLOCK, 0)


@needs_git
def test_marker_shaped_ledger_line_refuses_instead_of_misparsing():
    wide_rule = "=" * _mod._WIDE + "\n"
    with pytest.raises(_mod.MergeFileFailed):
        merge_append("h\nx\n", "h\n" + wide_rule + "A\n", "h\nB\n")


# --- 4: genuine conflicts -----------------------------------------------------

@needs_git
def test_same_text_rewritten_on_both_sides_is_a_real_conflict():
    merged, unresolved = merge_append("x\ny\nz\n", "x\nY ours\nz\n", "x\nY theirs\nz\n")
    assert unresolved == 1
    assert merged == "x\n<<<<<<< ours\nY ours\n=======\nY theirs\n>>>>>>> theirs\nz\n"


# --- add/add ------------------------------------------------------------------

def test_addadd_shares_the_common_prefix_once():
    head = "# Readings\n\nintro\n"
    merged, unresolved = merge_append("", head + "ours\n", head + "theirs\n")
    assert (merged, unresolved) == (head + "ours\ntheirs\n", 0)


# --- main(): git's driver contract ----------------------------------------------

def _sides(tmp_path, base, ours, theirs):
    paths = []
    for name, text in (("base", base), ("ours", ours), ("theirs", theirs)):
        p = tmp_path / name
        p.write_text(text, encoding="utf-8", newline="")
        paths.append(str(p))
    return paths


def test_main_writes_the_merge_to_ours_and_exits_0(tmp_path):
    b, o, t = _sides(tmp_path, BASE, BASE + OURS_BLOCK, BASE + THEIRS_BLOCK)
    assert main(["drv", b, o, t, "core/config/x.md"]) == 0
    assert Path(o).read_text(encoding="utf-8") == BASE + OURS_BLOCK + THEIRS_BLOCK


@needs_git
def test_main_writes_markers_and_exits_1_on_conflict(tmp_path):
    b, o, t = _sides(tmp_path, "x\ny\nz\n", "x\nY1\nz\n", "x\nY2\nz\n")
    assert main(["drv", b, o, t]) == 1
    text = Path(o).read_text(encoding="utf-8")
    assert "<<<<<<< ours" in text and "Y1" in text and "Y2" in text


def test_main_tolerates_a_missing_base(tmp_path):
    _, o, t = _sides(tmp_path, "", "row A\n", "row B\n")
    assert main(["drv", str(tmp_path / "absent"), o, t]) == 0
    assert Path(o).read_text(encoding="utf-8") == "row A\nrow B\n"


def test_main_refuses_an_unreadable_theirs_without_touching_ours(tmp_path):
    b, o, _ = _sides(tmp_path, BASE, BASE + OURS_BLOCK, "")
    assert main(["drv", b, o, str(tmp_path / "absent")]) == 1
    assert Path(o).read_text(encoding="utf-8") == BASE + OURS_BLOCK


def test_main_preserves_crlf(tmp_path):
    base = "a\r\nb\r\n"
    b, o, t = _sides(tmp_path, base, base + "O\r\n", base + "T\r\n")
    assert main(["drv", b, o, t]) == 0
    assert Path(o).read_bytes() == b"a\r\nb\r\nO\r\nT\r\n"


def test_main_rejects_short_argv():
    assert main(["drv", "only-one"]) == 1


# --- live git merge through the registered wrapper (guard-1290) ---------------

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


@needs_git
def test_live_git_merge_of_concurrent_readings_selfheals(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    # POSIX-form absolute path: git hands the command to a shell, which would eat
    # Windows backslashes (see test_git_merge_ayoai_ledger.py, ).
    wrapper = Path(_SCRIPTS, "git-merge-append-ledger.sh").as_posix()
    _git(repo, "config", "merge.ayoai-append-ledger.driver", f"bash {wrapper} %O %A %B %P")
    (repo / ".gitattributes").write_text("core/config/*-readings.md merge=ayoai-append-ledger\n")
    ledger = repo / "core" / "config" / "replay-instrument-readings.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(BASE, encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "theirs")
    ledger.write_text(BASE + THEIRS_BLOCK, encoding="utf-8", newline="")
    _git(repo, "commit", "-qam", "zeta reading")
    _git(repo, "checkout", "-q", "main")
    ledger.write_text(BASE + OURS_BLOCK, encoding="utf-8", newline="")
    _git(repo, "commit", "-qam", "bravo reading")

    res = _git(repo, "merge", "theirs", "-m", "merge")

    assert res.returncode == 0, f"merge aborted (driver not invoked?): {res.stdout}{res.stderr}"
    assert ledger.read_text(encoding="utf-8") == BASE + OURS_BLOCK + THEIRS_BLOCK
    assert not (repo / ".git" / "MERGE_HEAD").exists()
