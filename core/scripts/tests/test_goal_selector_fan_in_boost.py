"""test_goal_selector_fan_in_boost.py —  item (2) (2026-08-28).

Pins apply_fan_in_boost, the GRAPH-keyed post-scoring pass wired into
goal-selector.py cmd_select. Its two siblings are TIME-keyed (starvation_boost:
a goal rises because it has WAITED) and EVENT-keyed (pull_boost: a goal rises
because what it consumes has ARRIVED). This one is GRAPH-keyed: a goal rises
because other goals cannot start until it lands.

Before it, `blocked_by` was read only to SUPPRESS the dependent, never to LIFT
the blocker — so a READY root holding up five downstream goals competed on its
own unaided merit and lost. The inverse map already existed, but in the WRONG
COMMAND: cmd_blocked builds root_groups[...]["downstream_ids"], and cmd_select
never sees it.

Contracts pinned:
  * THE ACCEPTANCE BAR (the goal's own outcome (c)): a goal with 3 HIGH
    dependents outranks a SAME-PRIORITY lane goal, and does NOT under the
    disabled config — the positive control, so this cannot pass vacuously.
  * DECAY IS STRUCTURAL: only non-terminal dependents count, so the lift falls as
    dependents close and reaches zero at the last one. No signal to clear means
    no lost-clear failure mode (contrast pull_boost's max_age_hours valve).
  * NO-REGRESSION BY CONSTRUCTION: a goal nothing waits on is byte-identical.
  * BLOCKED_BY ONLY, NEVER depends_on (guard-4554): depends_on is the
    {goal_id, expects} output-passing annotation, not the sequencing field. A
    goal sequenced with depends_on alone must get NO lift here, exactly as it
    gets no suppression elsewhere.
  * SIZING (guard-1895 (2)): the 3-HIGH lift exceeds the measured noise width,
    because an intervention smaller than the noise looks like a fix and is not.
  * THE CAP holds the lift BELOW directive_boost's 4.5 raw ceiling, so a fresh
    USER directive still outranks a machine-derived fan-in.
  * enabled=false -> byte-identical no-op; boost-only (no score is lowered).
  * the shipped aspirations.yaml fan_in_boost block loads (default ON).

Pattern mirrors test_goal_selector_pull_boost.py: spec_from_file_location load of
the hyphen-named module, capture/restore MIND_AGENT around import, pure
in-memory dicts, no subprocess, no daemon.
"""
from __future__ import annotations

import copy
import importlib.util
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gs = _load("goal_selector_fanin", "goal-selector.py")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


# --- config + fixtures ------------------------------------------------------

CFG = {
    "enabled": True,
    "per_dependent": 1.2,
    "cap": 4.0,
    "priority_weights": {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.25},
}

# Measured live on this queue 2026-08-28 (cc-07, 1368 candidates): weighted
# exploration_noise ~ U(0, 1.220) over 100% of candidates. The pass must clear it.
MEASURED_NOISE_WIDTH = 1.220

# directive_boost's raw ceiling. A machine-derived graph term must stay under it.
DIRECTIVE_BOOST_RAW_MAX = 4.5


def _entry(*, goal_id="g-x-1", score=5.0):
    """A synthetic scored-entry dict of the shape score_goal emits."""
    return {
        "goal_id": goal_id,
        "aspiration_id": "asp-x",
        "score": score,
        "recurring": False,
        "breakdown": {},
        "raw": {"priority": 3},
    }


def _dep(gid, blocked_by, priority="HIGH", status="pending"):
    return {"id": gid, "priority": priority, "status": status, "blocked_by": list(blocked_by)}


def _asps(goals, status="active"):
    return [{"id": "asp-x", "status": status, "goals": goals}]


# --- the acceptance bar -----------------------------------------------------


def test_three_high_dependents_outrank_a_same_priority_peer():
    """outcome (c): the root wins, and ONLY because of the fan-in pass."""
    root = _entry(goal_id="g-x-root", score=7.05)
    peer = _entry(goal_id="g-x-peer", score=7.05 + MEASURED_NOISE_WIDTH)
    asps = _asps([_dep(f"g-x-d{i}", ["g-x-root"]) for i in range(3)])

    scored = [copy.deepcopy(root), copy.deepcopy(peer)]
    gs.apply_fan_in_boost(scored, asps, CFG)
    by = {s["goal_id"]: s for s in scored}
    assert by["g-x-root"]["score"] > by["g-x-peer"]["score"]

    # POSITIVE CONTROL: with the pass disabled the root LOSES, so the assertion
    # above is measuring the pass and not the fixture.
    ctrl = [copy.deepcopy(root), copy.deepcopy(peer)]
    gs.apply_fan_in_boost(ctrl, asps, {**CFG, "enabled": False})
    cby = {s["goal_id"]: s for s in ctrl}
    assert cby["g-x-root"]["score"] < cby["g-x-peer"]["score"]


def test_three_high_lift_clears_the_measured_noise_width():
    """guard-1895 (2): sized against the NOISE WIDTH, not the deficit."""
    scored = [_entry(goal_id="g-x-root")]
    gs.apply_fan_in_boost(scored, _asps([_dep(f"g-x-d{i}", ["g-x-root"]) for i in range(3)]), CFG)
    lift = scored[0]["breakdown"]["fan_in_boost"]
    assert lift > MEASURED_NOISE_WIDTH, f"{lift} does not clear noise {MEASURED_NOISE_WIDTH}"


def test_cap_holds_the_lift_below_the_directive_boost_ceiling():
    """A machine-derived graph term must never outrank a fresh USER directive."""
    scored = [_entry(goal_id="g-x-root")]
    many = _asps([_dep(f"g-x-d{i}", ["g-x-root"]) for i in range(25)])
    gs.apply_fan_in_boost(scored, many, CFG)
    assert scored[0]["breakdown"]["fan_in_boost"] == CFG["cap"]
    assert scored[0]["breakdown"]["fan_in_boost"] < DIRECTIVE_BOOST_RAW_MAX


# --- decay ------------------------------------------------------------------


def test_lift_decays_as_dependents_close_and_is_gone_at_the_last():
    lifts = []
    for n_open in (3, 2, 1, 0):
        goals = [_dep(f"g-x-d{i}", ["g-x-root"],
                      status="pending" if i < n_open else "completed")
                 for i in range(3)]
        scored = [_entry(goal_id="g-x-root")]
        gs.apply_fan_in_boost(scored, _asps(goals), CFG)
        lifts.append(scored[0]["breakdown"].get("fan_in_boost", 0.0))
    assert lifts == sorted(lifts, reverse=True), lifts
    assert lifts[-1] == 0.0, "a root whose dependents have all closed must get no lift"
    assert lifts[0] > lifts[-1]


def test_terminal_dependents_do_not_count():
    scored = [_entry(goal_id="g-x-root")]
    gs.apply_fan_in_boost(
        scored, _asps([_dep("g-x-d0", ["g-x-root"], status="completed")]), CFG)
    assert "fan_in_boost" not in scored[0]["breakdown"]


# --- no-regression ----------------------------------------------------------


def test_a_goal_nothing_depends_on_is_byte_identical():
    before = _entry(goal_id="g-x-lonely", score=9.9)
    scored = [copy.deepcopy(before)]
    gs.apply_fan_in_boost(scored, _asps([_dep("g-x-d0", ["g-x-other"])]), CFG)
    assert scored[0] == before


def test_disabled_config_is_a_noop():
    before = _entry(goal_id="g-x-root", score=9.9)
    scored = [copy.deepcopy(before)]
    gs.apply_fan_in_boost(scored, _asps([_dep("g-x-d0", ["g-x-root"])]),
                          {**CFG, "enabled": False})
    assert scored[0] == before


def test_the_pass_never_lowers_a_score():
    scored = [_entry(goal_id="g-x-root", score=5.0), _entry(goal_id="g-x-other", score=5.0)]
    gs.apply_fan_in_boost(scored, _asps([_dep("g-x-d0", ["g-x-root"])]), CFG)
    assert all(s["score"] >= 5.0 for s in scored)


# --- guard-4554: blocked_by ONLY --------------------------------------------


def test_depends_on_alone_produces_no_lift():
    """guard-4554: depends_on is the output-passing annotation, not sequencing.

    A goal sequenced with depends_on alone is not suppressed elsewhere in the
    selector; it must not be lifted here either, or the two halves would
    disagree about what a dependency IS.
    """
    dep = {"id": "g-x-d0", "priority": "HIGH", "status": "pending",
           "depends_on": [{"goal_id": "g-x-root", "expects": "a value"}]}
    scored = [_entry(goal_id="g-x-root")]
    gs.apply_fan_in_boost(scored, _asps([dep]), CFG)
    assert "fan_in_boost" not in scored[0]["breakdown"]


def test_dict_valued_depends_on_does_not_raise():
    """The crash guard-4554 warns about: dict values must never reach a set/str op."""
    dep = {"id": "g-x-d0", "priority": "HIGH", "status": "pending",
           "blocked_by": ["g-x-root"],
           "depends_on": [{"goal_id": "g-x-root", "expects": "a value"}]}
    scored = [_entry(goal_id="g-x-root")]
    gs.apply_fan_in_boost(scored, _asps([dep]), CFG)  # must not raise
    assert scored[0]["breakdown"]["fan_in_boost"] > 0


# --- census correctness -----------------------------------------------------


def test_priority_weighting_ranks_high_above_low():
    def lift(pri):
        scored = [_entry(goal_id="g-x-root")]
        gs.apply_fan_in_boost(
            scored, _asps([_dep("g-x-d0", ["g-x-root"], priority=pri)]), CFG)
        return scored[0]["breakdown"].get("fan_in_boost", 0.0)
    assert lift("HIGH") > lift("MEDIUM") > lift("LOW")


def test_self_reference_is_excluded():
    scored = [_entry(goal_id="g-x-root")]
    gs.apply_fan_in_boost(scored, _asps([_dep("g-x-root", ["g-x-root"])]), CFG)
    assert "fan_in_boost" not in scored[0]["breakdown"]


def test_inactive_aspirations_do_not_contribute():
    scored = [_entry(goal_id="g-x-root")]
    gs.apply_fan_in_boost(
        scored, _asps([_dep("g-x-d0", ["g-x-root"])], status="paused"), CFG)
    assert "fan_in_boost" not in scored[0]["breakdown"]


def test_census_is_recorded_for_telemetry():
    scored = [_entry(goal_id="g-x-root")]
    gs.apply_fan_in_boost(scored, _asps([_dep(f"g-x-d{i}", ["g-x-root"]) for i in range(2)]), CFG)
    assert scored[0]["raw"]["fan_in_dependents"] == 2
    assert scored[0]["raw"]["fan_in_weight"] == 2.0


def test_malformed_blocked_by_does_not_raise():
    for bad in (None, 17, {"a": 1}):
        dep = {"id": "g-x-d0", "priority": "HIGH", "status": "pending", "blocked_by": bad}
        scored = [_entry(goal_id="g-x-root")]
        gs.apply_fan_in_boost(scored, _asps([dep]), CFG)  # must not raise


def test_a_bare_string_blocked_by_counts_as_ONE_id_not_its_characters():
    """A LIVE data shape, not a hypothetical (measured 2026-08-28 on the world queue).

    Some records store blocked_by as a bare STRING rather than a list. Iterating
    that string yields CHARACTERS, so a naive census attributes dependents to
    ids like "g", "-", "2" and "3" -- which is exactly what a hand-rolled census
    did before this test was written. _ensure_list wraps the string, so the pass
    itself is correct; this pins that, because "does not raise" would pass just
    as happily on the char-splitting version.
    """
    dep = {"id": "g-x-d0", "priority": "HIGH", "status": "pending", "blocked_by": "g-x-root"}
    scored = [_entry(goal_id="g-x-root"), _entry(goal_id="g"), _entry(goal_id="-")]
    gs.apply_fan_in_boost(scored, _asps([dep]), CFG)
    by = {s["goal_id"]: s for s in scored}
    assert by["g-x-root"]["breakdown"]["fan_in_boost"] == 1.2
    assert "fan_in_boost" not in by["g"]["breakdown"]
    assert "fan_in_boost" not in by["-"]["breakdown"]


# --- shipped config ---------------------------------------------------------


def test_shipped_config_loads_and_is_enabled():
    cfg = gs.load_fan_in_config()
    assert cfg["enabled"] is True
    assert cfg["per_dependent"] > 0 and cfg["cap"] > 0
    assert float(cfg["priority_weights"]["HIGH"]) * 3 * float(cfg["per_dependent"]) > MEASURED_NOISE_WIDTH
    assert cfg["cap"] < DIRECTIVE_BOOST_RAW_MAX
