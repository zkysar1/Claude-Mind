"""Unit tests for hypothesis-discovered-overdue-sweep.py classify_overdue ().

Pure-classifier tests only -- no daemon, no file I/O (classify_overdue takes the
record list + `now` explicitly). Daemon-safe: hermetic, no wm.py env I/O.
"""
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # core/scripts


def _load_sweep():
    """Load the hyphen-named module by path (not importable by name)."""
    spec = importlib.util.spec_from_file_location(
        "hypothesis_discovered_overdue_sweep",
        SCRIPT_DIR / "hypothesis-discovered-overdue-sweep.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SW = _load_sweep()
NOW = datetime(2026, 6, 24, 12, 0, 0)


def _iso(offset_days):
    return (NOW + timedelta(days=offset_days)).replace(microsecond=0).isoformat()


def _well_formed(**over):
    """A discovered record that PASSES validate_formation_quality at stage=active
    (claim>=20 + resolves_by + resolution method>=10 + short measurement channel>=5)."""
    rec = {
        "id": "2026-06-19_well-formed",
        "title": "well formed hypothesis",
        "stage": "discovered",
        "horizon": "short",
        "type": "calibration",
        "confidence": 0.6,
        "position": "YES",
        "claim": "The promoted record carries a full testable assertion of >=20 chars.",
        "resolves_by": _iso(-5),
        "resolution_criteria": "compare metric X before/after via the logged channel",
        "measurement_channel": "core/logs/metric.jsonl",
    }
    rec.update(over)
    return rec


def _bare(**over):
    """A discovered record that FAILS the active-formation gate (bare position)."""
    rec = {
        "id": "2026-06-19_bare",
        "title": "bare position",
        "stage": "discovered",
        "horizon": "short",
        "type": "exploration",
        "confidence": 0.5,
        "position": "redundant probes are wasteful",
        "claim": "",  # bare -> fails claim>=20 at active
        "resolves_by": _iso(-5),
    }
    rec.update(over)
    return rec


def test_empty_records():
    c = SW.classify_overdue([], NOW)
    assert c["scanned"] == 0 and c["overdue"] == 0
    assert c["expire"] == [] and c["promote"] == [] and c["needs_judgment"] == []


def test_future_resolves_by_not_overdue():
    rec = _well_formed(resolves_by=_iso(+10))
    c = SW.classify_overdue([rec], NOW)
    assert c["overdue"] == 0
    assert rec not in c["expire"] and rec not in c["promote"] and rec not in c["needs_judgment"]


def test_non_discovered_ignored():
    rec = _well_formed(stage="active", resolves_by=_iso(-40))
    c = SW.classify_overdue([rec], NOW)
    assert c["overdue"] == 0


def test_old_short_horizon_expires():
    # 40d overdue, short horizon, default expire_days_short=30 -> EXPIRE
    rec = _well_formed(resolves_by=_iso(-40))
    c = SW.classify_overdue([rec], NOW)
    assert rec in c["expire"]
    assert rec not in c["promote"] and rec not in c["needs_judgment"]


def test_recent_well_formed_promotes():
    rec = _well_formed(resolves_by=_iso(-5))  # 5d overdue, passes active-formation
    c = SW.classify_overdue([rec], NOW)
    assert rec in c["promote"]
    assert rec not in c["expire"] and rec not in c["needs_judgment"]


def test_recent_bare_needs_judgment():
    rec = _bare(resolves_by=_iso(-5))  # 5d overdue, fails formation -> judgment
    c = SW.classify_overdue([rec], NOW)
    assert rec in c["needs_judgment"]
    assert rec not in c["expire"] and rec not in c["promote"]


def test_old_bare_expires_not_judgment():
    # 40d overdue + bare: window closed dominates -> EXPIRE (never auto-resolved,
    # but UNRESOLVABLE archive is gate-exempt so it is safe mechanically).
    rec = _bare(resolves_by=_iso(-40))
    c = SW.classify_overdue([rec], NOW)
    assert rec in c["expire"]
    assert rec not in c["needs_judgment"]


def test_session_horizon_uses_short_threshold():
    rec = _bare(horizon="session", resolves_by=_iso(-40))
    c = SW.classify_overdue([rec], NOW)
    assert rec in c["expire"]  # 40 > 30 short threshold


def test_long_horizon_recent_formed_promotes_not_expired():
    # long horizon, 40d overdue: below the 90d long threshold -> NOT expired.
    # Well-formed (long horizon active-formation needs no measurement channel) -> promote.
    rec = _well_formed(horizon="long", resolves_by=_iso(-40))
    c = SW.classify_overdue([rec], NOW)
    assert rec in c["promote"]
    assert rec not in c["expire"]


def test_long_horizon_very_old_expires():
    rec = _well_formed(horizon="long", resolves_by=_iso(-100))  # > 90 long threshold
    c = SW.classify_overdue([rec], NOW)
    assert rec in c["expire"]


def test_session_end_sentinel_skipped():
    # session_end is not a parseable date -> never date-overdue, skipped.
    rec = _bare(horizon="session", resolves_by="session_end")
    c = SW.classify_overdue([rec], NOW)
    assert c["overdue"] == 0


def test_passes_active_formation_helper():
    assert SW.passes_active_formation(_well_formed()) is True
    assert SW.passes_active_formation(_bare()) is False


def test_custom_expire_threshold():
    # With expire_days_short=3, a 5d-overdue well-formed record EXPIRES instead of promotes.
    rec = _well_formed(resolves_by=_iso(-5))
    c = SW.classify_overdue([rec], NOW, expire_days_short=3)
    assert rec in c["expire"]
    assert rec not in c["promote"]


def test_counts_aggregate():
    recs = [
        _well_formed(id="p1", resolves_by=_iso(-5)),     # promote
        _bare(id="n1", resolves_by=_iso(-5)),            # needs_judgment
        _well_formed(id="e1", resolves_by=_iso(-40)),    # expire
        _well_formed(id="future", resolves_by=_iso(+5)), # not overdue
    ]
    c = SW.classify_overdue(recs, NOW)
    assert c["scanned"] == 4
    assert c["overdue"] == 3
    assert [r["id"] for r in c["promote"]] == ["p1"]
    assert [r["id"] for r in c["needs_judgment"]] == ["n1"]
    assert [r["id"] for r in c["expire"]] == ["e1"]


# ---------------------------------------------------------------------------
# : formed_date + horizon fallback when resolves_by is ABSENT.
# Before this, resolves_by-absent records (the common draft shape) were skipped
# entirely and never swept;  had to hand-triage 165 such records.
# ---------------------------------------------------------------------------


def _no_rb(**over):
    """A discovered record with NO resolves_by (the common draft shape). Bare
    (fails active-formation), so a resolves_by-absent record can only EXPIRE or
    NEEDS_JUDGMENT -- never PROMOTE (active-formation requires resolves_by per
    guard-798)."""
    rec = _bare(**over)
    rec.pop("resolves_by", None)
    return rec


def test_no_resolves_by_old_short_expires_via_id_date():
    # No resolves_by; id-date 2026-04-15 (~70d before NOW), short horizon.
    # effective deadline = formed(04-15) + 14 = 04-29 -> 56d overdue > 30 -> EXPIRE.
    rec = _no_rb(id="2026-04-15_old-short")
    c = SW.classify_overdue([rec], NOW)
    assert rec in c["expire"]
    assert c["overdue"] == 1


def test_no_resolves_by_recent_short_not_overdue():
    # id-date 2026-06-20 (4d before NOW); short window +14 -> 07-04 (future) -> not overdue.
    rec = _no_rb(id="2026-06-20_recent-short")
    c = SW.classify_overdue([rec], NOW)
    assert c["overdue"] == 0
    assert rec not in c["expire"] and rec not in c["needs_judgment"]


def test_no_resolves_by_long_horizon_recent_needs_judgment():
    # id-date 2026-03-19 + long window 90 = 06-17 -> 7d overdue < 90 long thresh ->
    # not expired; bare (no resolves_by => fails formation) -> NEEDS_JUDGMENT.
    # Mirrors the  manual call: keep recent long-horizon predictions.
    rec = _no_rb(id="2026-03-19_old-long", horizon="long")
    c = SW.classify_overdue([rec], NOW)
    assert rec in c["needs_judgment"]
    assert rec not in c["expire"]


def test_no_resolves_by_no_parseable_date_skipped():
    # No resolves_by AND id carries no YYYY-MM-DD prefix AND no formed_date field
    # -> no derivable deadline -> skipped entirely (not overdue). Fail-safe.
    rec = _no_rb(id="no-date-prefix-here")
    c = SW.classify_overdue([rec], NOW)
    assert c["overdue"] == 0
    assert rec not in c["expire"] and rec not in c["needs_judgment"]


def test_explicit_formed_date_preferred_over_id_date():
    # formed_date field present -> used even though the id-date would look recent.
    rec = _no_rb(id="2026-06-20_would-be-recent", formed_date="2026-04-15T00:00:00")
    c = SW.classify_overdue([rec], NOW)
    assert rec in c["expire"]  # formed 04-15 + 14 = 04-29 -> 56d overdue > 30


def test_resolves_by_present_still_wins_over_formed_date():
    # Backward-compat: when resolves_by IS present it is used, ignoring formed_date/id.
    rec = _bare(id="2026-04-15_old", horizon="short", resolves_by=_iso(+10),
                formed_date="2026-04-15T00:00:00")
    c = SW.classify_overdue([rec], NOW)
    assert c["overdue"] == 0  # future resolves_by wins -> not overdue


def test_effective_deadline_basis():
    # resolves_by present -> basis 'resolves_by'
    d, basis = SW.effective_deadline(_bare(resolves_by=_iso(-5)))
    assert basis == "resolves_by" and d is not None
    # resolves_by absent + id-date -> basis 'formed+horizon', deadline = formed + window
    d, basis = SW.effective_deadline(_no_rb(id="2026-04-15_x"))
    assert basis == "formed+horizon"
    assert d == datetime(2026, 4, 15) + timedelta(days=14)
    # resolves_by absent + no parseable date -> (None, None)
    d, basis = SW.effective_deadline(_no_rb(id="no-date"))
    assert d is None and basis is None


def test_non_date_sentinel_not_fallback_even_when_old():
    # A PRESENT-but-non-date resolves_by ('session_end') is a deliberate sentinel,
    # NOT an absent value -- so it must NOT trigger the formed_date+horizon
    # fallback, even with a stale id-date. Distinguishes intentional-sentinel-skip
    # from truly-absent-fallback (guards the test_session_end_sentinel_skipped
    # contract against re-break by 's fallback).
    rec = _bare(id="2026-01-01_very-old", horizon="session", resolves_by="session_end")
    c = SW.classify_overdue([rec], NOW)
    assert c["overdue"] == 0
    d, basis = SW.effective_deadline(rec)
    assert d is None and basis is None


# ---------------------------------------------------------------------------
# : DATE-GRANULARITY boundary (guard-2073).
#
# Every fixture above uses _iso(), which returns a full ISO *datetime*. So until
# this section the date-only shape -- 93.6% of live non-terminal pipeline
# records, measured 2026-07-31 -- was not exercised by a single test, and the
# N=0 (due-today) boundary was unpinned in either shape. That is why
# classify_overdue could parse a date-only deadline to MIDNIGHT and call a
# record with ~24h remaining overdue, while the pinned sibling
# pipeline_write._is_stale_unactivated said the opposite, for as long as it did.
# ---------------------------------------------------------------------------


def _date_only(offset_days):
    """A DATE-ONLY 'YYYY-MM-DD' deadline, the dominant live shape."""
    return (NOW + timedelta(days=offset_days)).date().isoformat()


def test_date_only_due_today_not_overdue():
    # THE regression this section exists for. Date-only today = END of today, so
    # it has NOT passed at NOW (12:00). Under the old midnight parse this was
    # overdue and indistinguishable from due-yesterday.
    rec = _well_formed(resolves_by=_date_only(0))
    c = SW.classify_overdue([rec], NOW)
    assert c["overdue"] == 0
    assert rec not in c["expire"] and rec not in c["promote"] and rec not in c["needs_judgment"]


def test_date_only_due_yesterday_is_overdue():
    # The other side of the same boundary: one day earlier IS past.
    rec = _well_formed(resolves_by=_date_only(-1))
    c = SW.classify_overdue([rec], NOW)
    assert c["overdue"] == 1
    assert rec in c["promote"]  # well-formed + recent -> review path


def test_date_only_due_tomorrow_not_overdue():
    rec = _well_formed(resolves_by=_date_only(+1))
    c = SW.classify_overdue([rec], NOW)
    assert c["overdue"] == 0


def test_datetime_due_today_earlier_instant_not_overdue():
    # DELIBERATE, and the pin that stops granularity-branching being reintroduced:
    # a datetime deadline is truncated to its DATE too, so an instant earlier
    # today (09:00 vs NOW 12:00) is NOT overdue. One predicate shared with the
    # sibling beats sub-day precision the day-scale buckets cannot use, and a
    # later boundary is fail-safe for a sweep whose expire[] bucket is one-way.
    rec = _well_formed(resolves_by=NOW.replace(hour=9).isoformat())
    c = SW.classify_overdue([rec], NOW)
    assert c["overdue"] == 0


def test_overdue_days_counts_calendar_days():
    # overdue_days must be date-granular too, or the expire threshold drifts
    # against the comparison that selected the record. Date-only 31 days back
    # crosses the 30-day short threshold; 30 days back does not.
    rec_expire = _well_formed(id="e", resolves_by=_date_only(-31))
    rec_keep = _well_formed(id="k", resolves_by=_date_only(-30))
    c = SW.classify_overdue([rec_expire, rec_keep], NOW)
    assert rec_expire in c["expire"]
    assert rec_keep not in c["expire"]


def test_formed_horizon_fallback_due_today_not_overdue():
    # The fallback basis inherits the same boundary: id-date 06-10 + short
    # window 14 = 06-24 = today -> not yet overdue. 06-09 + 14 = 06-23 -> overdue.
    today_rec = _no_rb(id="2026-06-10_lands-today")
    past_rec = _no_rb(id="2026-06-09_landed-yesterday")
    assert SW.classify_overdue([today_rec], NOW)["overdue"] == 0
    assert SW.classify_overdue([past_rec], NOW)["overdue"] == 1


def test_agrees_with_pinned_sibling_predicate_across_the_boundary():
    """Anti-drift pin: classify_overdue must agree with the authority it was
    reconciled to. Replicates pipeline_write._is_stale_unactivated's deadline
    test (`date.fromisoformat(str(rb)[:10]) < today`) rather than importing it,
    so this stays a hermetic pure-classifier test with no daemon coupling. If
    someone restores datetime-granularity here, the -0 and the intra-day cases
    diverge and this fails."""
    from datetime import date as _date

    def sibling_says_past(rb):
        return _date.fromisoformat(str(rb)[:10]) < NOW.date()

    cases = [_date_only(n) for n in (-31, -2, -1, 0, +1, +5)]
    cases.append(NOW.replace(hour=9).isoformat())   # intra-day datetime, today
    cases.append(NOW.replace(hour=23).isoformat())  # later-today datetime
    cases.append(_iso(-3))                          # datetime, clearly past
    for rb in cases:
        rec = _well_formed(resolves_by=rb)
        mine = SW.classify_overdue([rec], NOW)["overdue"] == 1
        assert mine == sibling_says_past(rb), f"disagreement on resolves_by={rb!r}"
