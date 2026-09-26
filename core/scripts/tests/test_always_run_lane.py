"""Tests for always-run-lane.py decide() ()."""
import datetime as dt
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "always_run_lane", os.path.join(_HERE, "..", "always-run-lane.py"))
arl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arl)

NOW = dt.datetime(2026, 9, 26, 12, 0, 0)


def goal(**kw):
    base = {"id": "g-900-01", "source": "world", "recurring": True, "status": "pending",
            "interval_hours": 36, "lastAchievedAt": "2026-09-21T12:00:00",
            "dispatch_lane": "always-run"}
    base.update(kw)
    return base


def test_overdue_goal_is_pinned():
    r = arl.decide([goal()], NOW, "alpha")
    assert r["pin"] == "g-900-01"
    assert r["overdue_x"] == round(120 / 36, 2)


def test_not_yet_due_is_not_pinned():
    r = arl.decide([goal(lastAchievedAt="2026-09-26T00:00:00")], NOW, "alpha")
    assert r["pin"] is None and r["due"] == []


def test_never_achieved_counts_as_due():
    r = arl.decide([goal(lastAchievedAt=None)], NOW, "alpha")
    assert r["pin"] == "g-900-01" and r["overdue_x"] == "never"


def test_owner_scoped_lane_pins_only_for_its_owner():
    g = goal(dispatch_lane="always-run:bravo")
    assert arl.decide([g], NOW, "bravo")["pin"] == "g-900-01"
    assert arl.decide([g], NOW, "alpha")["pin"] is None


def test_gated_states_are_never_pinned():
    for kw in ({"status": "in-progress"}, {"status": "blocked"},
               {"claimed_by": "zeta"}, {"defer_reason": "precondition_unmet: x"},
               {"recurring": False}, {"dispatch_lane": None},
               {"dispatch_lane": "always-run:"}, {"interval_hours": 0}):
        assert arl.decide([goal(**kw)], NOW, "alpha")["pin"] is None, kw


def test_own_claim_does_not_block_the_pin():
    assert arl.decide([goal(claimed_by="alpha")], NOW, "alpha")["pin"] == "g-900-01"


def test_most_overdue_wins_and_due_set_is_ordered():
    a = goal(id="g-900-01", lastAchievedAt="2026-09-24T12:00:00")  # 48h / 36h
    b = goal(id="g-900-02", lastAchievedAt="2026-09-20T12:00:00")  # 144h / 36h
    r = arl.decide([a, b], NOW, "alpha")
    assert r["pin"] == "g-900-02" and r["due"] == ["g-900-02", "g-900-01"]


def test_main_fails_open_without_an_agent(capsys):
    assert arl.main(["--agent", ""]) == 0
    out = capsys.readouterr().out
    assert '"pin": null' in out and '"error"' in out
