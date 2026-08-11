"""Unit tests for core/scripts/liveness_check.decide_liveness ().

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


# --- Retirement tombstone dominates freshness () -----------------
# A retired agent's shard SURVIVES (delete-less store) and keeps getting
# written, so shard freshness alone reports a decommissioned agent as alive.
# The retirement write itself refreshes that signal, so retiring an agent made
# it look MORE alive for a full threshold window. Measured on `meta-tiebreaker`
# 2026-07-28: retired_at 17:08:19, authoritative-store push 17:08:20, verdict
# "alive" 2.8h later.

RETIRED = {"retired": True, "retired_at": "2026-07-14T09:00:00", "retired_by": "bravo"}


def test_retired_beats_fresh_shard_signal():
    # The exact production shape: last_active absent (composing the roster drops
    # retired rows), shard push 10 minutes old -> would have been "alive".
    r = decide_liveness(None, _ago(minutes=10), threshold_hours=6, now=NOW,
                        retired_entry=RETIRED)
    assert r["verdict"] == "retired"
    assert r["signal"] == "retirement_tombstone"


def test_retired_beats_the_fresh_last_active_fast_path():
    # Ordering is load-bearing: an agent retired moments ago STILL has a fresh
    # last_active, so a freshness-first ordering would report it alive.
    r = decide_liveness(_ago(minutes=1), _ago(minutes=1), threshold_hours=6, now=NOW,
                        retired_entry=RETIRED)
    assert r["verdict"] == "retired"


def test_retired_reason_names_who_and_when():
    r = decide_liveness(None, _ago(minutes=10), threshold_hours=6, now=NOW,
                        retired_entry=RETIRED)
    assert "2026-07-14T09:00:00" in r["reason"] and "bravo" in r["reason"]


def test_retired_is_not_dormant_so_goals_stay_routed():
    # goal-selector._liveness_confirms_dormant tests `verdict == "dormant"`.
    # "retired" must NOT satisfy it — retired and dormant authorise different
    # things, and False is the fail-safe direction (goals stay routed).
    r = decide_liveness(None, _ago(days=7), threshold_hours=6, now=NOW,
                        retired_entry=RETIRED)
    assert r["verdict"] == "retired"
    assert r["verdict"] != "dormant"


def test_absent_tombstone_preserves_every_existing_verdict():
    # retired_entry defaults to None, so pre-existing callers are byte-identical.
    for la, fs, expected in (
        (_ago(minutes=30), None, "alive"),
        (_ago(days=7), _ago(minutes=4), "alive"),
        (_ago(days=7), _ago(days=7), "dormant"),
        (_ago(days=7), None, "unknown"),
    ):
        assert decide_liveness(la, fs, threshold_hours=6, now=NOW)["verdict"] == expected
        assert decide_liveness(la, fs, threshold_hours=6, now=NOW,
                               retired_entry=None)["verdict"] == expected


def test_revived_agent_is_not_retired_here():
    # _team_state._is_retired owns the revival rule (a heartbeat newer than
    # retired_at un-retires) and is applied by fetch_retirement_tombstone, which
    # then passes None. Assert the pure function honors that contract: given
    # None it must fall through to the freshness verdict, never a sticky retired.
    r = decide_liveness(_ago(minutes=5), None, threshold_hours=6, now=NOW,
                        retired_entry=None)
    assert r["verdict"] == "alive"


# --- Mind vs Body: the shard OBJECT time is not mind liveness (-e) ---
#
# The shard object's write time says "something on that box wrote this shard".
# Under the Mind/Body split that something can be a worker Body while the
# reducer is dead, so object freshness alone must never promote to "alive".


def test_fresh_object_with_stale_authoritative_value_is_not_alive():
    """THE REGRESSION. A worker Body writing the shard refreshes the object while
    the mind's own heartbeat has aged out. Before the fix this returned alive."""
    r = decide_liveness(_ago(days=7), _ago(minutes=4), threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(days=7))
    assert r["verdict"] != "alive"
    assert r["verdict"] == "unknown"


def test_body_write_does_not_make_a_dead_reducer_dormant_either():
    """guard-1042 + the goal-selector contract. `dormant` is the ONLY verdict
    _liveness_confirms_dormant acts on, so answering dormant here would leak an
    active agent's routed goals cross-agent. Not alive, and not dormant."""
    r = decide_liveness(_ago(days=7), _ago(minutes=4), threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(days=7))
    assert r["verdict"] != "dormant"


def test_fresh_authoritative_value_is_alive_and_names_its_signal():
    r = decide_liveness(_ago(days=7), _ago(days=7), threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(minutes=10))
    assert r["verdict"] == "alive"
    assert r["signal"] == "authoritative_last_active"


def test_fresh_authoritative_value_beats_a_stale_object():
    # Mind heartbeating but the object read came back old: still alive. The VALUE
    # is the mind signal; object time is only corroboration.
    r = decide_liveness(None, _ago(days=3), threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(minutes=1))
    assert r["verdict"] == "alive"


def test_both_authoritative_and_object_stale_is_still_dormant():
    # Two independent authoritative signals agree the agent is quiet.
    r = decide_liveness(_ago(days=7), _ago(days=7), threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(days=7))
    assert r["verdict"] == "dormant"


def test_stale_authoritative_with_unreadable_object_is_unknown_not_dormant():
    # The object read failed, so there is no corroboration for a death claim.
    r = decide_liveness(_ago(days=7), None, threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(days=7))
    assert r["verdict"] == "unknown"


def test_retirement_still_dominates_a_fresh_authoritative_value():
    # A just-retired agent's last heartbeat is still fresh; retirement wins.
    r = decide_liveness(_ago(minutes=1), _ago(minutes=1), threshold_hours=6, now=NOW,
                        retired_entry={"retired": True, "retired_at": "2026-07-14T09:00:00"},
                        authoritative_last_active_iso=_ago(minutes=1))
    assert r["verdict"] == "retired"


def test_absent_authoritative_value_preserves_every_existing_verdict():
    """Backward-compat twin of test_absent_tombstone_preserves_every_existing_verdict.
    Omitting the new argument must leave every pre-existing caller byte-identical —
    including the legacy object-freshness-implies-alive row, which is still correct
    when no mind signal could be read at all."""
    for la, fs, expected in (
        (_ago(minutes=30), None, "alive"),
        (_ago(days=7), _ago(minutes=4), "alive"),
        (_ago(days=7), _ago(days=7), "dormant"),
        (_ago(days=7), None, "unknown"),
    ):
        assert decide_liveness(la, fs, threshold_hours=6, now=NOW)["verdict"] == expected
        assert decide_liveness(la, fs, threshold_hours=6, now=NOW,
                               authoritative_last_active_iso=None)["verdict"] == expected


def test_authoritative_age_is_reported_in_every_result():
    # The new field must be present on all verdicts so callers can log why.
    r = decide_liveness(_ago(days=7), _ago(minutes=4), threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(days=7))
    assert "authoritative_last_active_age_min" in r
    assert r["authoritative_last_active_age_min"] > 6 * 60
    r2 = decide_liveness(_ago(minutes=5), None, threshold_hours=6, now=NOW)
    assert r2["authoritative_last_active_age_min"] is None
