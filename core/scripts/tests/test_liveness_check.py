"""Unit tests for core/scripts/liveness_check.decide_liveness (9).

Exercises the PURE decision function only — no backend / fresh-signal-fetch IO —
so the tests run under the daemon-safe hermetic suite with no credentials.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from liveness_check import decide_liveness, _parse_iso, _age  # noqa: E402

NOW = datetime(2026, 7, 14, 10, 0, 0)


def _ago(**kw):
    """ISO string for a time `kw` before NOW (e.g. _ago(minutes=30))."""
    return (NOW - timedelta(**kw)).isoformat()


# --- Fast path: a fresh last_active is sufficient -------------------------

def test_fresh_last_active_is_alive():
    r = decide_liveness(_ago(minutes=30), None, threshold_hours=6, now=NOW)
    assert r["verdict"] == "alive"
    assert r["signal"] == "last_active"


def test_fresh_last_active_wins_even_if_fresh_signal_stale():
    # Fast path short-circuits before the fresh signal matters.
    r = decide_liveness(_ago(minutes=10), _ago(days=7), threshold_hours=6, now=NOW)
    assert r["verdict"] == "alive"
    assert r["signal"] == "last_active"


# --- THE bug-fix case: stale last_active but the partner is active ---------

def test_stale_last_active_fresh_signal_is_alive():
    # bravo scenario: local last_active mirror 7d stale, but the shard's
    # authoritative-store push time is 4 minutes old -> ALIVE, not dormant.
    r = decide_liveness(_ago(days=7), _ago(minutes=4), threshold_hours=6, now=NOW)
    assert r["verdict"] == "alive"
    assert r["signal"] == "fresh_signal"


def test_absent_last_active_fresh_signal_is_alive():
    r = decide_liveness(None, _ago(minutes=45), threshold_hours=6, now=NOW)
    assert r["verdict"] == "alive"
    assert r["signal"] == "fresh_signal"


# --- Genuine dormancy: both real signals say old --------------------------

def test_both_stale_is_dormant():
    r = decide_liveness(_ago(days=7), _ago(days=7), threshold_hours=6, now=NOW)
    assert r["verdict"] == "dormant"
    assert r["signal"] is None


def test_absent_last_active_stale_fresh_signal_is_dormant():
    r = decide_liveness(None, _ago(hours=9), threshold_hours=6, now=NOW)
    assert r["verdict"] == "dormant"


# --- False-dormant guard: unavailable fresh signal -> unknown -------------

def test_stale_last_active_unavailable_fresh_signal_is_unknown():
    # fresh-signal fetch failed / no creds / shard absent -> fresh signal is None.
    # Concluding dormant here would be a false-dormant on a transient fetch failure.
    r = decide_liveness(_ago(days=7), None, threshold_hours=6, now=NOW)
    assert r["verdict"] == "unknown"


def test_absent_last_active_unavailable_fresh_signal_is_unknown():
    r = decide_liveness(None, None, threshold_hours=6, now=NOW)
    assert r["verdict"] == "unknown"


def test_garbage_fresh_signal_is_unknown_not_dormant():
    # Unparseable fresh signal must degrade to unavailable (unknown), never dormant.
    r = decide_liveness(_ago(days=3), "not-a-timestamp", threshold_hours=6, now=NOW)
    assert r["verdict"] == "unknown"


# --- Threshold boundary ----------------------------------------------------

def test_exactly_at_threshold_is_alive():
    r = decide_liveness(_ago(hours=6), None, threshold_hours=6, now=NOW)
    assert r["verdict"] == "alive"  # <= threshold is fresh


def test_just_over_threshold_last_active_falls_through_to_fresh_signal():
    # 6h1m last_active is not fresh; a fresh signal rescues it.
    r = decide_liveness(_ago(hours=6, minutes=1), _ago(minutes=5), threshold_hours=6, now=NOW)
    assert r["verdict"] == "alive"
    assert r["signal"] == "fresh_signal"


def test_custom_threshold_widens_freshness():
    # A 12h last_active is stale at 6h but fresh at a 24h threshold.
    stale6 = decide_liveness(_ago(hours=12), None, threshold_hours=6, now=NOW)
    fresh24 = decide_liveness(_ago(hours=12), None, threshold_hours=24, now=NOW)
    assert stale6["verdict"] == "unknown"      # no fresh signal to fall back on
    assert fresh24["verdict"] == "alive"


# --- _parse_iso / _age tolerance ------------------------------------------

def test_parse_iso_tolerates_quotes_and_z():
    assert _parse_iso('"2026-07-14T09:00:00"') == datetime(2026, 7, 14, 9, 0, 0)
    # Z / offset normalized to naive local — just assert it parses to a datetime.
    assert isinstance(_parse_iso("2026-07-14T09:00:00Z"), datetime)


def test_parse_iso_none_and_empty():
    for v in (None, "", "null", "none", '""'):
        assert _parse_iso(v) is None


def test_age_future_skew_clamped_to_zero():
    # A peer clock slightly ahead must read as fresh (age 0), not negative.
    future = (NOW + timedelta(minutes=3)).isoformat()
    a = _age(future, NOW)
    assert a is not None and a.total_seconds() == 0


def test_age_missing_returns_none():
    assert _age(None, NOW) is None
    assert _age("garbage", NOW) is None
