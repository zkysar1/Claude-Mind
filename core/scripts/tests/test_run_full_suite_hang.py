"""Tests for run-full-suite's DETERMINISTIC-HANG classifier ().

WHY THIS EXISTS, and why it is a separate axis from the contention classifier
pinned in test_run_full_suite.py: a hang and contention both invalidate a run,
but their remedies are OPPOSITE. The documented response to a contended verdict
is to climb the chunk ladder and re-run when the fleet is quiet. Against a
deterministic hang that is pure waste -- it reproduces SOLO, every time.
Measured on DESKTOP-O91DLK2 (bravo, 2026-08-14): three consecutive runs, two of
them BYTE-IDENTICAL 6990-byte logs, spent climbing the ladder before anyone
recognised the stall as a hang.

WHAT THE CLASSIFIER ACTUALLY SAID BEFORE THIS LANDED -- measured on a real Linux
faulthandler log, not assumed from the incident report. A hung `-q` run never
prints a `[NN%]` progress marker, so `_progress_tally` returns None and
`_looks_aborted` is FALSE; the run therefore fell through to the SILENT-ZERO
branch and was reported as "log empty, truncated, or corrupted". That is not
merely uninformative, it is a THIRD wrong direction: it points at the
NUL-byte/own-cloud log-corruption remedy (`--out` outside the synced tree) for a
run whose log is intact and complete. Hence case 6 below, which pins that the
corruption reason is SUPPRESSED when a hang explains the missing output.

The fixture is REAL captured output (pytest faulthandler_timeout on this box),
never a hand-written approximation of the format -- a parser written against an
imagined log shape is the defect class this whole goal is about.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
TARGET = SCRIPT_DIR.parent / "run-full-suite.py"

# Verbatim pytest faulthandler output, trimmed to the frames that matter. Note
# the deepest frame is the TEST, and every frame under it is runner internals --
# that ordering is what _hang_marker's noise filter exists to walk past.
REAL_HANG_LOG = '''.Timeout (0:00:05)!
Thread 0x000076c419e99080 (most recent call first):
  File "/tmp/u119-hangfix/test_synthetic_hang.py", line 5 in test_this_one_hangs
  File "/usr/lib/python3/dist-packages/_pytest/python.py", line 194 in pytest_pyfunc_call
  File "/usr/lib/python3/dist-packages/pluggy/_callers.py", line 102 in _multicall
  File "/usr/lib/python3/dist-packages/_pytest/main.py", line 350 in pytest_runtestloop
  File "<frozen runpy>", line 198 in _run_module_as_main
'''

# A hang whose DEEPEST frame is a stdlib/runner frame -- the shape produced when
# a test hangs inside subprocess. Naming subprocess.py as the culprit would send
# the reader to the wrong file entirely.
NESTED_HANG_LOG = '''..Timeout (0:10:00)!
Thread 0x00007f0000000000 (most recent call first):
  File "/usr/lib/python3.12/subprocess.py", line 1253 in _wait_for_tstate_lock
  File "/usr/lib/python3.12/subprocess.py", line 1209 in communicate
  File "/opt/ayoai-mind/core/scripts/tests/test_tree_update_stdin_and_argv_guards.py", line 247 in test_reverting_the_bounded_read_reddens
  File "/usr/lib/python3/dist-packages/_pytest/python.py", line 194 in pytest_pyfunc_call
'''

HEALTHY_LOG = "..........\n[100%]\n10 passed in 1.2s"
GENUINE_LOG = ("..F..  [100%]\n4 passed, 1 failed in 1.0s\n"
               "FAILED core/scripts/tests/test_x.py::test_y")
ABORTED_LOG = "....  [ 51%]"
EMPTY_LOG = ""


def _load():
    spec = importlib.util.spec_from_file_location("run_full_suite_hang", TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_full_suite_hang"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


# --- the extractor ----------------------------------------------------------

def test_hang_marker_extracts_duration_file_and_test(mod):
    """Case 1: the marker yields all four fields off REAL captured output."""
    got = mod._hang_marker(REAL_HANG_LOG)
    assert got is not None, "a real faulthandler abort was not recognised at all"
    dur, path, line, func = got
    assert dur == "0:00:05"
    assert path.endswith("test_synthetic_hang.py"), path
    assert line == "5"
    assert func == "test_this_one_hangs"


def test_hang_marker_walks_past_runner_frames(mod):
    """Case 2: the deepest frame is often subprocess.py, not the test.

    Reporting the deepest frame verbatim would name a stdlib file as the
    culprit. The reader needs the TEST that hung.
    """
    dur, path, line, func = mod._hang_marker(NESTED_HANG_LOG)
    assert dur == "0:10:00"
    assert "subprocess.py" not in path, (
        "named a stdlib frame as the hang site: %s" % path)
    assert path.endswith("test_tree_update_stdin_and_argv_guards.py"), path
    assert func == "test_reverting_the_bounded_read_reddens"


def test_hang_marker_is_silent_on_healthy_output(mod):
    """Case 3: no false positive. A detector that fires on clean runs gets
    ignored, which is how guard-580 decayed to times_noise=30."""
    assert mod._hang_marker(HEALTHY_LOG) is None
    assert mod._hang_marker(GENUINE_LOG) is None
    assert mod._hang_marker(ABORTED_LOG) is None
    assert mod._hang_marker(EMPTY_LOG) is None


# --- both classify() entry points (guard-3448) ------------------------------

def test_chunked_path_reports_hang_naming_the_file(mod):
    """Case 4: the per-chunk door."""
    verdict, reasons = mod.classify(REAL_HANG_LOG, 0, chunks=[REAL_HANG_LOG])
    assert verdict == "contended"
    hung = [r for r in reasons if "HUNG after" in r]
    assert hung, "no HUNG reason emitted for a real hang: %r" % (reasons,)
    assert "test_synthetic_hang.py" in hung[0], hung[0]
    assert "test_this_one_hangs" in hung[0], hung[0]


def test_unchunked_path_reports_hang_too(mod):
    """Case 5: the SECOND door (guard-3448 -- a gate is only as broad as its
    entry points). An un-chunked run is exactly how a suspected hang gets
    reproduced, so a detector wired only into the chunk loop would be absent at
    the moment it is most needed."""
    verdict, reasons = mod.classify(REAL_HANG_LOG, 0, chunks=None)
    assert verdict == "contended"
    hung = [r for r in reasons if "HUNG after" in r]
    assert hung, "un-chunked path missed the hang: %r" % (reasons,)
    assert "test_synthetic_hang.py" in hung[0], hung[0]


def test_hang_suppresses_the_misleading_corruption_reason(mod):
    """Case 6: the regression that motivated the goal.

    A hung log has no counts and no final progress marker, so the silent-zero
    branch would otherwise report "log empty, truncated, or corrupted" and send
    the reader to the own-cloud `--out` remedy for an intact log.
    """
    _, reasons = mod.classify(REAL_HANG_LOG, 0, chunks=[REAL_HANG_LOG])
    corruption = [r for r in reasons if "truncated, or corrupted" in r]
    assert not corruption, (
        "a hang was still reported as log corruption -- the wrong remedy: %r"
        % (corruption,))


# --- the pre-existing branches must be untouched ----------------------------

def test_healthy_run_still_clean(mod):
    verdict, reasons = mod.classify(HEALTHY_LOG, 0, chunks=[HEALTHY_LOG])
    assert verdict == "clean"
    assert reasons == []


def test_genuine_failures_still_genuine(mod):
    verdict, reasons = mod.classify(GENUINE_LOG, 1, chunks=[GENUINE_LOG])
    assert verdict == "genuine"
    assert reasons == []


def test_real_contention_still_reported_as_stopped(mod):
    """A run that stopped mid-progress is contention, NOT a hang -- it must keep
    the ladder remedy."""
    verdict, reasons = mod.classify(ABORTED_LOG, 0, chunks=[ABORTED_LOG])
    assert verdict == "contended"
    assert any("stopped at 51%" in r for r in reasons), reasons
    assert not any("HUNG after" in r for r in reasons), reasons


def test_silent_zero_without_a_hang_still_reports_corruption(mod):
    """The suppression in case 6 must be scoped to hangs only -- an empty log
    with no hang marker is still the corruption/truncation signal."""
    verdict, reasons = mod.classify(EMPTY_LOG, 0, chunks=[EMPTY_LOG])
    assert verdict == "contended"
    assert any("truncated, or corrupted" in r for r in reasons), reasons


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
