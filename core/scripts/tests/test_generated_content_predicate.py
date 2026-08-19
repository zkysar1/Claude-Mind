"""Tests for _generated_content_predicate + generated-content-commit-audit ().

The predicate is an SSOT shared between the Layer-C detective here and any
future Layer-A gate. Its two siblings (_gradle_tests_predicate,
_swakeup_predicate) both carry a test file for the same reason: the whole value
of the split is that the enforcing and observing layers cannot disagree, and an
untested predicate drifts silently in both at once.

The high/ambiguous SPLIT is what these tests mostly pin. It is a calibration
decision, not a fact about the filesystem — `vendor` moved from HIGH to
AMBIGUOUS after the first live run flagged a deliberate vendoring — so it is
exactly the kind of judgment a later reader will "tidy" without knowing why.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from _generated_content_predicate import (  # noqa: E402
    AMBIGUOUS_SEGMENTS,
    HIGH_CONFIDENCE_SEGMENTS,
    classify_path,
    classify_paths,
    evaluate_commit,
)

AUDIT_PY = SCRIPTS / "generated-content-commit-audit.py"
AUDIT_SH = SCRIPTS / "generated-content-commit-audit.sh"


def _load_audit():
    spec = importlib.util.spec_from_file_location("gcca", AUDIT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- classify_path


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/.venv/lib/python3.11/site-packages/x.py", ".venv"),
        ("node_modules/left-pad/index.js", "node_modules"),
        ("a/b/__pycache__/mod.cpython-311.pyc", "__pycache__"),
        (".tox/py311/bin/pytest", ".tox"),
        ("build/mod.pyc", "*.pyc"),          # suffix wins without the ambiguous set
        ("lib/libfoo.so", "*.so"),
        ("pkg.egg-info/PKG-INFO", "*.egg-info"),
        ("src/main.py", None),
        ("README.md", None),
        ("", None),
    ],
)
def test_classify_path_high_confidence(path, expected):
    assert classify_path(path) == expected


def test_ambiguous_segments_are_opt_in():
    """The default set must stay quiet on legitimate project layouts.

    A detective that fires on every repo's `build/` is one whose readers learn
    to skip it, and a skipped detective is worth as much as no detective.
    """
    for seg in AMBIGUOUS_SEGMENTS:
        path = "%s/some/file.txt" % seg
        assert classify_path(path) is None, seg
        assert classify_path(path, include_ambiguous=True) == seg, seg


def test_vendor_is_ambiguous_not_high():
    """Regression pin for the 8c18bf7 calibration (2026-08-11).

    `vendor` started in the HIGH set and flagged a commit that vendored a
    third-party repo ON PURPOSE. The dividing line is not "is it third-party"
    but "did anyone CHOOSE to commit it" — guard-793 is about content the
    author did not intend, so a reviewed vendoring is outside it by definition.
    """
    assert "vendor" in AMBIGUOUS_SEGMENTS
    assert "vendor" not in HIGH_CONFIDENCE_SEGMENTS


def test_backslash_is_not_a_path_separator():
    """git reports forward slashes on every platform.

    So a backslash is a literal filename character. Splitting on it would make
    a file legitimately named `weird\\.venv\\name.txt` read as vendored.
    """
    assert classify_path(r"src/weird\.venv\name.txt") is None
    assert classify_path("src/.venv/name.txt") == ".venv"


def test_segment_match_is_exact_not_substring():
    """`.venvish/` and `myvenv/` are ordinary directories."""
    assert classify_path("myvenv/file.py") is None
    assert classify_path(".venvish/file.py") is None
    assert classify_path("venv/file.py") == "venv"


def test_classify_paths_counts_markers():
    gen, markers = classify_paths([".venv/a", ".venv/b", "node_modules/c", "src/main.py"])
    assert len(gen) == 3
    assert markers == {".venv": 2, "node_modules": 1}


# -------------------------------------------------------------- evaluate_commit


def test_evaluate_commit_flags_the_reference_shape():
    """The 59e15c1 shape: a one-line subject carrying a vendored virtualenv."""
    added = [".venv/lib/f%d.py" % i for i in range(545)]
    all_paths = added + ["src/safe_key.py", "tests/test_safe_key.py"]
    v = evaluate_commit(all_paths, added)
    assert v["flagged"] is True
    assert v["total_files"] == 547
    assert v["generated_added"] == 545
    assert v["non_generated_files"] == 2
    assert v["markers"] == {".venv": 545}
    assert len(v["sample"]) == 5, "sample must stay bounded — 545 paths is not evidence"


def test_cleanup_commit_is_not_a_violation():
    """A commit that DELETES vendored content is the good outcome.

    It touches generated paths too, so keying on `all_paths` would report the
    cleanup as the thing it cleaned up — inverting the signal.
    """
    v = evaluate_commit([".venv/a", ".venv/b", "README.md"], added_paths=[])
    assert v["flagged"] is False
    assert v["cleanup_only"] is True
    assert v["generated_total"] == 2
    assert v["generated_added"] == 0


def test_ordinary_commit_is_clean():
    v = evaluate_commit(["src/main.py", "tests/test_main.py"], ["tests/test_main.py"])
    assert v["flagged"] is False
    assert v["cleanup_only"] is False
    assert v["generated_total"] == 0


def test_min_added_threshold():
    added = ["__pycache__/a.pyc", "__pycache__/b.pyc"]
    assert evaluate_commit(added, added, min_added=2)["flagged"] is True
    assert evaluate_commit(added, added, min_added=3)["flagged"] is False


def test_evaluate_commit_tolerates_none():
    v = evaluate_commit(None, None)
    assert v["flagged"] is False
    assert v["total_files"] == 0


# ------------------------------------------------------------- discover_repos


def test_discover_repos_reports_unreachable_roots(tmp_path, monkeypatch):
    """A missing root must be REPORTED, never silently dropped.

    A zero-finding run over 2 of 3 roots and one over 3 of 3 print the same
    `findings: []`; the coverage line is what makes the zero falsifiable
    (guard-1760, rb-245). Measured on cc-03: 1 of 3 configured roots absent.
    """
    mod = _load_audit()

    real = tmp_path / "repo-a"
    (real / ".git").mkdir(parents=True)
    parent = tmp_path / "parent"
    (parent / "child" / ".git").mkdir(parents=True)
    missing = tmp_path / "not-here"

    monkeypatch.setenv(
        "AGENT_WRITE_PATH", "%s;%s;%s" % (real, parent, missing)
    )
    repos, unreachable = mod.discover_repos()

    assert str(real) in repos, "an entry that IS a repo must be scanned"
    assert str(parent / "child") in repos, "an entry that PARENTS repos must expand"
    assert [u["root"] for u in unreachable] == [str(missing)]
    assert "does not exist" in unreachable[0]["reason"]


def test_discover_repos_semicolon_separated(tmp_path, monkeypatch):
    """AGENT_WRITE_PATH is a LIST. The first draft treated it as one parent
    directory and enumerated zero repos while reporting success."""
    mod = _load_audit()
    for name in ("r1", "r2"):
        (tmp_path / name / ".git").mkdir(parents=True)
    monkeypatch.setenv(
        "AGENT_WRITE_PATH", "%s;%s" % (tmp_path / "r1", tmp_path / "r2")
    )
    repos, unreachable = mod.discover_repos()
    assert len(repos) == 2 and unreachable == []


def test_zero_repos_is_reported_as_coverage_failure(tmp_path, monkeypatch):
    mod = _load_audit()
    monkeypatch.setenv("AGENT_WRITE_PATH", "")
    repos, _ = mod.discover_repos()
    assert repos == []


# ------------------------------------------------- unscanned-commit accounting


def test_failed_added_query_is_unscanned_never_cleanup(tmp_path, monkeypatch):
    """The fresh-eyes finding: a failed --diff-filter=A must NOT read as cleanup.

    Defaulting added_paths to [] on failure makes generated_added zero while
    generated_total stays high — which is precisely the signature of a CLEANUP.
    A 545-file vendoring violation then reports as the GOOD outcome, silently.
    """
    mod = _load_audit()
    repo = str(tmp_path)

    vendored = [".venv/lib/f%d.py" % i for i in range(545)]

    def fake_git(_repo, *args):
        if "rev-parse" in args:
            return 0, "true\n"
        if "log" in args:
            return 0, "SHA\x1fabc1234\x1fauthor\x1f2026-08-11\x1ffix: one-line change\n"
        if "--diff-filter=A" in args:
            return 128, ""                       # the ADDED query fails
        return 0, "\n".join(vendored + ["src/safe.py"]) + "\n"

    monkeypatch.setattr(mod, "_git", fake_git)
    findings, err, unscanned = mod.audit_repo(repo, 90, False, 1)

    assert err is None
    assert findings == [], "a commit we could not read must not produce a verdict"
    assert len(unscanned) == 1, "it must be COUNTED, not silently dropped"
    assert unscanned[0]["rc_add"] == 128
    assert unscanned[0]["rc_all"] == 0


def test_failed_all_query_is_counted_not_silently_skipped(tmp_path, monkeypatch):
    """The milder twin: skipping on rc_all overstated coverage.

    repos_scanned still counted the repo, so a zero-finding run over a repo whose
    commits all failed was indistinguishable from a clean one — in a tool whose
    entire contract is a falsifiable denominator.
    """
    mod = _load_audit()

    def fake_git(_repo, *args):
        if "rev-parse" in args:
            return 0, "true\n"
        if "log" in args:
            return 0, "SHA\x1fabc1234\x1fauthor\x1f2026-08-11\x1fsubject\n"
        return 128, ""                            # both diff-tree calls fail

    monkeypatch.setattr(mod, "_git", fake_git)
    findings, err, unscanned = mod.audit_repo(str(tmp_path), 90, False, 1)

    assert findings == [] and err is None
    assert len(unscanned) == 1 and unscanned[0]["rc_all"] == 128


def test_min_added_zero_is_rejected():
    """0 flags every commit (0 >= 0), turning the detector into a firehose."""
    out = subprocess.run(
        [sys.executable, str(AUDIT_PY), "--repo", ".", "--min-added", "0"],
        capture_output=True, text=True,
    )
    assert out.returncode == 2, "argparse.error exits 2"
    assert "min-added" in (out.stderr or "")


# ------------------------------------------------------------------ report-only


def test_audit_invokes_no_write_capable_git_verb():
    """Structural proof of the goal's third verification outcome.

    History rewriting is out of scope by the goal that commissioned this and
    would be unrecoverable across a fleet that shares these working trees.
    """
    src = AUDIT_PY.read_text(encoding="utf-8")
    write_verbs = (
        "commit", "add", "push", "amend", "rebase", "reset", "filter-branch",
        "checkout", "merge", "cherry-pick", "revert", "restore", "update-ref",
    )
    for verb in write_verbs:
        assert '"%s"' % verb not in src, "write-capable git verb %r present" % verb
    assert src.count("subprocess.run") == 1, "all git access must route through _git()"


def test_wrapper_exports_agent_write_path():
    """rb-2563: `_paths.sh` SETS but does not EXPORT.

    `exec` passes the ENVIRONMENT, so without the export the Python child reads
    an empty AGENT_WRITE_PATH and enumerates ZERO repos — reporting a confident
    all-clear over nothing. Measured on cc-03 while building this script.
    """
    assert "export AGENT_WRITE_PATH" in AUDIT_SH.read_text(encoding="utf-8")


def test_audit_runs_end_to_end_on_a_synthetic_repo(tmp_path):
    """Positive control: the audit must FIND a violation it is pointed at.

    Without this, every other test could pass against a sweep that returns
    empty for a reason unrelated to the corpus being clean.

    THE SINGLE COMMIT HERE IS A ROOT COMMIT, AND THAT IS THE POINT — do not
    "improve" this fixture by committing something first. `diff-tree` emits
    ZERO paths for a root commit unless `--root` is passed, so before that flag
    was added this test failed exactly here, which is how the defect was found.
    A repo's initial import is the highest-risk moment for vendoring a whole
    .venv, so root commits are the LAST population that may go unscanned.
    Confirmed live the same day: e60c8d2 (Ayoai-Game-System) is a root commit
    carrying 12 vendored paths that the pre-fix sweep could not see at all.
    The assertion below pins the root-ness so the coverage cannot vanish
    silently.
    """
    repo = tmp_path / "synthetic"
    repo.mkdir()
    env_git = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}

    def git(*args):
        import os
        e = dict(os.environ); e.update(env_git)
        return subprocess.run(["git", "-C", str(repo)] + list(args),
                              capture_output=True, text=True, env=e)

    git("init", "-q")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("x = 1\n")
    venv = repo / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "vendored.py").write_text("y = 2\n")
    git("add", "-A")
    r = git("commit", "-q", "-m", "fix: a one-line change")
    if r.returncode != 0:
        pytest.skip("git unavailable in this environment: %s" % r.stderr.strip())

    depth = git("rev-list", "--count", "HEAD").stdout.strip()
    assert depth == "1", (
        "fixture must stay a ROOT commit — see the docstring. Got depth=%s, which "
        "silently removes the --root regression coverage." % depth
    )

    out = subprocess.run(
        [sys.executable, str(AUDIT_PY), "--repo", str(repo), "--json"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    import json
    report = json.loads(out.stdout)
    assert report["repos_scanned"] == 1
    assert report["findings_count"] == 1
    f = report["findings"][0]
    assert f["generated_added"] == 1
    assert f["markers"] == {".venv": 1}
    assert f["non_generated_files"] == 1
    assert f["subject"] == "fix: a one-line change"

    # --exit-on-hits must carry the verdict in the exit code.
    out2 = subprocess.run(
        [sys.executable, str(AUDIT_PY), "--repo", str(repo), "--exit-on-hits"],
        capture_output=True, text=True,
    )
    assert out2.returncode == 1
