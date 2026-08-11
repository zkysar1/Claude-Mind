"""test_handoff_currency.py — pins the boot handoff-currency gate ().

The gate refuses boot's ABBREVIATED auto-continuation path when handoff.yaml is
far behind the journal, which under own-cloud means it was resurrected from the
backend after boot's local-only consume (guard-1493).

Two properties matter and they pull in opposite directions, so both are pinned:
  RED  — it must actually fire on the measured fleet values, or it is decoration.
  OPEN — it must fail OPEN on every one of its own dependency errors (guard-142),
         or a gate bug blocks every boot.

Runs under pytest AND standalone (`py -3 <file>`).
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from handoff_currency import decide, DEFAULT_MAX_AGE_DAYS  # noqa: E402

NOW = datetime(2026, 8, 8, 3, 0, 0)


def _handoff(ts: str, session_number: int = 113) -> str:
    return f"session_number: {session_number}\ntimestamp: '{ts}'\nfirst_action:\n  goal_id: g-000-00\n"


# --- RED: it fires on the values actually measured on the fleet -------------

def test_fires_on_every_stale_agent_measured_2026_08_08():
    """The measurement that motivated the gate. All five journals read
    last_updated 2026-08-07; four of the five handoffs are far behind it.
    If this test ever goes green-by-passing-everything, the gate is inert."""
    measured = {                       # agent: (handoff timestamp, expect_stale)
        "alpha":   ("2026-08-05T18:56:05", False),   # ~2d  — under threshold
        "bravo":   ("2026-07-26T17:41:33", True),    # ~13d
        "echo":    ("2026-07-28T11:30:00", True),    # ~11d
        "foxtrot": ("2026-07-26T20:06:00", True),    # ~13d
        "zeta":    ("2026-07-26T20:25:00", True),    # ~13d
    }
    for agent, (ts, expect_stale) in measured.items():
        r = decide(_handoff(ts), "2026-08-07", NOW)
        got = r["verdict"] == "stale"
        assert got is expect_stale, f"{agent}: expected stale={expect_stale}, got {r}"


def test_stale_verdict_carries_the_evidence_not_just_a_flag():
    r = decide(_handoff("2026-07-26T20:25:00"), "2026-08-07", NOW)
    assert r["verdict"] == "stale"
    assert r["age_days"] > 10
    assert "g-115-4671" in r["reason"] and "guard-1493" in r["reason"]


# --- OPEN: every dependency error yields "current" (guard-142) --------------

def test_fails_open_on_missing_timestamp():
    r = decide("session_number: 113\nfirst_action: {}\n", "2026-08-07", NOW)
    assert r["verdict"] == "current" and "failing open" in r["reason"]


def test_fails_open_on_unparseable_timestamp():
    r = decide(_handoff("not-a-date-at-all"), "2026-08-07", NOW)
    assert r["verdict"] == "current" and "failing open" in r["reason"]


def test_fails_open_on_unreadable_journal():
    """Without the freshness reference the JOURNAL arm cannot ESTABLISH staleness,
    and 'cannot establish' must never become 'refuse'.

    Re-anchored for the two-arm engine (g-115-5313): the handoff must be RECENT,
    or the wall-clock arm legitimately fires and this test would be pinning the
    journal arm's fail-open using a case where the other arm has real evidence."""
    for journal in ("", None, "garbage"):
        r = decide(_handoff("2026-08-07T12:00:00"), journal, NOW)
        assert r["verdict"] == "current", (journal, r)
        assert "failing open" in r["reason"], (journal, r)


def test_fails_open_on_empty_handoff():
    assert decide("", "2026-08-07", NOW)["verdict"] == "current"


# --- the discriminator is the TIMESTAMP, not session_number ----------------

def test_session_number_gap_alone_never_triggers():
    """guard-1476. The originating goal proposed comparing handoff
    session_number against the journal's session count; those are DIFFERENT
    counters (measured: bravo handoff 62 vs journal total_sessions 426), so
    using them would be an unmeasured mechanism claim. A wild session_number
    with a FRESH timestamp must stay current."""
    r = decide(_handoff("2026-08-07T12:00:00", session_number=1), "2026-08-07", NOW)
    assert r["verdict"] == "current", r
    assert r["session_number"] == 1, "session_number is still reported, just not decisive"


# --- threshold behaviour ----------------------------------------------------

def test_threshold_boundary_is_strict_greater_than():
    """Journal-arm boundary. `now` is pinned close to the handoff so the
    wall-clock arm stays silent — otherwise this pins the wrong arm."""
    ts = "2026-08-04"                       # exactly 3d before 2026-08-07
    quiet = datetime(2026, 8, 4, 12, 0, 0)  # 0.5d of wall age: arm 2 silent
    assert decide(_handoff(ts), "2026-08-07", quiet, 3.0)["verdict"] == "current"
    r = decide(_handoff(ts), "2026-08-07", quiet, 2.0)
    assert r["verdict"] == "stale" and r["stale_arms"] == ["journal"], r


def test_default_threshold_is_three_days():
    assert DEFAULT_MAX_AGE_DAYS == 3.0


def test_a_handoff_ahead_of_the_journal_is_current():
    """Negative age must not wrap into staleness."""
    r = decide(_handoff("2026-08-09T00:00:00"), "2026-08-07", NOW)
    assert r["verdict"] == "current" and r["age_days"] < 0


# ---  defect 1: the wall-clock arm ---------------------------------
#
# Every fixture ABOVE reuses the one timestamp shape and one journal-vs-now
# relationship measured on the live fleet, which is why the suite passed 10/10
# against all three probed defects. These pin the axes those fixtures never
# varied.

def test_dormant_agent_is_stale_even_though_the_journal_tracks_the_handoff():
    """THE defect-1 probe, verbatim. A handoff of 2026-01-01 whose journal reads
    2026-01-02 has a journal lag of 1d — well under threshold — so the journal
    arm is correct to stay silent. Seven months of wall-clock later it is still
    stale, and before the fix this returned `current`. This is the dormant,
    crash-heavy population the gate exists for: their journal is stale too, so
    the journal arm can NEVER fire for them."""
    r = decide(_handoff("2026-01-01T00:00:00"), "2026-01-02", NOW)
    assert r["verdict"] == "stale", r
    assert r["stale_arms"] == ["wall-clock"], r
    assert r["age_days"] == 1.0, "journal arm must stay silent — 1d lag is not stale"
    assert r["wall_age_days"] > 200, r


def test_wall_clock_boundary_is_strict_greater_than():
    """Arm-2 boundary, with the journal pinned to the handoff so arm 1 is
    silent — the mirror of the journal-arm boundary test above."""
    ts = "2026-08-05T03:00:00"               # exactly 3d before NOW
    assert decide(_handoff(ts), ts, NOW, 3.0)["verdict"] == "current"
    r = decide(_handoff(ts), ts, NOW, 2.0)
    assert r["verdict"] == "stale" and r["stale_arms"] == ["wall-clock"], r


def test_wall_clock_arm_fires_without_any_journal_at_all():
    """The journal arm failing open must not suppress arm 2 — gating arm 2
    behind the journal would re-create the exact blind spot, because a dormant
    agent is the case where the journal is missing."""
    r = decide(_handoff("2026-01-01T00:00:00"), "", NOW)
    assert r["verdict"] == "stale" and r["stale_arms"] == ["wall-clock"], r


def test_unusable_clock_leaves_the_wall_arm_silent_not_refusing():
    """guard-142. `now` absent/garbage must not refuse; the journal arm decides
    alone, exactly as before the two-arm change."""
    for bad_now in (None, "2026-08-08", 0):
        r = decide(_handoff("2026-01-01T00:00:00"), "2026-01-02", bad_now)
        assert r["verdict"] == "current", (bad_now, r)
        assert r["wall_age_days"] is None, (bad_now, r)


def test_a_future_handoff_does_not_wrap_into_wall_clock_staleness():
    r = decide(_handoff("2026-12-01T00:00:00"), "2026-12-01", NOW)
    assert r["verdict"] == "current" and r["wall_age_days"] < 0, r


# ---  defect 2: realistic ISO shapes -------------------------------

def test_all_four_realistic_iso_shapes_are_decisive():
    """Probed before the fix: 3 of these 4 missed the timestamp pattern and the
    gate went permissive with NO log line. All four denote the same instant, so
    all four must reach the same verdict."""
    shapes = [
        "2026-07-26T20:25:00",              # plain — the only shape ever tested
        "2026-07-26T20:25:00.123456",       # fractional seconds
        "2026-07-26T20:25:00Z",             # Z suffix
        "2026-07-26T20:25:00+00:00",        # numeric offset
    ]
    for ts in shapes:
        r = decide(_handoff(ts), "2026-08-07", NOW)
        assert r["verdict"] == "stale", (ts, r)
        assert "no parseable timestamp" not in r["reason"], (ts, r)


def test_a_numeric_offset_is_normalized_not_silently_dropped():
    """The widened character class lets offsets through, so they must be
    CONVERTED rather than truncated away (the prefix-slicing parser dropped
    them, a multi-hour error on a 3-day threshold; rb-3741)."""
    r = decide(_handoff("2026-08-08T00:00:00+06:00"), "2026-08-07", NOW)
    # 00:00+06:00 is 2026-08-07T18:00Z, i.e. 9h before NOW (2026-08-08T03:00).
    assert r["verdict"] == "current", r
    assert abs(r["wall_age_days"] - 0.375) < 0.01, r


# ---  defect 3: column-0 anchoring ---------------------------------

def test_a_nested_timestamp_above_the_top_level_one_does_not_shadow_it():
    """Probed before the fix: `^\\s*timestamp` + re.M + .search takes the
    EARLIEST match, so an indented fresh value made a 12d-stale handoff read
    current. The top-level key is the one at column 0."""
    text = ("session_number: 113\n"
            "first_action:\n"
            "  timestamp: '2026-08-07T23:00:00'\n"   # nested, fresh, a decoy
            "timestamp: '2026-07-26T20:25:00'\n")    # top-level, the real one
    r = decide(text, "2026-08-07", NOW)
    assert r["handoff_timestamp"] == "2026-07-26T20:25:00", r
    assert r["verdict"] == "stale", r


def test_a_nested_session_number_does_not_shadow_the_top_level_one():
    """Same one-character defect in the sibling pattern. session_number is
    reported but never decisive, so this pins the emitted value only."""
    text = ("first_action:\n"
            "  session_number: 999\n"
            "session_number: 113\n"
            "timestamp: '2026-08-07T12:00:00'\n")
    assert decide(text, "2026-08-07", NOW)["session_number"] == 113


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
