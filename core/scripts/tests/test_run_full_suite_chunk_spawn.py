#!/usr/bin/env python3
"""A chunk's argv cannot reach the platform limit, and a chunk that cannot spawn
refuses to certify the run (g-115-8769).

MEASURED 2026-09-03 on DESKTOP-O91DLK2 (Windows, native CPython 3.12): the
runner passed every test path as its own argv item, so a chunk's command line
cost (mean path length x files per chunk) -- guard-5635's product. At the
DEFAULT --chunks 4, this repo's 1,358 files at an 83.7-char mean built a
29,824-char argv IN THE MAIN REPO, 9% under the 32,767-char CreateProcess
ceiling and rising with every test file added; from a worktree (+26 chars per
path) the same chunk measured 38,664 and died `FileNotFoundError: [WinError
206]` before collection.

TWO defects are pinned here and the SECOND is the dangerous one:

  1. Argv length must not scale with the chunk's file count -- WHERE THE
     INSTALLED pytest SUPPORTS AN @argfile. The file list travels in one when
     it does, so the ceiling stops being reachable instead of being sized
     around; the chunk COUNT is left alone either way, since the ladder is a
     retry protocol whose per-chunk diagnostics are read by index (guard-1448).
     SUPPORT IS NOT UNIVERSAL. pytest 7.4.4 does not set fromfile_prefix_chars,
     and there `@<path>` is taken as a literal test path: pytest reports `file
     or directory not found`, collects ZERO, and every chunk log lands with no
     summary line -- which _parse_counts reads as (0,0,0) and classify() calls
     INVALID (contended), sending the reader up a chunk ladder that can never
     fix it. Measured on cc-07 2026-09-04, hours after this file shipped: all 4
     chunks, 1,365 files, 0 tests run, on a tree that had run 15,513 tests the
     same morning. So the runner PROBES support once per run and falls back to
     argv when it is absent (a POSIX box has a ~2MB ARG_MAX and never needed
     the indirection). The Windows ceiling this file exists for is real and
     unchanged; only the assumption of universal support is gone.

  2. A chunk that never spawned must not read as a chunk that ran. The OSError
     used to escape main(); `sys.exit(main())` rendered it rc=1; and
     run-full-suite.sh -- which DOES separate did-not-run from ran-and-failed --
     faithfully reported "rc=1 genuine failures" over a half in which zero tests
     executed. This is guard-1760 exactly: the runner reported what it ran and
     never what it declined to look for. Defect 2 is pinned INDEPENDENTLY of
     defect 1 on purpose: the next argv-shaped surprise will carry a different
     errno, and refusing to certify must not depend on knowing the cause.

Run: STORAGE_BACKEND=local py -3 -m pytest core/scripts/tests/test_run_full_suite_chunk_spawn.py -q
"""

import importlib.util
import os
import subprocess
import types
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TARGET = SCRIPT_DIR.parent / "run-full-suite.py"

# The hard ceiling this whole change exists to stay under (Windows
# CreateProcess). Named once so the assertions below read against the real
# constraint rather than an arbitrary number.
WINDOWS_CMDLINE_CEILING = 32767


def _load():
    spec = importlib.util.spec_from_file_location("run_full_suite_chunk_spawn", TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_full_suite_chunk_spawn"] = mod
    spec.loader.exec_module(mod)
    return mod


RFS = _load()


class _WinError206(OSError):
    """An OSError carrying winerror=206, constructible on any platform.

    A real FileNotFoundError's `winerror` is a read-only C attribute and does
    not exist at all off Windows, so the branch that explains WinError 206
    would otherwise be untestable on the boxes that run this suite.
    """

    winerror = 206


def _fake_tree(tmp_path, n_files, name_len=60):
    """A project root whose pytest.ini declares one tree of `n_files` tests.

    `name_len` pads the file names so a test can drive the argv cost without
    needing a deep directory, which is the same product (path length x count).
    """
    tests = tmp_path / "core" / "scripts" / "tests"
    tests.mkdir(parents=True)
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\ntestpaths = core/scripts/tests\n", encoding="utf-8")
    for i in range(n_files):
        stem = ("test_%04d_" % i) + ("x" * max(0, name_len - 11))
        (tests / (stem + ".py")).write_text("def test_a():\n    pass\n", encoding="utf-8")
    return tmp_path


def _run_main(monkeypatch, tmp_path, *, n_files=8, chunks=1, raise_on_pytest=None,
              argfile_supported=None, pytest_rc=None, name_len=60):
    """Drive main() with subprocess stubbed. Returns (rc, spawned_cmds, out_dir).

    `pytest_rc` makes the stub return a result carrying that returncode, for the
    cases where a chunk SPAWNS and then refuses. Left None the stub keeps
    returning None, which is the shape the older tests were written against and
    which run-full-suite.py deliberately tolerates.

    `argfile_supported` forces the @argfile capability probe, so the fallback
    branch is exercised on a box whose own pytest supports argfiles (otherwise
    the branch would only ever run where the suite is least likely to be run).

    `name_len` pads the generated file names so a test can drive argv cost
    without needing a deep tree -- the same (path length x count) product.
    """
    root = _fake_tree(tmp_path, n_files, name_len=name_len)
    out = tmp_path / "out"
    out.mkdir()
    seen = []

    def _fake_run(cmd, *a, **k):
        cmd = list(cmd)
        seen.append(cmd)
        if raise_on_pytest is not None and "pytest" in cmd:
            raise raise_on_pytest
        if pytest_rc is not None and "pytest" in cmd:
            # A CompletedProcess-SHAPED result, for the tests about the chunk
            # loop's exit-code check (). The default None below
            # deliberately stays: six harnesses in this repo stub
            # subprocess.run that way, and the loop must NARRATE on them
            # rather than die -- which is its own test at the bottom.
            return types.SimpleNamespace(returncode=pytest_rc, stdout="", stderr="")
        # Stubbed like the sibling fleet_layout tests: main() tolerates a None
        # return (see run-full-suite.py's _git_head, which documents it).
        return None

    # Force the @argfile branch when a test is about one specific side of it.
    # Left None, the real probe runs against the stub above, returns False (the
    # stub yields None, so there is no returncode to read) and the argv branch
    # is exercised -- which is also what a pytest-7.4.4 box does for real.
    if argfile_supported is not None:
        monkeypatch.setattr(RFS, "_pytest_expands_argfile",
                            lambda *a, **k: argfile_supported)
    monkeypatch.setattr(RFS, "PROJECT_ROOT", root)
    monkeypatch.setattr(RFS, "_populated_agents", lambda *a, **k: (["alpha", "bravo"], None))
    monkeypatch.setattr(RFS.subprocess, "run", _fake_run)

    rc = RFS.main(["--out", str(out), "--chunks", str(chunks)])
    # EXCLUDE THE CAPABILITY PROBE. It is a pytest argv that precedes the chunk
    # loop, so a bare "pytest" in cmd filter counts it as a chunk spawn -- which
    # silently breaks every caller that asserts a SPAWN COUNT (the chunk-count
    # and abort-on-first-failure tests below). Keyed on the probe's own argfile
    # name, not on position, so another pre-loop call cannot re-break it.
    chunk_cmds = [c for c in seen
                  if "pytest" in c
                  and not any("argfile-probe" in str(tok) for tok in c)]
    return rc, chunk_cmds, out


# ── defect 1: argv length is decoupled from the chunk's file count ───────────

def test_file_list_travels_in_an_argfile_when_pytest_supports_one(monkeypatch, tmp_path):
    """THE REGRESSION: where @argfile works, no test path may ride on argv."""
    _rc, cmds, out = _run_main(monkeypatch, tmp_path, n_files=12, chunks=1,
                               argfile_supported=True)

    assert cmds, "main() never invoked pytest"
    cmd = cmds[0]
    assert not [tok for tok in cmd if tok.endswith(".py")], \
        "test paths must not ride on argv: %r" % (cmd,)

    at_args = [tok for tok in cmd if tok.startswith("@")]
    assert len(at_args) == 1, "expected exactly one @argfile, got %r" % (at_args,)

    argfile = Path(at_args[0][1:])
    assert argfile.parent == out, "the argfile belongs in the run's own out dir"
    listed = argfile.read_text(encoding="utf-8").split()
    assert len(listed) == 12, "every file in the chunk must reach the argfile"
    assert all(p.endswith(".py") for p in listed)


def test_file_list_falls_back_to_argv_when_pytest_has_no_argfile(monkeypatch, tmp_path):
    """The pytest-7.4.4 path, and the one this box actually takes.

    An unexpanded `@<path>` is not a degraded run, it is a SILENT EMPTY one:
    pytest treats it as a test path, collects nothing, and the chunk log
    carries no summary for _parse_counts to read. So the requirement is not
    merely that argv works -- it is that the `@` token never reaches a pytest
    that cannot expand it.
    """
    _rc, cmds, out = _run_main(monkeypatch, tmp_path, n_files=12, chunks=1,
                               argfile_supported=False)

    assert cmds, "main() never invoked pytest"
    cmd = cmds[0]
    assert not [tok for tok in cmd if str(tok).startswith("@")], \
        "an @argfile must never reach a pytest that cannot expand it: %r" % (cmd,)

    on_argv = [tok for tok in cmd if str(tok).endswith(".py")]
    assert len(on_argv) == 12, \
        "every file in the chunk must reach argv when the argfile is unusable: %r" % (cmd,)

    # The .args artifact is still written either way: this module's docstring
    # promises it and --triage reads chunk file lists back out of it.
    assert (out / "chunk-00.args").exists(), \
        "the chunk .args artifact must be written even on the argv path"


def test_argv_stays_far_under_the_ceiling_even_for_a_huge_chunk(monkeypatch, tmp_path):
    """400 long-named files in ONE chunk -- the shape that measured 38,664.

    SCOPED TO THE @argfile BRANCH, which is the only one that can hold this
    property: the fallback puts the paths back on argv precisely because that
    is the mechanism that works when the indirection does not. See the
    counterpart below for what is asserted on the other branch.
    """
    _rc, cmds, _out = _run_main(monkeypatch, tmp_path, n_files=400, chunks=1,
                                argfile_supported=True)

    cost = sum(len(tok) + 3 for tok in cmds[0])
    assert cost < 2000, \
        "argv cost %d should be a small constant, not a function of file count" % cost
    assert cost < WINDOWS_CMDLINE_CEILING // 10


def test_argv_cost_does_not_grow_with_the_chunk(monkeypatch, tmp_path):
    """The invariant, stated directly: 20 files and 300 files cost the same.

    A byte-budget chunker would also keep every chunk under the ceiling, so
    asserting only "under 32767" would pass for either design. This asserts the
    property that distinguishes them -- argv is CONSTANT -- which is what lets
    the chunk count stay the user's setting.

    Scoped to the @argfile branch for the reason given above.
    """
    _rc, small, _o = _run_main(monkeypatch, tmp_path / "a", n_files=20, chunks=1,
                               argfile_supported=True)
    _rc, large, _o = _run_main(monkeypatch, tmp_path / "b", n_files=300, chunks=1,
                               argfile_supported=True)

    small_cost = sum(len(t) for t in small[0])
    large_cost = sum(len(t) for t in large[0])
    # The two differ only by the tmp path and the chunk index, never by 280 files.
    assert abs(large_cost - small_cost) < 200, \
        "argv grew %d chars for 280 more files" % (large_cost - small_cost)


def test_without_an_argfile_argv_does_grow_and_chunks_is_the_lever(monkeypatch, tmp_path):
    """The counterpart, stated so the limitation is recorded rather than implied.

    The fallback does NOT inherit the constant-argv property, and pretending
    otherwise would be the more dangerous error: on a Windows box whose pytest
    lacks fromfile_prefix_chars, argv is once again a function of the chunk's
    file count and guard-5635's ceiling is once again reachable. That is not a
    regression this change introduces -- it is the state that preceded the
    @argfile commit, now reached only where the indirection cannot work -- but
    it IS the reason the spawn-failure path must send that reader to --chunks
    instead of telling them argv is already constant.
    """
    _rc, small, _o = _run_main(monkeypatch, tmp_path / "a", n_files=20, chunks=1,
                               argfile_supported=False)
    _rc, large, _o = _run_main(monkeypatch, tmp_path / "b", n_files=300, chunks=1,
                               argfile_supported=False)

    small_cost = sum(len(t) for t in small[0])
    large_cost = sum(len(t) for t in large[0])
    assert large_cost > small_cost * 5, (
        "on the argv fallback the cost MUST scale with the file count "
        "(%d -> %d); if it does not, the fallback is not carrying the paths "
        "and the chunk is collecting nothing" % (small_cost, large_cost))


def test_requested_chunk_count_is_honoured_exactly(monkeypatch, tmp_path):
    """The ladder is a retry protocol: --chunks 5 must spawn 5, never 6.

    Sizing chunks to a byte budget would silently re-split, changing the index
    the per-chunk diagnostics are read by (guard-1448 chunk-confinement).
    """
    _rc, cmds, _out = _run_main(monkeypatch, tmp_path, n_files=200, chunks=5)

    assert len(cmds) == 5, "expected exactly 5 chunk spawns, got %d" % len(cmds)


def test_the_argfile_probe_agrees_with_this_pytest(tmp_path):
    """External-contract pin, rewritten to survive being right ().

    THIS TEST USED TO ASSERT THAT pytest HONOURS AN @argfile, on the stated
    premise that "the whole fix rests on pytest's `@` prefix". That premise no
    longer holds -- the runner probes and falls back -- and the old assertion
    had a failure mode worth recording, because it is the one that let a
    suite-disabling change propagate:

      It is PLATFORM-DEPENDENT. Green on the Windows box the @argfile change
      was authored on; RED on Linux/pytest 7.4.4 at the same commit. Measured
      on cc-07 2026-09-04 -- one failure at HEAD, this one, naming its cause in
      a single line exactly as designed. The pin was correct and it FIRED; what
      no one did was run it on a box of the other kind before the change
      shipped there. A red that is only reachable on hardware nobody runs it on
      is not a gate.

    So the assertion is now the one that holds EVERYWHERE and is what the
    runner's correctness actually depends on: the probe's answer must match
    what this pytest really does. It deliberately does NOT assert which branch
    this box takes -- doing so would just re-create the platform-dependence.
    """
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"          # guard-955, as for any runner
    claimed = RFS._pytest_expands_argfile(tmp_path, env)

    t = tmp_path / "test_argfile_probe.py"
    t.write_text("def test_probe():\n    pass\n", encoding="utf-8")
    argfile = tmp_path / "chunk-00.args"
    argfile.write_text(str(t) + "\n", encoding="utf-8")

    r = subprocess.run(
        [sys.executable, "-m", "pytest", "@" + str(argfile), "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(tmp_path))
    actual = (r.returncode == 0 and "test_argfile_probe.py" in (r.stdout or ""))

    assert claimed == actual, (
        "the probe said support=%r but pytest actually %s expand the argfile "
        "(rc=%s). A wrong probe is worse than no probe: it picks the branch "
        "that collects nothing. stdout=%.200r stderr=%.200r"
        % (claimed, "DOES" if actual else "DOES NOT", r.returncode,
           r.stdout, r.stderr))


# ── defect 2: a chunk that cannot spawn refuses to certify ──────────────────

def test_spawn_failure_exits_invalid_not_genuine_failures(monkeypatch, tmp_path):
    """rc=2 ("this number means NOTHING"), never rc=1 ("genuine failures").

    The wrapper reads this code to decide whether the reader is looking at a
    regression to triage or a setup fault to fix; rc=1 sends them hunting a bug
    that never executed.
    """
    rc, _cmds, _out = _run_main(
        monkeypatch, tmp_path, n_files=8, chunks=2,
        raise_on_pytest=_WinError206(206, "The filename or extension is too long"))

    assert rc == 2, "a chunk that could not spawn must exit 2, got %r" % rc


def test_spawn_failure_stops_the_run_instead_of_continuing(monkeypatch, tmp_path):
    """FATAL, not skip-and-continue -- outcome 1 of the goal, stated literally."""
    _rc, cmds, _out = _run_main(
        monkeypatch, tmp_path, n_files=40, chunks=4,
        raise_on_pytest=OSError(2, "boom"))

    assert len(cmds) == 1, \
        "the run must abort on the first failed spawn, not attempt %d" % len(cmds)


def test_spawn_failure_names_the_files_that_never_ran(monkeypatch, tmp_path, capsys):
    """The verdict logic must COUNT the skipped half, not merely mention it."""
    _rc, _cmds, _out = _run_main(
        monkeypatch, tmp_path, n_files=40, chunks=4,
        raise_on_pytest=OSError(2, "boom"))

    text = capsys.readouterr().out
    assert "VERDICT: INVALID" in text
    assert "could not spawn" in text
    assert "40 of 40 test files never ran" in text, \
        "the skipped count must be explicit, not inferable: %s" % text
    # The partial counts must be labelled as a non-result, or a reader files
    # goals from them.
    assert "NOT a result" in text


def test_winerror_206_is_explained_as_a_command_line_length(monkeypatch, tmp_path, capsys):
    """206 has a specific cause; a bare errno sends the reader to --chunks."""
    _rc, _cmds, _out = _run_main(
        monkeypatch, tmp_path, n_files=8, chunks=1,
        raise_on_pytest=_WinError206(206, "The filename or extension is too long"))

    text = capsys.readouterr().out
    assert "32,767" in text, "the ceiling must be named: %s" % text
    assert "argfile" in text


def test_a_non_206_spawn_failure_gets_no_command_line_explanation(monkeypatch, tmp_path, capsys):
    """Do not misattribute an unrelated errno to argv length."""
    _rc, _cmds, _out = _run_main(
        monkeypatch, tmp_path, n_files=8, chunks=1,
        raise_on_pytest=OSError(13, "Permission denied"))

    text = capsys.readouterr().out
    assert "VERDICT: INVALID" in text
    assert "32,767" not in text, \
        "a permission error must not be explained as a length problem: %s" % text


# ── defect 3: a chunk that SPAWNED but ran nothing refuses to certify ────────
# (.) Defects 1 and 2 above both assume the chunk either ran or never
# started. The case that actually shipped is neither: pytest started, rejected
# its own arguments, and exited rc=4 having collected nothing. The rc was
# DISCARDED, so the usage error reached _parse_counts as an ordinary log, parsed
# to (0, 0, 0), and the run reported `VERDICT: INVALID (contended)` -- sending
# the operator up a chunk ladder that can never clear a usage error.


def test_a_chunk_that_exits_rc4_refuses_to_certify(monkeypatch, tmp_path):
    """THE REGRESSION: rc=4 must exit 2, not be parsed as a chunk of zero tests."""
    rc, _cmds, _out = _run_main(monkeypatch, tmp_path, n_files=8, chunks=2,
                                pytest_rc=4)

    assert rc == 2, "a chunk that ran nothing must exit 2, got %r" % rc


def test_rc4_verdict_names_the_real_cause_not_contention(monkeypatch, tmp_path, capsys):
    """The whole cost of this defect was the verdict blaming the wrong thing."""
    _rc, _cmds, _out = _run_main(monkeypatch, tmp_path, n_files=8, chunks=2,
                                 pytest_rc=4)

    text = capsys.readouterr().out
    assert "VERDICT: INVALID" in text
    assert "rc=4" in text, "the returncode must be named: %s" % text
    assert "contended" not in text, \
        "a usage error must never be reported as contention: %s" % text
    assert "do NOT climb the chunk ladder" in text, \
        "the verdict must stop the reader retrying something that cannot work"


def test_rc4_run_stops_at_the_first_refusing_chunk(monkeypatch, tmp_path):
    """FATAL, not skip-and-continue -- same contract as the spawn-failure branch."""
    _rc, cmds, _out = _run_main(monkeypatch, tmp_path, n_files=40, chunks=4,
                                pytest_rc=4)

    assert len(cmds) == 1, \
        "the run must abort on the first refusing chunk, not attempt %d" % len(cmds)


def test_rc1_is_a_normal_test_failure_and_does_not_abort(monkeypatch, tmp_path):
    """The guard must not fire on the ordinary case it sits next to.

    pytest rc=1 means "tests ran and some failed" -- the single most common
    outcome of a real suite. A guard that aborted on it would convert every red
    run into an INVALID one and destroy the signal it exists to protect.
    """
    _rc, cmds, _out = _run_main(monkeypatch, tmp_path, n_files=40, chunks=4,
                                pytest_rc=1)

    assert len(cmds) == 4, \
        "rc=1 must not abort the run; expected 4 chunks, got %d" % len(cmds)


def test_rc5_no_tests_collected_does_not_abort(monkeypatch, tmp_path):
    """rc=5 is legitimate when a marker deselects a whole chunk."""
    _rc, cmds, _out = _run_main(monkeypatch, tmp_path, n_files=40, chunks=4,
                                pytest_rc=5)

    assert len(cmds) == 4, \
        "rc=5 must not abort the run; expected 4 chunks, got %d" % len(cmds)


def test_an_unreadable_returncode_does_not_abort(monkeypatch, tmp_path):
    """The stub returns None; guard the RESULT as _git_head documents.

    Aborting a 30-minute run over a missing attribute that only a test stub can
    produce would be a cure worse than the disease.
    """
    _rc, cmds, _out = _run_main(monkeypatch, tmp_path, n_files=40, chunks=4)

    assert len(cmds) == 4, "a None result must not abort the run"


# ── defect 3b: the fallback for a pytest that cannot read @argfiles ──────────


def test_fallback_passes_repo_relative_paths_when_argfiles_unsupported(
        monkeypatch, tmp_path):
    """Paths ride on argv, and RELATIVE -- the property that keeps it bounded.

    Relative is not cosmetic here. guard-5635's worktree failure was entirely
    the PREFIX (+26 chars per path, 29,824 -> 38,664); relative paths make argv
    invariant to where the repo lives, which is what lets this fallback be safe
    on the very boxes that cannot use the argfile.
    """
    _rc, cmds, _out = _run_main(monkeypatch, tmp_path, n_files=12, chunks=1,
                                argfile_supported=False)

    assert cmds, "main() never invoked pytest"
    cmd = cmds[0]
    assert not [t for t in cmd if t.startswith("@")], \
        "the fallback must not pass an @argfile: %r" % (cmd,)
    paths = [t for t in cmd if t.endswith(".py")]
    assert len(paths) == 12, "every file in the chunk must reach argv: %r" % (cmd,)
    for p in paths:
        assert not os.path.isabs(p), "fallback path must be relative: %r" % p
        assert str(tmp_path) not in p, "the repo prefix must not appear: %r" % p


def test_fallback_still_writes_the_argfile_as_a_diagnostic(monkeypatch, tmp_path):
    """Nothing reads it programmatically, but it is how a reader re-runs a chunk."""
    _rc, _cmds, out = _run_main(monkeypatch, tmp_path, n_files=12, chunks=1,
                                argfile_supported=False)

    argfile = out / "chunk-00.args"
    assert argfile.exists(), "the per-chunk diagnostic must survive the fallback"
    assert len(argfile.read_text(encoding="utf-8").split()) == 12


def test_fallback_refuses_before_spawning_when_argv_would_not_fit(
        monkeypatch, tmp_path, capsys):
    """Refuse with a legible sentence rather than dying with WinError 206.

    Deliberately NOT a silent re-chunk: the ladder is a retry protocol whose
    per-chunk diagnostics are read by index (guard-1448), so the runner tells
    the caller to raise --chunks -- which is what guard-5635 already prescribes.
    """
    rc, cmds, _out = _run_main(monkeypatch, tmp_path, n_files=300, chunks=1,
                               argfile_supported=False, name_len=120)

    assert rc == 2, "an over-budget chunk must exit 2, got %r" % rc
    assert not cmds, "it must refuse BEFORE spawning, not after"
    text = capsys.readouterr().out
    assert "VERDICT: INVALID" in text
    assert "--chunks" in text, "the verdict must name its own remedy: %s" % text


# ── : the chunk loop's exit-code check ─────────────────────────────
#
# The loop DISCARDED subprocess.run's return value -- it was the one pytest call
# site in the module with no rc check at all, while `_solo` carried the correct
# one inline twelve hundred lines up. These pin the reuse AT THE LOOP, not in a
# helper nobody calls: a predicate wired into neither of its two doors presents
# as mechanical while being honour-system at both (guard-3448).
#
# Scope note, because it is easy to over-read these: the rc check does NOT catch
# the defect that motivated the goal. That one is a process dying mid-run with
# rc=0 and a dot tally that still parses non-zero, and it is caught by the
# completion-marker half in test_run_full_suite.py. guard-1501 says the same
# from the other side -- "rc=0 is not the tell; the ABSENT SUMMARY LINE is".
# These two lanes are complements; neither subsumes the other.


def test_a_chunk_that_exits_usage_error_does_not_certify_the_run(monkeypatch, tmp_path, capsys):
    """rc=4 is pytest saying "I never ran your tests" -- not "0 failures"."""
    rc, cmds, out = _run_main(monkeypatch, tmp_path, n_files=8, chunks=2, pytest_rc=4)
    text = capsys.readouterr().out
    assert rc == 2, text
    assert "NOT A MEASUREMENT" in text, text
    assert "pytest rc=4" in text, text
    assert "VERDICT: INVALID" in text, text


def test_rc_zero_with_no_parsed_tests_is_invalid(monkeypatch, tmp_path, capsys):
    """goal check 1, at the loop: rc=0 + zero accounted tests is NOT a pass.

    The stub writes nothing to the chunk log, so `_parse_counts` returns
    (0, 0, 0) while the process reports success -- byte-identical to a chunk
    whose pytest died before emitting anything.
    """
    rc, cmds, out = _run_main(monkeypatch, tmp_path, n_files=8, chunks=1, pytest_rc=0)
    text = capsys.readouterr().out
    assert rc == 2, text
    assert "NOT A MEASUREMENT" in text and "pytest rc=0" in text, text


def test_the_invalid_reason_warns_off_the_chunk_ladder(monkeypatch, tmp_path, capsys):
    """The actionable half. A setup fault and contention share a verdict here
    (both exit 2, both print INVALID) but NOT a remedy: climbing the ladder is
    the documented response to contention and is pure waste against rc=4. The
    NUL check already carries its remedy in the reason string; this follows that
    precedent rather than inventing a fourth verdict label."""
    _run_main(monkeypatch, tmp_path, n_files=8, chunks=1, pytest_rc=4)
    text = capsys.readouterr().out
    assert "do NOT climb the chunk ladder" in text, text
    assert "chunk-00.log" in text, text


def test_an_unreadable_return_code_is_narrated_not_skipped(monkeypatch, tmp_path, capsys):
    """A check that quietly declines to run reports success by default (guard-1760).

    The default stub returns None, so the rc half cannot run. That is a THIRD
    outcome -- not "fine" -- and the run must say so. It must NOT flip the
    verdict: the completion-marker half still judges these chunks, and turning a
    stubbed rc into an INVALID would break six harnesses in this repo that are
    about argv length and chunk counts, not about this check.
    """
    rc, cmds, out = _run_main(monkeypatch, tmp_path, n_files=8, chunks=2)
    text = capsys.readouterr().out
    assert "exit-code check: NOT RUN" in text, text
    assert "NOT certified" in text, text
    assert "NOT A MEASUREMENT" not in text, text


def test_a_healthy_chunk_process_is_not_flagged(monkeypatch, tmp_path, capsys):
    """NEGATIVE CONTROL -- the discriminator must be the rc/counts pair.

    rc=0 over a log that parses is the ordinary case and must produce no reason
    and exit 0. Without this pairing, a check that flagged EVERY chunk would
    satisfy all four tests above.
    """
    root = _fake_tree(tmp_path, 8)
    out = tmp_path / "out"
    out.mkdir()

    def _fake_run(cmd, *a, **k):
        if "pytest" in list(cmd):
            fh = k.get("stdout")
            if fh is not None and hasattr(fh, "write"):
                fh.write("." * 72 + " [100%]\n8 passed in 1.0s\n")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return None

    monkeypatch.setattr(RFS, "PROJECT_ROOT", root)
    monkeypatch.setattr(RFS, "_populated_agents", lambda *a, **k: (["alpha", "bravo"], None))
    monkeypatch.setattr(RFS.subprocess, "run", _fake_run)
    rc = RFS.main(["--out", str(out), "--chunks", "1"])
    text = capsys.readouterr().out
    assert "NOT A MEASUREMENT" not in text, text
    assert "exit-code check: NOT RUN" not in text, text
    assert rc == 0, text
