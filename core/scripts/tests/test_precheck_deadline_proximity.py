#!/usr/bin/env python3
"""test_precheck_deadline_proximity.py — precheck-eval.py cmd_deadline_proximity
contract (g-115-3701).

Pins the iteration-header surface that makes an aspiration-level `deadline`
visible during precheck, ahead of Phase 1 SELECT. The check is REPORT-ONLY: it
never mutates, never gates, and deliberately emits NO flags.

Three properties carry the design and each has a dedicated pin, because each
was a deliberate refusal that a later reader could plausibly "fix":

  1. NO FLAGS, EVER (test_near_deadline_still_emits_no_flags). A `deadline_near`
     flag would re-present identically on every iteration for the whole week
     before a deadline with no discharge path — the guard-4794 shape. `near`
     rides in the payload instead so a consumer can act without re-deriving.
  2. COVERAGE IS UNCONDITIONAL (test_coverage_reported_when_none_carry_one).
     A bare "asp-X 43d" line reads as complete deadline coverage; printing
     "1 of 25" keeps "checked one" distinguishable from "checked all"
     (guard-963 partial-coverage corollary).
  3. THE CALL SITE EXISTS (test_registered_in_both_registries). cmd_run_all
     iterates SUBCMDS, not DISPATCH, so a DISPATCH-only entry would give this
     a CLI name while it never fired in the precheck — the g-115-5890
     "sweep with no call site" defect. Pinned so a refactor cannot drop it
     silently.
"""

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location("precheck_eval", SCRIPT_DIR / "precheck-eval.py")
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)

CONFIG = {}


class _Args:
    pass


def _asp(asp_id, status="active", deadline=None, title="t"):
    a = {"id": asp_id, "status": status, "title": title, "goals": []}
    if deadline is not None:
        a["deadline"] = deadline
    return a


def _iso_in(days):
    return (date.today() + timedelta(days=days)).isoformat()


def _run(aspirations):
    return pe.cmd_deadline_proximity(_Args(), CONFIG, {"aspirations": aspirations})


# ── the happy path ────────────────────────────────────────────────────────

def test_future_deadline_is_clocked_with_days_remaining():
    res = _run([_asp("asp-1", deadline=_iso_in(43)), _asp("asp-2")])
    assert res["subcommand"] == "deadline-proximity"
    assert len(res["clocked"]) == 1
    assert res["clocked"][0]["asp_id"] == "asp-1"
    assert res["clocked"][0]["days_remaining"] == 43
    assert "asp-1 43d" in res["summary"]


def test_past_deadline_reports_negative_days():
    res = _run([_asp("asp-1", deadline=_iso_in(-5))])
    assert res["clocked"][0]["days_remaining"] == -5


def test_nearest_deadline_sorts_first():
    res = _run([
        _asp("asp-far", deadline=_iso_in(90)),
        _asp("asp-near", deadline=_iso_in(2)),
        _asp("asp-mid", deadline=_iso_in(30)),
    ])
    assert [e["asp_id"] for e in res["clocked"]] == ["asp-near", "asp-mid", "asp-far"]


# ── property 1: no flags, ever ────────────────────────────────────────────

def test_near_deadline_still_emits_no_flags():
    """A deadline inside the warn window populates `near` but NEVER flags.

    This is the guard-4794 refusal. If a future change adds a flag here it
    must delete this test deliberately, not discover it as a surprise.
    """
    res = _run([_asp("asp-1", deadline=_iso_in(3))])
    assert res["flags"] == []
    assert len(res["near"]) == 1
    assert res["near"][0]["asp_id"] == "asp-1"


def test_far_deadline_is_not_near():
    res = _run([_asp("asp-1", deadline=_iso_in(_far := 60))])
    assert res["flags"] == []
    assert res["near"] == []
    assert _far > pe._DEADLINE_WARN_DAYS


def test_warn_window_matches_goal_selector_urgency_tier():
    """The header's notion of "near" must not drift from the scorer's ramp.

    goal-selector.py's deadline_urgency gives its last SHORT-horizon step at
    <=7d before falling to the long-horizon ramp; this surface explains that
    ranking rather than inventing a second one.
    """
    assert pe._DEADLINE_WARN_DAYS == 7
    assert _run([_asp("a", deadline=_iso_in(7))])["near"] != []
    assert _run([_asp("a", deadline=_iso_in(8))])["near"] == []


# ── property 2: coverage is unconditional ─────────────────────────────────

def test_coverage_reported_when_none_carry_one():
    res = _run([_asp("asp-1"), _asp("asp-2"), _asp("asp-3")])
    assert res["clocked"] == []
    assert res["coverage"] == {"clocked": 0, "active": 3, "uncovered": 3}
    assert "none" in res["summary"]
    assert "0 of 3" in res["summary"]


def test_coverage_counts_only_active_aspirations():
    res = _run([
        _asp("asp-live", deadline=_iso_in(10)),
        _asp("asp-done", status="completed", deadline=_iso_in(1)),
        _asp("asp-plain"),
    ])
    assert [e["asp_id"] for e in res["clocked"]] == ["asp-live"]
    assert res["coverage"]["active"] == 2  # the completed one is not counted
    assert res["coverage"]["uncovered"] == 1


def test_partial_coverage_is_visible_in_the_summary():
    """The guard-963 pin: one clocked aspiration among many must not read as
    complete coverage."""
    asps = [_asp("asp-1", deadline=_iso_in(43))] + [_asp(f"asp-{i}") for i in range(2, 26)]
    res = _run(asps)
    assert "coverage 1 of 25" in res["summary"]


# ── property 3: malformed dates are surfaced, not swallowed ───────────────

def test_unparseable_deadline_is_reported_not_dropped():
    res = _run([_asp("asp-bad", deadline="Q4 2026")])
    assert res["clocked"] == []
    assert res["malformed"] == [{"asp_id": "asp-bad", "deadline": "Q4 2026"}]
    assert "UNPARSEABLE" in res["summary"]
    assert "asp-bad" in res["summary"]
    # still counted as covered — the field IS declared, it is just unreadable
    assert res["coverage"]["clocked"] == 1


def test_empty_string_deadline_is_treated_as_absent():
    """fromisoformat("") raises; an empty field is ABSENT, not malformed —
    the same empty-date trap cmd_temp_pressure documents at its stall_age_h."""
    res = _run([_asp("asp-1", deadline="")])
    assert res["clocked"] == []
    assert res["malformed"] == []
    assert res["coverage"]["clocked"] == 0


def test_days_until_helper_contract():
    assert pe._days_until(None) is None
    assert pe._days_until("") is None
    assert pe._days_until("not-a-date") is None
    assert pe._days_until(_iso_in(0)) == 0


# ── property 3: the call site exists ──────────────────────────────────────

def test_registered_in_both_registries():
    assert "deadline-proximity" in dict(pe.SUBCMDS), (
        "cmd_run_all iterates SUBCMDS — a missing entry means the check has a "
        "CLI name but never fires in the precheck (g-115-5890)"
    )
    assert pe.DISPATCH.get("deadline-proximity") is pe.cmd_deadline_proximity


def test_run_all_includes_it_and_adds_no_flags():
    """Wiring pin: it reaches run-all, and it contributes nothing to the flag
    list, so the existing sweep's behaviour is unchanged."""
    compact = {"aspirations": [_asp("asp-1", deadline=_iso_in(2))]}
    sub = dict(pe.SUBCMDS)["deadline-proximity"]
    r = sub(_Args(), CONFIG, compact)
    assert r["flags"] == []
    assert r["summary"].startswith("deadline-proximity:")


def test_run_all_top_line_carries_the_nearest_clock():
    """The clock must reach run-all's TOP-LINE summary, not just `results`.

    Phase 0.5.0's action table keys every documented action on a flags[] entry
    and never directs a reader to a subcommand's summary, so a no-flag check
    buried in `results` is computed-and-never-read. This pin is the difference
    between a visibility feature and a dead one.
    """
    compact = {"aspirations": [
        _asp("asp-near", deadline=_iso_in(5)),
        _asp("asp-far", deadline=_iso_in(80)),
        _asp("asp-plain"),
    ]}

    class _A:
        apply = False

    res = pe.cmd_run_all(_A(), {}, compact)
    assert "deadline: asp-near 5d" in res["summary"], res["summary"]
    # the FAR one must not crowd the line — nearest only
    assert "asp-far" not in res["summary"]


def test_run_all_top_line_silent_without_any_deadline():
    """No deadline anywhere => the top line is untouched."""
    class _A:
        apply = False

    res = pe.cmd_run_all(_A(), {}, {"aspirations": [_asp("asp-1"), _asp("asp-2")]})
    assert "deadline:" not in res["summary"]


def test_no_aspirations_at_all_is_safe():
    res = _run([])
    assert res["coverage"] == {"clocked": 0, "active": 0, "uncovered": 0}
    assert res["flags"] == []
