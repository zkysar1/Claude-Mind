"""test_goal_selector_strategic_focus.py — .

Proves goal-selector.py consumes `world/team-state.yaml strategic_focus`, the
STANDING USER DIRECTIVE that names which aspiration(s) currently outrank routine
work.

WHY THIS EXISTS
strategic_focus is set by the user and acknowledged by every agent. Its live text
reads "Product goals outrank routine infra sweeps AT SELECTION TIME until asp-335
drains." Before this fix, `grep -rln strategic_focus` found exactly two behavioral
readers — boot/SKILL.md and create-aspiration/SKILL.md — and goal-selector.py was
not one of them. A directive whose own wording names selection time had no path
into selection, so five agents acknowledged it and the ranking never changed.
Three same-day symptoms of that one gap: bravo's 109-asp-115-vs-16-asp-335 mix,
zeta g-115-3127, alpha g-115-3092; g-115-1977 / g-115-1778 / g-115-2625 are three
COMPLETED hand-filed "rebalance toward product" goals — the shape kept recurring
because the rebalance was re-derived by hand instead of wired in.

The boost rides the EXISTING `directive_boost` criterion rather than adding a new
one, so `test_goal_selector_weights_contract.py` and every breakdown consumer stay
untouched. `test_directive_boost_criterion_is_not_split_into_a_new_knob` pins that
choice — re-splitting it would silently break those consumers.

Fixture shape mirrors test_goal_selector_idle_reallocation.py: pin MIND_AGENT
around import, monkeypatch `_load_team_state_cached`, and reset the module-level
caches the loaders memoize into.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

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


LIVE_PRIMARY = (
    "Product completeness: asp-335 Vinheim+Lodestar parity (user directive "
    "2026-07-04) and asp-334 box live-agent bring-up. Product goals outrank "
    "routine infra sweeps at selection time until asp-335 drains."
)


@pytest.fixture(autouse=True)
def _reset_caches():
    """Both loaders memoize into module globals; clear them around every test."""
    gs._STRATEGIC_FOCUS = None
    gs._TEAM_STATE_CACHE = None
    yield
    gs._STRATEGIC_FOCUS = None
    gs._TEAM_STATE_CACHE = None


@pytest.fixture
def focus(monkeypatch):
    """set(team_state_dict) -> installs it as the cached team-state read."""
    def _set(state):
        monkeypatch.setattr(gs, "_load_team_state_cached", lambda: state)
        gs._STRATEGIC_FOCUS = None
    return _set


# ------------------------------------------------------------------ parsing


def test_parses_every_aspiration_id_from_the_live_directive(focus):
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    assert gs.load_strategic_focus()["aspirations"] == {"asp-335", "asp-334"}


def test_secondary_field_is_parsed_when_present(focus):
    focus({"strategic_focus": {"primary": "asp-100 first",
                               "secondary": "then asp-200"}})
    assert gs.load_strategic_focus()["aspirations"] == {"asp-100", "asp-200"}


def test_word_boundary_prevents_prefix_collisions(focus):
    """`asp-33` must not match inside `asp-335` — a substring match would boost
    a whole unrelated aspiration off one directive mentioning its longer twin."""
    focus({"strategic_focus": {"primary": "drive asp-335 to done"}})
    asps = gs.load_strategic_focus()["aspirations"]
    assert asps == {"asp-335"}
    assert gs.strategic_focus_boost("asp-33", 0.5) == 0.0


# --------------------------------------------------------------- fail-open


@pytest.mark.parametrize("state", [
    {},                                              # no strategic_focus key
    {"strategic_focus": None},                       # explicitly null
    {"strategic_focus": "a bare string, not a map"},  # wrong type
    {"strategic_focus": {"primary": None}},          # null prose
    {"strategic_focus": {"primary": "ship the product faster"}},  # names no asp
])
def test_absent_or_malformed_focus_boosts_nothing(focus, state):
    focus(state)
    assert gs.load_strategic_focus()["aspirations"] == set()
    assert gs.strategic_focus_boost("asp-335", 0.5) == 0.0


def test_unparseable_prose_warns_instead_of_boosting_nothing_silently(
        focus, capsys):
    """A live directive that parses to ZERO targets must be LOUD.

    Silence here is the vacuous-pass shape: the user writes "asp 335" (space) or
    renames a lane, the regex matches nothing, the boost does nothing, and the
    directive LOOKS honored because no error was raised. Nothing else in the
    loop audits team-state prose, so this stderr line is the only signal.
    """
    focus({"strategic_focus": {"primary": "prioritise asp 335, the product lane",
                               "set_by": "zachary"}})
    assert gs.load_strategic_focus()["aspirations"] == set()
    err = capsys.readouterr().err
    assert "names no asp-NNN" in err
    assert "zachary" in err, "the warning must name who set the directive"


def test_absent_focus_does_not_warn(focus, capsys):
    """No directive is a normal state, not a misparse — stay quiet."""
    focus({})
    gs.load_strategic_focus()
    assert "names no asp-NNN" not in capsys.readouterr().err


def test_unreadable_team_state_fails_open_without_raising(monkeypatch, capsys):
    """team-state is ADVISORY scoring input — a read failure must warn on stderr
    and disengage the boost, never abort selection (same posture as rb-2429).

    Patches via `monkeypatch`, NOT a raw `gs.x = ...` + `importlib.reload` in a
    finally. That earlier shape passed this file in isolation and FAILED in the
    full suite: reload re-executes goal-selector at module level and hands every
    later test a different module object than the one they imported. monkeypatch
    unwinds the single attribute and touches nothing else.
    """
    def _boom():
        raise OSError("team-state.yaml vanished mid-read")
    monkeypatch.setattr(gs, "_load_team_state_cached", _boom)
    assert gs.load_strategic_focus()["aspirations"] == set()
    assert "strategic_focus unreadable" in capsys.readouterr().err


# ------------------------------------------------------------------ boost


def test_named_aspiration_gets_the_configured_weight(focus):
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    w = gs.load_strategic_focus()["weight"]
    assert w > 0, "default weight must be positive or the wiring is inert"
    assert gs.strategic_focus_boost("asp-335", 0.83) == w
    assert gs.strategic_focus_boost("asp-334", 0.10) == w


def test_unnamed_aspiration_gets_nothing(focus):
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    assert gs.strategic_focus_boost("asp-115", 0.96) == 0.0


def test_empty_aspiration_id_is_safe(focus):
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    assert gs.strategic_focus_boost("", 0.5) == 0.0
    assert gs.strategic_focus_boost(None, 0.5) == 0.0


# ----------------------------------------------------------- self-retiring


def test_drained_aspiration_stops_being_boosted(focus):
    """THE SELF-RETIREMENT GUARD.

    The directive says "until asp-335 drains". Without this, stale directive
    prose would keep boosting a finished lane forever and nobody would notice,
    because nothing in the loop audits team-state prose for staleness.
    """
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    assert gs.strategic_focus_boost("asp-335", 0.999) > 0.0, "still draining"
    assert gs.strategic_focus_boost("asp-335", 1.0) == 0.0, "drained"
    assert gs.strategic_focus_boost("asp-335", 1.5) == 0.0, "over-complete"


def test_unknown_completion_ratio_still_boosts(focus):
    """A missing ratio must not silently disable a live directive — fail toward
    honoring the user, since the ratio is only the retirement condition."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    assert gs.strategic_focus_boost("asp-335", None) > 0.0


# ------------------------------------------------------------ wiring pins


def test_directive_boost_criterion_is_not_split_into_a_new_knob():
    """The boost must ride the EXISTING directive_boost term.

    Adding a separate criterion would need a WEIGHTS entry + a new breakdown key,
    which every scorer-contract test and breakdown consumer enumerates. Reuse
    keeps that surface byte-identical.
    """
    assert "strategic_focus_boost" not in gs.WEIGHTS, (
        "strategic_focus was split into its own criterion — it is meant to ride "
        "directive_boost (see g-115-3136); adding a knob breaks the weights "
        "contract and every breakdown consumer."
    )
    assert "directive_boost" in gs.WEIGHTS


def test_score_goal_adds_the_boost_into_directive_boost():
    """Structural pin: the 13b call site must SUM both directive sources.

    Ordering/omission here is the whole fix — a behavior test on the helpers
    alone still passes if someone drops the term from score_goal.
    """
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    i = src.index('raw["directive_boost"]')
    window = src[i:i + 400]
    assert "directive_boost_score(" in window
    assert "strategic_focus_boost(" in window, (
        "REGRESSION: score_goal no longer folds strategic_focus into "
        "directive_boost — the standing user directive is back to having no "
        "path into selection (g-115-3136)."
    )


# -------------------------------------------- STRATEGIC-FOCUS banner ()
#
# The scalar above answers "how much is a lane goal worth?". It structurally
# CANNOT answer "does this lane goal beat THAT competitor?", because a per-goal
# term never sees what it competes against. The live directive is exactly that
# pairwise claim ("product goals outrank ROUTINE INFRA SWEEPS"), so the pairwise
# half lives in emit_strategic_focus_banner. These tests pin the predicate --
# especially the SILENT cases, which are what keep it from becoming noise.


def _c(gid, score, *, asp="asp-115", recurring=False, ia="either", title="t"):
    """Minimal scored-candidate row, in ranked (descending-score) order."""
    return {"goal_id": gid, "aspiration_id": asp, "score": score,
            "recurring": recurring, "intended_agent": ia, "title": title}


def test_banner_fires_when_a_routine_sweep_outranks_the_lane(focus, capsys):
    """The canonical live case, measured on 2026-07-28: zeta's top eligible pick
    was g-115-831 (recurring infra sweep) outranking the asp-335 lane by 1.29."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    scored = [_c("g-115-831", 11.29, recurring=True, title="Recurring: tripwire"),
              _c("g-335-285", 10.0, asp="asp-335", ia="zeta")]
    warnings = gs.emit_strategic_focus_banner(scored, "zeta")
    assert len(warnings) == 1
    err = capsys.readouterr().err
    assert "STRATEGIC-FOCUS" in err
    assert "g-115-831" in err and "g-335-285" in err
    assert "1.29" in err, "the gap must be stated — it is the actionable number"
    assert "meta-tiebreaker" in err, "must name the sanctioned deviation code"


def test_silent_when_top_eligible_pick_is_not_a_sweep(focus, capsys):
    """`recurring` IS the predicate — the noun the directive itself uses. A
    non-recurring top pick (verified-defect work) is exactly what the directive
    never meant to outrank, so the banner must say nothing."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    scored = [_c("g-115-999", 11.29, recurring=False),
              _c("g-335-285", 10.0, asp="asp-335", ia="zeta")]
    assert gs.emit_strategic_focus_banner(scored, "zeta") == []
    assert "STRATEGIC-FOCUS" not in capsys.readouterr().err


def test_silent_when_the_sweep_itself_is_lane_work(focus):
    """A recurring goal UNDER a named lane already honors the directive."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    scored = [_c("g-335-900", 11.0, asp="asp-335", recurring=True, ia="zeta"),
              _c("g-335-285", 10.0, asp="asp-335", ia="zeta")]
    assert gs.emit_strategic_focus_banner(scored, "zeta") == []


def test_silent_when_lane_already_outranks_the_sweep(focus):
    """Nothing to correct — the directive is being honored by the ranking."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    scored = [_c("g-335-285", 11.5, asp="asp-335", ia="zeta"),
              _c("g-115-831", 11.29, recurring=True)]
    assert gs.emit_strategic_focus_banner(scored, "zeta") == []


def test_silent_when_no_lane_candidate_is_eligible_to_this_agent(focus):
    """A lane goal routed to ANOTHER agent must not nag this one — otherwise the
    banner fires forever on work this agent is not allowed to claim."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    scored = [_c("g-115-831", 11.29, recurring=True),
              _c("g-335-285", 10.0, asp="asp-335", ia="foxtrot")]
    assert gs.emit_strategic_focus_banner(scored, "zeta") == []


def test_foxtrot_per_agent_correction_no_blanket_product_thumb(focus):
    """foxtrot's addendum to : a blanket product bias would be WRONG,
    because foxtrot's own mix is already product-majority. Keying on the agent's
    OWN eligible top pick delivers that for free — when an agent's top pick is
    its own non-recurring product work, the banner stays silent."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    scored = [_c("g-326-68", 12.2, asp="asp-326", ia="foxtrot", recurring=False),
              _c("g-335-285", 10.0, asp="asp-335", ia="foxtrot")]
    assert gs.emit_strategic_focus_banner(scored, "foxtrot") == []


def test_silent_when_directive_names_no_aspiration(focus, capsys):
    focus({"strategic_focus": {"primary": "work harder on the product please"}})
    scored = [_c("g-115-831", 11.29, recurring=True),
              _c("g-335-285", 10.0, asp="asp-335", ia="zeta")]
    assert gs.emit_strategic_focus_banner(scored, "zeta") == []
    assert "STRATEGIC-FOCUS" not in capsys.readouterr().err


def test_unrouted_goals_count_as_eligible(focus):
    """intended_agent unset/None means open to anyone — treating it as ineligible
    would silently hide most of the queue from the check."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    scored = [_c("g-115-831", 11.29, recurring=True, ia=None),
              _c("g-335-285", 10.0, asp="asp-335", ia=None)]
    assert len(gs.emit_strategic_focus_banner(scored, "zeta")) == 1


# ------------------------------------------- clause (ii) nominee filter ()
#
# The directive excludes recurring goals from what counts as lane work remaining
# -- "both re-supply continuously from the lane's own cadence and neither is
# unbuilt product work". The top-pick side of the banner has always applied that
# field test; the NOMINEE side did not, so the advisory could answer "a routine
# sweep outranks the lane" by nominating another routine sweep. Measured
# 2026-08-08 (bravo, cc-05): it nominated recurring  over recurring
#  and prescribed --deviation meta-tiebreaker for the swap, which files
# a directive-compliance record for work the directive excludes and pollutes the
# Layer-C deviation audit.


def test_recurring_lane_goal_is_never_nominated_as_the_product_substitute(
        focus, capsys):
    """The defect case. A recurring lane candidate outranks a non-recurring one,
    so the pre-fix `next(...)` picked the recurring goal purely because it came
    first. Both are present deliberately: with only the recurring one the test
    could pass against a filter that broke nomination outright."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    scored = [_c("g-115-831", 12.0, recurring=True, title="Recurring: tripwire"),
              _c("g-335-658", 11.0, asp="asp-335", recurring=True, ia="zeta",
                 title="Recurring: cross-repo audit"),
              _c("g-335-285", 10.0, asp="asp-335", ia="zeta",
                 title="Build the parity surface")]
    warnings = gs.emit_strategic_focus_banner(scored, "zeta")
    assert len(warnings) == 1, "a valid non-recurring nominee exists -- still fire"
    err = capsys.readouterr().err
    assert "g-335-285" in err, "the non-recurring lane goal is the only nominee"
    assert "g-335-658" not in err, (
        "clause (ii): a recurring lane goal is not unbuilt product work, so "
        "nominating it swaps one routine sweep for another")
    assert "2.0" in err, "the gap must be measured against the NOMINEE, not the "\
                         "higher-scoring goal that was filtered out"


def test_banner_is_silent_when_every_lane_candidate_is_recurring(focus, capsys):
    """With no non-recurring lane candidate the directive's outrank rule has no
    valid target, so the correct output is silence -- not a nomination. This is
    the conclusion bravo reached by hand on 2026-08-08 and then had to argue for
    in prose; here it is the code path."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    scored = [_c("g-115-831", 12.0, recurring=True, title="Recurring: tripwire"),
              _c("g-335-658", 11.0, asp="asp-335", recurring=True, ia="zeta")]
    assert gs.emit_strategic_focus_banner(scored, "zeta") == []
    assert "STRATEGIC-FOCUS" not in capsys.readouterr().err


def test_clause_ii_did_not_leak_into_the_scalar_boost(focus):
    """Clause (ii) belongs to the ADVISORY, not to the ranking. strategic_focus_
    boost takes (asp_id, completion_ratio) and has no access to `recurring` at
    all, so it cannot discriminate -- pinned here because the obvious next 'fix'
    is to extend the exclusion to the boost, which would silently down-rank every
    recurring lane goal and change selection rather than advice."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    import inspect
    params = list(inspect.signature(gs.strategic_focus_boost).parameters)
    assert params == ["asp_id", "completion_ratio"], (
        f"boost signature changed to {params} -- if `recurring` was threaded in, "
        "clause (ii) has escaped the advisory and now moves the ranking")
    assert gs.strategic_focus_boost("asp-335", 0.5) > 0


@pytest.mark.parametrize("scored,agent", [
    ([], "zeta"),
    ([_c("g-1", 1.0)], ""),
    ([{"goal_id": "g-1"}], "zeta"),                       # no score / no fields
    ([_c("g-115-831", 11.0, recurring=True), {}], "zeta"),  # malformed row
    # non-numeric score: found by the  fresh-eyes probe. float("high")
    # raised ValueError straight out of the function while its docstring promised
    # it never raises — the call site caught it, so the loop was safe and the
    # false contract was invisible.
    ([_c("g-115-831", "high", recurring=True),
      _c("g-335-285", 10.0, asp="asp-335", ia="zeta")], "zeta"),
])
def test_banner_never_raises(focus, scored, agent):
    """Fail-open is the contract: a banner bug must never block goal selection."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    assert isinstance(gs.emit_strategic_focus_banner(scored, agent), list)


def test_banner_never_writes_to_stdout(focus, capsys):
    """STDOUT IS THE ORCHESTRATOR'S DATA CHANNEL. goal-selector.py prints the
    ranked-candidate JSON to stdout and aspirations-select parses it; one stray
    print() here corrupts that parse for every agent, every iteration. The
    firing test asserts the text IS on stderr — this asserts it is NOT on
    stdout, which is the half that actually breaks the loop if it regresses."""
    focus({"strategic_focus": {"primary": LIVE_PRIMARY}})
    scored = [_c("g-115-831", 11.29, recurring=True),
              _c("g-335-285", 10.0, asp="asp-335", ia="zeta")]
    assert len(gs.emit_strategic_focus_banner(scored, "zeta")) == 1
    captured = capsys.readouterr()
    assert "STRATEGIC-FOCUS" in captured.err
    assert captured.out == "", (
        f"REGRESSION: the strategic-focus banner wrote to STDOUT "
        f"({captured.out[:120]!r}) — that channel carries the ranked-candidate "
        f"JSON the orchestrator parses (g-115-3251)."
    )


def test_banner_is_wired_into_the_selection_path():
    """Behavior tests on the helper still pass if nobody calls it (the exact
    failure mode of sh-004, which had ZERO code readers for two months)."""
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    assert "emit_strategic_focus_banner(scored, AGENT_NAME)" in src, (
        "REGRESSION: the strategic-focus banner is defined but never invoked — "
        "the pairwise half of the standing directive is unenforced again "
        "(g-115-3251)."
    )


def test_scalar_was_not_raised_to_force_product_work():
    """Pins the  decision. Three agents independently proposed raising
    this knob; the measured gaps (1.29/1.38/1.39/1.69) all exceed the +1.5 it
    already contributes only because a SCALAR biases the lane against everything
    equally. Any value large enough to clear a routine sweep also overrides the
    verified-defect work the directive never meant to outrank. If this assert
    fails, read the DO NOT RAISE block in core/config/aspirations.yaml first."""
    import yaml
    cfg = yaml.safe_load(
        (CORE_SCRIPTS.parent / "config" / "aspirations.yaml").read_text(
            encoding="utf-8"))
    assert cfg["strategic_focus_boost"]["weight"] == 1.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
