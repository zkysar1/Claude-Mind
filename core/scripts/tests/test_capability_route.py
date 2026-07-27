"""Tests for capability_route Tier 3 high-delta override ().

Validates the structural classifier fix: a high-conviction Tier 3 description
heuristic (delta >= 0.30) overrides a low-confidence Tier 1/2 signal
(best_conf < 0.85), while a strong Tier 1 prefix (>= 0.85) still wins.

Routing tables and the active-agent set are monkeypatched to controlled
fixtures so the cases are independent of the live world overlay
(world/config/capability-routing.yaml).
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
for _p in (_SCRIPTS, _SCRIPTS / "gates"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import capability_route as cr  # noqa: E402


def _setup(monkeypatch):
    """Controlled routing tables — independent of the live world overlay."""
    monkeypatch.setattr(cr, "_active_agents",
                        lambda: ("alpha", "bravo", "zeta", "echo"))
    monkeypatch.setattr(cr, "TITLE_PREFIX_ROUTES", {
        "apply:": ("alpha", 0.80, "apply prefix -> implementer alpha"),
        "build:": ("alpha", 0.80, "build prefix -> implementer alpha"),
        "investigate:": ("zeta", 0.88, "investigate prefix -> zeta"),
    })
    monkeypatch.setattr(cr, "CATEGORY_ROUTES", {})
    monkeypatch.setattr(cr, "DESCRIPTION_HEURISTICS", [
        ("solver_v0", "echo", 0.35, "solver_v0 -> echo (arc-agi-3)"),
        ("owner echo", "echo", 0.30, "owner echo -> echo"),
        ("arc-vertical", "zeta", 0.20, "arc-vertical -> zeta"),
    ])


def test_apply_solver_v0_owner_echo_overrides_to_echo(monkeypatch):
    """Case 1: Apply prefix (alpha 0.80) + solver_v0/owner-echo heuristics
    (echo, delta 0.35) -> FLIP to echo (override)."""
    _setup(monkeypatch)
    r = cr._classify(
        "Apply: wire solver_v0 entrypoint",
        "framework-decomposition",
        "owner echo; solver_v0 harness for arc-agi-3",
    )
    assert r["intended_agent"] == "echo", r
    assert "OVERRIDE" in r["rationale"], r


def test_build_owner_alpha_reinforces_no_flip(monkeypatch):
    """Case 2: Build prefix (alpha 0.80) + alpha-targeted heuristic -> reinforce
    (no flip needed; best_agent already == heuristic agent)."""
    _setup(monkeypatch)
    monkeypatch.setattr(cr, "DESCRIPTION_HEURISTICS", [
        ("bash hook", "alpha", 0.30, "bash hook -> alpha"),
    ])
    r = cr._classify(
        "Build: bash hook for agent inject",
        "framework-loop",
        "bash hook owned by alpha",
    )
    assert r["intended_agent"] == "alpha", r
    assert "OVERRIDE" not in r["rationale"], r


def test_investigate_high_tier1_conf_wins_no_override(monkeypatch):
    """Case 3: Investigate prefix (zeta 0.88 >= 0.85) + conflicting echo
    heuristic -> Tier 1 confidence wins, no override."""
    _setup(monkeypatch)
    r = cr._classify(
        "Investigate: arc-vertical perception gap",
        "framework-decomposition",
        "solver_v0 owner echo signal present",
    )
    assert r["intended_agent"] == "zeta", r
    assert "OVERRIDE" not in r["rationale"], r
    assert "kept zeta" in r["rationale"], r


def test_apply_framework_no_heuristic_regression(monkeypatch):
    """Case 4 (regression): Apply prefix (alpha) + no matching heuristic phrase
    -> stays alpha, unchanged behavior."""
    _setup(monkeypatch)
    r = cr._classify(
        "Apply: framework fix for loop-state",
        "framework-decomposition",
        "patch the loop_state serialization path",
    )
    assert r["intended_agent"] == "alpha", r
    assert "OVERRIDE" not in r["rationale"], r


def test_vertical_category_overrides_generic_prefix(monkeypatch):
    """ (Tier-2 vertical override): a high-conf vertical category
    (npc-behavior -> foxtrot, 0.80 >= CATEGORY_VERTICAL_OVERRIDE_CONF) overrides
    the generic 'investigate:'->zeta title prefix (Tier 1, 0.88). Before the fix
    foxtrot's 'Investigate:' npc-behavior goals routed to zeta, causing
    cross_lane_refused on every foxtrot claim attempt."""
    _setup(monkeypatch)
    monkeypatch.setattr(cr, "_active_agents",
                        lambda: ("alpha", "bravo", "zeta", "echo", "foxtrot"))
    monkeypatch.setattr(cr, "CATEGORY_ROUTES", {
        "npc-behavior": ("foxtrot", 0.80, "npc-behavior -> foxtrot vertical"),
    })
    r = cr._classify(
        "Investigate: original-cohort dip in the two 2026-07 sessions",
        "npc-behavior",
        "OHS regression analysis",
    )
    assert r["intended_agent"] == "foxtrot", r
    assert "OVERRIDE" in r["rationale"], r


def test_subthreshold_category_no_override(monkeypatch):
    """Threshold guard: a specific-agent category BELOW
    CATEGORY_VERTICAL_OVERRIDE_CONF (0.55 < 0.75) does NOT override the title
    prefix — the generic work-type still wins for non-vertical categories, so
    the override cannot creep onto weak categorizations."""
    _setup(monkeypatch)
    monkeypatch.setattr(cr, "CATEGORY_ROUTES", {
        "npc-intelligence": ("alpha", 0.55, "npc-intelligence -> alpha (weak)"),
    })
    r = cr._classify(
        "Investigate: npc-intelligence benchmark drift",
        "npc-intelligence",
        "",
    )
    assert r["intended_agent"] == "zeta", r
    assert "OVERRIDE" not in r["rationale"], r


def test_boundary_category_at_threshold_overrides_prefix(monkeypatch):
    """ (threshold boundary): a vertical category at EXACTLY
    CATEGORY_VERTICAL_OVERRIDE_CONF (client-runtime-regression -> foxtrot, 0.75)
    overrides a generic 'fix:'->alpha title prefix. The override branch gates on
    cat_conf >= CATEGORY_VERTICAL_OVERRIDE_CONF, so 0.75 (the exact threshold)
    MUST fire — this is the client-runtime-regression bump 0.70->0.75 that lifts
    foxtrot's client/runtime vertical above a generic Fix: prefix (alpha cannot
    reach the product repo). Guards the >= (not >) comparison against regression."""
    _setup(monkeypatch)
    monkeypatch.setattr(cr, "_active_agents",
                        lambda: ("alpha", "bravo", "zeta", "echo", "foxtrot"))
    monkeypatch.setattr(cr, "TITLE_PREFIX_ROUTES", {
        "fix:": ("alpha", 0.82, "fix prefix -> implementer alpha"),
    })
    monkeypatch.setattr(cr, "CATEGORY_ROUTES", {
        "client-runtime-regression": ("foxtrot", 0.75, "client-runtime -> foxtrot vertical"),
    })
    r = cr._classify(
        "Fix: client-runtime regression in NPC behavior client surface",
        "client-runtime-regression",
        "regression in the Roblox client runtime",
    )
    assert r["intended_agent"] == "foxtrot", r
    assert "OVERRIDE" in r["rationale"], r
