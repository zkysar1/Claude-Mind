"""gates/body_hold.py: the long-hold predicate the selector and the claim endpoint share ().

A worker Body keeps its goal past the claim timeout while its row names the goal within
the 24 h cap and its own carrier is fresh and not closed. Each conjunct is pinned alone,
at its boundary, and against the inputs a failed read leaves behind (None), so a change
to any one of them shows up here before it can desync the two consumers.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gates.body_hold import (  # noqa: E402
    CARRIER_FRESH_MINUTES,
    CLOSED_BODY_STATES,
    MAX_BODY_HOLD_HOURS,
    evaluate,
)

NOW = datetime(2026, 9, 27, 21, 0, 0)
SID = "47b99d45-0000-4000-8000-000000000001"
GOAL = "g-306-523"


def _stamp(**ago) -> str:
    return (NOW - timedelta(**ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _row(**ago) -> dict:
    return {"goal_id": GOAL, "claimed_at": _stamp(**ago), "phase": "4"}


def _carrier(sid: str = SID, body_state: str = "active", **ago) -> dict:
    return {"sid": sid, "agent": "alpha", "host": "test-box", "ts": _stamp(**ago),
            "body_state": body_state}


def _eval(row, carrier) -> dict:
    return evaluate(row, carrier, goal_id=GOAL, sid=SID, now=NOW)


def test_a_body_hours_into_its_goal_with_a_fresh_carrier_holds_it():
    # The measured shape: claimed 6 h 40 m ago, carrier 25 minutes old.
    got = _eval(_row(hours=6, minutes=40), _carrier(minutes=25))
    assert got == {"holds": True, "failed": [], "reason": "holds"}


def test_each_conjunct_alone_releases_the_goal_and_is_named():
    cases = {
        "row_names_goal": ({**_row(hours=6), "goal_id": "g-999-99"}, _carrier(minutes=5)),
        "within_hold_cap": (_row(hours=MAX_BODY_HOLD_HOURS, seconds=1), _carrier(minutes=5)),
        "carrier_fresh": (_row(hours=6), _carrier(minutes=CARRIER_FRESH_MINUTES, seconds=1)),
        "carrier_is_its_own": (_row(hours=6), _carrier(sid="some-other-body", minutes=5)),
        "carrier_not_closed": (_row(hours=6), _carrier(body_state="closed-graceful", minutes=5)),
    }
    for conjunct, (row, carrier) in cases.items():
        got = _eval(row, carrier)
        assert got["holds"] is False, conjunct
        assert got["failed"] == [conjunct], (conjunct, got)


def test_the_boundaries_are_inclusive():
    assert _eval(_row(hours=MAX_BODY_HOLD_HOURS), _carrier(minutes=5))["holds"]
    assert _eval(_row(hours=6), _carrier(minutes=CARRIER_FRESH_MINUTES))["holds"]


def test_unread_inputs_fail_every_conjunct_they_feed_not_just_the_first():
    # A failed read leaves None; the predicate must report every consequence
    # (guard-3644), and must release: missing evidence never keeps a claim.
    got = _eval(None, None)
    assert got["holds"] is False
    assert got["failed"] == ["row_names_goal", "within_hold_cap", "carrier_fresh",
                             "carrier_is_its_own"]
    assert _eval(_row(hours=6), None)["failed"] == ["carrier_fresh", "carrier_is_its_own"]


def test_unparseable_stamps_release():
    assert _eval({**_row(hours=6), "claimed_at": "yesterday"}, _carrier(minutes=5))[
        "failed"] == ["within_hold_cap"]
    assert _eval(_row(hours=6), {**_carrier(minutes=5), "ts": ""})["failed"] == [
        "carrier_fresh"]


def test_a_parked_body_is_alive_and_keeps_its_goal():
    # `parked` is resumable (): only the CLOSED set withdraws liveness.
    assert "parked" not in CLOSED_BODY_STATES
    assert _eval(_row(hours=6), _carrier(body_state="parked", minutes=5))["holds"]


def test_the_windows_are_the_measured_ones():
    # 24 h: above the longest legitimate hold measured (17 h). 100 min: above the
    # longest worker cycle measured (92 min), the carrier's write cadence.
    assert MAX_BODY_HOLD_HOURS == 24
    assert CARRIER_FRESH_MINUTES == 100
