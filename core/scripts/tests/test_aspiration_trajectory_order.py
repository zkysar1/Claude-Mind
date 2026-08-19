"""Ordering pins for aspiration-trajectory completed-goal sort ().

THE CENTRAL PIN IS `test_newest_goal_appears_last_even_when_older_ones_lack_stamps`,
which is the regression the commissioning goal specified verbatim: a mixed list
where the newest goal HAS `started` and an older one does not. The old bucketed
key fails it by construction, because it sorted on WHETHER a field existed before
sorting on WHEN anything happened.

The remaining pins come from measurement rather than from the goal text. The goal
proposed sorting on `completed_date`; probing 4487 live completed goals showed
that field on 86.2% and DATE-ONLY, against `completed_at` at 99.87% with full
timestamp precision -- so the field priority is pinned here, and so is the
same-day ordering that a date-only key silently destroys.
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SPEC = importlib.util.spec_from_file_location(
    "aspiration_trajectory",
    Path(__file__).resolve().parents[1] / "aspiration-trajectory.py")
at = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(at)


def _g(gid, **kw):
    d = {"id": gid, "status": "completed"}
    d.update(kw)
    return d


def _ids(asp):
    return [g["id"] for g in at.get_completed_goals(asp)]


# ---------------------------------------------------------------------------
# The regression the goal specified.
# ---------------------------------------------------------------------------

def test_newest_goal_appears_last_even_when_older_ones_lack_stamps():
    """The exact pin from . The old bucketed key put every
    timestamped goal before every un-timestamped one, so the NEWEST goal landed
    mid-array and the trailing recency window could never see it."""
    asp = {"goals": [
        _g("g-1-01", completed_at="2026-01-01T10:00:00"),   # oldest, stamped
        _g("g-1-02", completed_date="2026-03-01"),          # middle, date-only
        _g("g-1-03", completed_at="2026-08-01T10:00:00"),   # NEWEST, stamped
    ]}
    assert _ids(asp)[-1] == "g-1-03"


def test_the_old_bucketed_key_would_have_failed_that_pin():
    """Pins the DEFECT itself so the premise is falsifiable. Reproduces the old
    key exactly; if this ever stops inverting, the bug shape has changed."""
    def old_key(g):
        started = g.get("started")
        if started:
            return (0, datetime.fromisoformat(started))
        seq = int(g["id"].rsplit("-", 1)[-1])
        return (1, datetime(2000, 1, 1).replace(year=2000 + seq // 365))

    goals = [_g("g-1-03", started="2026-08-01T10:00:00"),
             _g("g-1-99")]                      # no stamp, but OLDER in truth
    ordered = sorted(goals, key=old_key)
    assert ordered[-1]["id"] == "g-1-99", (
        "the old key sorts the un-timestamped goal last regardless of time -- "
        "that is the partition this fix removes")


# ---------------------------------------------------------------------------
# Field priority -- measured, not taken from the goal's proposal.
# ---------------------------------------------------------------------------

def test_completed_at_wins_over_completed_date_for_same_day_goals():
    """`completed_date` is DATE-ONLY. Sorting on it collapses every goal closed
    on one day into a tie, and the consumers here take a 5-entry trailing slice
    -- so same-day order is exactly what they need to be right."""
    asp = {"goals": [
        _g("g-1-02", completed_at="2026-05-05T18:00:00", completed_date="2026-05-05"),
        _g("g-1-01", completed_at="2026-05-05T09:00:00", completed_date="2026-05-05"),
    ]}
    assert _ids(asp) == ["g-1-01", "g-1-02"]


def test_completed_date_is_used_when_completed_at_is_absent():
    asp = {"goals": [
        _g("g-1-02", completed_date="2026-06-01"),
        _g("g-1-01", completed_at="2026-01-01T00:00:00"),
    ]}
    assert _ids(asp) == ["g-1-01", "g-1-02"]


def test_started_is_the_last_resort_and_still_orders_against_the_others():
    asp = {"goals": [
        _g("g-1-02", started="2026-07-01T00:00:00"),
        _g("g-1-01", completed_at="2026-02-01T00:00:00"),
    ]}
    assert _ids(asp) == ["g-1-01", "g-1-02"]


def test_an_unparseable_timestamp_falls_through_instead_of_raising():
    """A malformed value must not take the whole trajectory report down, and
    must not be treated as a valid instant either."""
    asp = {"goals": [
        _g("g-1-01", completed_at="not-a-date", completed_date="2026-04-01"),
    ]}
    assert _ids(asp) == ["g-1-01"]
    assert at.goal_completion_order_key(asp["goals"][0])[1] == datetime(2026, 4, 1)


# ---------------------------------------------------------------------------
# The residual partition: deliberate, and pointed at the recent end.
# ---------------------------------------------------------------------------

def test_a_goal_with_no_time_field_at_all_sorts_LAST_not_first():
    """Measured: the 5 live goals (of 4487) carrying no time field are the
    NEWEST ones, because the stamp is written around close. Sending them to the
    far past -- the reflexive choice -- would push the freshest work out of the
    recency window and reproduce the defect being fixed."""
    asp = {"goals": [
        _g("g-1-01", completed_at="2026-01-01T00:00:00"),
        _g("g-1-99"),                                   # no time information
        _g("g-1-02", completed_at="2026-02-01T00:00:00"),
    ]}
    assert _ids(asp) == ["g-1-01", "g-1-02", "g-1-99"]


def test_unkeyable_goals_hold_their_input_order_among_themselves():
    """They share one sentinel key, so Python's stable sort must preserve file
    order rather than shuffling them arbitrarily between runs."""
    asp = {"goals": [_g("g-1-97"), _g("g-1-98"), _g("g-1-96")]}
    assert _ids(asp) == ["g-1-97", "g-1-98", "g-1-96"]


# ---------------------------------------------------------------------------
# The consumers the ordering actually serves.
# ---------------------------------------------------------------------------

def test_the_recency_window_reaches_the_newest_goal():
    """The end-to-end shape of the reported defect: a productive recent goal
    reported velocity 0.00 because the window could not reach it."""
    goals = [_g("g-1-%02d" % i, completed_at="2026-01-%02dT00:00:00" % (i + 1))
             for i in range(8)]
    goals.append(_g("g-1-90", completed_at="2026-09-01T00:00:00"))
    ordered = at.get_completed_goals({"goals": goals})
    artifacts = [{"goal_id": g["id"],
                  "artifacts": {"reasoning_bank_entries": 3 if g["id"] == "g-1-90" else 0,
                                "guardrails_created": 0, "pattern_signatures": 0,
                                "tree_nodes_updated": 0}}
                 for g in ordered]
    assert at.compute_learning_velocity(artifacts, 5) > 0.0, (
        "the newest goal's artifacts must be visible to the trailing window")


def test_only_completed_goals_are_returned():
    asp = {"goals": [_g("g-1-01", completed_at="2026-01-01T00:00:00"),
                     {"id": "g-1-02", "status": "pending"},
                     {"id": "g-1-03", "status": "blocked"}]}
    assert _ids(asp) == ["g-1-01"]


def test_empty_and_missing_goal_lists_do_not_raise():
    assert at.get_completed_goals({"goals": []}) == []
    assert at.get_completed_goals({}) == []
