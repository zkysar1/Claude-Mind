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
from datetime import datetime, timedelta
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


# --------------------------------------------------------------------------
# Event-keyed admission: a live pull_signal ( outcome (a))
#
# The lane was purely TIME-keyed -- eligibility came only from
# overdue_exemption_level, so a consumer goal whose dependency had JUST
# materialized could not reach the one mechanism that bypasses both the urgency
# clamp and the noise lottery. These pin the OR-admission and its ordering.
# --------------------------------------------------------------------------

def _pulled(gid, score, ratio=0.0, age_h=0.5, **kw):
    """A row carrying a pull_signal stamped `age_h` hours ago."""
    r = row(gid, score, ratio=ratio, **kw)
    stamp = datetime.now() - timedelta(hours=age_h)
    r["pull_signal"] = {
        "set_at": stamp.strftime("%Y-%m-%dT%H:%M:%S"),
        "by": "alpha/test",
        "reason": "carrier ref deadbeef, 1 framework file(s)",
    }
    return r


def test_live_pull_signal_admits_a_not_overdue_recurring_goal(tmp_path):
    """The whole point: ratio 0.0 is NOT overdue, so the time-keyed exemption
    refuses it. The live signal admits it anyway, and it takes the top slot
    despite having the LOWEST score in the list."""
    scored = [
        row("g-top", 16.0, recurring=False),
        row("g-other", 12.0, ratio=0.0),
        _pulled("g-pulled", 8.0, ratio=0.0),
    ]
    picked = gs.apply_drain_lane(scored, cfg(), primed(tmp_path))
    assert picked is not None, "a live pull_signal must admit an un-overdue goal"
    assert picked["goal_id"] == "g-pulled"
    assert scored[0]["goal_id"] == "g-pulled"
    assert scored[0].get("drain_lane_pick") is True


def test_pulled_goal_sorts_ahead_of_a_more_overdue_one(tmp_path):
    """Admission without ordering would be a no-op: a pulled goal is typically
    ratio 0.0, so under the old key it sorted LAST among eligible rows."""
    scored = [
        row("g-starved", 9.0, ratio=10.63),
        _pulled("g-pulled", 8.0, ratio=0.0),
    ]
    picked = gs.apply_drain_lane(scored, cfg(), primed(tmp_path))
    assert picked["goal_id"] == "g-pulled", (
        "an EVENT (the dependency arrived) outranks elapsed time in this lane"
    )


def test_stale_pull_signal_does_not_admit(tmp_path):
    """NEGATIVE CONTROL. A signal past max_age_hours is not live, so the goal
    falls back to the time-keyed test and stays ineligible -- this is what keeps
    a lost CLEAR from pinning a goal in the lane forever."""
    stale = gs.PULL_CONFIG.get("max_age_hours", 24.0) + 5.0
    scored = [
        row("g-other", 12.0, ratio=0.0),
        _pulled("g-stale", 8.0, ratio=0.0, age_h=stale),
    ]
    assert gs.apply_drain_lane(scored, cfg(), primed(tmp_path)) is None


def test_no_pull_signal_leaves_the_lane_byte_identical(tmp_path):
    """NO-REGRESSION. ~no goal carries the field, so the common path must be
    unchanged: an ineligible list stays ineligible and is not mutated."""
    scored = [row("g-a", 12.0, ratio=0.0), row("g-b", 9.0, ratio=0.0)]
    snapshot = json.dumps(scored, sort_keys=True)
    assert gs.apply_drain_lane(scored, cfg(), primed(tmp_path)) is None
    assert json.dumps(scored, sort_keys=True) == snapshot


def test_pull_does_not_bypass_the_k_cadence(tmp_path):
    """The cadence is the anti-flood guarantee and a pull must NOT escape it:
    a producer setting many signals is still bounded to one lane pick per K."""
    d = tmp_path / "agent"
    (d / "session").mkdir(parents=True, exist_ok=True)
    gs.write_drain_lane_state(d, {"invocations_since_pick": 0})
    picks = []
    for _ in range(5):
        scored = [row("g-top", 16.0, recurring=False),
                  _pulled("g-pulled", 8.0, ratio=0.0)]
        picks.append(gs.apply_drain_lane(scored, cfg(), d) is not None)
    assert picks == [False, False, False, False, True], picks


def test_pull_admission_is_ored_not_substituted(tmp_path):
    """The time-keyed arm must still work on its own -- the new clause is an OR,
    not a replacement. Without this, swapping the condition would pass every
    pull test above while silently deleting the lane's original purpose."""
    scored = [row("g-starved", 9.0, ratio=10.63)]
    picked = gs.apply_drain_lane(scored, cfg(), primed(tmp_path))
    assert picked is not None and picked["goal_id"] == "g-starved"


# --------------------------------------------------------------------------
# Role gate ()
# --------------------------------------------------------------------------
#
# THE DEFECT. The lane's eligibility predicate filtered on overdue ratio and
# pull_signal only -- grepping its whole span for role|reducer returned ZERO
# hits -- so a reducer-only recurring goal could be hoisted to index 0 for a
# WORKER Body, and emit_drain_lane_banner then told that worker verbatim "This
# IS the sanctioned top pick -- claim it without a deviation code" against a
# goal whose own description orders a worker to release and re-select.
# apply_reducer_only_floor IS role-aware but yields to any prior hoist, so the
# one path that knew about roles deferred to the one that did not.
#
# Measured twice on  (recurring, executable_by_role=reducer): a
# release_negative on cc-07 2026-08-31 and a worker relay 2026-09-03.

import contextlib


@contextlib.contextmanager
def _body(role=None, sid=None, agent_dir=None, running_sid=None):
    """Set the three signals role_of reads, and restore them after.

    Deliberately sets BOTH env vars every time (to a value or to absent) rather
    than only the ones a case cares about: these are process-global, so a case
    that left one set would silently decide the NEXT case's role. That is the
    same leak class the module's own frozen-clock note records.
    """
    saved = {k: os.environ.get(k) for k in ("BODY_ROLE", "MIND_SID")}
    for k, v in (("BODY_ROLE", role), ("MIND_SID", sid)):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if agent_dir is not None and running_sid is not None:
        (agent_dir / "session").mkdir(parents=True, exist_ok=True)
        (agent_dir / "session" / "running-session-id").write_text(
            running_sid, encoding="utf-8")
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _reducer_only(gid, score, ratio=10.63, **kw):
    r = row(gid, score, ratio=ratio, **kw)
    r["executable_by_role"] = "reducer"
    return r


def test_worker_body_never_gets_a_reducer_only_drain_pick(tmp_path):
    """The defect, pinned. A worker Body must not have reducer-only work hoisted."""
    d = primed(tmp_path)
    scored = [row("g-top", 16.0, recurring=False),
              _reducer_only("g-reducer-only", 9.0, ratio=10.63)]
    with _body(role="worker", sid="sid-worker"):
        picked = gs.apply_drain_lane(scored, cfg(), d)
    assert picked is None, "a worker was handed reducer-only work by the drain lane"
    assert scored[0]["goal_id"] == "g-top", "index 0 must be untouched for a worker"


def test_reducer_body_still_gets_its_reducer_only_drain_pick(tmp_path):
    """The no-regression half, and the reason the gate is role-keyed rather than
    an unconditional exclusion: for a single-Body reducer the floor's own hoist
    is gated on live_workers >= threshold, so dropping this row from the lane
    outright would leave it with no hoist at all."""
    d = primed(tmp_path)
    scored = [row("g-top", 16.0, recurring=False),
              _reducer_only("g-reducer-only", 9.0, ratio=10.63)]
    with _body(role=None, sid="sid-red", agent_dir=d, running_sid="sid-red"):
        picked = gs.apply_drain_lane(scored, cfg(), d)
    assert picked is not None and picked["goal_id"] == "g-reducer-only"
    assert scored[0]["goal_id"] == "g-reducer-only"


def test_unknown_role_excludes_reducer_only(tmp_path):
    """An unbound ad-hoc run and an observer session resolve to ROLE_UNKNOWN.
    They exclude, mirroring apply_reducer_only_floor's posture that only
    ROLE_REDUCER proceeds. The asymmetry is the argument: excluding costs one
    deferred hoist on a row that still competes on score, including costs a
    claim that must be released and re-selected."""
    d = primed(tmp_path)
    scored = [_reducer_only("g-reducer-only", 9.0, ratio=10.63)]
    with _body(role=None, sid=None):
        assert gs.apply_drain_lane(scored, cfg(), d) is None


def test_gate_is_narrow_ordinary_starved_rows_still_promote_for_a_worker(tmp_path):
    """The gate must exclude ONLY reducer-only rows. Without this, widening the
    predicate to 'workers get no drain lane' would pass every test above while
    silently deleting the lane for the role that runs most of the goals."""
    d = primed(tmp_path)
    scored = [row("g-top", 16.0, recurring=False),
              _reducer_only("g-reducer-only", 12.0, ratio=20.0),
              row("g-starved", 9.0, ratio=10.63)]
    with _body(role="worker", sid="sid-worker"):
        picked = gs.apply_drain_lane(scored, cfg(), d)
    assert picked is not None, "an ordinary starved row must still promote"
    assert picked["goal_id"] == "g-starved", (
        "the more-overdue reducer-only row must be skipped, not win")


def test_state_file_records_the_role_and_what_it_excluded(tmp_path):
    """A silent filter is the failure mode this whole class keeps hitting: the
    original defect survived two measured incidents partly because nothing named
    what the lane had considered. The state file must make the gate falsifiable
    without re-running the selector."""
    d = primed(tmp_path)
    scored = [_reducer_only("g-a", 9.0, ratio=10.63),
              _reducer_only("g-b", 8.0, ratio=9.0),
              row("g-ordinary", 7.0, ratio=0.0)]
    with _body(role="worker", sid="sid-worker"):
        gs.apply_drain_lane(scored, cfg(), d)
    state = gs.read_drain_lane_state(d)
    assert state["role"] == "worker"
    assert state["role_excluded_reducer_only"] == 2, state


def test_reducer_run_reports_zero_excluded(tmp_path):
    """The counter must mean 'excluded', not 'present' -- otherwise a reducer's
    state file would report exclusions that never happened and the telemetry
    above would be unreadable."""
    d = primed(tmp_path)
    scored = [_reducer_only("g-a", 9.0, ratio=10.63)]
    with _body(role=None, sid="sid-red", agent_dir=d, running_sid="sid-red"):
        gs.apply_drain_lane(scored, cfg(), d)
    state = gs.read_drain_lane_state(d)
    assert state["role"] == "reducer"
    assert state["role_excluded_reducer_only"] == 0, state


def test_excluded_counter_counts_kept_out_not_merely_present(tmp_path):
    """The counter must mean 'kept out of the lane', not 'reducer-only and present'.

    test_state_file_records_the_role_and_what_it_excluded above does NOT
    discriminate this: both its rows are lane-admissible (ratio 10.63 / 9.0 vs
    the 5.0 exempt threshold), so a counter that counts mere presence and one
    that counts real exclusions agree there. This case separates them with a
    reducer-only row that is NOT admissible (ratio 0.0, no pull): the role gate
    changes nothing for it, so counting it would report an exclusion that never
    happened -- the guard-1760 shape, and it would make the very telemetry added
    to make this gate falsifiable actively misleading.
    """
    d = primed(tmp_path)
    scored = [_reducer_only("g-admissible", 9.0, ratio=10.63),
              _reducer_only("g-not-overdue", 8.0, ratio=0.0)]
    with _body(role="worker", sid="sid-worker"):
        gs.apply_drain_lane(scored, cfg(), d)
    state = gs.read_drain_lane_state(d)
    assert state["role_excluded_reducer_only"] == 1, (
        f"only the lane-admissible reducer-only row was actually kept out; "
        f"got {state['role_excluded_reducer_only']}")
