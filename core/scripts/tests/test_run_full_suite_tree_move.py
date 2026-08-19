"""Tests for run-full-suite's mid-run tree-move detector ().

WHY THE DETECTOR EXISTS: the runner computes its chunk file lists AT LAUNCH.
If a merge lands while the run is in flight, chunk 00 executed against one tree
and the later chunks against another -- possibly against paths the merge
DELETED. The TOTAL is then a mixed-tree number that means nothing, and it looks
completely normal. Measured 2026-08-18 on cc-07: the worker loop's Phase -0.3
pull (`iteration-push.sh --no-push`) integrated 4 origin commits mid-run, two of
them deletions. The worker net forces a Skill(worker-loop) re-entry on EVERY
turn-end and every re-entry runs Phase -0.3, so any suite longer than one turn
is exposed at every turn boundary -- structural, not one agent's bad luck.

These tests pin the FAIL-OPEN contract of `_git_head`, which is the half that
already broke once. The detector must never take the suite down with it: a
missing git, a non-repo tree, a hang, or a stubbed subprocess must all degrade
to "NOT RUN" and let the run finish. The inverse error (crashing the runner to
report on the runner) would be strictly worse than the mixed-tree run it exists
to catch.

The `returns None` case is not hypothetical and is why the result is guarded as
well as the call: this suite's own tests stub `subprocess.run`, and some stubs
RETURN None rather than raising, so a try/except around only the CALL still dies
on `r.returncode`. That shipped and was caught by
test_run_full_suite_triage.py::test_run_clears_stale_chunk_logs_from_a_prior_run.
"""
import importlib.util
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


class _Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


# ── positive control ────────────────────────────────────────────────────────

def test_reads_a_real_head_from_the_project_root():
    """Positive control. Without this, every None-returning test below would
    pass just as happily against a helper that can never read anything."""
    head = RFS._git_head(RFS.PROJECT_ROOT)
    assert head is not None, "PROJECT_ROOT is a git checkout; HEAD must read"
    assert len(head) >= 7, head
    assert all(c in "0123456789abcdef" for c in head), head


def test_two_reads_of_an_unmoving_tree_agree():
    """The detector's no-fire path: an unchanged tree must compare equal, or
    every run would be reported as tree-moved."""
    assert RFS._git_head(RFS.PROJECT_ROOT) == RFS._git_head(RFS.PROJECT_ROOT)


# ── fail-open contract ──────────────────────────────────────────────────────

def test_stub_returning_none_is_not_a_crash(monkeypatch):
    """The regression that actually shipped: guarding the call but not the
    result. A stub returning None must yield None, never AttributeError."""
    monkeypatch.setattr(RFS.subprocess, "run", lambda *a, **k: None)
    assert RFS._git_head(RFS.PROJECT_ROOT) is None


def test_nonzero_returncode_is_none(monkeypatch):
    monkeypatch.setattr(RFS.subprocess, "run",
                        lambda *a, **k: _Result(returncode=128, stdout=""))
    assert RFS._git_head(RFS.PROJECT_ROOT) is None


def test_raising_subprocess_is_none(monkeypatch):
    def _boom(*a, **k):
        raise OSError("git not found")
    monkeypatch.setattr(RFS.subprocess, "run", _boom)
    assert RFS._git_head(RFS.PROJECT_ROOT) is None


def test_timeout_is_none(monkeypatch):
    """A hung `git rev-parse` must not hang the suite's own verdict."""
    def _slow(*a, **k):
        raise RFS.subprocess.TimeoutExpired(cmd="git", timeout=10)
    monkeypatch.setattr(RFS.subprocess, "run", _slow)
    assert RFS._git_head(RFS.PROJECT_ROOT) is None


def test_empty_stdout_is_none_not_empty_string(monkeypatch):
    """None and "" must not be conflated: the caller renders None as NOT RUN,
    and an empty string would compare EQUAL to another empty string and be
    reported as a confirmed unmoved tree -- a false all-clear."""
    monkeypatch.setattr(RFS.subprocess, "run",
                        lambda *a, **k: _Result(returncode=0, stdout="  \n"))
    assert RFS._git_head(RFS.PROJECT_ROOT) is None
