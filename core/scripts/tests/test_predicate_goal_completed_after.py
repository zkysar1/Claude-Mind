"""goal_completed_after predicate ( / rb-4371) — date-only completed_date
end-of-day extension (FIX 1).

A DATE-ONLY completed_date ('2026-07-19', ~95% of the store) previously parsed to
midnight, spuriously FAILING an intra-day same-day cutoff (predicate.py:268/273).
FIX 1 extends a date-only value to end-of-day (23:59:59) before the >= comparison
— the goal provably completed by that day's end.

_lookup_goal_record is monkeypatched; after_ref uses the real iso: resolver.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import predicate  # noqa: E402


def _goal(monkeypatch, record):
    monkeypatch.setattr(predicate, "_lookup_goal_record", lambda gid: record)


def test_date_only_completed_passes_same_day_intra_day_cutoff(monkeypatch):
    # FIX 1 core case: date-only '2026-07-19' completion, cutoff 14:30 same day.
    # Pre-fix: midnight (00:00:00) < 14:30 -> FALSE. Post-fix: EOD 23:59:59 -> TRUE.
    _goal(monkeypatch, {"completed_date": "2026-07-19", "status": "completed"})
    r = predicate.evaluate({
        "id": "p1", "type": "goal_completed_after",
        "goal_id": "g-x", "after_ref": "iso:2026-07-19T14:30:00",
    })
    assert r.passed is True, r.reason


def test_date_only_completed_fails_next_day_cutoff(monkeypatch):
    # Regression guard: the EOD extension must NOT over-extend into the next day.
    # A date-only completion on 07-19 does not satisfy a cutoff on 07-20.
    _goal(monkeypatch, {"completed_date": "2026-07-19", "status": "completed"})
    r = predicate.evaluate({
        "id": "p2", "type": "goal_completed_after",
        "goal_id": "g-x", "after_ref": "iso:2026-07-20T10:00:00",
    })
    assert r.passed is False, r.reason


def test_full_datetime_completed_before_cutoff_still_fails(monkeypatch):
    # Regression guard: FIX 1 only affects date-only strings. A full datetime
    # completed BEFORE the cutoff still fails (no spurious EOD extension).
    _goal(monkeypatch, {"completed_date": "2026-07-19T09:00:00", "status": "completed"})
    r = predicate.evaluate({
        "id": "p3", "type": "goal_completed_after",
        "goal_id": "g-x", "after_ref": "iso:2026-07-19T14:30:00",
    })
    assert r.passed is False, r.reason


def test_lastachievedat_date_only_also_extended(monkeypatch):
    # The date-only branch applies to the recurring lastAchievedAt path too
    # (completed_ts_str = completed_date OR lastAchievedAt).
    _goal(monkeypatch, {"lastAchievedAt": "2026-07-19", "status": "completed"})
    r = predicate.evaluate({
        "id": "p4", "type": "goal_completed_after",
        "goal_id": "g-x", "after_ref": "iso:2026-07-19T23:00:00",
    })
    assert r.passed is True, r.reason
