"""test_goal_selector_class_balance_directive_ordering.py — .

Pins the COMPOSED ORDERING between two scoring terms that have no declared
precedence: `class_balance_bonus` (session-local work-mix balancer) and the
standing user directive that rides `directive_boost` via `strategic_focus_boost`.

WHY A COMPOSITION TEST AND NOT A CRITERION TEST
`test_goal_selector_strategic_focus.py` proves the boost FIRES, and it passes
throughout the defect this file exists to prevent. That is the whole point: the
boost fired correctly on the product goal and the product goal still lost, because
a sibling term outvoted it inside the weighted sum. No single-criterion test can
see an interaction — the same shape as a green unit suite over a broken
composition. So every assertion here reads a TOTAL or an ordering, never one term.

THE MEASURED DEFECT (foxtrot, LAPTOP-3IOFCNEO / WSL2, 2026-07-30T01:39, 413
candidates). Every directive-aligned term favored the product goal — directive_boost
+1.500, role_affinity +0.500, variety_bonus +0.450, completion_pressure +0.240 — and
class_balance_bonus alone erased them: +0.640 on the framework goal against -1.600 on
the product goal, a -2.240 swing that turned a +1.470 product win into a -0.770 loss.

The asymmetry is structural rather than an unlucky draw. class_balance_bonus spans
raw [-2.0, +2.0] at weight 0.8 (weighted swing 3.2); strategic_focus contributes raw
1.0 at weight 1.5 (weighted +1.5). A term with 2.1x the swing of the directive it is
supposed to yield to wins whenever the session is lane-heavy — which is precisely the
state that OBEYING the directive produces. Obedience fed the term that punished it.

REPRODUCES ACROSS AGENTS, AND DEEPENS (measured 2026-08-11, bravo, cc-05, read from
meta/goal-selection-strategy.yaml agent_role_multipliers — no selector invocation).
role_affinity delta (product - framework) x weight 1.0, plus the constant +1.5:
    foxtrot  +0.5 -> +2.0   <- where the defect was measured: the BEST case
    echo     +0.5 -> +2.0
    alpha    +0.2 -> +1.7
    bravo    +0.0 -> +1.5
    charlie  +0.0 -> +1.5
    zeta     -0.7 -> +0.8   <- role_affinity actively opposes the directive here
The interaction does not invert per agent (the goal's check (c) asked whether it
might); it gets worse. foxtrot's observed -2.24 already beat foxtrot's +2.0.

Hermetic: monkeypatches CLASS_BALANCE_CONFIG and the team-state read; no live world,
no daemon, no network. Safe with a live fleet running.
Run: STORAGE_BACKEND=local python -m pytest \
     core/scripts/tests/test_goal_selector_class_balance_directive_ordering.py -q
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


DIRECTIVE = (
    "Product completeness: asp-335 parity. Product goals outrank routine infra "
    "sweeps at selection time until asp-335 has no executable goals remaining."
)

# Explicit rather than inherited from the live config, so a deployment retuning
# class_balance cannot silently turn these assertions vacuous. The live values are
# asserted separately in test_the_live_config_still_gives_this_mechanism_a_subject.
TEST_CB_CONFIG = {
    "targets": {"product": 0.4, "framework": 0.25, "hygiene": 0.15, "research": 0.2},
    "window_size": 20,
    "max_boost": 2.0,
    "max_penalty": -2.0,
}

# A product-saturated session: observed product fraction 1.0 against target 0.4, so
# the balancer maximally penalizes the product lane and rewards framework. This is
# the state obeying a product directive produces.
PRODUCT_HEAVY = [{"work_class": "product"} for _ in range(10)]


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    gs._STRATEGIC_FOCUS = None
    gs._TEAM_STATE_CACHE = None
    monkeypatch.setattr(gs, "CLASS_BALANCE_CONFIG", TEST_CB_CONFIG)
    yield
    gs._STRATEGIC_FOCUS = None
    gs._TEAM_STATE_CACHE = None


@pytest.fixture
def focus(monkeypatch):
    """set(prose_or_None) -> installs it as the cached team-state read."""
    def _set(primary):
        state = {"strategic_focus": {"primary": primary}} if primary else {}
        monkeypatch.setattr(gs, "_load_team_state_cached", lambda: state)
        gs._STRATEGIC_FOCUS = None
    return _set


def _asp(asp_id, *, drained=False):
    """Aspiration with a completion_ratio of 1.0 (drained) or 0.5 (live)."""
    done = 10 if drained else 5
    return {
        "id": asp_id,
        "goals": (
            [{"id": f"{asp_id}-d{i}", "status": "completed"} for i in range(done)]
            + [{"id": f"{asp_id}-p{i}", "status": "pending"} for i in range(10 - done)]
        ),
    }


def _score(goal_id, asp_id, work_class, *, drained=False, category="product",
           completions=PRODUCT_HEAVY):
    goal = {
        "id": goal_id,
        "title": f"{work_class} goal",
        "priority": "MEDIUM",
        "status": "pending",
        "work_class": work_class,
        "category": category,
        "participants": ["agent"],
    }
    cand = {"goal": goal, "aspiration": _asp(asp_id, drained=drained),
            "source": "world"}
    # epsilon=0.0 zeroes the exploration-noise term (noise_weight = epsilon *
    # noise_scale, added to `total` in both scoring paths). DO NOT restore the
    # default: score_goal draws raw["exploration_noise"] from random.random() per
    # candidate, weighted ~1.2 live, which is comparable to the ~1.6 margin these
    # tests assert on. Every score comparison here would then be a coin-flip
    # decided by the RNG rather than by the composition under test.
    #
    # Learned the expensive way, in this file, on the first full-suite run: all 10
    # tests passed 10/10 in isolation and
    # test_without_the_waiver_the_directive_lane_would_have_lost — the tightest
    # margin, sitting nearest the crossover — failed inside the suite. Green-solo /
    # red-in-suite normally reads as contention or test-order pollution
    # (guard-1448); here it was neither. It was a flaky assertion that had been
    # passing on lucky draws, and the only thing that surfaced it was running the
    # whole suite. Noise is DELIBERATELY random in production (see the
    # scorer-sovereignty tree node), so a composition test must neutralize it —
    # not seed it, not average over it, and above all not re-run until green.
    return gs.score_goal(cand, {}, [], completions, epsilon=0.0)


# ------------------------------------------------------- the composed ordering


def test_directive_lane_goal_outranks_the_balancer_favored_goal(focus):
    """THE REGRESSION TEST. Product goal in the directive's lane must win.

    Both halves matter. The ordering assertion is what the directive promises;
    the waiver-telemetry assertion is the POSITIVE CONTROL proving the balancer
    genuinely wanted to penalize this goal — without it, an ordering that passed
    because class_balance never engaged would look identical to a real pass
    (rb-245: a zero you did not falsify is not a measurement).
    """
    focus(DIRECTIVE)
    product = _score("g-335-1", "asp-335", "product")
    framework = _score("g-115-1", "asp-115", "framework", category="framework")

    waived = product.get("class_balance_penalty_waived")
    assert waived is not None and waived < 0, (
        "the balancer did not penalize the directive lane in this fixture, so "
        "the ordering assertion below proves nothing — re-check PRODUCT_HEAVY "
        "and TEST_CB_CONFIG targets"
    )
    assert product["raw"]["class_balance_bonus"] == 0.0
    assert product["score"] > framework["score"], (
        "REGRESSION: a session-local work-mix balancer outranks the standing "
        "user directive it is supposed to yield to (g-115-3988)"
    )


def test_without_the_waiver_the_directive_lane_would_have_lost(focus):
    """Reconstructs the pre-fix total from the recorded telemetry.

    Proves the clamps are LOAD-BEARING rather than incidental: re-applying BOTH
    waived amounts (the lane goal's penalty, 13b-i, AND the competitor's bonus,
    13b-ii — the reconstruction must undo the whole fix, not half of it) flips
    the ordering back. If this stops flipping, the fixture has drifted away
    from the measured defect and the test above is no longer guarding it.
    """
    focus(DIRECTIVE)
    product = _score("g-335-1", "asp-335", "product")
    framework = _score("g-115-1", "asp-115", "framework", category="framework")

    w = gs.WEIGHTS["class_balance_bonus"]
    pre_fix_product = product["score"] + product["class_balance_penalty_waived"] * w
    pre_fix_framework = (
        framework["score"] + (framework["class_balance_bonus_waived"] or 0.0) * w)
    assert pre_fix_product < pre_fix_framework, (
        "the pre-fix composition no longer loses, so this file is not "
        "reproducing the g-115-3988 defect any more"
    )


# --------------------------------------------------- the clamp stays scoped


def test_no_live_directive_leaves_the_penalty_intact(focus):
    """The majority case. No directive = the balancer is unmodified, which is
    why this fix is an ORDERING change and not a weight change."""
    focus(None)
    product = _score("g-335-1", "asp-335", "product")
    assert product["raw"]["class_balance_bonus"] < 0
    assert product["class_balance_penalty_waived"] is None


def test_a_drained_lane_stops_being_protected(focus):
    """Self-retirement, inherited rather than re-derived. strategic_focus_boost
    already returns 0.0 at completion_ratio >= 1.0, so keying the clamp on its
    return means stale directive prose costs nothing here for exactly the reason
    it costs nothing there — and there is no second liveness test to drift."""
    focus(DIRECTIVE)
    product = _score("g-335-1", "asp-335", "product", drained=True)
    assert product["raw"]["class_balance_bonus"] < 0
    assert product["class_balance_penalty_waived"] is None


def test_an_unnamed_aspiration_is_not_protected(focus):
    focus(DIRECTIVE)
    other = _score("g-115-1", "asp-115", "product")
    assert other["raw"]["class_balance_bonus"] < 0
    assert other["class_balance_penalty_waived"] is None


def test_a_board_directive_alone_does_not_waive_the_penalty(focus, monkeypatch):
    """guard-2412, applied to the WRITE side.

    `directive_boost` is a COMPOSITE of two unrelated mechanisms: board directives
    and the standing user directive. Only the latter carries a PRECEDENCE claim
    ("product goals outrank routine infra sweeps"), so only it can justify waiving
    a sibling term. Keying the clamp on `raw["directive_boost"] > 0` would let any
    board directive silently disable the work-mix balancer for its target — a
    scoping bug invisible in the breakdown, because a composite hides its addends.
    """
    focus(None)  # no strategic focus at all
    monkeypatch.setattr(gs, "_get_directives", lambda: [
        {"target_goals": ["g-335-1"], "target_categories": [], "weight": 2.0}])
    product = _score("g-335-1", "asp-335", "product")
    assert product["raw"]["directive_boost"] > 0, "fixture inert — no board boost"
    assert product["raw"]["class_balance_bonus"] < 0, (
        "a BOARD directive waived the class-balance penalty; the clamp must key "
        "on the strategic_focus addend, not on the composite directive_boost"
    )
    assert product["class_balance_penalty_waived"] is None


def test_a_positive_balance_bonus_passes_through_unchanged(focus):
    """The clamp is a FLOOR, not an override. When the directive lane is
    under-represented the balancer and the directive agree, and the boost must
    survive intact — clamping to zero in both directions would silently convert
    an agreement into a loss."""
    focus(DIRECTIVE)
    framework_heavy = [{"work_class": "framework"} for _ in range(10)]
    product = _score("g-335-1", "asp-335", "product", completions=framework_heavy)
    assert product["raw"]["class_balance_bonus"] > 0
    assert product["class_balance_penalty_waived"] is None


# ------------------------------------------- the bonus half ()


def test_the_competitors_bonus_is_waived_while_the_lane_is_live(focus):
    """13b-ii: a goal OUTSIDE a live lane forgoes its class_balance BONUS.

    13b-i killed the penalty on the lane goal; the tie measured on zeta
    (9.32 == 9.32) showed the surviving bonus on the lane's COMPETITOR moves
    the ordering exactly as far. Both halves of the balancer's swing must
    yield to the directive, or the directive decides nothing whenever
    role_affinity opposes it by the residual margin.
    """
    focus(DIRECTIVE)
    framework = _score("g-115-1", "asp-115", "framework", category="framework")
    assert framework["raw"]["class_balance_bonus"] == 0.0
    waived = framework["class_balance_bonus_waived"]
    assert waived is not None and waived > 0, (
        "the balancer never wanted to boost the competitor in this fixture, "
        "so the zero above proves nothing — re-check PRODUCT_HEAVY"
    )
    assert framework["class_balance_penalty_waived"] is None


def test_the_competitors_bonus_stands_when_no_directive(focus):
    """The bonus waiver is scoped exactly like the penalty waiver: no live
    directive prose = the balancer is untouched, in both directions."""
    focus(None)
    framework = _score("g-115-1", "asp-115", "framework", category="framework")
    assert framework["raw"]["class_balance_bonus"] > 0
    assert framework["class_balance_bonus_waived"] is None


def test_ordering_holds_when_role_affinity_opposes_the_directive(focus, monkeypatch):
    """THE MEASURED TIE, pinned hermetically ().

    zeta's live multipliers (product 0.8 / framework 1.5) oppose the directive
    by 0.7 weighted; the competitor's surviving cbb bonus added 0.8; the
    directive's whole authority is 1.5. 1.5 - 0.7 - 0.8 = 0.0 — an exact tie,
    so the standing user directive decided nothing (assert 9.32 > 9.32, zeta,
    cc-02, 2026-08-12). The prior tests all ran under whatever agent the
    environment supplied (the module-level setdefault pins alpha only when
    MIND_AGENT is unset), which is why this file was green on alpha boxes and
    red on zeta's — the composition defect was agent-conditional. This test
    pins the multipliers instead of inheriting them, so it fails everywhere or
    nowhere.
    """
    focus(DIRECTIVE)
    monkeypatch.setattr(gs, "AGENT_NAME", "opposed-agent")
    monkeypatch.setattr(gs, "AGENT_ROLE_MULTIPLIERS",
                        {"opposed-agent": {"product": 0.8, "framework": 1.5}})
    product = _score("g-335-1", "asp-335", "product")
    framework = _score("g-115-1", "asp-115", "framework", category="framework")
    assert framework["raw"]["role_affinity"] - product["raw"]["role_affinity"] == pytest.approx(0.7), (
        "fixture inert — the multipliers no longer oppose the directive"
    )
    assert product["score"] > framework["score"], (
        "REGRESSION: with role_affinity opposing the directive, the balancer's "
        "surviving bonus half re-erodes the margin to a tie (g-115-6069)"
    )


# ------------------------------------------------------------ structural pins


def test_the_clamp_keys_on_the_addend_not_the_composite():
    """Behavioral cover for the scoping above exists, but only for the shapes the
    fixtures reach. This pins the source form so a refactor toward the composite
    fails loudly rather than passing every test with the wrong predicate."""
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    # Bound the window by the NEXT SECTION MARKER, not a character count. A magic
    # count silently goes vacuous the moment the comment block grows past it —
    # which it already did once here, at 3000 against a 3074-char block. The
    # section marker moves with the code it delimits.
    i = src.index("13b-i.")
    window = src[i:src.index("13c.", i)]
    assert "_sf_boost > 0" in window, (
        "REGRESSION: the class-balance clamp no longer keys on the "
        "strategic_focus addend (g-115-3988 / guard-2412)"
    )


def test_the_telemetry_key_is_not_a_weights_key():
    """guard-760: every WEIGHTS key must have a raw value on EVERY candidate, and
    the L3875 total iterates WEIGHTS. `class_balance_penalty_waived` is written on
    only the waived subset, so promoting it to a criterion would make the sum
    depend on a key most candidates lack. It is telemetry — same posture as
    apply_substantive_demotion's substantive_demotion_pre_score."""
    assert "class_balance_penalty_waived" not in gs.WEIGHTS


def test_the_live_config_still_gives_this_mechanism_a_subject():
    """The fixtures above pin their own class_balance config, so they would keep
    passing against a deployment that had disabled the balancer entirely. This
    reads the LIVE values and asserts the interaction is still reachable — and
    re-asserts the magnitude asymmetry that motivated the clamp, so a retune that
    removes the problem is visible here rather than leaving a fix nobody needs."""
    import yaml
    cfg = yaml.safe_load(
        (CORE_SCRIPTS.parent / "config" / "aspirations.yaml").read_text(
            encoding="utf-8"))
    cb = cfg["class_balance"]
    assert cb["targets"], "class_balance has no targets — the term is inert"
    cb_swing = (cb["max_boost"] - cb["max_penalty"]) * gs.WEIGHTS[
        "class_balance_bonus"]
    directive_authority = (
        cfg["strategic_focus_boost"]["weight"] * gs.WEIGHTS["directive_boost"])
    assert cb_swing > directive_authority, (
        "class_balance can no longer outswing the directive; if this is "
        "deliberate, the 13b-i clamp may be retirable (read its comment first)"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
