"""test_goal_selector_saturation_waiver.py —  (2026-08-30).

Pins 13b-iii of goal-selector.score_goal: per_goal_saturation is WAIVED when the
repeat was explicitly asked for (a live pull_signal, or a directive naming the
goal).

THE DEFECT. pull_boost (+4.0) and per_goal_saturation (raw -5.0 x weight 0.8 =
-4.0) were sized INDEPENDENTLY, each against a different reference, and cancel
exactly. Neither config block references the other. Measured 2026-08-30 on the
live store: g-306-284 -- pull_boost's own HARDCODED carrier_consumer_goal --
scored 11.25 at RANK 370 of 1392 WITH the boost fully applied, because it had
just fired.

They are ANTI-CORRELATED BY CONSTRUCTION, which is what makes it pathological
rather than unlucky: the consumer is a RECURRING drain goal, so it is saturated
precisely when it has just run, and the pull signal exists to make it run AGAIN
the moment a fresh carrier lands. The signal is strongest exactly when the
penalty is maximal.

WHY THE ORDERING TESTS COME IN PAIRS. The invariant "a fresh USER directive
outranks a machine-set pull" is the thing this change could plausibly break, and
the PRE-EXISTING test of it asserts only
``load_pull_boost_config()["boost"] <= 4.5`` -- a CONFIG VALUE. That cannot see an
inversion introduced by a sibling term, which is exactly the shape of this
change. So the invariant is measured END-TO-END here, at two weights, each with a
positive control that shows the assertion is not vacuous:

  * at WEIGHTS["directive_boost"] = 3.0 (what this box runs) the pull-ONLY
    waiver ALSO preserves the ordering -- so a test at 3.0 alone would pass
    whether or not the directive half exists, and would prove nothing about it.
  * at 1.5 -- the value three comments in goal-selector.py state, sourced from
    meta/goal-selection-strategy.yaml -- the pull-only waiver INVERTS it. That
    is what the directive half buys, and test_pull_only_waiver_inverts_at_1_5
    is the control proving the claim is real rather than asserted.

Pattern mirrors test_goal_selector_pull_boost.py: spec_from_file_location load of
the hyphen-named module, MIND_AGENT captured/restored around import, pure
in-memory dicts, no subprocess, no daemon.
"""
from __future__ import annotations

import copy
import importlib.util
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

_KEEP = os.environ.get("MIND_AGENT")
_spec = importlib.util.spec_from_file_location(
    "gs_saturation_waiver", str(CORE_SCRIPTS / "goal-selector.py"))
gs = importlib.util.module_from_spec(_spec)
sys.modules["gs_saturation_waiver"] = gs
_spec.loader.exec_module(gs)
if _KEEP is not None:
    os.environ["MIND_AGENT"] = _KEEP


def _sig(hours_ago=0.5):
    return {"set_at": (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
            "by": "alpha/cc-08", "reason": "carrier ref, 1 framework file"}


def _goal(gid="g-306-284", *, pull=False):
    return {"id": gid, "goal_id": gid, "title": "Recurring: disposition worker carrier refs",
            "priority": "MEDIUM", "status": "pending", "recurring": True,
            "interval_hours": 7.81, "category": "framework",
            "pull_signal": _sig() if pull else None}


def _asp():
    return {"id": "asp-306", "priority": "HIGH", "status": "active", "progress": {}}


def _score(gid="g-306-284", *, pull=False, saturated=False, directives=(), seed=1234):
    gs._ACTIVE_DIRECTIVES = list(directives)
    cand = {"goal": _goal(gid, pull=pull), "aspiration": _asp(), "source": "world"}
    completions = [{"goal_id": gid}] if saturated else []
    random.seed(seed)
    return gs.score_goal(cand, {}, [], completions)


DIRECTIVE = {"target_goals": ["g-DIRECTIVE"], "target_categories": [], "weight": 3.0}


# --- the defect ------------------------------------------------------------

def test_saturated_pull_consumer_is_waived():
    """The designed use case: the carrier landed, so the repeat is WANTED."""
    r = _score(pull=True, saturated=True)
    assert (r["breakdown"] or {}).get("per_goal_saturation") == 0.0
    assert r["per_goal_saturation_waived"] == gs.PER_GOAL_SATURATION_CONFIG["suppress_penalty"]


def test_waiver_restores_exactly_the_cancelled_amount():
    """+4.0 back, which is the whole arithmetic of the defect."""
    without = _score(pull=True, saturated=False)["score"]
    with_sat = _score(pull=True, saturated=True)["score"]
    assert round(with_sat - without, 2) == 0.0, (
        "a saturated pulled goal must score the same as an unsaturated one -- "
        "the penalty is waived, not merely reduced")


# --- controls: the penalty must still fire when nobody asked ---------------

def test_saturated_without_demand_keeps_the_penalty():
    """No pull, no directive: the rb-390 rapid-repeat penalty is untouched."""
    r = _score(pull=False, saturated=True)
    raw_pen = gs.PER_GOAL_SATURATION_CONFIG["suppress_penalty"]
    assert (r["breakdown"] or {}).get("per_goal_saturation") == round(
        raw_pen * gs.WEIGHTS["per_goal_saturation"], 2)
    assert r["per_goal_saturation_waived"] is None


def test_unsaturated_is_untouched_with_and_without_a_pull():
    for pull in (False, True):
        r = _score(pull=pull, saturated=False)
        assert (r["breakdown"] or {}).get("per_goal_saturation") == 0.0
        assert r["per_goal_saturation_waived"] is None


def test_aged_out_pull_signal_does_not_waive():
    """The safety valve composes: a signal past max_age_hours is not demand."""
    gs._ACTIVE_DIRECTIVES = []
    g = _goal(pull=True)
    g["pull_signal"] = _sig(hours_ago=1000)
    cand = {"goal": g, "aspiration": _asp(), "source": "world"}
    random.seed(1234)
    r = gs.score_goal(cand, {}, [], [{"goal_id": g["id"]}])
    assert r["per_goal_saturation_waived"] is None, (
        "an aged-out signal must not waive -- otherwise a lost CLEAR pins the "
        "goal's saturation off forever, the failure the age valve exists for")


def test_malformed_pull_signal_does_not_waive_and_does_not_raise():
    gs._ACTIVE_DIRECTIVES = []
    for bad in ({}, {"set_at": "not-a-timestamp"}, {"set_at": None}, "a string", 7):
        g = _goal()
        g["pull_signal"] = bad
        cand = {"goal": g, "aspiration": _asp(), "source": "world"}
        random.seed(1234)
        r = gs.score_goal(cand, {}, [], [{"goal_id": g["id"]}])
        assert r["per_goal_saturation_waived"] is None, bad


# --- the directive half ----------------------------------------------------

def test_directive_naming_the_goal_waives():
    r = _score("g-DIRECTIVE", pull=False, saturated=True, directives=[DIRECTIVE])
    assert r["per_goal_saturation_waived"] is not None


def test_strategic_focus_alone_does_not_waive():
    """13b-iii keys on the DIRECTIVE addend, never the composite.

    A goal merely sitting inside a live strategic_focus LANE has not been named
    by anyone, and lane-wide waiving is far broader than this change intends.
    """
    original = gs.strategic_focus_boost
    gs.strategic_focus_boost = lambda *a, **k: 3.0
    try:
        r = _score(pull=False, saturated=True, directives=[])
        assert (r["breakdown"] or {}).get("directive_boost"), "fixture inert: no sf boost applied"
        assert r["per_goal_saturation_waived"] is None, (
            "strategic focus is a LANE signal, not a request for THIS goal")
    finally:
        gs.strategic_focus_boost = original


# --- the ordering invariant, end-to-end, at BOTH weights -------------------

def _ordering_pair(weight):
    """Return (directive_score, pull_score) with both goals saturated."""
    original_w = gs.WEIGHTS["directive_boost"]
    original_sf = gs.strategic_focus_boost
    gs.WEIGHTS["directive_boost"] = weight
    gs.strategic_focus_boost = lambda *a, **k: 0.0   # isolate the directive addend
    try:
        d = _score("g-DIRECTIVE", pull=False, saturated=True, directives=[DIRECTIVE])
        p = _score("g-PULL", pull=True, saturated=True, directives=[DIRECTIVE])
        entry = dict(p)
        entry.setdefault("breakdown", {})
        entry.setdefault("raw", {})
        gs.apply_pull_boost([entry], gs.PULL_CONFIG)   # production applies the post-pass
        return d["score"], entry["score"], d, p
    finally:
        gs.WEIGHTS["directive_boost"] = original_w
        gs.strategic_focus_boost = original_sf


def test_directive_outranks_pull_under_saturation_at_runtime_weight():
    d, p, _, _ = _ordering_pair(gs.WEIGHTS["directive_boost"])
    assert d > p, f"directive {d} must outrank pull {p} under saturation"


def test_directive_outranks_pull_under_saturation_at_documented_weight_1_5():
    """1.5 is the weight three comments in goal-selector.py state.

    This is the case the directive half of the waiver exists for.
    """
    d, p, _, _ = _ordering_pair(1.5)
    assert d > p, f"directive {d} must outrank pull {p} under saturation at weight 1.5"


def test_pull_only_waiver_inverts_at_1_5():
    """POSITIVE CONTROL for the test directly above -- it must not pass vacuously.

    Simulates the NARROWER change (waive for the pull only) by adding the
    directive's waived penalty back, and shows the ordering INVERTS at 1.5. If
    this ever stops inverting, the test above is no longer evidence for anything
    and the directive half can be reconsidered on measurement rather than belief.
    """
    d, p, d_rec, _ = _ordering_pair(1.5)
    waived = d_rec["per_goal_saturation_waived"]
    assert waived is not None, "fixture inert: the directive goal was not waived"
    d_pull_only = round(d + waived * gs.WEIGHTS["per_goal_saturation"], 2)
    assert d_pull_only < p, (
        "expected the pull-only variant to INVERT the ordering at weight 1.5 "
        f"(directive {d_pull_only} vs pull {p}); it did not, so "
        "test_directive_outranks_pull_under_saturation_at_documented_weight_1_5 "
        "no longer proves the directive half is load-bearing")


def test_runtime_weight_alone_would_not_prove_the_directive_half():
    """The other half of the honesty check: at 3.0 the narrow change also holds.

    Pins the measured fact that motivated testing at two weights instead of one.
    """
    d, p, d_rec, _ = _ordering_pair(3.0)
    waived = d_rec["per_goal_saturation_waived"]
    d_pull_only = round(d + waived * gs.WEIGHTS["per_goal_saturation"], 2)
    assert d_pull_only > p, (
        "at weight 3.0 the pull-only waiver was measured to preserve the "
        "ordering; if that changed, the two-weight rationale needs re-deriving")


# --- telemetry -------------------------------------------------------------

def test_waived_amount_is_visible_not_silently_zeroed():
    r = _score(pull=True, saturated=True)
    assert "per_goal_saturation_waived" in r
    assert r["per_goal_saturation_waived"] < 0


def test_waiver_field_is_not_a_scoring_criterion():
    """KNOWN_CRITERIA is a manifest of things that GET WEIGHTS. Telemetry rides
    out as a top-level field, never in raw -- same posture as
    class_balance_penalty_waived."""
    r = _score(pull=True, saturated=True)
    assert "per_goal_saturation_waived" not in (r.get("raw") or {})
    assert "per_goal_saturation_waived" not in gs.KNOWN_CRITERIA
