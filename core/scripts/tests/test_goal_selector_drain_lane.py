"""Unit tests for the bounded drain lane (; decision (b)).

The lane REORDERS the already-sorted candidate list so one genuinely-starved
recurring goal takes the top slot, at most once per K selector invocations. It
never writes `score` -- that is what makes acceptance bucket 3 (non-lane picks
byte-identical) true by construction rather than by measurement.

THE SCALE TEST IS THE POINT OF THIS FILE. recurring-starvation-check headlines
age/basis; goal-selector computes overdue_ratio = (age - interval)/interval,
exactly 1.0 LOWER. g-115-4118's acceptance criteria were authored from the
detector's numbers, so two of the five rows it named as past-exempt (g-115-151
at a headline 5.53x, g-115-1364 at 5.53->4.57 / 5.72->4.76 measured on cc-04
2026-07-30) are NOT eligible on the scale the predicate actually compares.
test_eligibility_uses_selector_scale_not_detector_scale pins that so a future
bucket can never again be authored from the wrong instrument without a red test
(guard-2004 third trap).

Fixture idiom mirrors test_goal_selector_idle_reallocation.py: pin MIND_AGENT
around import so module-level agent resolution is deterministic.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
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


CONFIG = {
    "drain_lane_enabled": True,
    "drain_lane_interval_iterations": 5,
    "substantive_demotion_overdue_exempt_ratio": 5.0,
    "substantive_demotion_short_interval_hours": 6.0,
    "substantive_demotion_short_interval_exempt_ratio": 1.0,
}


def cfg(**over):
    c = dict(CONFIG)
    c.update(over)
    return c


def row(gid, score, ratio=0.0, interval=24.0, recurring=True, asp="asp-115"):
    """A scored candidate in the shape cmd_select emits (fields read by the lane
    are top-level on the row: recurring, recurring_overdue_ratio,
    recurring_interval_hours -- same names apply_substantive_demotion reads)."""
    return {
        "goal_id": gid, "aspiration_id": asp, "score": score,
        "recurring": recurring, "recurring_overdue_ratio": ratio,
        "recurring_interval_hours": interval, "raw": {}, "breakdown": {},
    }


def primed(tmp_path, k=5):
    """An agent_dir whose counter is already at K-1, so the NEXT invocation is
    the one eligible to pick. Keeps the cadence out of tests that are not
    about cadence."""
    d = tmp_path / "agent"
    (d / "session").mkdir(parents=True, exist_ok=True)
    gs.write_drain_lane_state(d, {"invocations_since_pick": k - 1})
    return d


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------

def test_promotes_most_overdue_eligible_to_top(tmp_path):
    scored = [
        row("g-sub", 12.0, recurring=False),
        row("g-mild", 10.0, ratio=5.2),
        row("g-worst", 9.0, ratio=10.63),
        row("g-mid", 9.5, ratio=7.86),
    ]
    picked = gs.apply_drain_lane(scored, cfg(), primed(tmp_path))
    assert picked is not None
    assert picked["goal_id"] == "g-worst", "most overdue must win, not highest score"
    assert scored[0]["goal_id"] == "g-worst"
    assert scored[0].get("drain_lane_pick") is True


def test_tiebreak_is_deterministic(tmp_path):
    a = [row("g-115-9", 9.0, ratio=7.0), row("g-115-2", 9.0, ratio=7.0)]
    b = [row("g-115-2", 9.0, ratio=7.0), row("g-115-9", 9.0, ratio=7.0)]
    pa = gs.apply_drain_lane(a, cfg(), primed(tmp_path / "a"))
    pb = gs.apply_drain_lane(b, cfg(), primed(tmp_path / "b"))
    assert pa["goal_id"] == pb["goal_id"], "equal ratios must not reorder run-to-run"


# --------------------------------------------------------------------------
# The scale -- the finding this file exists to pin
# --------------------------------------------------------------------------

def test_eligibility_uses_selector_scale_not_detector_scale(tmp_path):
    """A row the DETECTOR calls 5.72x is 4.76 on the selector scale -> NOT
    eligible. Measured live on cc-04 2026-07-30 for g-115-1364, and corroborated
    by that row carrying raw.substantive_demotion_applied=True (it is being
    demoted, the direct observable for 'not exempt')."""
    scored = [row("g-115-1364", 10.73, ratio=4.76, interval=24.0)]
    assert gs.apply_drain_lane(scored, cfg(), primed(tmp_path)) is None
    assert "drain_lane_pick" not in scored[0]
    # And the detector's own headline number WOULD have been eligible -- which is
    # precisely the mistake this guards against.
    assert gs.overdue_exemption_level(5.72, 24.0, cfg()) >= 1.0
    assert gs.overdue_exemption_level(4.76, 24.0, cfg()) < 1.0


def test_optimistic_declared_interval_is_eligible_though_detector_ranks_it_low(tmp_path):
    """The divergence runs in BOTH directions, and this is the direction that
    HID a row from g-115-4118's bucket 1 entirely.

    recurring-starvation-check uses basis = max(declared_interval,
    recent_actual_p50); goal-selector keys on the DECLARED interval. When a
    goal's declared interval is optimistic relative to its demonstrated cadence,
    the detector's basis-suppression ranks it LOW while the selector ranks it
    very high. Live on cc-04 2026-07-30, g-001-05 'Run hippocampal replay':

        139.8h = 4.83x  basis 28.92h (recent_actual_p50)  [declared 10h -> 13.98x]

    4.83 is below the 5.0 cut that built bucket 1, so it was never listed -- yet
    (139.8-10)/10 = 12.98 on the selector scale makes it MORE overdue than every
    row that was. Keying the lane on the declared interval is deliberate: it is
    the same input overdue_exemption_level's other two consumers use, and
    changing it here would re-introduce the drift the shared predicate prevents.
    """
    scored = [row("g-001-05", 9.0, ratio=12.98, interval=10.0)]
    picked = gs.apply_drain_lane(scored, cfg(), primed(tmp_path))
    assert picked is not None and picked["goal_id"] == "g-001-05"
    # The detector's headline number for this same goal would NOT have qualified.
    assert gs.overdue_exemption_level(4.83, 10.0, cfg()) < 1.0


def test_short_interval_arm_still_grants_eligibility(tmp_path):
    """The predicate's second arm is reused unchanged: a monitor-class interval
    (<= 6h) is eligible well below the pure-ratio bar."""
    scored = [row("g-monitor", 8.0, ratio=1.5, interval=4.0)]
    picked = gs.apply_drain_lane(scored, cfg(), primed(tmp_path))
    assert picked is not None and picked["goal_id"] == "g-monitor"


def test_exempt_ratio_le_zero_grants_no_exemption(tmp_path):
    """Pinned decision for the <=0 edge (alpha finding msg-20260730-112436).

    overdue_exemption_level's pure-ratio arm is guarded by `if exempt_ratio > 0`,
    so at <=0 frac stays 0.0 and NOTHING is exempt -- the inverse of the
    pre-refactor `ratio >= exempt_ratio` test, which at 0 was unconditionally
    true. The docstring's claim of behavioral identity is false at this edge, and
    the edge is settable via config-overrides with no lower bound.

    NO-EXEMPTION is the semantics we choose and pin here, because it is the safe
    direction for THIS consumer: a misconfigured 0 disables the lane rather than
    making every recurring goal eligible, which is exactly the flood the
    anti-flood guard exists to prevent.
    """
    c = cfg(substantive_demotion_overdue_exempt_ratio=0.0,
            substantive_demotion_short_interval_hours=0.0)
    assert gs.overdue_exemption_level(99.0, 24.0, c) < 1.0
    scored = [row("g-huge", 9.0, ratio=99.0, interval=24.0)]
    assert gs.apply_drain_lane(scored, c, primed(tmp_path)) is None


def test_non_recurring_never_eligible(tmp_path):
    scored = [row("g-substantive", 9.0, ratio=50.0, recurring=False)]
    assert gs.apply_drain_lane(scored, cfg(), primed(tmp_path)) is None


# --------------------------------------------------------------------------
# Cadence + anti-flood (acceptance bucket 4)
# --------------------------------------------------------------------------

def test_k_cadence_holds_until_kth_invocation(tmp_path):
    d = tmp_path / "agent"
    (d / "session").mkdir(parents=True, exist_ok=True)
    picks = []
    for _ in range(12):
        scored = [row("g-sub", 12.0, recurring=False), row("g-starved", 9.0, ratio=10.0)]
        picks.append(gs.apply_drain_lane(scored, cfg(), d) is not None)
    # K=5 -> fires on invocations 5 and 10 only.
    assert picks == [False, False, False, False, True,
                     False, False, False, False, True,
                     False, False], picks
    assert sum(picks) == 2, "at most one pick per K invocations"


def test_counter_advances_even_when_nothing_is_eligible(tmp_path):
    """A quiet stretch must not stall the counter -- otherwise the lane would owe
    a K-length wait starting only from the first eligible row."""
    d = tmp_path / "agent"
    (d / "session").mkdir(parents=True, exist_ok=True)
    for _ in range(4):
        gs.apply_drain_lane([row("g-nope", 9.0, ratio=0.0)], cfg(), d)
    state = gs.read_drain_lane_state(d)
    assert state["invocations_since_pick"] == 4
    scored = [row("g-starved", 9.0, ratio=10.0)]
    assert gs.apply_drain_lane(scored, cfg(), d) is not None


# --------------------------------------------------------------------------
# Acceptance bucket 3: non-lane behavior byte-identical
# --------------------------------------------------------------------------

def test_never_mutates_scores(tmp_path):
    scored = [row("g-sub", 12.0, recurring=False), row("g-starved", 9.0, ratio=10.0)]
    before = {r["goal_id"]: r["score"] for r in scored}
    before_raw = json.dumps([r["raw"] for r in scored], sort_keys=True)
    before_bd = json.dumps([r["breakdown"] for r in scored], sort_keys=True)
    gs.apply_drain_lane(scored, cfg(), primed(tmp_path))
    assert {r["goal_id"]: r["score"] for r in scored} == before
    assert json.dumps([r["raw"] for r in scored], sort_keys=True) == before_raw
    assert json.dumps([r["breakdown"] for r in scored], sort_keys=True) == before_bd


def test_disabled_leaves_order_and_rows_untouched(tmp_path):
    scored = [row("g-sub", 12.0, recurring=False), row("g-starved", 9.0, ratio=10.0)]
    snapshot = json.dumps(scored, sort_keys=True)
    assert gs.apply_drain_lane(scored, cfg(drain_lane_enabled=False), primed(tmp_path)) is None
    assert json.dumps(scored, sort_keys=True) == snapshot


def test_k_non_positive_disables_rather_than_dividing(tmp_path):
    scored = [row("g-starved", 9.0, ratio=10.0)]
    snapshot = json.dumps(scored, sort_keys=True)
    assert gs.apply_drain_lane(scored, cfg(drain_lane_interval_iterations=0),
                               primed(tmp_path)) is None
    assert json.dumps(scored, sort_keys=True) == snapshot


def test_empty_candidate_list_is_safe(tmp_path):
    assert gs.apply_drain_lane([], cfg(), primed(tmp_path)) is None


# --------------------------------------------------------------------------
# Config wiring -- the allowlist trap
# --------------------------------------------------------------------------

def test_lane_knobs_survive_the_recurring_config_allowlist():
    """load_recurring_config iterates `defaults` as an ALLOWLIST, so a key in
    aspirations.yaml but absent from that dict is discarded with no parse error
    and no warning -- the K knob would silently read as its default forever."""
    c = gs.load_recurring_config()
    assert "drain_lane_enabled" in c
    assert "drain_lane_interval_iterations" in c
    assert int(c["drain_lane_interval_iterations"]) >= 1
