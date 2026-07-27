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
