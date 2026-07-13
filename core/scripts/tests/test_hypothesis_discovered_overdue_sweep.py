"""Unit tests for hypothesis-discovered-overdue-sweep.py classify_overdue (9).

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
# 1: formed_date + horizon fallback when resolves_by is ABSENT.
# Before this, resolves_by-absent records (the common draft shape) were skipped
# entirely and never swept; 6 had to hand-triage 165 such records.
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
    # Mirrors the 6 manual call: keep recent long-horizon predictions.
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
    # contract against re-break by 1's fallback).
    rec = _bare(id="2026-01-01_very-old", horizon="session", resolves_by="session_end")
    c = SW.classify_overdue([rec], NOW)
    assert c["overdue"] == 0
    d, basis = SW.effective_deadline(rec)
    assert d is None and basis is None
