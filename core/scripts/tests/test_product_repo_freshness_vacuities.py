"""Tests for the two vacuities fixed in product-repo-freshness.py ().

Both defects share a grammar: a probe that CANNOT RUN produces output byte-
identical to a probe that ran and found nothing. That shape is invisible to an
ordinary test, because the healthy path and the broken path agree — so every
test here is written as a DISCRIMINATION GATE with an explicit control that
must produce the opposite result. A test that merely asserts the fixed
behaviour would pass just as happily against the defect.

DEFECT A -- Windows CRLF (measured 2026-08-03, DESKTOP-O91DLK2, Windows 10 /
MSYS2; board msg-20260803-155838-alpha-5117). Python's text-mode stdout
translates "\\n" to os.linesep, so `--list` emitted a trailing CR per path.
`IFS= read -r r` preserves it, so consumers ran `git -C '<path><CR>'` ->
rc=128 on all 57 repos, printed no findings, and the scan read as ALL CLEAN.

  THE PLATFORM PROBLEM, and why these tests are not vacuous on Linux
  (guard-2982: a criterion of the form "X whenever Y" is vacuously satisfied by
  any run in which Y never occurred). On Linux os.linesep is "\\n", so
  asserting "--list emits no CR" passes with OR without the fix and proves
  nothing. The Windows translation is reproduced PORTABLY instead: a
  TextIOWrapper opened with newline="\\r\\n" performs exactly the translation
  Windows text-mode stdout performs. `test_..._control` asserts that wrapper
  really does emit CR when the fix is not applied, so the paired assertion has
  measurable resolving power on every platform.

DEFECT B -- name-based selection (measured 2026-08-05, cc-05). Selection
matches a repo's DIRECTORY NAME against goal text; code goals routinely cite
paths, packages and class names instead. Measured over all 1,158 completed
work_class=product goals using the LIVE field set (title + description, which
is what goal_text() returns -- NOT outcome_note): 727 of them, 62.8%, select
ZERO repos. Before this fix those runs printed zero bytes at rc=0, identical
to a clean check.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:                                    # SSOT first, chain only as fallback
    from _paths import PROJECT_ROOT
except Exception:
    # parents[3], NOT parents[2] -- tests=0, scripts=1, core=2, root=3. The
    # one-level-short chain yields <root>/core/core/... ( class); the
    # sibling suite carries the same note for the same reason.
    PROJECT_ROOT = Path(__file__).resolve().parents[3]

SCRIPT = PROJECT_ROOT / "core" / "scripts" / "product-repo-freshness.py"


def _load():
    """Import the hyphenated script as a module (it cannot be plain-imported)."""
    spec = importlib.util.spec_from_file_location("prf_vac", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load()


# ── DEFECT A1: --list must emit LF, never CRLF ────────────────────────────────

def _windows_like_stdout():
    """A stdout that translates "\\n" -> "\\r\\n", exactly as Windows does."""
    buf = io.BytesIO()
    return buf, io.TextIOWrapper(buf, encoding="utf-8", newline="\r\n")


def test_windows_like_stdout_really_emits_cr_without_the_fix():
    """THE CONTROL. If this ever goes green-to-red the harness has stopped
    reproducing the defect, and every assertion below becomes vacuous."""
    buf, w = _windows_like_stdout()
    w.write("a\nb\n")
    w.flush()
    assert b"\r\n" in buf.getvalue(), (
        "the newline='\\r\\n' wrapper no longer performs the Windows "
        "translation — the paired test below can no longer discriminate")


def test_force_lf_stdout_removes_the_cr():
    buf, w = _windows_like_stdout()
    real, sys.stdout = sys.stdout, w
    try:
        m._force_lf_stdout()
        print("a")
        print("b")
        sys.stdout.flush()
    finally:
        sys.stdout = real
    out = buf.getvalue()
    assert b"\r" not in out, "CR survived _force_lf_stdout(): %r" % out
    assert out == b"a\nb\n", "content changed, not just the line ending: %r" % out


def test_force_lf_stdout_is_fail_open_on_a_stream_without_reconfigure():
    """A caller that swapped in a StringIO (every capsys-based test does) must
    not crash an advisory probe. Fail-open is the contract, not an accident."""
    real, sys.stdout = sys.stdout, io.StringIO()
    try:
        m._force_lf_stdout()          # must not raise
        print("still works")
        assert "still works" in sys.stdout.getvalue()
    finally:
        sys.stdout = real


# ── DEFECT A2: a failed git probe must not read as a clean repo ───────────────

def _stub_git(fail_on):
    """Replace _git so the named subcommand fails while the rest succeed."""
    def fake(repo, *args, **kw):
        sub = args[0] if args else ""
        if sub in fail_on:
            return 1, "", "stubbed failure for %s" % sub
        if sub == "remote":
            return 0, "origin", ""
        if sub == "for-each-ref":
            return 0, "main", ""
        if sub == "rev-list":
            return 0, "0", ""
        if sub == "symbolic-ref":
            return 0, "refs/remotes/origin/main", ""
        return 0, "", ""
    return fake


def test_failed_status_probe_is_not_reported_as_a_clean_tree(monkeypatch):
    monkeypatch.setattr(m, "_git", _stub_git({"status"}))
    rec = m.sweep_status(Path("/nonexistent/repo-a"))
    assert rec["dirty_probe_ok"] is False, "a failed `git status` was recorded as a successful probe"
    assert rec["severity"] != "clean", (
        "a repo whose dirty probe FAILED still reports severity=clean — the "
        "exact vacuity this change exists to remove")


def test_clean_tree_and_failed_probe_are_distinguishable(monkeypatch):
    """THE CONTROL for the test above. Both states yield dirty_files == 0; only
    `dirty_probe_ok` separates them, which is the whole point of the field."""
    monkeypatch.setattr(m, "_git", _stub_git(set()))
    ok = m.sweep_status(Path("/nonexistent/repo-b"))
    monkeypatch.setattr(m, "_git", _stub_git({"status"}))
    bad = m.sweep_status(Path("/nonexistent/repo-b"))
    assert ok["dirty_files"] == bad["dirty_files"] == 0, "fixture drift: counts should agree"
    assert ok["dirty_probe_ok"] is True and bad["dirty_probe_ok"] is False
    assert ok["severity"] == "clean" and bad["severity"] != "clean"


def test_dirty_paths_returns_none_on_probe_failure_and_list_when_clean(monkeypatch):
    monkeypatch.setattr(m, "_git", _stub_git({"status"}))
    assert m._dirty_paths(Path("/nonexistent/repo-c")) is None
    monkeypatch.setattr(m, "_git", _stub_git(set()))
    assert m._dirty_paths(Path("/nonexistent/repo-c")) == []


def test_unreadable_branch_count_is_recorded_not_silently_skipped(monkeypatch):
    monkeypatch.setattr(m, "_git", _stub_git({"rev-list"}))
    rec = m.sweep_status(Path("/nonexistent/repo-d"))
    assert rec["branch_probe_failures"] == ["main"], (
        "a branch whose unpushed count could not be read was dropped silently, "
        "which renders identically to 'this branch has nothing unpushed'")
    assert rec["severity"] != "clean"


def test_render_sweep_speaks_about_probe_failure_at_every_severity():
    """Gating the CANNOT CHECK line on severity=='unknown' would drop the
    caveat from exactly the records that look most authoritative."""
    rec = {"repo": "/x/HighRepo", "name": "HighRepo", "no_remote": False,
           "unpushed": [{"branch": "main", "count": 2, "patches_absent": 2,
                         "on_default": True}],
           "unpushed_total": 2, "merged_equivalent": [], "dirty_files": 0,
           "dirty_age_h": None, "default_branch": "main",
           "dirty_probe_ok": False, "branch_probe_failures": ["wip"],
           "severity": "high", "detail": ""}
    out = m.render_sweep([rec], 1)
    assert "CANNOT CHECK" in out, "probe failure invisible on a high-severity record"
    assert "UNMEASURED, not 0" in out
    assert "UNMEASURED, not clean" in out
    assert "wip" in out


def test_render_sweep_clean_line_cannot_coexist_with_a_failed_probe(monkeypatch):
    """The CLEAN banner asserts '0 dirty' about repos it may not have read.
    Severity promotion is what keeps a failed-probe record out of that branch;
    this pins the two together so removing the promotion breaks a test."""
    monkeypatch.setattr(m, "_git", _stub_git({"status"}))
    rec = m.sweep_status(Path("/nonexistent/repo-e"))
    out = m.render_sweep([rec], 1)
    assert "CLEAN:" not in out, "a repo with an unread working tree was announced CLEAN"


# ── DEFECT B1: an empty selection must not render as a clean check ────────────

def _goal_text_stub(text, ok=True, work_class="product"):
    """Mimics goal_text's out-dict contract so main() sees a work_class."""
    def stub(gid, src, meta=None):
        if meta is not None:
            meta["work_class"] = work_class
        return (text, ok)
    return stub


def test_zero_selection_with_nonempty_enumeration_speaks(monkeypatch, capsys):
    monkeypatch.setattr(m, "enumerate_repos", lambda: [Path("/x/RepoOne"), Path("/x/RepoTwo")])
    monkeypatch.setattr(m, "goal_text", _goal_text_stub("touches nothing nameable"))
    rc = m.main(["--goal-id", "g-000-01", "--no-fetch"])
    cap = capsys.readouterr()
    assert rc == 0, "the advisory must stay non-fatal"
    assert cap.out == "", "stdout should stay quiet; the notice belongs on stderr"
    assert "CANNOT CHECK" in cap.err
    assert "NOT an all-clear" in cap.err
    assert "DIRECTORY NAME" in cap.err, (
        "the notice must name WHY nothing matched, or a reader cannot act on it")


def test_nonzero_selection_stays_silent_when_in_sync(monkeypatch, capsys):
    """THE CONTROL. Silence must still mean checked-and-clean — if the new
    notice fired on a healthy selection it would be noise, and an advisory that
    speaks on the clean path stops being read (the file's own render() rule)."""
    monkeypatch.setattr(m, "enumerate_repos", lambda: [Path("/x/RepoOne")])
    monkeypatch.setattr(m, "goal_text", _goal_text_stub("work on RepoOne"))
    monkeypatch.setattr(m, "freshness", lambda r, do_fetch=True: {
        "repo": str(r), "name": r.name, "behind": 0, "ahead": 0, "branch": "main",
        "upstream": "origin/main", "verdict": "in-sync", "detail": ""})
    m.main(["--goal-id", "g-000-02", "--no-fetch"])
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err == "", (
        "a healthy selection produced output: %r / %r" % (cap.out, cap.err))


@pytest.mark.parametrize("work_class,should_speak", [
    ("product", True),
    ("framework", False),
    ("hygiene", False),
    ("", False),
])
def test_zero_selection_notice_is_gated_on_work_class(monkeypatch, capsys,
                                                      work_class, should_speak):
    """THE DESIGN DECISION, pinned because it is the part most likely to be
    "simplified" back into an ungated check by a future reader who sees only
    g-115-5013's wording ("when selected_count==0 while enumerated_count>0,
    print one line") and not the count that qualified it.

    Ungated, the notice fires on 3,516 of 4,355 completed goals (80.7%),
    including 97.3% of `framework` goals — which have no product repo to check
    at all, so silence there is the correct answer and the noise would
    desensitize a reader to the 16.7% of firings that are real. That is
    precisely the objection `test_a_goal_that_simply_names_no_repo_stays_silent`
    in the sibling suite was written to raise; gating reconciles the two, and
    that test passes UNCHANGED as a result. If this parametrization is ever
    reduced to the `product` row alone, the reconciliation has been lost.
    """
    monkeypatch.setattr(m, "enumerate_repos", lambda: [Path("/x/RepoOne")])
    monkeypatch.setattr(m, "goal_text",
                        _goal_text_stub("names no repo", work_class=work_class))
    m.main(["--goal-id", "g-000-05", "--no-fetch"])
    err = capsys.readouterr().err
    if should_speak:
        assert "CANNOT CHECK" in err, (
            "a product goal whose repo went unexamined stayed silent")
    else:
        assert err == "", (
            "work_class=%r produced advisory noise on a goal with no product "
            "repo to check: %r" % (work_class, err))


def test_failed_goal_lookup_still_uses_its_own_distinct_notice(monkeypatch, capsys):
    """Three different zeros, three different messages. Collapsing any two
    would re-create the ambiguity the whole file is defending against."""
    monkeypatch.setattr(m, "enumerate_repos", lambda: [Path("/x/RepoOne")])
    monkeypatch.setattr(m, "goal_text", _goal_text_stub("", ok=False))
    m.main(["--goal-id", "g-000-03", "--no-fetch"])
    err = capsys.readouterr().err
    assert "could not read goal" in err
    assert "DIRECTORY NAME" not in err, "the two zero-selection notices have collapsed"


def test_empty_enumeration_notice_is_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(m, "enumerate_repos", lambda: [])
    monkeypatch.setattr(m, "goal_text", _goal_text_stub("anything"))
    m.main(["--goal-id", "g-000-04", "--no-fetch"])
    err = capsys.readouterr().err
    assert "enumerated 0 repos" in err
    assert "DIRECTORY NAME" not in err, (
        "the zero-enumeration and zero-selection notices have collapsed — they "
        "have different causes and different remedies")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
