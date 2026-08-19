"""Regression pins for the --triage SCOPE declaration ().

`triage()` globs `chunk-*.log` and nothing else, so its verdict is scoped to the
chunked pytest half. That verdict is HONEST about the population it read, which
is what makes it dangerous: nothing in its output named what it declined to look
at, so "0 genuine" read as a clean SUITE. Measured 2026-08-02 (g-115-4447, echo,
cc-03, 16 chunks): triage printed `2 environmental | 0 genuine` while two shell
files in the pytest-invisible half were red, and red SOLO -- genuine.

WHAT THESE PIN, and why each is separate:

  1. The declaration fires on the CLEAN path. This is the load-bearing one. A
     failing run already prompts the reader to look further; a clean one is
     exactly where an unstated scope becomes "the suite is green". A test that
     only checked the failing path would pass against a version that stays
     silent when it matters most.
  2. A recorded FAILING half is named WITH its summary and flips the exit code.
     Naming without the rc change would leave `--triage` returning 0 on a run
     with a red half -- the machine-readable half of the same false all-clear.
  3. An ABSENT record reads NOT RECORDED, never PASS. This is the fail-safe
     direction: every log dir written before this change, and every direct
     `run-full-suite.py` invocation, has no record, and inventing a pass for
     them would rebuild the defect one layer down.
  4. `ran: false` is distinct from `rc: 0`. The deferred testpath is announced
     on every run and executed on almost none, so "did not run" and "passed"
     must not render alike.

The shell-side pins are TEXT assertions against the shipped wrapper, matching
the precedent of `test_wrapper_short_circuits_triage_before_other_suites` in the
sibling file: the recording happens inside a script whose first action is to run
the whole suite, so it cannot be sourced for a behavioural test. They read the
SHIPPED file and never re-declare its contents (guard-920). The live wiring is
proven by an actual full-suite run, not by these.
"""

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TARGET = SCRIPT_DIR.parent / "run-full-suite.py"
WRAPPER = SCRIPT_DIR.parent / "run-full-suite.sh"


def _load():
    spec = importlib.util.spec_from_file_location("run_full_suite_scope", TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_full_suite_scope"] = mod
    spec.loader.exec_module(mod)
    return mod


RFS = _load()

CLEAN_CHUNK = "..........  [100%]\n10 passed in 1.23s\n"


def _clean_log_dir(tmp_path):
    (tmp_path / "chunk-00.log").write_text(CLEAN_CHUNK, encoding="utf-8")
    return tmp_path


def _write_halves(tmp_path, rows):
    (tmp_path / RFS.HALVES_RECORD).write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


# ── 1. the declaration fires on the CLEAN path ──────────────────────────────

def test_clean_triage_still_names_the_halves_it_did_not_read(tmp_path, capsys):
    """The whole point: a clean verdict must not read as a clean suite."""
    rc = RFS.triage(_clean_log_dir(tmp_path), SCRIPT_DIR.parent.parent, {})
    out = capsys.readouterr().out
    assert rc == 0
    assert "SCOPE" in out
    assert "chunked pytest half ONLY" in out
    for key, _label in RFS.OTHER_HALVES:
        assert key in out, "clean path must still name the %r half" % key


def test_every_declared_half_is_named_even_with_no_record(tmp_path, capsys):
    RFS.triage(_clean_log_dir(tmp_path), SCRIPT_DIR.parent.parent, {})
    out = capsys.readouterr().out
    assert out.count("NOT RECORDED") == len(RFS.OTHER_HALVES)


# ── 2. a recorded FAILING half is named, summarised, and changes the rc ─────

def test_failing_invisible_half_is_named_with_its_summary(tmp_path, capsys):
    """The  shape: chunked half clean, invisible half red solo."""
    _write_halves(tmp_path, [
        {"half": "invisible", "rc": 1, "ran": True,
         "summary": "95/102 files passed, 0 quarantined"},
        {"half": "domain", "rc": 0, "ran": True, "summary": "242 passed"},
    ])
    rc = RFS.triage(_clean_log_dir(tmp_path), SCRIPT_DIR.parent.parent, {})
    out = capsys.readouterr().out
    assert "FAIL(rc=1)" in out
    assert "95/102 files passed" in out, "the summary itself must be reported"
    assert "does NOT cover them" in out
    assert rc == 1, "a red half must not leave --triage returning 0"


def test_failing_half_is_restated_where_the_conclusion_is_drawn(
        tmp_path, monkeypatch, capsys):
    """'Nothing to file' is the exact sentence that gets misread.

    The candidates path ends in a reassurance. A scope note that appeared only
    in the header would be forty lines above it by then, so the exclusion is
    restated at the conclusion. This pins that, not merely the header.
    """
    (tmp_path / "chunk-00.log").write_text(
        "FAILED core/scripts/tests/test_thing.py::test_x - boom\n"
        "1 failed, 9 passed in 1.0s\n", encoding="utf-8")
    _write_halves(tmp_path, [
        {"half": "domain", "rc": 2, "ran": True, "summary": "domain suite red"},
    ])
    monkeypatch.setattr(RFS, "_solo", lambda *a, **k: (9, 0, None))
    rc = RFS.triage(tmp_path, SCRIPT_DIR.parent.parent, {})
    out = capsys.readouterr().out
    tail = out.split("TRIAGE RESULT")[-1]
    assert "Nothing to file" in tail
    assert "do NOT read the above as a clean suite" in tail
    assert "domain" in tail
    assert rc == 1


def test_all_halves_passing_keeps_the_clean_exit(tmp_path, capsys):
    """A positive control: the rc flip must be caused by the FAILURE.

    Without this, a test asserting rc==1 on a red half is equally consistent
    with a version that returns 1 whenever any record exists.
    """
    _write_halves(tmp_path, [
        {"half": "invisible", "rc": 0, "ran": True, "summary": "102/102 passed"},
        {"half": "deferred", "rc": 0, "ran": False, "summary": "NOT RUN"},
        {"half": "domain", "rc": 0, "ran": True, "summary": "242 passed"},
    ])
    rc = RFS.triage(_clean_log_dir(tmp_path), SCRIPT_DIR.parent.parent, {})
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
    assert "FAILED" not in out.split("SCOPE")[1].split("\n\n")[0]


# ── 3. absent record is NOT RECORDED, never PASS ────────────────────────────

def test_absent_record_never_renders_as_a_pass(tmp_path, capsys):
    RFS.triage(_clean_log_dir(tmp_path), SCRIPT_DIR.parent.parent, {})
    scope = capsys.readouterr().out.split("SCOPE")[1]
    assert "PASS" not in scope, "an unread half must never render as passed"


def test_unreadable_record_lines_are_skipped_not_guessed(tmp_path, capsys):
    """A corrupt line must not become a phantom half or crash the triage."""
    (tmp_path / RFS.HALVES_RECORD).write_text(
        "not json at all\n"
        + json.dumps({"half": "domain", "rc": 0, "ran": True, "summary": "ok"})
        + "\n", encoding="utf-8")
    rc = RFS.triage(_clean_log_dir(tmp_path), SCRIPT_DIR.parent.parent, {})
    out = capsys.readouterr().out
    assert rc == 0
    assert "domain     PASS" in out
    assert "invisible  NOT RECORDED" in out


# ── 4. ran:false is distinct from rc:0 ──────────────────────────────────────

def test_did_not_run_is_not_rendered_as_a_pass(tmp_path, capsys):
    _write_halves(tmp_path, [
        {"half": "deferred", "rc": 0, "ran": False,
         "summary": "NOT RUN: mind_api/tests (RUN_DEFERRED=1 to include)"},
    ])
    RFS.triage(_clean_log_dir(tmp_path), SCRIPT_DIR.parent.parent, {})
    out = capsys.readouterr().out
    assert "DID NOT RUN" in out
    line = [ln for ln in out.splitlines() if ln.strip().startswith("deferred")][0]
    assert "PASS" not in line


def test_stale_record_is_cleared_by_a_new_run(tmp_path, monkeypatch):
    """A halves record from an EARLIER run must not describe this one.

    Same reasoning as the chunk-log clear it sits beside (g-115-4321): a stale
    PASS is worse than no record, because NOT RECORDED is loud and a stale PASS
    reads as coverage. Stubbed the same way as
    test_run_clears_stale_chunk_logs_from_a_prior_run in the sibling file --
    the clear lives on the RUN path, so the run path is what has to execute.
    """
    fake_tests = tmp_path / "tests"
    fake_tests.mkdir()
    (fake_tests / "test_x.py").write_text("def test_x():\n    pass\n",
                                          encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    _write_halves(out, [{"half": "domain", "rc": 0, "ran": True,
                         "summary": "stale record from a PRIOR run"}])
    monkeypatch.setattr(RFS, "_testpaths", lambda: [fake_tests])
    # PROJECT_ROOT too: the run path prints each testpath relative to it, which
    # raises for a tmp dir outside the repo. Patching it keeps the fixture out
    # of the working tree instead of writing a fake tests dir into it.
    monkeypatch.setattr(RFS, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(RFS.subprocess, "run", lambda *a, **k: None)

    RFS.main(["--out", str(out), "--chunks", "1"])

    assert not (out / RFS.HALVES_RECORD).exists(), \
        "a halves record from a prior run survived into this one"


# ── the shell side: one resolver, and every half recorded ───────────────────

def test_print_out_dir_flag_exists_for_the_wrapper():
    """The wrapper must not re-derive the default log dir in bash.

    Two derivations of `agents/<agent>/temp/suite-run` would be two sources of
    truth, and the shell's copy would silently write the halves record into a
    dir that --triage does not read.
    """
    src = TARGET.read_text(encoding="utf-8")
    assert "--print-out-dir" in src
    wrapper = WRAPPER.read_text(encoding="utf-8")
    assert "--print-out-dir" in wrapper
    # EXECUTABLE lines only. The first draft of this assertion scanned the whole
    # file and reddened on the COMMENT that explains why the path is not
    # re-derived here -- a use-vs-mention failure that would have pushed the
    # author to delete the explanation in order to pass the test.
    code = [ln for ln in wrapper.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    assert not any("temp/suite-run" in ln for ln in code), \
        "the wrapper must ASK for the log dir, never re-derive it"


def test_wrapper_records_every_declared_half():
    """Each half named in OTHER_HALVES must actually be recorded by the shell.

    This is the join that makes the declaration honest: a half declared in the
    python and never recorded by the shell would print NOT RECORDED forever,
    which is safe but useless.
    """
    wrapper = WRAPPER.read_text(encoding="utf-8")
    for key, _label in RFS.OTHER_HALVES:
        assert "_record_half %s" % key in wrapper, \
            "%r is declared in OTHER_HALVES but never recorded by the wrapper" % key


def test_wrapper_records_both_branches_of_each_half():
    """Absent/not-run must be recorded too, not just the executed branch.

    Recording only the ran branch leaves the commonest real states (no domain
    runner, deferred not run) indistinguishable from a lost record.
    """
    wrapper = WRAPPER.read_text(encoding="utf-8")
    for key, _label in RFS.OTHER_HALVES:
        calls = [ln for ln in wrapper.splitlines()
                 if ("_record_half %s" % key) in ln]
        assert any(" true " in ln for ln in calls), "%s: no ran=true record" % key
        assert any(" false " in ln for ln in calls), "%s: no ran=false record" % key


def test_wrapper_reads_the_runner_rc_not_the_pipe():
    """tee makes the half's rc the FIRST element, never `$?` (guard-1150).

    A trailing pipe reports the pipe's success as the command's, which here
    would record a red half as a pass -- reintroducing the false all-clear
    through the very mechanism added to prevent it.
    """
    wrapper = WRAPPER.read_text(encoding="utf-8")
    for var in ("INVISIBLE_RC", "DOMAIN_RC"):
        assert "%s=${PIPESTATUS[0]}" % var in wrapper, \
            "%s must come from PIPESTATUS[0], not $?" % var


# ── 5. the default log dir stays OFF the fleet-synced tree ──────────────────


def test_default_log_dir_is_not_under_the_agents_tree(monkeypatch, capsys):
    """The default must never resolve inside agents/ ().

    Under own-cloud, agents/<agent>/temp/ is a FLEET-SYNCED surface
    (guard-3422), and the sync REPLACES a file at a new inode while a writer
    still holds an fd on the old one -- so the writer keeps appending to an
    orphaned inode. Chunk logs are exactly that shape: the log is opened and
    the fd held across a multi-minute subprocess.run(stdout=fh).

    That is invisible to every check a reader reaches for. The truncated log
    has a clean prefix, ZERO NUL bytes, and the producer exits 0, so it is
    byte-indistinguishable from a short run -- which is why it read as
    "contended" for three false INVALID verdicts before the cause was found.

    This pins the DIRECTION, not the exact path: a future move to some other
    non-synced location should keep this green. It asserts absoluteness too,
    because `--print-out-dir` feeds the bash wrapper (guard-552).
    """
    monkeypatch.setenv("MIND_AGENT", "alpha")
    assert RFS.main(["--print-out-dir"]) == 0
    out = Path(capsys.readouterr().out.strip())

    assert out.is_absolute(), "resolver must return an absolute path, got %s" % out
    agents_root = RFS.PROJECT_ROOT / "agents"
    assert not out.is_relative_to(agents_root), (
        "default log dir %s is inside the fleet-synced agents/ tree -- a "
        "long-running redirect there loses content silently (g-115-6409)" % out)


def test_out_flag_still_overrides_the_default(monkeypatch, capsys, tmp_path):
    """--out is the escape hatch; moving the default must not disable it.

    Kept beside the pin above because the two fail in opposite directions: a
    hardcoded default would break this one, and a default that silently
    ignored --out would leave anyone wanting durable logs with no route.
    """
    monkeypatch.setenv("MIND_AGENT", "alpha")
    target = tmp_path / "explicit-logs"
    assert RFS.main(["--print-out-dir", "--out", str(target)]) == 0
    assert Path(capsys.readouterr().out.strip()) == target
