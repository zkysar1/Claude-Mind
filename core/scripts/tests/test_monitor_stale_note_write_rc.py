""" DEFECT 1 — the outcome_note write rc must reach the metrics row.

_apply_completion checked the rc of the STATUS write and returned on failure,
then made the outcome_note write with `_py(...)` and DISCARDED its rc. The
monitor_stale_completed row that follows asserts `preserved_prior_note_chars`
unconditionally, so on a failed note write the sweep returned True, the goal was
completed with NO sweep annotation, and the metrics row stated a preservation
that did not happen.

The unchecked rc is pre-existing; the POSITIVE FALSE CLAIM built on it is what
g-115-6415 shipped and what this pins. Store direction is safe either way (a
failed write leaves the prior note intact), so this is a telemetry defect — and
telemetry is exactly the guard-1231 surface a filer uses to learn a sweep
terminated their goal, which makes a false row worse than a missing one.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location(
        "monitor_stale_note_rc", CORE_SCRIPTS / "monitor-stale-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _harness(monkeypatch, note_rc):
    """Drive _apply_completion with the status write OK and the note write at
    `note_rc`. Returns the metric rows it emitted."""
    mod = _load()
    goal = {"id": "g-999-01", "_source": "world", "_asp_id": "asp-999"}
    fresh = {"id": "g-999-01", "status": "pending",
             "outcome_note": "PRIOR NOTE CONTENT"}

    monkeypatch.setattr(mod, "_reread_goal_authoritative",
                        lambda s, g: (fresh, "authoritative"))
    monkeypatch.setattr(mod, "_stale_candidate_reason", lambda f, p: None)

    def _fake_py(args, input_text=None):
        # discriminate the two writes by their field argument
        if "outcome_note" in args:
            return (note_rc, "", "simulated note-write failure" if note_rc else "")
        return (0, "", "")

    monkeypatch.setattr(mod, "_py", _fake_py)
    rows = []
    monkeypatch.setattr(mod, "_append_metric", lambda p, r: rows.append(r))
    ok, reason = mod._apply_completion(goal, "run-1", metrics_path=None)
    return rows, ok, reason


def _completed_row(rows):
    return next(r for r in rows if r.get("type") == "monitor_stale_completed")


def test_successful_note_write_still_reports_the_preserved_chars(monkeypatch):
    """The behaviour being preserved — this must not become a blanket zero."""
    rows, ok, _ = _harness(monkeypatch, note_rc=0)
    row = _completed_row(rows)
    assert ok is True
    assert row["preserved_prior_note_chars"] == len("PRIOR NOTE CONTENT")
    assert not row.get("note_write_failed")


def test_a_failed_note_write_is_not_reported_as_a_preservation(monkeypatch):
    """THE REGRESSION PIN. Pre-fix this row claimed 18 preserved characters on a
    write that never landed — a false positive on the one surface a filer reads
    to learn their goal was terminated by a sweep (guard-1231)."""
    rows, ok, _ = _harness(monkeypatch, note_rc=1)
    row = _completed_row(rows)
    assert row.get("note_write_failed") is True, (
        "a failed outcome_note write left no trace in the metrics row")
    assert row["preserved_prior_note_chars"] == 0, (
        f"row claims {row['preserved_prior_note_chars']} preserved chars on a "
        "write that returned non-zero")
    # POSITIVE CONTROL: the prior note was non-empty, so a naive implementation
    # reporting len(prior) would look plausible rather than obviously wrong.
    assert len("PRIOR NOTE CONTENT") == 18


def test_the_status_write_result_is_unchanged_by_this_fix(monkeypatch):
    """Store direction is safe: a failed NOTE write must not turn the completion
    itself into a failure — the goal really was completed."""
    rows, ok, reason = _harness(monkeypatch, note_rc=1)
    assert ok is True, "a telemetry failure must not be reported as a failed sweep"
    assert reason
