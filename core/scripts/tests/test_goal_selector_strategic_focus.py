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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
