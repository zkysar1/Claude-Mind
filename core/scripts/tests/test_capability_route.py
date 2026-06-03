"""Tests for capability_route Tier 3 high-delta override (4).

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
