"""Regression tests for the stale-narrative-source gate ().

TWO LAYERS, and the second is the one that matters.

The predicate tests below are cheap branch coverage. The SEAM tests drive the
REAL `iteration-close.sh` and `closure-evidence-write.sh` with the REAL flag
shape production uses, because this gate's whole failure mode is a wiring one:
a gate that unit-tests green while its call site never invokes it is
indistinguishable from a gate that always passes (guard-5421 — a test that
re-implements the code under test verifies the copy, not the ship; rb-9583 —
writer+reader seams need a live end-to-end probe).

For the same reason there is no `assert "stale-summary-source-gate" in
script_src` here. A substring check would pass against a commented-out call.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
GATE = SCRIPT_DIR / "stale-summary-source-gate.py"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# Never a bare "bash" argv[0] (guard-580), and script paths as_posix()
# (guard-581). The pre-commit gate refused the first version of this file, and
# the fix-up patch then aborted on its own check while pytest went on passing
# against the UNPATCHED file — a green run proving nothing about the change.
from _bash_helpers import BASH  # noqa: E402

from gates.stale_summary_source import GRACE_SECONDS, evaluate  # noqa: E402

# A goal id that cannot exist, so `claimed_at` resolves to None and the gate
# falls through to its session_start reference. Using a REAL goal would make
# these tests depend on that goal's claim state, which changes underneath them.
ABSENT_GOAL = "g-000-00"


def _run(cmd, **kw):
    env = dict(os.environ)
    # guard-955: never let a test's writes reach the production storage backend.
    env["STORAGE_BACKEND"] = "local"
    return subprocess.run(
        cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
        timeout=180, env=env, **kw
    )


def _aged_file(tmp_path, name="stale-note.md", epoch=946684800.0):
    """A narrative file whose mtime is far in the past (2000-01-01 by default).

    Older than any plausible session_start, so the seam tests do not depend on
    how long this box's session has been running.
    """
    p = tmp_path / name
    p.write_text("prose that belongs to some other unit of work\n", encoding="utf-8")
    os.utime(p, (epoch, epoch))
    return p


def _fresh_file(tmp_path, name="fresh-note.md"):
    p = tmp_path / name
    p.write_text("prose written during this unit of work\n", encoding="utf-8")
    now = time.time()
    os.utime(p, (now, now))
    return p


def _reference_is_resolvable(path):
    """True when the gate can resolve ANY reference time for `path`.

    Without this the seam tests would silently degrade into vacuous passes on a
    box whose session_start is unset: the gate would return `no-reference-time`,
    refuse nothing, and the RED assertion would fail for a reason that has
    nothing to do with the wiring under test. Detected, never assumed.
    """
    out = _run([sys.executable, str(GATE), "--path", str(path),
                "--goal", ABSENT_GOAL, "--caller", "pytest-precheck"])
    return '"decision_path": "no-reference-time"' not in out.stdout


# --------------------------------------------------------------------------
# Layer 1 — the pure predicate
# --------------------------------------------------------------------------

def test_stale_source_is_blocked():
    v = evaluate("/t/note.txt", 1000.0, 1000.0 + 7200, "claimed_at")
    assert v["blocked"] is True
    assert v["decision_path"] == "stale-source:claimed_at"
    assert v["age_seconds"] == pytest.approx(7200.0)
    assert "--override-stale-source" in v["message"]


def test_source_newer_than_reference_passes():
    v = evaluate("/t/note.txt", 2000.0, 1000.0, "claimed_at")
    assert v["blocked"] is False
    assert v["decision_path"] == "source-newer-than-reference"
    assert v["message"] is None


def test_grace_window_is_clock_skew_only():
    """Inside GRACE passes; one second past it blocks. Pins the boundary so a
    future widening is a deliberate edit with a failing test, not a drift."""
    ref = 10_000.0
    assert evaluate("/t/n", ref - GRACE_SECONDS, ref, "claimed_at")["blocked"] is False
    assert evaluate("/t/n", ref - GRACE_SECONDS - 1, ref, "claimed_at")["blocked"] is True


def test_unstattable_source_is_not_this_gates_refusal():
    v = evaluate("/t/gone.txt", None, 1000.0, "claimed_at")
    assert v["blocked"] is False
    assert v["decision_path"] == "no-source-mtime"


def test_absent_reference_does_not_block():
    """'Cannot judge' must not become 'suspicious' — otherwise a box with
    unreadable session state refuses every narrative write."""
    v = evaluate("/t/note.txt", 1000.0, None, "none")
    assert v["blocked"] is False
    assert v["decision_path"] == "no-reference-time"


def test_every_branch_has_a_distinct_decision_path():
    """guard-502: gate telemetry is useless if two branches report the same
    path — a firing log could not tell WHY the gate decided."""
    paths = {
        evaluate("/t/n", 1000.0, 8200.0, "claimed_at")["decision_path"],
        evaluate("/t/n", 8200.0, 1000.0, "claimed_at")["decision_path"],
        evaluate("/t/n", None, 1000.0, "claimed_at")["decision_path"],
        evaluate("/t/n", 1000.0, None, "none")["decision_path"],
    }
    assert len(paths) == 4


# --------------------------------------------------------------------------
# Layer 2 — the seams, driven with the production arg shape
# --------------------------------------------------------------------------

def test_seam_iteration_close_refuses_stale_summary_file(tmp_path):
    stale = _aged_file(tmp_path)
    if not _reference_is_resolvable(stale):
        pytest.skip("no claimed_at and no session_start on this box — the gate "
                    "correctly cannot judge, so the seam cannot be exercised")
    out = _run([BASH, (SCRIPT_DIR / "iteration-close.sh").as_posix(),
                "--phase", "verify", "--goal", ABSENT_GOAL, "--source", "world",
                "--outcome", "deep", "--summary-file", str(stale)])
    assert out.returncode == 2, out.stdout + out.stderr
    assert "BLOCKED" in out.stderr and "stale-source clobber" in out.stderr
    # The refusal must land during arg resolution, BEFORE the phase runs —
    # otherwise a partial close has already happened by the time we refuse.
    assert "phase_start" not in out.stdout


def test_seam_iteration_close_allows_a_fresh_summary_file(tmp_path):
    """The positive control. Omitting --phase makes the run stop at the USAGE
    check, which sits BELOW the gate — so reaching that message proves the gate
    allowed the file, without executing a real close."""
    fresh = _fresh_file(tmp_path)
    out = _run([BASH, (SCRIPT_DIR / "iteration-close.sh").as_posix(),
                "--goal", ABSENT_GOAL, "--source", "world",
                "--summary-file", str(fresh)])
    assert "BLOCKED" not in out.stderr, out.stderr
    assert "usage: iteration-close.sh" in out.stderr


def test_seam_iteration_close_override_reaches_past_the_gate(tmp_path):
    stale = _aged_file(tmp_path)
    out = _run([BASH, (SCRIPT_DIR / "iteration-close.sh").as_posix(),
                "--goal", ABSENT_GOAL, "--source", "world",
                "--summary-file", str(stale),
                "--override-stale-source", "pytest: deliberate reuse"])
    assert "BLOCKED" not in out.stderr, out.stderr
    assert "usage: iteration-close.sh" in out.stderr


def test_seam_closure_evidence_write_refuses_stale_summary_file(tmp_path):
    stale = _aged_file(tmp_path)
    if not _reference_is_resolvable(stale):
        pytest.skip("no resolvable reference time on this box")
    out = _run([BASH, (SCRIPT_DIR / "closure-evidence-write.sh").as_posix(),
                "--goal", ABSENT_GOAL, "--source", "world",
                "--summary-file", str(stale),
                "--prefix", "[worker-loop] close:"])
    assert "refusing stale narrative source" in out.stderr, out.stdout + out.stderr
    # rc=0 is deliberate here and differs from iteration-close's rc=2: this
    # script's existing posture for a bad --summary-file is warn-and-write-
    # nothing, because a provenance fault must not break the close itself.
    assert out.returncode == 0


def test_seam_closure_evidence_write_allows_a_fresh_summary_file(tmp_path):
    fresh = _fresh_file(tmp_path)
    out = _run([BASH, (SCRIPT_DIR / "closure-evidence-write.sh").as_posix(),
                "--goal", ABSENT_GOAL, "--source", "world",
                "--summary-file", str(fresh),
                "--prefix", "[worker-loop] close:"])
    assert "refusing stale narrative source" not in out.stderr, out.stderr


def test_gate_is_registered_in_gates_yaml():
    """_gate_log.log's docstring: a gate_id absent from gates.yaml is invisible
    to the retirement evaluator, so its firings are collected and never read."""
    import yaml
    doc = yaml.safe_load((PROJECT_ROOT / "core/config/gates.yaml").read_text(encoding="utf-8"))
    entry = [g for g in doc["gates"] if g["id"] == "stale-summary-source-gate"]
    assert len(entry) == 1
    assert entry[0]["override_flag"] == "--override-stale-source"
    assert {s["file"] for s in entry[0]["sites"]} == {
        "core/scripts/iteration-close.sh",
        "core/scripts/closure-evidence-write.sh",
    }
