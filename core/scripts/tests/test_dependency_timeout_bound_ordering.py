""" — the dependency-timeout-check lane's two bounds must stay ordered.

THE DEFECT THIS PINS. `dependency-timeout-check` shells out to
`goal-selector.sh blocked`, an O(queue) re-score, under its own timeout. Its
production caller, `precheck-always-run-battery.py`, applied ONE uniform
``_LANE_TIMEOUT_S = 120`` to every lane while that inner call was allowed 180 s.
So the outer cap sat BELOW the inner window: on any box slow enough for the call
to exceed 120 s the lane was hard-killed with no payload and reported BLIND, and
the honest ``blocked_view_measured: false`` the module goes to real trouble to
emit (g-115-9447) was UNREACHABLE through the only caller that matters.

Read the asymmetry, because it is what makes this worth a test rather than a
comment: raising ONLY the inner bound — which is what the goal's own title
prescribes — could not have changed battery behaviour at all, and a reader who
made that change would have measured a green lane on a fast box and closed it.
guard-918 states the general invariant ("ensure the wrapper cap exceeds the
inner timeout so it never kills a call still inside its own window"); these
tests make it executable for this specific pair.

WHY NOT ASSERT THE LITERAL NUMBERS. The bound is O(queue) and box-dependent —
42.3 s on cc-03 against 740 blocked goals, >180 s on LAPTOP-3IOFCNEO on the SAME
shared queue (guard-3704: measure the population before rewriting; here the
population is shared world state, so the spread is box speed). Pinning 300/360
would make a legitimate retune fail. What must never regress is the ORDERING and
the SHARED KNOB.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

LANE = "dependency-timeout-check"


#: Plant the knob's ABSENCE. Passing this is not the same as passing nothing:
#: an env-derived import-time constant read without planting measures whatever
#: the launching shell happened to export, so a test named "at_default" would
#: silently assert about a box, not about the default (guard-2337). It still
#: PASSES either way — the ordering holds at any int — which is precisely why
#: the omission is invisible and has to be closed at the helper.
DEFAULT_ENV = {"DEP_BLOCKED_VIEW_TIMEOUT_S": None}


def _load(stem: str, env: dict | None = None):
    """Import a hyphenated script fresh under an explicitly planted env.

    Fresh every call: both modules read the knob at IMPORT time, so a cached
    module would silently test the previous environment. A fresh exec_module
    re-runs the module body, which re-reads os.environ — so unlike the cached
    import guard-2337 was written against, an in-process patch IS a working
    lever here and a child process is not required. Its RULE still binds: plant
    the precondition, never inherit it. A value of None plants ABSENCE.
    """
    path = SCRIPTS / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"_t_{stem.replace('-', '_')}", path)
    mod = importlib.util.module_from_spec(spec)
    old = {}
    if env:
        for k, v in env.items():
            old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    try:
        spec.loader.exec_module(mod)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


def _outer(battery) -> int:
    return battery._LANE_TIMEOUT_OVERRIDE_S[LANE]


def test_outer_cap_strictly_exceeds_inner_bound_at_default():
    """The  defect, stated as an assertion. FAILS against pre-fix HEAD."""
    battery = _load("precheck-always-run-battery", DEFAULT_ENV)
    lane = _load("dependency-timeout-check", DEFAULT_ENV)

    inner = lane.BLOCKED_VIEW_TIMEOUT_S
    outer = _outer(battery)
    assert outer > inner, (
        f"outer lane cap {outer}s does not exceed the inner goal-selector bound "
        f"{inner}s — the wrapper will kill a call still inside its own window, "
        "so the lane reports BLIND instead of an honest blocked_view_measured: "
        "false (guard-918, g-115-9897)"
    )


def test_both_sides_move_together_off_one_knob():
    """A shared knob is the point: two independent numbers re-create the inversion."""
    env = {"DEP_BLOCKED_VIEW_TIMEOUT_S": "900"}
    battery = _load("precheck-always-run-battery", env)
    lane = _load("dependency-timeout-check", env)

    assert lane.BLOCKED_VIEW_TIMEOUT_S == 900, (
        "the lane ignored DEP_BLOCKED_VIEW_TIMEOUT_S — the bound is still hardcoded"
    )
    assert _outer(battery) > 900, (
        f"the battery's cap for {LANE} ({_outer(battery)}s) did not follow the knob "
        "to 900s, so the two sides can drift back into an inversion"
    )


def test_inversion_is_detectable_not_merely_asserted():
    """Positive control (guard-1419): the assertion has teeth in BOTH directions.

    A test that only ever sees the healthy value cannot distinguish a real
    invariant from a vacuous one. Reconstruct the PRE-FIX pairing — outer 120,
    inner 180 — and confirm the same predicate rejects it.
    """
    pre_fix_outer, pre_fix_inner = 120, 180
    assert not (pre_fix_outer > pre_fix_inner), (
        "the historical 120/180 pairing must be REJECTED by this predicate; if it "
        "passes, the check is vacuous and would not have caught g-115-9897"
    )

    battery = _load("precheck-always-run-battery", DEFAULT_ENV)
    lane = _load("dependency-timeout-check", DEFAULT_ENV)
    assert _outer(battery) > lane.BLOCKED_VIEW_TIMEOUT_S


def test_other_lanes_keep_the_uniform_default():
    """Scope control: the override is per-lane, not a fleet-wide cap raise.

    A blanket raise would let ANY wedged lane hold the loop entry open for the
    longer window — the exact cost the uniform 120 s was chosen to bound.
    """
    battery = _load("precheck-always-run-battery", DEFAULT_ENV)
    assert battery._LANE_TIMEOUT_S == 120, (
        "the uniform default moved; this fix is meant to be a per-lane override"
    )
    overridden = set(battery._LANE_TIMEOUT_OVERRIDE_S)
    assert overridden == {LANE}, (
        f"unexpected lanes carry a timeout override: {sorted(overridden)} — each "
        "one needs its own measured justification"
    )
    names = {l["name"] for l in battery.LANES}
    assert overridden <= names, (
        f"override names an unknown lane: {sorted(overridden - names)} — a typo here "
        "is silent, because .get() falls back to the default"
    )


@pytest.mark.parametrize("knob", ["30", "300", "1200"])
def test_ordering_holds_across_the_knob_range(knob):
    """The invariant must be structural, not true only at the default."""
    env = {"DEP_BLOCKED_VIEW_TIMEOUT_S": knob}
    battery = _load("precheck-always-run-battery", env)
    lane = _load("dependency-timeout-check", env)
    assert _outer(battery) > lane.BLOCKED_VIEW_TIMEOUT_S


@pytest.mark.parametrize("bad", ["", "   ", "abc", "300s", "3.5"])
def test_a_malformed_knob_never_breaks_the_import(bad):
    """A tuning value must not be able to crash the loop-entry tier.

    Both constants are read at module TOP LEVEL, so a bare int() would make an
    empty or mistyped knob an IMPORT-time ValueError — and for the battery that
    import IS the always-run entry tier, so one bad character would take down
    every lane, not the one lane it tunes. That is the same shape g-115-9897
    fixed (an infrastructure fault rendering as something worse than the honest
    degraded reading), which is why it is pinned rather than left to review.
    """
    env = {"DEP_BLOCKED_VIEW_TIMEOUT_S": bad}
    lane = _load("dependency-timeout-check", env)
    battery = _load("precheck-always-run-battery", env)

    assert lane.BLOCKED_VIEW_TIMEOUT_S == 300
    assert _outer(battery) == 360
    assert _outer(battery) > lane.BLOCKED_VIEW_TIMEOUT_S


def test_the_fallback_does_not_swallow_a_VALID_knob():
    """Positive control for the guard above — it must not eat good values.

    A fallback wide enough to catch every malformed input can also silently
    ignore a correct one, and that failure is worse: the operator sets 900,
    gets 300, and nothing says so.
    """
    env = {"DEP_BLOCKED_VIEW_TIMEOUT_S": "450"}
    lane = _load("dependency-timeout-check", env)
    battery = _load("precheck-always-run-battery", env)

    assert lane.BLOCKED_VIEW_TIMEOUT_S == 450, "the fallback swallowed a valid knob"
    assert _outer(battery) == 510
