"""Tests for the voided-run ATTRIBUTION half of run-full-suite ().

WHAT THIS COVERS, and why it is separate from test_run_full_suite_tree_move.py:
that file pins the DETECTOR (does the runner notice the tree moved). This file
pins what the runner SAYS once it has noticed -- who moved it, and a
machine-readable record so a later cadence can count voided runs.

THE ORIGINAL DEFECT: the verdict said INVALID (tree-moved) and printed two
opaque shas. It never named the commits, and it prints at the END of a run --
hours later, to the Body that LAUNCHED it, which is never the Body that caused
the void. So the cost was real, recurring, and structurally unattributable:
nobody who caused it ever learned they did. Measured 2026-09-04: one ~2h run,
8 chunks, ~8,200 tests, voided by FIVE commits, two of which were
iteration-push's OWN self-heal (the framework voiding its own run).

NOTE ON SCOPE, because the filing goal's premise was half wrong and confirming
it at the source is what shrank this work: the start sha was ALREADY recorded
(g-115-6685 `head_at_launch`) and already printed. Only the offender list and
the machine-readable record were missing. A reader coming here from the goal
text should not go looking for a start-sha bug that never existed.
"""
import ast
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TARGET = SCRIPT_DIR.parent / "run-full-suite.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_full_suite", TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_full_suite"] = mod
    spec.loader.exec_module(mod)
    return mod


RFS = _load()


# ── _git_offenders: positive controls first ─────────────────────────────────

def test_names_real_commits_from_the_real_repo():
    """POSITIVE CONTROL, and the one the filing goal explicitly demanded: the
    feature only fires when HEAD MOVES, so a green suite proves nothing. Every
    None/[] assertion below would pass just as happily against a helper that
    can never read anything -- this is what separates 'found none' from
    'cannot look'."""
    rows = RFS._git_offenders(RFS.PROJECT_ROOT, "HEAD~1", "HEAD")
    assert rows is not None, "PROJECT_ROOT is a git checkout; the range must read"
    assert rows, "HEAD~1..HEAD must contain at least one commit"
    for c in rows:
        assert set(c) == {"sha", "author", "subject"}, c
        assert c["sha"], c
        assert all(ch in "0123456789abcdef" for ch in c["sha"]), c


def test_an_empty_range_is_a_list_not_none():
    """[] and None are DIFFERENT answers. HEAD..HEAD is genuinely empty, and
    must NOT be reported the same way as a git failure -- the verdict renders
    them with different text because 'HEAD moved but the range is empty' means
    a reset/rebase moved HEAD backwards, which is a finding, not an error."""
    rows = RFS._git_offenders(RFS.PROJECT_ROOT, "HEAD", "HEAD")
    assert rows == [], rows


def test_subject_containing_the_separator_does_not_split_a_row():
    """The rows are packed with \\x1f rather than a printable delimiter, and
    the split is bounded at 2 -- a commit subject containing the separator (or
    a pipe, or a tab) must not manufacture a fourth field and silently drop
    the row."""
    packed = "abc1234\x1fSome Author\x1ffix: a\x1fweird\x1fsubject\n"

    class _R:
        returncode = 0
        stdout = packed

    RFS.subprocess.run, real = (lambda *a, **k: _R()), RFS.subprocess.run
    try:
        rows = RFS._git_offenders(RFS.PROJECT_ROOT, "a", "b")
    finally:
        RFS.subprocess.run = real
    assert rows == [{"sha": "abc1234", "author": "Some Author",
                     "subject": "fix: a\x1fweird\x1fsubject"}], rows


# ── _git_offenders: fail-open contract (mirrors _git_head's) ────────────────

def test_stub_returning_none_is_not_a_crash(monkeypatch):
    """Same regression that shipped once for _git_head: guarding the call but
    not the result. A stub returning None must yield None, never
    AttributeError -- a suite run must not die reporting on itself."""
    monkeypatch.setattr(RFS.subprocess, "run", lambda *a, **k: None)
    assert RFS._git_offenders(RFS.PROJECT_ROOT, "a", "b") is None


def test_nonzero_git_is_none_not_empty(monkeypatch):
    """A failed `git log` must read as 'could not look', never as 'no
    offenders' -- the guard-1760 defect the verdict text calls out."""
    class _R:
        returncode = 128
        stdout = ""
    monkeypatch.setattr(RFS.subprocess, "run", lambda *a, **k: _R())
    assert RFS._git_offenders(RFS.PROJECT_ROOT, "a", "b") is None


def test_raising_git_is_none(monkeypatch):
    def _boom(*a, **k):
        raise OSError("git missing")
    monkeypatch.setattr(RFS.subprocess, "run", _boom)
    assert RFS._git_offenders(RFS.PROJECT_ROOT, "a", "b") is None


# ── _emit_void_record ───────────────────────────────────────────────────────

def test_record_is_one_parseable_json_line(capsys):
    RFS._emit_void_record("tree-moved", head_at_launch="aaa", offenders=[])
    out = capsys.readouterr().out.strip()
    assert out.startswith(RFS.VOID_RECORD_PREFIX), out
    rec = json.loads(out[len(RFS.VOID_RECORD_PREFIX):])
    assert rec["cause"] == "tree-moved"
    assert rec["head_at_launch"] == "aaa"
    assert rec["at"], rec
    assert "agent" in rec and "sid" in rec


def test_record_never_raises_on_unserialisable_payload(capsys):
    """Fail-open like every other diagnostic here: an odd field must not take
    the run down at the exact moment it is reporting a problem."""
    RFS._emit_void_record("contended", weird=object())
    # No exception. The line is either absent or well-formed; never a crash.
    out = capsys.readouterr().out
    assert "Traceback" not in out


# ── guard-3948: the key belongs on EVERY exit path ──────────────────────────

def _return_two_sites():
    """-> {enclosing function name: [(lineno, guarded?), ...]} for `return 2`."""
    tree = ast.parse(TARGET.read_text(encoding="utf-8"))
    found = {}

    def walk_body(body, fname):
        for i, node in enumerate(body):
            if (isinstance(node, ast.Return)
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == 2):
                guarded = any(
                    isinstance(prev, ast.Expr)
                    and isinstance(prev.value, ast.Call)
                    and isinstance(prev.value.func, ast.Name)
                    and prev.value.func.id == "_emit_void_record"
                    for prev in body[:i])
                found.setdefault(fname, []).append((node.lineno, guarded))
            for attr in ("body", "orelse", "finalbody"):
                inner = getattr(node, attr, None)
                if isinstance(inner, list):
                    walk_body(inner, fname)
            for h in getattr(node, "handlers", []) or []:
                walk_body(h.body, fname)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            walk_body(node.body, node.name)
    return found


def test_every_voiding_return_emits_a_record():
    """guard-3948: when you add a key to a contract output, add it to EVERY
    exit path. A cadence counting voided runs must not silently miss the
    chunk-spawn and contended voids the way the unattributed verdict did.

    `triage()` is the ONE documented exception and is asserted as such rather
    than skipped silently: it re-reads a PRIOR run's chunk logs and executes
    no run, so it voids nothing -- emitting there would double-count a run
    that already recorded its own void.
    """
    sites = _return_two_sites()
    assert sites, "expected `return 2` sites; the AST walk found none"
    unguarded = {fn: [ln for ln, ok in rows if not ok]
                 for fn, rows in sites.items()}
    unguarded = {fn: lns for fn, lns in unguarded.items() if lns}
    assert set(unguarded) <= {"triage"}, (
        "every voiding `return 2` outside triage() must be preceded by "
        "_emit_void_record in the same block; unguarded: %r" % unguarded)


def test_the_cause_vocabulary_is_pinned_exactly():
    """The exact-equality half of guard-3948 -- an `in` check passes while a
    new exit path quietly ships with no record. If this fails because you
    ADDED a void path, add its cause here AND teach whatever counts these
    records about it; do not just widen the set."""
    tree = ast.parse(TARGET.read_text(encoding="utf-8"))
    causes = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_emit_void_record"
                and node.args):
            a = node.args[0]
            if isinstance(a, ast.Constant):
                causes.add(a.value)
            elif isinstance(a, ast.IfExp):          # "hung" if hung else ...
                for side in (a.body, a.orelse):
                    if isinstance(side, ast.Constant):
                        causes.add(side.value)
    assert causes == {"tree-moved", "contended", "hung", "argv-too-long",
                      "chunk-spawn-failed", "chunk-rc-without-running"}, causes


# ── the output caps ─────────────────────────────────────────────────────────
#
# These exist because the FIRST working version was unbounded, and only running
# it against the real repo showed why that matters: `HEAD~5..HEAD` resolves to
# THIRTY commits here, not five, because a merge brings a whole branch into the
# range and the worker loop merges on every turn-end. Unbounded, one void
# printed 30 lines and emitted a ~4KB JSON line. The unit tests all passed
# throughout -- the defect was only visible by rendering real data.

def test_show_limit_is_below_the_read_limit():
    """Showing more rows than were read is incoherent, and would silently make
    the '... and N more' arithmetic negative."""
    assert 0 < RFS._OFFENDER_SHOW_LIMIT < RFS._OFFENDER_READ_LIMIT


def test_the_read_limit_actually_reaches_git(monkeypatch):
    """The cap must be applied by git, not by slicing afterwards -- otherwise a
    voided run on a busy tree pays for thousands of rows it then discards."""
    seen = {}

    class _R:
        returncode = 0
        stdout = ""

    def _spy(cmd, *a, **k):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(RFS.subprocess, "run", _spy)
    RFS._git_offenders(RFS.PROJECT_ROOT, "a", "b", limit=7)
    assert "-n" in seen["cmd"], seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("-n") + 1] == "7", seen["cmd"]


def test_limit_is_honoured_against_the_real_repo():
    """POSITIVE CONTROL for the cap: a range known to hold many commits must
    come back truncated to exactly the limit, proving the cap is real and not
    merely passed."""
    rows = RFS._git_offenders(RFS.PROJECT_ROOT, "HEAD~5", "HEAD", limit=3)
    assert rows is not None and len(rows) == 3, rows


# ── _render_offenders: the branch text itself ───────────────────────────────
#
# THIS BLOCK IS THE POINT OF THE EXTRACTION (guard-5867). A clean reading is
# evidence about a conditional mechanism ONLY IF the mechanism's triggering
# precondition was actually met -- and this text prints on exactly one branch,
# reachable only by a real suite whose HEAD moves underneath it. While the
# render sat inline in main(), the only way to "check" it was to re-type its
# logic in a scratch script and eyeball the output, which tests the copy.
# Every assertion below runs the SHIPPING code.

def test_unreadable_renders_did_not_look_not_found_none():
    """The guard-1760 distinction, at the one place a reader sees it: a failed
    `git log` must never render as an absence of offenders."""
    lines = RFS._render_offenders(None)
    assert len(lines) == 1, lines
    assert "COULD NOT READ" in lines[0]
    assert "NOT 'there were none'" in lines[0]


def test_empty_range_says_head_moved_backwards():
    """[] is a FINDING (a reset or rebase), not an error and not a no-op -- the
    run is still void and the text must say so."""
    lines = RFS._render_offenders([])
    assert len(lines) == 1, lines
    assert "EMPTY" in lines[0]
    assert "BACKWARDS" in lines[0]
    assert "still void" in lines[0]


def test_normal_case_names_every_author_and_lists_the_commits():
    rows = [{"sha": "aaa1111", "author": "Ann", "subject": "fix: one"},
            {"sha": "bbb2222", "author": "Bob", "subject": "fix: two"},
            {"sha": "ccc3333", "author": "Ann", "subject": "fix: three"}]
    lines = RFS._render_offenders(rows)
    head = lines[0]
    assert "Offending commits: 3" in head, head
    assert "AT LEAST" not in head, "3 is under the cap; no floor marker"
    assert "Ann, Bob" in head, "authors deduped and sorted"
    body = "\n".join(lines)
    for r in rows:
        assert r["sha"] in body and r["subject"] in body
    assert "not shown" not in body, "3 is under the show limit"


def test_over_the_show_limit_truncates_and_says_how_many_it_hid():
    n = RFS._OFFENDER_SHOW_LIMIT + 4
    rows = [{"sha": "s%05d" % i, "author": "Ann", "subject": "c%d" % i}
            for i in range(n)]
    lines = RFS._render_offenders(rows)
    shown = [ln for ln in lines if ln.startswith("    s")]
    assert len(shown) == RFS._OFFENDER_SHOW_LIMIT, len(shown)
    assert any("... and 4 more not shown" in ln for ln in lines), lines


def test_at_the_read_cap_the_count_is_marked_as_a_floor():
    """A merge brings a whole branch into the range, so a capped read must not
    report its count as a total -- the reader would under-estimate the blast
    radius of the very event being reported."""
    rows = [{"sha": "s%05d" % i, "author": "Ann", "subject": "c%d" % i}
            for i in range(RFS._OFFENDER_READ_LIMIT)]
    lines = RFS._render_offenders(rows)
    assert lines[0].startswith("  Offending commits: AT LEAST "), lines[0]
    assert any("is a FLOOR, not a total" in ln for ln in lines), lines


def test_the_uncommitted_caveat_rides_on_every_non_empty_render():
    """guard-5987: a suite is ALSO voided by an uncommitted mid-run edit, which
    moves no sha and is invisible here. The offender list is the COMMITTED half
    only, and a reader who is not told that will read it as complete."""
    rows = [{"sha": "aaa1111", "author": "Ann", "subject": "fix: one"}]
    body = "\n".join(RFS._render_offenders(rows))
    assert "COMMITTED half only" in body
    assert "guard-5987" in body


def test_render_survives_a_subject_that_is_pure_padding_width():
    """The %-18s author column and the [:72] subject slice are formatting, not
    validation -- an over-long field must truncate, never raise."""
    rows = [{"sha": "a" * 12, "author": "A" * 60, "subject": "S" * 400}]
    lines = RFS._render_offenders(rows)
    assert len(lines) >= 2
    assert "S" * 72 in lines[1] and "S" * 73 not in lines[1]


def test_the_shipping_branch_calls_the_helper_rather_than_re_rendering():
    """The extraction is only worth anything if main() actually routes through
    it. Pins the wiring so a future edit cannot quietly re-inline the text and
    leave these tests passing against a helper nothing calls (guard-1943:
    pinning the writer says nothing about the wiring)."""
    src = TARGET.read_text(encoding="utf-8")
    assert "for line in _render_offenders(offenders):" in src
    # The old inline text must exist in exactly ONE place -- the helper.
    assert src.count("Offending commits: COULD NOT READ") == 1, (
        "the render text appears more than once; main() has re-inlined it")


def test_render_against_offenders_read_from_the_real_repo():
    """END-TO-END POSITIVE CONTROL, and the closest reachable approximation of
    the live branch: real git output, through the real reader, into the real
    renderer -- the same two calls main() makes, in the same order."""
    offenders = RFS._git_offenders(RFS.PROJECT_ROOT, "HEAD~1", "HEAD")
    lines = RFS._render_offenders(offenders)
    assert lines and lines[0].startswith("  Offending commits: ")
    assert "COULD NOT READ" not in lines[0]
    assert any(c["sha"] in "\n".join(lines) for c in offenders)
