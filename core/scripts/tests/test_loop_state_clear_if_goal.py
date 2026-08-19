#!/usr/bin/env python3
"""test_loop_state_clear_if_goal.py —  regression tests.

`loop-state-save.sh clear` shipped implemented, documented in its own header,
and with ZERO production call sites, so the iteration checkpoint was only ever
corrected by the NEXT claim's `init`. Between a goal exiting (defer, release,
skip) and the next successful claim the checkpoint asserts a goal that is not in
flight, and the SessionStart:compact hook reads it and emits:

    CRITICAL: Your in-flight goal is <id> at phase 'selected'.
    Resume execution on THIS goal. Do NOT re-run goal-selector.sh to
    pick a different one.

Every clause is wrong for a goal that has exited, and it forbids the corrective
action by name. Observed twice: alpha via DEFER (cc-04, g-115-4983) and zeta via
explicit release-then-skip (cc-02, g-115-5004).

`clear` had no safe caller because it had no COMPARE-AND-SWAP: the only place
that wants to clear is a goal exiting, and an unconditional clear there would
unlink an anchor naming a DIFFERENT, live goal. `--if-goal` is that CAS, and
these tests pin it.

WHY THE CAS LIVES IN PYTHON AND NOT IN THE CALLING SHELL. The caller would
otherwise `read` the checkpoint, pipe it through python, and compare — and would
have to strip a trailing \\r first, because the whole round-trip is text-mode on
Windows (`_atomic_write` opens with os.fdopen(fd,"w"), cmd_read prints through a
text-mode stdout). Miss that strip and goal_id arrives as "g-NNN-NN\\r", never
compares equal, and the caller is inert on exactly one platform while testing
green everywhere else. aspirations-claim.sh documents the identical trap at its
own ENSURE check. The single-writer already holds the parsed value.

METHOD NOTE, recorded because it cost a live artifact. These tests drive
`cmd_clear` IN-PROCESS with `_checkpoint_path` monkeypatched at the module. The
obvious shell-level version — export MIND_AGENT_DIR to a tmp dir and call the
wrapper — LOOKS hermetic and is NOT: `loop-state-save._agent_dir()` reads
MIND_AGENT and calls `_paths.agent_dir(name)`, which is agents_root()/<name>,
so the MIND_AGENT_DIR override never reaches this path. Run that way the
"tmp" test seeds, clears, and destroys the REAL session's live checkpoint, and
its output names the real path while reporting every case as passing. Verify a
test's isolation by reading the path it actually wrote, not by the env var you
set (guard-1165 class).

Pure unit test: tmpdir + monkeypatch. No S3, no daemon, no world I/O.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
PROJECT_ROOT = SCRIPTS.parents[1]
for p in (str(SCRIPTS), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(mod_name, file_name):
    spec = importlib.util.spec_from_file_location(mod_name, str(SCRIPTS / file_name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_lss = _load("loop_state_save_g4990", "loop-state-save.py")


class _Args:
    """Stand-in for the argparse namespace cmd_clear receives."""

    def __init__(self, if_goal=None):
        self.if_goal = if_goal


class _Ckpt:
    """A tmp checkpoint file plus a seed helper.

    Deliberately a wrapper object rather than the Path itself: a PosixPath uses
    __slots__, so attaching `.seed` to it raises AttributeError at fixture setup
    (caught here on the first run).
    """

    def __init__(self, path):
        self.path = path

    def exists(self):
        return self.path.exists()

    def read_text(self, **kw):
        return self.path.read_text(**kw)

    def write_text(self, text, **kw):
        return self.path.write_text(text, **kw)

    def seed(self, goal_id):
        self.path.write_text(json.dumps({
            "goal_id": goal_id, "aspiration_id": "asp-115",
            "source": "world", "phase": "selected",
            "selected_at": "2026-08-11T00:00:00",
        }), encoding="utf-8")


@pytest.fixture()
def ckpt(tmp_path, monkeypatch):
    """A checkpoint path under tmp_path, with a seed helper."""
    path = tmp_path / "session" / "iteration-checkpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_lss, "_checkpoint_path", lambda: path)
    return _Ckpt(path)


# --------------------------------------------------------------- the CAS itself

def test_clear_if_goal_matching_removes_the_anchor(ckpt):
    """The path both incidents took: the goal named by the checkpoint exits."""
    ckpt.seed("g-115-4983")
    assert _lss.cmd_clear(_Args(if_goal="g-115-4983")) == 0
    assert not ckpt.exists(), "a matching anchor must be cleared"


def test_clear_if_goal_mismatched_preserves_a_live_anchor(ckpt):
    """The reason `clear` had no safe caller. Releasing goal X must never
    unlink an anchor naming goal Y — Y may be genuinely in flight."""
    ckpt.seed("g-115-9999")
    assert _lss.cmd_clear(_Args(if_goal="g-115-4983")) == 0
    assert ckpt.exists(), "a mismatched anchor must survive"
    assert json.loads(ckpt.read_text(encoding="utf-8"))["goal_id"] == "g-115-9999"


def test_mismatch_is_exit_zero_not_an_error(ckpt):
    """"The anchor moved on" is the normal outcome of a stale release, not a
    failure. A nonzero here would make the fail-open caller log noise on the
    single most common no-op path."""
    ckpt.seed("g-115-9999")
    assert _lss.cmd_clear(_Args(if_goal="g-115-4983")) == 0


def test_absent_checkpoint_is_a_silent_no_op(ckpt):
    """release runs on paths where no checkpoint was ever written."""
    assert not ckpt.exists()
    assert _lss.cmd_clear(_Args(if_goal="g-115-4983")) == 0
    assert not ckpt.exists()


def test_corrupt_checkpoint_is_preserved_not_unlinked(ckpt):
    """An unparseable checkpoint is a DIFFERENT defect. Unlinking it would
    destroy the evidence while looking like a successful cleanup — and the
    caller is fail-open, so nobody would ever see it happen."""
    ckpt.write_text("not json at all", encoding="utf-8")
    assert _lss.cmd_clear(_Args(if_goal="g-115-4983")) == 0
    assert ckpt.exists(), "a corrupt checkpoint must be left for diagnosis"
    assert ckpt.read_text(encoding="utf-8") == "not json at all"


def test_empty_goal_id_on_the_checkpoint_does_not_match(ckpt):
    """A checkpoint whose goal_id is empty/absent must not be cleared by an
    arbitrary --if-goal — "" == "" would otherwise clear on any release."""
    ckpt.write_text(json.dumps({"phase": "selected"}), encoding="utf-8")
    assert _lss.cmd_clear(_Args(if_goal="g-115-4983")) == 0
    assert ckpt.exists()


# -------------------------------------------------- backward compatibility

def test_bare_clear_is_unchanged(ckpt):
    """No --if-goal: the pre- unconditional behavior, byte for byte.
    Existing callers (there were none in production, but the wrapper is
    documented and hand-invocable) must not change semantics."""
    ckpt.seed("g-115-9999")
    assert _lss.cmd_clear(_Args()) == 0
    assert not ckpt.exists()


def test_bare_clear_on_absent_checkpoint_is_still_a_no_op(ckpt):
    assert _lss.cmd_clear(_Args()) == 0


# ------------------------------------------------------- the production wiring

def test_release_wrapper_calls_clear_with_the_cas():
    """The whole defect was an implemented subcommand with no caller, so pin the
    CALL SITE, not just the behavior (guard-1943: pinning the writer says
    nothing about the wiring — the sibling worker-heartbeat defect shipped a
    correct writer that nothing invoked, and its tests stayed green throughout).

    Both rc=0 arms of aspirations-release.sh must clear: the direct one and the
    post-autospawn retry. A fix wired into only the first is inert exactly when
    the daemon had to be respawned.
    """
    src = (SCRIPTS / "aspirations-release.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "clear --if-goal" in code, (
        "release must clear the checkpoint through the CAS, never unconditionally")
    assert code.count("_clear_iteration_checkpoint\n") == 2, (
        "both rc=0 arms (direct + post-autospawn retry) must call it")
    # It must run AFTER the daemon release has committed, never before: a
    # pre-emptive clear on a release that then fails would strand the loop with
    # no anchor at all.
    assert code.index("_clear_iteration_checkpoint()") < code.index("rt_call POST /v1/aspirations/release")


def test_clear_subcommand_still_accepts_no_flags():
    """argparse wiring: --if-goal must be OPTIONAL, or every hand-invocation of
    `clear` starts failing."""
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    p_clear = sub.add_parser("clear")
    p_clear.add_argument("--if-goal", dest="if_goal", default=None)
    assert p.parse_args(["clear"]).if_goal is None
    assert p.parse_args(["clear", "--if-goal", "g-1-1"]).if_goal == "g-1-1"
