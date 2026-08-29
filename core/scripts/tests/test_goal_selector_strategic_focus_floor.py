"""test_goal_selector_strategic_focus_floor.py --  regression.

Guards the STRATEGIC-FOCUS FLOOR and its inertness banner in goal-selector.

The floor is the implementation half of the standing user directive: the
per-goal `strategic_focus_boost` scalar (+1.5 final) cannot close the measured
deficit from a directive-lane row to the top of the pool (4.41 on cc-07,
2026-08-29, 1329 candidates, against an exploration_noise width of 1.22), and
guard-1895 rule (2) says an intervention sized below the contested band changes
almost nothing WHILE LOOKING LIKE A FIX. So the floor REORDERS one slot instead
of trying to win the lottery -- the remedy guard-1895's own corollary names.

The inertness banner is the other half, and it is the one that would have
prevented this goal's own false premise. `emit_strategic_focus_banner` returns
[] in SILENCE when the directive's lanes contribute no eligible candidate, which
is indistinguishable from "the directive is being honored". Measured the same
day: directive_boost was 0.00 on all 1329 candidates -- not outweighed, but
applied to nothing, because every lane goal was filtered upstream
(routed_to_agent 46, hypothesis_gate 12, deferred 2 of 68).

Module-load pattern mirrors test_goal_selector_allblocked_reread.py: capture and
restore MIND_AGENT around the module-level import.
"""

from __future__ import annotations

import importlib
import io
import os
import sys
from contextlib import redirect_stderr
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


LANES = {"asp-368", "asp-369"}


def _pin_lanes(monkeypatch, lanes=LANES, weight=1.0):
    """Pin load_strategic_focus so the tests never read live team-state."""
    monkeypatch.setattr(
        gs, "load_strategic_focus",
        lambda: {"aspirations": set(lanes), "weight": weight})


def _row(gid, asp, score, *, recurring=False, ia="either", routed=False):
    return {
        "goal_id": gid, "aspiration_id": asp, "score": score,
        "recurring": recurring, "intended_agent": ia, "routed_to_me": routed,
        "title": f"title for {gid}",
    }


# --------------------------------------------------------------------------
# The floor fires
# --------------------------------------------------------------------------

def test_floor_hoists_lane_goal_over_higher_scoring_framework_goal(monkeypatch):
    """The whole point: a lane goal ranked LAST takes the top slot."""
    _pin_lanes(monkeypatch)
    scored = [
        _row("g-115-1", "asp-115", 11.6),
        _row("g-115-2", "asp-115", 10.4),
        _row("g-368-9", "asp-368", 6.27),   # bottom of the pool
    ]
    picked, status = gs.apply_strategic_focus_floor(scored, "alpha")
    assert picked is not None
    assert picked["goal_id"] == "g-368-9"
    assert scored[0]["goal_id"] == "g-368-9"
    assert scored[0].get("strategic_focus_pick") is True
    assert status["picked"] == "g-368-9"
    assert status["claimable"] == 1


def test_floor_never_rewrites_scores(monkeypatch):
    """Like apply_drain_lane, the floor REORDERS and never rescores -- so every
    non-floor pick stays byte-identical to pre-floor behavior."""
    _pin_lanes(monkeypatch)
    scored = [
        _row("g-115-1", "asp-115", 11.6),
        _row("g-368-9", "asp-368", 6.27),
    ]
    before = {r["goal_id"]: r["score"] for r in scored}
    gs.apply_strategic_focus_floor(scored, "alpha")
    after = {r["goal_id"]: r["score"] for r in scored}
    assert before == after


def test_floor_picks_highest_scoring_lane_row(monkeypatch):
    _pin_lanes(monkeypatch)
    scored = [
        _row("g-115-1", "asp-115", 11.6),
        _row("g-369-5", "asp-369", 7.9),
        _row("g-368-9", "asp-368", 6.27),
    ]
    picked, _ = gs.apply_strategic_focus_floor(scored, "alpha")
    assert picked["goal_id"] == "g-369-5"


# --------------------------------------------------------------------------
# The floor correctly declines -- each of these is a distinct no-op path
# --------------------------------------------------------------------------

def test_floor_yields_to_a_live_drain_lane_pick(monkeypatch):
    """guard-2331 direction B: displacing a lane pick re-starves what it just
    rescued, and the drain lane cannot retry for another full K-cycle."""
    _pin_lanes(monkeypatch)
    scored = [
        _row("g-001-8", "asp-001", 9.0, recurring=True),
        _row("g-368-9", "asp-368", 6.27),
    ]
    picked, status = gs.apply_strategic_focus_floor(
        scored, "alpha", drain_lane_fired=True)
    assert picked is None
    assert status["yielded_to_drain_lane"] is True
    assert scored[0]["goal_id"] == "g-001-8"   # lane pick undisturbed


def test_floor_noop_when_top_eligible_pick_is_already_lane_work(monkeypatch):
    _pin_lanes(monkeypatch)
    scored = [
        _row("g-368-1", "asp-368", 12.0),
        _row("g-115-1", "asp-115", 11.6),
    ]
    picked, status = gs.apply_strategic_focus_floor(scored, "alpha")
    assert picked is None
    assert status["picked"] is None
    assert scored[0]["goal_id"] == "g-368-1"


def test_floor_does_not_nominate_a_recurring_lane_goal(monkeypatch):
    """Clause (ii), : swapping a routine sweep for a routine sweep
    cannot honor a directive whose premise is that product work outranks
    sweeps."""
    _pin_lanes(monkeypatch)
    scored = [
        _row("g-115-1", "asp-115", 11.6),
        _row("g-369-14", "asp-369", 8.0, recurring=True),
    ]
    picked, status = gs.apply_strategic_focus_floor(scored, "alpha")
    assert picked is None
    assert status["claimable"] == 0
    assert status["pool_lane_rows"] == 1   # seen, but not a nominee


def test_floor_does_not_nominate_work_routed_to_another_agent(monkeypatch):
    """The load-bearing pool constraint. Measured 2026-08-29: 46 of 68
    executable lane goals were routed to other agents, and a domain guardrail forbids
    running that host-bound GUI-tool work off-host. A floor reading the QUEUE rather
    than the post-filter pool would promote exactly those."""
    _pin_lanes(monkeypatch)
    scored = [
        _row("g-115-1", "asp-115", 11.6),
        _row("g-368-7", "asp-368", 9.0, ia="foxtrot"),
    ]
    picked, status = gs.apply_strategic_focus_floor(scored, "alpha")
    assert picked is None
    assert status["claimable"] == 0


def test_floor_nominates_a_goal_routed_to_me(monkeypatch):
    """Positive control for the predicate above: same shape, routed TO this
    agent, must fire. Without this, the routing test passes for a floor that
    never nominates anything at all."""
    _pin_lanes(monkeypatch)
    scored = [
        _row("g-115-1", "asp-115", 11.6),
        _row("g-368-7", "asp-368", 9.0, ia="alpha"),
    ]
    picked, _ = gs.apply_strategic_focus_floor(scored, "alpha")
    assert picked is not None and picked["goal_id"] == "g-368-7"


def test_floor_noop_without_a_directive(monkeypatch):
    _pin_lanes(monkeypatch, lanes=set())
    scored = [_row("g-115-1", "asp-115", 11.6), _row("g-368-9", "asp-368", 6.0)]
    picked, status = gs.apply_strategic_focus_floor(scored, "alpha")
    assert picked is None
    assert status["lanes"] == []
    assert scored[0]["goal_id"] == "g-115-1"


def test_floor_fails_open_on_unreadable_directive(monkeypatch):
    """Never raise, never block selection -- the ranked output every agent
    depends on must survive a broken directive."""
    def _boom():
        raise RuntimeError("team-state unreadable")
    monkeypatch.setattr(gs, "load_strategic_focus", _boom)
    scored = [_row("g-115-1", "asp-115", 11.6)]
    picked, status = gs.apply_strategic_focus_floor(scored, "alpha")
    assert picked is None
    assert status["picked"] is None


def test_floor_noop_on_empty_pool(monkeypatch):
    _pin_lanes(monkeypatch)
    picked, status = gs.apply_strategic_focus_floor([], "alpha")
    assert picked is None
    assert status["pool_lane_rows"] == 0


# --------------------------------------------------------------------------
# The inertness banner -- the half that was missing
# --------------------------------------------------------------------------

def test_inert_banner_fires_when_no_lane_goal_reached_the_pool():
    """The measured live state on cc-07, 2026-08-29: directive names four lanes,
    zero of their goals are in the pool, so directive_boost is 0.00 everywhere
    and the floor cannot fire -- however either is tuned."""
    status = {"lanes": ["asp-368", "asp-369"], "pool_lane_rows": 0,
              "claimable": 0, "picked": None}
    buf = io.StringIO()
    with redirect_stderr(buf):
        warns = gs.emit_strategic_focus_inert_banner(status)
    out = buf.getvalue()
    assert len(warns) == 1
    assert "STRATEGIC-FOCUS INERT" in out
    assert "asp-368" in out and "asp-369" in out
    # It must NOT let a reader conclude the lanes are drained -- that conflation
    # is the defect (six prior diagnoses read DRAINED as OUTRANKED).
    assert "NOT evidence the lanes are drained" in out
    # And it must name the pure-read diagnostic rather than inviting a re-run of
    # `select`, which mutates drain-lane state (guard-2331).
    assert "goal-selector.sh blocked" in out


def test_inert_banner_silent_when_lane_rows_are_present():
    """Positive control (guard-4166): a fix whose effect is that something STOPS
    appearing needs a case shown NOT flipping, or the assertion above passes for
    a banner that never fires at all."""
    status = {"lanes": ["asp-368"], "pool_lane_rows": 3,
              "claimable": 1, "picked": "g-368-9"}
    buf = io.StringIO()
    with redirect_stderr(buf):
        warns = gs.emit_strategic_focus_inert_banner(status)
    assert warns == []
    assert buf.getvalue() == ""


def test_inert_banner_silent_without_a_directive():
    buf = io.StringIO()
    with redirect_stderr(buf):
        warns = gs.emit_strategic_focus_inert_banner(
            {"lanes": [], "pool_lane_rows": 0})
    assert warns == []
    assert buf.getvalue() == ""


def test_floor_banner_states_the_pick_is_sanctioned():
    """The claim chokepoint accepts a floor pick without a deviation code only
    because write_scorer_verdict records scored[0] AFTER the hoist. The banner
    must say so, or an agent files a needless deviation -- or worse, reads the
    hoist as a scoring anomaly (guard-2331's own prescription for lane picks)."""
    buf = io.StringIO()
    picked = _row("g-368-9", "asp-368", 6.27)
    with redirect_stderr(buf):
        gs.emit_strategic_focus_floor_banner(picked, {"claimable": 2})
    out = buf.getvalue()
    assert "STRATEGIC-FOCUS FLOOR" in out
    assert "g-368-9" in out
    assert "without a deviation code" in out
    assert "Scores are UNCHANGED" in out


def test_floor_banner_silent_when_nothing_was_picked():
    buf = io.StringIO()
    with redirect_stderr(buf):
        gs.emit_strategic_focus_floor_banner(None, {"claimable": 0})
    assert buf.getvalue() == ""
