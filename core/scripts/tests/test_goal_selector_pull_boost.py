"""test_goal_selector_pull_boost.py —  (2026-08-17).

Pins apply_pull_boost, the EVENT-keyed post-scoring pass wired into
goal-selector.py cmd_select. Every other anti-starvation term in that file is
TIME-keyed (recurring_urgency, starvation_boost, the drain lane, the monitor
interval arm), so a consumer goal that exists to drain a dependency could only
ever fire on its own interval — never WHEN the dependency actually landed. Its
urgency lives in the PRODUCER's event, which the scorer never saw. A producer
stamps `pull_signal` on the consumer goal; this pass turns that event into rank.

Contracts pinned:
  * NO-REGRESSION BY CONSTRUCTION: no pull_signal -> byte-identical no-op (this
    is stronger than the sibling passes' age threshold, because ~no goal carries
    the field at all).
  * enabled=false -> byte-identical no-op.
  * THE ACCEPTANCE BAR (the goal's own outcome (a)): a pulled goal outranks a top
    substantive 2 points above it, and does NOT under the OLD scorer — the
    disabled config is the positive control, so the test cannot pass vacuously.
  * SIZING (guard-1895 (2)): the default boost exceeds the measured noise width,
    because an intervention smaller than the noise changes almost nothing while
    looking exactly like a fix.
  * THE SAFETY VALVE: a signal older than max_age_hours stops lifting, so a lost
    CLEAR cannot pin a goal at rank 1 forever. Measured against
    coordination_merge._merge_goal, clear-by-key-removal RESURRECTS and
    clear-by-null loses on a same-or-older last_modified, so the clear is not
    something this pass may depend on.
  * a signal stamped far in the FUTURE is bogus, not clamped-to-live.
  * fail-open: malformed/missing pull_signal or set_at -> no boost, no raise.
  * boost-only: no candidate's score is ever lowered.
  * EMITTER (guard-1362): score_goal itself emits pull_signal into the candidate
    dict, so the emitter is tested and not only the consumer.
  * the shipped aspirations.yaml pull_boost block loads (default ON).

Pattern mirrors test_goal_selector_starvation_boost.py: spec_from_file_location
load of the hyphen-named module, capture/restore MIND_AGENT around import, pure
in-memory dicts, no subprocess, no daemon.
"""
from __future__ import annotations

import copy
import importlib.util
import os
import sys
from datetime import datetime, timedelta
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


gs = _load("goal_selector_pull", "goal-selector.py")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


# --- config + fixtures ------------------------------------------------------

CFG = {"enabled": True, "boost": 4.0, "max_age_hours": 24.0}

# Measured live on this queue 2026-08-17 (cc-07, 1163 candidates): exploration
# noise ~ U(0, 1.210) over 99.6% of candidates. The pass must clear it.
MEASURED_NOISE_WIDTH = 1.210


def _ts(hours_ago):
    """A naive-local ISO timestamp `hours_ago` hours in the past (TZ=UTC fleet-wide)."""
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _sig(hours_ago=0.5, **kw):
    s = {"set_at": _ts(hours_ago), "by": "alpha/cc-07", "reason": "carrier ref, 4 framework files"}
    s.update(kw)
    return s


def _entry(*, goal_id="g-x-1", score=5.0, pull_signal=None):
    """A synthetic scored-entry dict of the shape score_goal emits."""
    return {
        "goal_id": goal_id,
        "aspiration_id": "asp-x",
        "score": score,
        "recurring": False,
        "breakdown": {},
        "raw": {"priority": 3},
        "pull_signal": pull_signal,
    }


# --- emitter (guard-1362) ---------------------------------------------------

def test_score_goal_emits_pull_signal():
    """guard-1362: the emitter (score_goal), not only the consumer, carries it."""
    gs._ACTIVE_DIRECTIVES = []
    sig = {"set_at": "2026-08-17T23:00:00", "by": "alpha/cc-07", "reason": "r"}
    goal = {"id": "g-x-1", "title": "t", "priority": "HIGH", "pull_signal": sig}
    cand = {"goal": goal, "aspiration": {"id": "asp-x"}, "source": "world"}
    result = gs.score_goal(cand, {}, [], [])
    assert result["pull_signal"] == sig
    # routing fields guard-1362 protects must still be present
    assert "intended_agent" in result
    assert "routed_to_me" in result


def test_score_goal_pull_signal_absent_is_none_not_missing():
    """The consumer reads .get('pull_signal'); the key must exist and be None so a
    KeyError can never depend on which goal was scored."""
    gs._ACTIVE_DIRECTIVES = []
    goal = {"id": "g-x-2", "title": "t", "priority": "HIGH"}
    cand = {"goal": goal, "aspiration": {"id": "asp-x"}, "source": "world"}
    result = gs.score_goal(cand, {}, [], [])
    assert "pull_signal" in result
    assert result["pull_signal"] is None


# --- no-regression ----------------------------------------------------------

def test_no_pull_signal_is_byte_identical():
    """The whole no-regression argument: ~no goal carries the field, so ~every
    candidate must come out of this pass untouched."""
    before = [_entry(goal_id="a", score=9.0), _entry(goal_id="b", score=8.0)]
    scored = copy.deepcopy(before)
    gs.apply_pull_boost(scored, CFG)
    assert scored == before


def test_disabled_is_byte_identical():
    before = [_entry(score=5.0, pull_signal=_sig())]
    scored = copy.deepcopy(before)
    gs.apply_pull_boost(scored, {"enabled": False, "boost": 4.0, "max_age_hours": 24.0})
    assert scored == before


def test_zero_boost_is_byte_identical():
    before = [_entry(score=5.0, pull_signal=_sig())]
    scored = copy.deepcopy(before)
    gs.apply_pull_boost(scored, {"enabled": True, "boost": 0.0, "max_age_hours": 24.0})
    assert scored == before


# --- the acceptance bar (goal outcome (a)) ----------------------------------

def test_pulled_goal_outranks_a_top_substantive_two_points_above():
    """THE BAR, stated by the goal itself. A pulled consumer must beat a top
    substantive 2 points above it -- otherwise the mechanism does not do the one
    thing it exists for."""
    pulled = _entry(goal_id="g-306-284", score=10.0, pull_signal=_sig())
    top = _entry(goal_id="g-top", score=12.0)
    scored = [pulled, top]
    gs.apply_pull_boost(scored, CFG)
    assert scored[0]["score"] > scored[1]["score"], (
        "pulled=%.2f top=%.2f" % (scored[0]["score"], scored[1]["score"])
    )


def test_old_scorer_positive_control_the_pulled_goal_LOSES():
    """THE POSITIVE CONTROL. Without it the test above passes for any pass that
    happens to add a number, and would still pass if the mechanism were inert but
    the fixture happened to rank correctly. Under the OLD behaviour (disabled) the
    same fixture must come out the other way."""
    pulled = _entry(goal_id="g-306-284", score=10.0, pull_signal=_sig())
    top = _entry(goal_id="g-top", score=12.0)
    scored = [pulled, top]
    gs.apply_pull_boost(scored, {"enabled": False, "boost": 4.0, "max_age_hours": 24.0})
    assert scored[0]["score"] < scored[1]["score"]


def test_default_boost_clears_the_measured_noise_width():
    """guard-1895 (2): size the fix against the NOISE WIDTH, not the deterministic
    deficit. A boost inside the noise changes almost nothing while looking exactly
    like a fix, so this pins the sizing rather than trusting the comment."""
    cfg = gs.load_pull_boost_config()
    assert cfg["boost"] > MEASURED_NOISE_WIDTH
    # and must clear the acceptance bar's 2-point gap ON TOP of the noise
    assert cfg["boost"] > 2.0 + MEASURED_NOISE_WIDTH


def test_the_FALLBACK_default_also_clears_the_noise_width():
    """The gap a mutation control found. The test above reads
    load_pull_boost_config(), which consults the overlay — so corrupting the code's
    own fallback literal left it GREEN, because the shipped aspirations.yaml
    overrode the damage. The fallback is not a formality: the loader swallows every
    overlay failure, so a world whose YAML carries no pull_boost block runs on
    exactly these numbers, where an under-sized boost would sit inside the noise and
    do nothing while looking like a feature."""
    d = gs._PULL_BOOST_DEFAULTS
    assert d["boost"] > 2.0 + MEASURED_NOISE_WIDTH
    assert d["enabled"] is True
    assert d["max_age_hours"] > 0


def test_shipped_yaml_and_fallback_cannot_silently_diverge():
    """If someone retunes the YAML and not the fallback (or vice versa), two worlds
    of the same fleet would rank pulls differently with nothing to say so."""
    assert gs.load_pull_boost_config() == gs._PULL_BOOST_DEFAULTS, (
        "shipped aspirations.yaml pull_boost has drifted from _PULL_BOOST_DEFAULTS; "
        "retune BOTH or state in the commit why they should differ"
    )


def test_default_boost_stays_below_a_user_directive():
    """A machine-set pull must never outrank a FRESH USER DIRECTIVE. Same ordering
    argument load_starvation_boost_config makes for its own 4.0 ceiling."""
    assert gs.load_pull_boost_config()["boost"] <= 4.5


# --- the safety valve -------------------------------------------------------

def test_signal_older_than_max_age_stops_lifting():
    """A lost CLEAR must not pin a goal at rank 1 forever. Measured against
    coordination_merge._merge_goal: clear-by-key-removal RESURRECTS, and
    clear-by-null loses on a same-or-older last_modified -- so the clear is not
    something this pass may rely on."""
    before = _entry(score=5.0, pull_signal=_sig(hours_ago=30))
    scored = [copy.deepcopy(before)]
    gs.apply_pull_boost(scored, CFG)
    assert scored[0] == before


def test_signal_just_inside_the_window_still_lifts():
    """The valve's other side -- without this, a max_age of 0 would pass the test
    above while disabling the feature entirely."""
    scored = [_entry(score=5.0, pull_signal=_sig(hours_ago=23))]
    gs.apply_pull_boost(scored, CFG)
    assert scored[0]["breakdown"]["pull_boost"] == 4.0
    assert scored[0]["score"] == 9.0


def test_far_future_signal_is_bogus_not_live():
    """Clamping a future stamp to 'live' would hold the boost for as long as the
    skew -- the unbounded case the valve exists to prevent."""
    before = _entry(score=5.0, pull_signal=_sig(hours_ago=-48))
    scored = [copy.deepcopy(before)]
    gs.apply_pull_boost(scored, CFG)
    assert scored[0] == before


def test_small_clock_skew_is_tolerated():
    """A few minutes of skew between producer and consumer boxes is normal and must
    not silently drop the signal."""
    scored = [_entry(score=5.0, pull_signal=_sig(hours_ago=-0.5))]
    gs.apply_pull_boost(scored, CFG)
    assert scored[0]["breakdown"]["pull_boost"] == 4.0


# --- fail-open / robustness -------------------------------------------------

def test_malformed_pull_signal_does_not_raise_and_does_not_boost():
    for bad in ("carrier landed", ["a"], 42, True, {}, {"by": "alpha"}, {"set_at": None},
                {"set_at": "not-a-timestamp"}):
        before = _entry(score=5.0, pull_signal=bad)
        scored = [copy.deepcopy(before)]
        gs.apply_pull_boost(scored, CFG)
        assert scored[0] == before, "boosted on malformed signal: %r" % (bad,)


def test_boost_only_never_lowers_a_score():
    scored = [_entry(goal_id="a", score=5.0, pull_signal=_sig()),
              _entry(goal_id="b", score=7.0),
              _entry(goal_id="c", score=3.0, pull_signal=_sig(hours_ago=99))]
    before = {e["goal_id"]: e["score"] for e in copy.deepcopy(scored)}
    gs.apply_pull_boost(scored, CFG)
    for e in scored:
        assert e["score"] >= before[e["goal_id"]]


def test_telemetry_is_recorded_for_audit():
    """A boost nobody can see in the breakdown is a term that silently went to
    zero -- the invisibility class several sibling passes were filed over."""
    scored = [_entry(score=5.0, pull_signal=_sig(hours_ago=2))]
    gs.apply_pull_boost(scored, CFG)
    assert scored[0]["breakdown"]["pull_boost"] == 4.0
    assert 1.9 <= scored[0]["raw"]["pull_signal_age_hours"] <= 2.1


def test_returns_the_same_list_object_in_place():
    scored = [_entry(score=5.0, pull_signal=_sig())]
    assert gs.apply_pull_boost(scored, CFG) is scored


# --- shipped config ---------------------------------------------------------

def test_shipped_aspirations_yaml_block_loads_and_is_on():
    cfg = gs.load_pull_boost_config()
    assert cfg["enabled"] is True
    assert cfg["max_age_hours"] > 0
    assert set(cfg) == {"enabled", "boost", "max_age_hours"}
