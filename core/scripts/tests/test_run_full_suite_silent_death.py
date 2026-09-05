"""Tests for run-full-suite's SILENT-DEATH classifier ().

WHY THIS IS ITS OWN AXIS, beside the contention classifier in
test_run_full_suite.py and the hang classifier in test_run_full_suite_hang.py:
all three invalidate a run, and all three have DIFFERENT remedies. Contention
wants a higher chunk rung. A deterministic hang wants the one file re-run under
a short faulthandler timeout. An in-process hard exit wants NEITHER -- it
reproduces at every rung and never trips faulthandler, because nothing is
stalled: `os._exit()` removes the interpreter from under pytest, skipping
exception handling, atexit and every teardown hook, and hands the OS status 0.

The run therefore looks SUCCESSFUL. Measured twice on real fleet runs:
chunk 04 at 13% (g-240-105) and chunk 09 at 88% (g-115-9018). The second cost
~2h climbing the ladder before a solo re-run falsified the contention premise,
and the truncation had been silently erasing 1131 results -- including 4 genuine
failures nobody could see -- while reporting exit 0.

Both instances had the same root cause: an uncancelled module-level
`threading.Timer(N, lambda: os._exit(0))` reaching a long-running process
through an `exec_module` of a script that normally lives milliseconds.

The fixture is REAL captured output -- the actual tail of the chunk-09 log from
the 2026-09-05 reproduction on cc-07, not a hand-written approximation of the
format. A parser written against an imagined log shape is the defect class this
goal is about.
"""
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TARGET = SCRIPT_DIR.parent / "run-full-suite.py"

# REAL tail of /tmp/c09_run.log (cc-07, 2026-09-05): rc=0, 1129 bytes, ends ON
# a bare partial progress row. Note there is no traceback, no INTERNALERROR and
# no summary line -- that absence IS the signature.
REAL_SILENT_DEATH_LOG = (
    "." * 72 + " [ 76%]\n"
    + "." * 72 + " [ 82%]\n"
    + "." * 72 + " [ 88%]\n"
    + "........."
)

# REAL shape of the same chunk after the fix: it reaches a summary line.
REAL_COMPLETED_LOG = (
    "." * 72 + " [ 88%]\n"
    + "." * 40 + " [100%]\n"
    "=========================== short test summary info ===========================\n"
    "FAILED core/scripts/tests/test_precheck_medium_battery.py::test_a_dropped_lane\n"
    "4 failed, 1131 passed, 1 skipped, 1 warning in 93.64s (0:01:33)\n"
)


def _load():
    spec = importlib.util.spec_from_file_location("run_full_suite_sd", TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_full_suite_sd"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_real_silent_death_log_is_detected():
    m = _load()
    assert m._looks_aborted(REAL_SILENT_DEATH_LOG)
    assert m._died_silently(REAL_SILENT_DEATH_LOG)


def test_completed_run_is_not_a_silent_death():
    """The predicate must be able to answer NO on the same chunk's good log."""
    m = _load()
    assert not m._looks_aborted(REAL_COMPLETED_LOG)
    assert not m._died_silently(REAL_COMPLETED_LOG)


def test_truncation_WITH_error_evidence_is_not_a_silent_death():
    """The discriminator, and the reason this is not just `_looks_aborted`.

    Resource starvation truncates a log too -- but something REPORTS it. Only
    an os._exit leaves the log ending on a bare progress row with nothing
    after it. If these cases stopped being separable the classifier would send
    readers up the ladder again, so each marker is pinned individually.
    """
    m = _load()
    for marker in (
        "INTERNALERROR> OSError: [Errno 24] Too many open files",
        "Traceback (most recent call last)",
        "Fatal Python error: Segmentation fault",
        "MemoryError",
        "STATUS_DLL_INIT_FAILED",
        "OSError: [WinError 206] The filename or extension is too long",
    ):
        noisy = REAL_SILENT_DEATH_LOG + "\n" + marker + "\n"
        assert not m._died_silently(noisy), marker


def test_empty_log_is_not_a_silent_death():
    m = _load()
    assert not m._died_silently("")


def test_marker_terminated_abort_is_NOT_a_silent_death():
    """The narrowing that keeps the ladder remedy where it belongs.

    An ordinary aborted chunk ends ON a complete `[ NN%]` marker; run-full-suite
    has always called that contention, and
    test_run_full_suite_hang.test_real_contention_still_reported_as_stopped
    pins that it keeps the chunk-ladder remedy. Only a stop PART-WAY through a
    row -- dots with no closing marker, because the interpreter vanished
    between two markers -- is the hard-exit signature. Without this exclusion
    the classifier relabels every ordinary abort and the ladder advice is lost.
    """
    m = _load()
    assert not m._died_silently("....  [ 51%]")
    assert not m._died_silently("." * 72 + " [ 88%]")


def test_NUL_corrupted_log_is_NOT_a_silent_death():
    """Log corruption () has its own cause AND its own remedy.

    A log rewritten under the writer by the sync layer also truncates without
    error text. Its fix is `--out` outside the synced tree -- nothing to do
    with a watchdog. Claiming a hard exit here would suppress the NUL-byte
    reason and send the reader to the wrong file entirely.
    """
    m = _load()
    assert not m._died_silently(REAL_SILENT_DEATH_LOG + "\x00\x00")


def test_classify_names_the_distinct_cause_and_forbids_the_ladder():
    """The whole point: the READER must not be routed to the chunk ladder.

    A rung changes how many processes the files are split across; a hard exit
    reproduces in all of them. So the reason text must both name the cause and
    say plainly not to climb.
    """
    m = _load()
    verdict, reasons = m.classify(
        REAL_SILENT_DEATH_LOG, 0, chunks=[REAL_SILENT_DEATH_LOG])
    blob = " ".join(reasons)
    assert "DIED SILENTLY" in blob
    assert "IN-PROCESS HARD EXIT" in blob
    assert "Do NOT climb the chunk ladder" in blob
    # and it must NOT be described with the generic wording that means
    # "just an unfinished chunk", which is what sent readers to the ladder.
    assert "it never finished, so the totals" not in blob
    # the run is still invalid -- naming the cause must not launder it clean
    assert verdict != "clean"


def test_ordinary_abort_keeps_the_generic_reason():
    """Forced-failure control for the branch above (guard-3534).

    An aborted chunk that DOES carry error evidence must still get the old
    generic reason, or this change would have replaced one blanket
    classification with another.
    """
    m = _load()
    noisy = REAL_SILENT_DEATH_LOG + "\nINTERNALERROR> OSError: too many files\n"
    verdict, reasons = m.classify(noisy, 0, chunks=[noisy])
    blob = " ".join(reasons)
    assert "it never finished, so the totals" in blob
    assert "DIED SILENTLY" not in blob
