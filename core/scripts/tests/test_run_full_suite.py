"""Tests for run-full-suite's contention classifier ().

WHY THIS TOOL EXISTS: a suite run contended with the live fleet reports
hundreds of BOGUS failures (measured: 564 failed / 4,672 passed on a tree that
was clean). The failures look completely real up close, so the number invites
two OPPOSITE errors -- fixing phantom regressions, or waving away a real one.
The tool's job is to refuse to answer when the measurement is invalid.

These tests pin the three properties that make that refusal trustworthy:
  1. it recognises the real incident's fingerprint,
  2. it does NOT cry contention on ordinary failures (a classifier that always
     says "contended" is worse than none -- it teaches readers to ignore it,
     which is exactly how guard-580 decayed to times_noise=30),
  3. it can count a chunk whose trailing count line is missing, which on this
     box is the COMMON case, not an edge.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
TARGET = SCRIPT_DIR.parent / "run-full-suite.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_full_suite", TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_full_suite"] = mod
    spec.loader.exec_module(mod)
    return mod


RFS = _load()


def _progress(rows):
    """Build a pytest -q progress block: [(chars, pct), ...]."""
    return "\n".join("%s [%3d%%]" % (c, p) for c, p in rows)


# ── counting ────────────────────────────────────────────────────────────────

def test_counts_come_from_the_explicit_line_when_present():
    assert RFS._parse_counts("2 failed, 1129 passed in 900s") == (1129, 2, 0)


def test_counts_fall_back_to_progress_chars_when_the_line_is_absent():
    # THE case this box actually produces: reaches [100%], prints warnings,
    # exits with no count line. Without the fallback a green chunk reads as
    # 0 passed / 0 failed and a clean run looks like an empty one.
    text = _progress([("." * 70, 50), ("." * 30 + "FF", 100)])
    assert RFS._parse_counts(text) == (100, 2, 0)


def test_progress_tally_reports_the_last_percentage():
    assert RFS._progress_tally(_progress([(".....", 40)]))[3] == 40


def test_no_progress_and_no_counts_is_zero_not_a_crash():
    assert RFS._parse_counts("collecting ...") == (0, 0, 0)


# ── the contention fingerprint ──────────────────────────────────────────────

def test_dll_init_failure_marker_alone_is_contention():
    # 0xC0000142 means the OS could not start a process. No test failure of any
    # kind produces it, so one occurrence is sufficient evidence on its own.
    text = _progress([("F" * 10, 100)]) + "\nCommand '['git', 'init']' returned non-zero exit status 3221225794."
    verdict, reasons = RFS.classify(text, 10)
    assert verdict == "contended"
    assert any("3221225794" in r for r in reasons)


def test_late_loaded_failures_are_contention():
    # The measured fingerprint: clean early, failure-soaked late.
    text = _progress([("." * 72, 10), ("." * 72, 30),
                      ("." * 60 + "F" * 12, 80), ("." * 50 + "F" * 22, 100)])
    verdict, reasons = RFS.classify(text, 34)
    assert verdict == "contended"
    assert any("late-loaded" in r or "climbs" in r for r in reasons)


def test_unfinished_run_is_contention():
    text = _progress([("." * 72, 20), ("." * 40, 44)])
    assert RFS.classify(text, 0)[0] == "contended"


def test_one_stalled_chunk_is_caught_even_when_the_others_finished():
    """THE tool's own measured failure (2026-07-26). Chunk 02 stopped at 51%,
    but judged on the CONCATENATION the healthy chunks' count lines satisfied
    the has-counts test and the trailing 100% hid the stall -- so the run was
    declared 'GENUINE -- trustworthy' with a quarter of the suite never run.
    Completeness is per-chunk or it is nothing."""
    ok_a = _progress([("." * 72, 50), ("." * 72, 100)]) + "\n1144 passed in 900s"
    stalled = _progress([("." * 72, 25), ("." * 36, 51)])          # no count line
    ok_b = _progress([("." * 72, 50), ("." * 72, 100)]) + "\n1300 passed in 900s"
    chunks = [ok_a, stalled, ok_b]

    # The bug: concatenation-only judgement calls this fine.
    assert RFS.classify("\n".join(chunks), 0)[0] == "clean"

    # The fix: per-chunk judgement catches it and names the culprit.
    verdict, reasons = RFS.classify("\n".join(chunks), 0, chunks=chunks)
    assert verdict == "contended", reasons
    assert any("chunk 01" in r and "51%" in r for r in reasons), reasons


def test_all_chunks_complete_stays_trustworthy():
    # The completeness check must not fire when every chunk really finished,
    # or every healthy chunked run would read as INVALID.
    chunks = [_progress([("." * 72, 50), ("." * 70 + "FF", 100)]) for _ in range(3)]
    verdict, _ = RFS.classify("\n".join(chunks), 6, chunks=chunks)
    assert verdict == "genuine"


# ── and, just as important, the NEGATIVE cases ──────────────────────────────

def test_failures_spread_evenly_are_GENUINE_not_contention():
    # A real regression fails from the START, because the changed code is
    # exercised throughout. This must never be laundered into "environmental".
    text = _progress([("." * 60 + "F" * 12, 25), ("." * 60 + "F" * 12, 50),
                      ("." * 60 + "F" * 12, 75), ("." * 60 + "F" * 12, 100)])
    text += "\n48 failed, 240 passed in 100s"
    verdict, reasons = RFS.classify(text, 48)
    assert verdict == "genuine", reasons


def test_early_failures_only_are_GENUINE():
    text = _progress([("." * 50 + "F" * 22, 30), ("." * 72, 70), ("." * 72, 100)])
    text += "\n22 failed, 194 passed in 100s"
    assert RFS.classify(text, 22)[0] == "genuine"


def test_a_tiny_late_uptick_is_GENUINE_not_contention():
    """The measured false positive (2026-07-25): a HEALTHY 5,246-test run with
    11 pre-existing failures profiled 0.1% early -> 0.3% late and tripped a
    ratio-only rule at '5.1x'. Ratio without an absolute floor calls a clean
    tree contended -- and a classifier that cries wolf on ordinary failures is
    the one people stop reading."""
    early = [("." * 71 + "F", 10), ("." * 72, 20), ("." * 72, 30)]
    late = [("." * 72, 70), ("." * 71 + "F", 85), ("." * 71 + "F", 100)]
    text = _progress(early + late) + "\n3 failed, 429 passed in 100s"
    verdict, reasons = RFS.classify(text, 3)
    assert verdict == "genuine", reasons


def test_late_floor_sits_clear_of_both_measured_anchors():
    # Real incident 20.4% late; healthy run 0.3% late. The floor must separate
    # them with room, or it will drift into one of the two failure modes.
    assert 0.003 < RFS.LATE_FLOOR < 0.204


def test_a_completed_green_run_is_CLEAN():
    text = _progress([("." * 72, 50), ("." * 72, 100)])
    assert RFS.classify(text, 0)[0] == "clean"


def test_clean_run_without_a_count_line_is_still_CLEAN():
    # Guards the interaction between the two fixes: the missing count line must
    # not be read as an abort once the bar reached 100%.
    text = _progress([("." * 72, 60), ("." * 72, 100)])
    assert "passed" not in text
    assert RFS.classify(text, 0)[0] == "clean"


# ── reporting helpers ───────────────────────────────────────────────────────

def test_failing_files_are_deduped_and_sorted():
    blob = ("FAILED core/scripts/tests/test_b.py::test_one\n"
            "FAILED core/scripts/tests/test_a.py::test_two\n"
            "FAILED core/scripts/tests/test_a.py::test_three\n")
    assert RFS.failing_files(blob) == ["core/scripts/tests/test_a.py",
                                       "core/scripts/tests/test_b.py"]


def test_chunking_covers_every_file_exactly_once():
    items = list(range(97))
    groups = RFS._chunk(items, 4)
    assert len(groups) == 4
    flat = [x for g in groups for x in g]
    assert flat == items, "chunking must preserve and not drop files"


def test_chunking_survives_more_chunks_than_files():
    groups = RFS._chunk([1, 2], 8)
    assert [x for g in groups for x in g] == [1, 2]
    assert all(g for g in groups), "no empty chunks"


# ── the real incident, end to end ───────────────────────────────────────────

@pytest.mark.skipif(not (SCRIPT_DIR.parent.parent.parent / "agents").exists(),
                    reason="needs a repo checkout")
def test_the_documented_incident_shape_classifies_contended():
    """The 2026-07-25 fingerprint: 0 failures early, ~20% late, plus 0xC0000142."""
    text = _progress([("." * 72, 5), ("." * 72, 15), ("." * 72, 25)]
                     + [("." * 58 + "F" * 14, 90), ("." * 53 + "F" * 19, 100)])
    text += "\nsubprocess.CalledProcessError: Command '['git', 'init', '-q']' returned non-zero exit status 3221225794."
    verdict, reasons = RFS.classify(text, 33)
    assert verdict == "contended"
    assert len(reasons) >= 2, "both the marker and the positional profile should fire"


# ── collection roots () ───────────────────────────────────────────
#
# THE DEFECT THESE PIN: pytest.ini declared THREE testpaths and this runner
# collected ONE. 109 files / 1,448 tests never ran -- the gate suite and the
# daemon-endpoint suite -- and nothing detected it, because the runner reports
# what it RAN and never what it declined to look for (guard-1760). The runner
# shipped five weeks AFTER the config already declared three paths, so the
# newer artifact was the divergent one.
#
# The durable fix is that collection roots are DERIVED from pytest.ini rather
# than hardcoded, so a new test tree joins the suite by being declared in the
# config. test_derives_roots_from_pytest_ini is the regression guard: it fails
# if anyone re-hardcodes a single dir.


def _ini(tmp_path, testpaths, make_dirs=True):
    """Write a pytest.ini declaring `testpaths` and create the dirs."""
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\ntestpaths = %s\n" % " ".join(testpaths), encoding="utf-8")
    if make_dirs:
        for frag in testpaths:
            (tmp_path / frag).mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_derives_roots_from_pytest_ini(tmp_path, monkeypatch):
    """ALL declared testpaths are collected, not just the first."""
    root = _ini(tmp_path, ["core/scripts/tests", "core/tests/gates"])
    monkeypatch.setattr(RFS, "PROJECT_ROOT", root)
    got = [p.relative_to(root).as_posix() for p in RFS._testpaths()]
    assert got == ["core/scripts/tests", "core/tests/gates"]


def test_deferred_testpaths_are_excluded_from_collection(tmp_path, monkeypatch):
    """A declared path in DEFERRED_TESTPATHS is NOT collected into the pool.

    The MECHANISM is pinned with a synthetic entry: the live set is empty
    since g-115-6942 (mind_api/tests folded back after g-115-5651 fixed the
    backend-cache poisoning), and the set exists precisely so a future tree
    can be deferred again — this test must keep working when it is.
    """
    deferred = "synthetic/deferred-tree"
    monkeypatch.setattr(RFS, "DEFERRED_TESTPATHS", {deferred})
    root = _ini(tmp_path, ["core/scripts/tests", deferred])
    monkeypatch.setattr(RFS, "PROJECT_ROOT", root)
    got = [p.relative_to(root).as_posix() for p in RFS._testpaths()]
    assert got == ["core/scripts/tests"]
    assert deferred not in got


def test_declared_but_absent_dirs_are_skipped(tmp_path, monkeypatch):
    """A testpath naming a dir that does not exist is skipped, not passed to pytest."""
    root = _ini(tmp_path, ["core/scripts/tests"])
    (root / "pytest.ini").write_text(
        "[pytest]\ntestpaths = core/scripts/tests does/not/exist\n", encoding="utf-8")
    monkeypatch.setattr(RFS, "PROJECT_ROOT", root)
    got = [p.relative_to(root).as_posix() for p in RFS._testpaths()]
    assert got == ["core/scripts/tests"]


def test_falls_back_to_tests_dir_when_ini_is_unusable(tmp_path, monkeypatch):
    """A missing/corrupt pytest.ini degrades to the historical single dir.

    Fail-SAFE direction: the runner keeps collecting what it always did rather
    than collecting nothing. A fallback to [] would turn a config typo into a
    silent zero-test run reported as a pass -- the rb-5650 shape.
    """
    root = tmp_path
    (root / "pytest.ini").write_text("<<< not ini >>>", encoding="utf-8")
    tests_dir = root / "core" / "scripts" / "tests"
    tests_dir.mkdir(parents=True)
    monkeypatch.setattr(RFS, "PROJECT_ROOT", root)
    monkeypatch.setattr(RFS, "TESTS_DIR", tests_dir)
    assert RFS._testpaths() == [tests_dir]


def test_live_config_declares_more_than_one_testpath():
    """Guards the ORIGINAL defect against the live tree, not a fixture.

    The fixture tests above would all still pass if this repo's pytest.ini
    silently lost a testpath. This one reads the real config: if it declares
    more than one collectible root, the runner must return more than one.
    """
    # THREE parents: tests -> scripts -> core -> PROJECT_ROOT. Two lands on
    # core/ and the guard SKIPS with "no pytest.ini in this tree" -- which
    # reads as a pass in a -q summary. Caught on first run only by asking
    # WHICH test skipped; a check that declines to run reports success by
    # default (guard-1977).
    root = SCRIPT_DIR.parent.parent.parent
    ini = root / "pytest.ini"
    if not ini.exists():
        pytest.skip("no pytest.ini in this tree")
    # configparser, NOT a hand-rolled line parse: this repo's pytest.ini writes
    # testpaths in the MULTI-LINE continuation form, so reading "the rest of the
    # testpaths line" yields "" and the guard skips itself into a false pass.
    # (Measured on the first two runs of this very test.)
    import configparser
    cp = configparser.ConfigParser()
    cp.read(ini, encoding="utf-8")
    declared = [f for f in cp.get("pytest", "testpaths", fallback="").split()
                if (root / f).is_dir()]
    if len(declared) < 2:
        pytest.skip("tree declares fewer than 2 existing testpaths")
    collected = {p.name for p in RFS._testpaths()}
    expected = {Path(f).name for f in declared if f not in RFS.DEFERRED_TESTPATHS}
    assert collected == expected, (
        "runner collected %s but config declares %s (minus deferred)"
        % (sorted(collected), sorted(expected)))


# ===========================================================================
#  — NUL-byte log-corruption detection
#
# The corruption incident was MEASURED on 2026-07-31 and written into
# run-full-suite.py's own comments ("the log had 1532 NUL bytes"), but nothing
# ever tested for them. So the evidence lived in prose while every run stayed
# blind, and the hypothesis asking "is INVALID actually corruption?" could not
# be answered: the documented remedy for a bad verdict (climb the chunk ladder
# and re-run) OVERWRITES the chunk logs before anyone inspects them.
# ===========================================================================


_CLEAN_CHUNK = _progress([("." * 72, 50), ("." * 72, 100)]) + "\n144 passed in 90s"


def _corrupt(chunk, n=4):
    """Splice NUL bytes into an otherwise-healthy chunk log.

    Mirrors the measured incident: the file is PARTIALLY overwritten while the
    runner reads it, so the surviving text still looks like a finished run.
    """
    return chunk[:20] + ("\x00" * n) + chunk[20:]


def test_nul_corrupted_chunk_that_still_parses_is_caught():
    """THE FALSE-GENUINE CASE — invisible to both pre-existing branches.

    `_looks_aborted` is False (the log reaches 100%) and the silent-zero branch
    is skipped (the has-counts regex matches "144 passed"), so before this
    check a rewritten log was certified trustworthy. A false GENUINE is
    strictly worse than a false INVALID: INVALID at least refuses to be
    trusted.
    """
    chunks = [_CLEAN_CHUNK, _corrupt(_CLEAN_CHUNK), _CLEAN_CHUNK]
    # The pre-existing branches genuinely do not see it: neither fires here.
    assert RFS._looks_aborted(chunks[1]) is False
    verdict, reasons = RFS.classify("\n".join(chunks), 0, chunks=chunks)
    assert verdict == "contended", reasons
    nul = [r for r in reasons if "NUL byte" in r]
    assert len(nul) == 1, reasons
    assert "chunk 01" in nul[0] and "4 NUL byte" in nul[0]


def test_the_same_chunk_without_nuls_is_clean():
    """NEGATIVE CONTROL — the discriminator must be the NULs, nothing else.

    Byte-identical to the case above except the NULs are absent. Without this
    pairing, a check that flagged every chunk would satisfy the test above.
    """
    chunks = [_CLEAN_CHUNK, _CLEAN_CHUNK, _CLEAN_CHUNK]
    verdict, reasons = RFS.classify("\n".join(chunks), 0, chunks=chunks)
    assert verdict == "clean", reasons
    assert not [r for r in reasons if "NUL" in r]


def test_nul_and_aborted_are_reported_together_not_exclusively():
    """Both reasons must surface — their REMEDIES are opposite.

    Aborted says "the box was starved, re-run with more chunks"; corrupted
    says "do NOT re-run, that overwrites your evidence". An elif would hide
    one of them and send the reader to the wrong remedy.
    """
    stalled = _progress([("." * 72, 25), ("." * 36, 51)])   # no count line
    both = _corrupt(stalled, 2)
    chunks = [_CLEAN_CHUNK, both]
    verdict, reasons = RFS.classify("\n".join(chunks), 0, chunks=chunks)
    assert verdict == "contended", reasons
    assert any("NUL byte" in r for r in reasons), reasons
    assert any("stopped at" in r for r in reasons), reasons


def test_nul_reason_names_the_right_remedy_and_warns_off_the_ladder():
    """The actionable half: climbing the ladder DESTROYS this evidence.

    A re-run writes into the same default log dir, so the standard response to
    a bad verdict overwrites the only artifact that can diagnose corruption.
    The reason string has to say so, or the reader does the destructive thing.
    """
    chunks = [_corrupt(_CLEAN_CHUNK)]
    _, reasons = RFS.classify(chunks[0], 0, chunks=chunks)
    r = next(x for x in reasons if "NUL byte" in x)
    assert "--out" in r
    assert "CORRUPTION, not contention" in r
    assert "ladder" in r


def test_every_corrupted_chunk_is_named_individually():
    """Per-chunk indices, so the reader knows WHICH log to inspect."""
    chunks = [_corrupt(_CLEAN_CHUNK, 1), _CLEAN_CHUNK, _corrupt(_CLEAN_CHUNK, 3)]
    _, reasons = RFS.classify("\n".join(chunks), 0, chunks=chunks)
    nul = [r for r in reasons if "NUL byte" in r]
    assert len(nul) == 2, reasons
    assert "chunk 00" in nul[0] and "chunk 02" in nul[1]


def test_unchunked_run_is_unaffected():
    """classify() is also called with chunks=None; that path must not change."""
    verdict, reasons = RFS.classify(_CLEAN_CHUNK, 0)
    assert verdict == "clean"
    assert not [r for r in reasons if "NUL" in r]
