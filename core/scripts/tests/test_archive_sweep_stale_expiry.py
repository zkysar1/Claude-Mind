"""Tests for archive_sweep auto-expiry of stale, never-activated hypotheses ().

archive_sweep previously archived ONLY stage==resolved records by age
(ARCHIVE_AGE_DAYS), so discovered- and measurement-pending-stage records that
were proposed but never activated accumulated indefinitely past their
resolves_by (the manual g-001-06 recurring sweep had to hand-archive 22 such
records in one session). The `_is_stale_unactivated` predicate (used by
archive_sweep's expiry branch) now wires the documented
measurement-pending->archived-at-resolves_by transition.

These tests exercise the pure predicate directly (hermetic — no daemon), so
they run in the daemon-safe suite. Both required cases from the goal are
covered: the record IS expired, and a not-yet-due record is left untouched.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
for _p in (str(CORE_SCRIPTS), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mind_api.src.world import pipeline_write  # noqa: E402

TODAY = date(2026, 7, 4)
PAST = "2026-07-01"          # strictly before TODAY -> due
FUTURE = "2026-07-10"        # after TODAY -> not due
TODAY_STR = "2026-07-04"     # equal to TODAY -> not yet past (strict <)
PAST_DATETIME = "2026-07-01T05:35:00"  # datetime form; [:10] date part is past


def _base(stage: str, **overrides) -> dict:
    rec = {
        "stage": stage,
        "resolves_by": PAST,
        "outcome": None,
        "experience_ref": None,
    }
    rec.update(overrides)
    return rec


# ---------------------------------------------------------------------------
# EXPIRE: never-activated discovered/measurement-pending, past resolves_by
# ---------------------------------------------------------------------------

EXPIRE_CASES = [
    ("discovered_past", _base("discovered")),
    ("measurement_pending_past", _base("measurement-pending")),
    ("discovered_datetime_resolves_by", _base("discovered", resolves_by=PAST_DATETIME)),
]


@pytest.mark.parametrize("label,rec", EXPIRE_CASES, ids=[c[0] for c in EXPIRE_CASES])
def test_is_stale_unactivated_expires(label, rec):
    assert pipeline_write._is_stale_unactivated(rec, TODAY) is True, (
        f"{label} should be flagged for EXPIRED archival")


# ---------------------------------------------------------------------------
# LEAVE UNTOUCHED: any failing clause -> no premature expiry
# ---------------------------------------------------------------------------

LEAVE_CASES = [
    # Not yet due (the goal's explicit "left in discovered stage" case).
    ("not_yet_due", _base("discovered", resolves_by=FUTURE)),
    # Due exactly today is not strictly past -> left (strict rb < today).
    ("due_today_not_past", _base("discovered", resolves_by=TODAY_STR)),
    # Already carries an outcome -> not a candidate.
    ("already_outcomed", _base("discovered", outcome="EXPIRED")),
    ("confirmed_outcome", _base("discovered", outcome="CONFIRMED")),
    # Has a formation trace -> excluded (the goal's experience_ref==null gate).
    ("has_experience_ref", _base("discovered", experience_ref="exp-foo")),
    # Wrong stages never expire here.
    ("resolved_stage", _base("resolved")),
    ("active_stage", _base("active")),
    ("archived_stage", _base("archived")),
    # Missing / empty / unparseable resolves_by -> left in live.
    ("no_resolves_by", _base("discovered", resolves_by=None)),
    ("empty_resolves_by", _base("discovered", resolves_by="")),
    ("garbage_resolves_by", _base("discovered", resolves_by="not-a-date")),
]


@pytest.mark.parametrize("label,rec", LEAVE_CASES, ids=[c[0] for c in LEAVE_CASES])
def test_is_stale_unactivated_leaves_untouched(label, rec):
    assert pipeline_write._is_stale_unactivated(rec, TODAY) is False, (
        f"{label} should NOT be flagged for expiry")


def test_expired_records_validate_cleanly_without_claim():
    """A claim-less discovered record, once EXPIRED+archived by the sweep,
    must pass _validate_record (which — unlike _validate_formation_quality —
    does not require claim>=20). Guards the design assumption that the sweep
    won't 400 on claim-less discovered records."""
    rec = {
        "id": "2026-06-01_stale-unactivated-slug",
        "title": "stale unactivated hypothesis",
        "stage": "archived",          # post-expiry stage
        "horizon": "short",
        "type": "calibration",
        "confidence": 0.5,
        "category": "framework-patterns",
        "formed_date": "2026-06-01",
        "outcome": "EXPIRED",
        "outcome_date": TODAY.isoformat(),
        "position": "YES -- a multi-word position that satisfies validation",
        # deliberately NO claim field
    }
    # Must not raise.
    pipeline_write._validate_record(rec)
