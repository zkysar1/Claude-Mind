"""test_infra_health_staleness.py --  status staleness guard.

Verifies the cmd_status staleness annotation: a stored component value whose
most-recent recorded result (max of last_success/last_failure) is older than
infra_health.status_staleness_hours is flagged stale=true, so a consumer does
not misread the last reading as the CURRENT state (the 2026-05-25 false
roblox-down-10-days alarm; structuralizes guard-647).

Unit-tests the pure helper _component_staleness across the cases that matter:
fresh, stale (the 10-day false-alarm shape), never-checked, malformed timestamp
(guard-514 safe-getter), None-guard (guard-420), the recovery-in-progress shape
(last_failure newer than last_success), and the just-over-threshold boundary.
Plus a config-load smoke test for _load_status_staleness_hours.

infra-health.py is hyphenated (not import-able as a module name), so it is
loaded by file path via importlib -- same indirection a hyphenated-script unit
test always needs.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# infra-health.py has a hyphen -> load it by file path.
_spec = importlib.util.spec_from_file_location(
    "infra_health_mod", CORE_SCRIPTS / "infra-health.py"
)
ih = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ih)

NOW = datetime(2026, 6, 16, 12, 0, 0)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def test_fresh_value_not_stale():
    entry = {"last_success": _iso(NOW - timedelta(hours=1)), "last_failure": None}
    stale, last_check, hours_ago = ih._component_staleness(entry, 6.0, NOW)
    assert stale is False
    assert hours_ago == 1.0
    assert last_check == _iso(NOW - timedelta(hours=1))


def test_old_value_is_stale_the_false_alarm_shape():
    # The canonical 2026-05-25 false-alarm shape: a 10-day-old stored failure
    # with a nonzero streak. Stored value looks like an outage but no probe ran.
    entry = {
        "last_success": None,
        "last_failure": _iso(NOW - timedelta(days=10)),
        "consecutive_failures": 5,
    }
    stale, last_check, hours_ago = ih._component_staleness(entry, 6.0, NOW)
    assert stale is True
    assert hours_ago == 240.0


def test_never_checked_is_stale():
    entry = {"last_success": None, "last_failure": None}
    stale, last_check, hours_ago = ih._component_staleness(entry, 6.0, NOW)
    assert stale is True
    assert last_check is None
    assert hours_ago is None


def test_malformed_timestamp_skipped_not_crashed():
    # guard-514: a malformed persisted timestamp must be skipped, never crash.
    entry = {"last_success": "not-a-date", "last_failure": None}
    stale, last_check, hours_ago = ih._component_staleness(entry, 6.0, NOW)
    assert stale is True  # no parseable reading -> stale by definition
    assert last_check is None


def test_none_guard_no_crash_on_empty_entry():
    # guard-420: None-guard before datetime arithmetic. Empty / non-dict inputs
    # must return cleanly, never TypeError.
    assert ih._component_staleness({}, 6.0, NOW) == (True, None, None)
    assert ih._component_staleness(None, 6.0, NOW) == (True, None, None)


def test_uses_most_recent_of_success_and_failure():
    # last_failure (1h ago) newer than last_success (2 days ago):
    # recovery-in-progress shape -- the newest result wins, so NOT stale.
    entry = {
        "last_success": _iso(NOW - timedelta(days=2)),
        "last_failure": _iso(NOW - timedelta(hours=1)),
    }
    stale, last_check, hours_ago = ih._component_staleness(entry, 6.0, NOW)
    assert stale is False
    assert hours_ago == 1.0
    assert last_check == _iso(NOW - timedelta(hours=1))


def test_boundary_just_over_threshold():
    entry = {"last_success": _iso(NOW - timedelta(hours=6, minutes=30))}
    stale, _, hours_ago = ih._component_staleness(entry, 6.0, NOW)
    assert stale is True
    assert hours_ago == 6.5


def test_boundary_exactly_at_threshold_not_stale():
    # hours_ago == threshold is NOT stale (strict > comparison).
    entry = {"last_success": _iso(NOW - timedelta(hours=6))}
    stale, _, hours_ago = ih._component_staleness(entry, 6.0, NOW)
    assert stale is False
    assert hours_ago == 6.0


def test_config_threshold_loads_positive_float():
    # Reads core/config/aspirations.yaml;  set status_staleness_hours,
    # and the fail-open default is also a positive float -- robust to retuning.
    hrs = ih._load_status_staleness_hours()
    assert isinstance(hrs, float)
    assert hrs > 0
