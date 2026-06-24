"""Tests for capability_route agent-coverage completion ( / charlie F-001).

Background: commit 38ffa51 / e5590737 dynamized ACTIVE_AGENTS via
_agents.get_active_agents() (reads world/team-state.yaml), but the route tables
were seeded once from the legacy 3-agent vocabulary. The dynamization grew the
active set to six agents while the tables stayed blind to charlie/delta — they
could never be returned as intended_agent. echo was patched separately
(g-115-1060, arc-agi-3 category).

This goal completes the fix in two parts:
  1. Code (this module): _route_covered_agents() / _uncovered_active_agents()
     + a fail-safe module-load stderr warning that names any active agent with
     no route — the systemic detector the dynamization lacked.
  2. Data (world/config/capability-routing.yaml): npc-evaluation -> charlie,
     roblox-integration -> delta (the two unambiguous documented-lane keys).

Coverage-helper cases monkeypatch the tables + active-agent set so they are
independent of the live world overlay (matching test_capability_route.py).
The end-to-end routability cases DO read the live overlay and skip when it is
absent (fresh deployment), since the data fix lives in the domain overlay.
"""
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
for _p in (_SCRIPTS, _SCRIPTS / "gates"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import capability_route as cr  # noqa: E402


# ── _route_covered_agents() — collects every non-"either" route target ──

def test_route_covered_agents_collects_all_table_targets(monkeypatch):
    monkeypatch.setattr(cr, "TITLE_PREFIX_ROUTES", {
        "build:": ("alpha", 0.85, "build -> alpha"),
        "investigate:": ("zeta", 0.88, "investigate -> zeta"),
        "unblock:": ("either", 0.40, "unblock -> either"),
    })
    monkeypatch.setattr(cr, "CATEGORY_ROUTES", {
        "arc-agi-3": ("echo", 0.78, "arc -> echo"),
        "framework-architecture": ("either", 0.45, "any agent"),
    })
    monkeypatch.setattr(cr, "DESCRIPTION_HEURISTICS", [
        ("lua", "delta", 0.10, "lua -> delta"),
    ])
    # "either" is excluded; every distinct agent target is collected.
    assert cr._route_covered_agents() == {"alpha", "zeta", "echo", "delta"}


def test_route_covered_agents_empty_tables_yield_empty_set(monkeypatch):
    monkeypatch.setattr(cr, "TITLE_PREFIX_ROUTES", {})
    monkeypatch.setattr(cr, "CATEGORY_ROUTES", {})
    monkeypatch.setattr(cr, "DESCRIPTION_HEURISTICS", [])
    assert cr._route_covered_agents() == set()


# ── _uncovered_active_agents() — active set minus covered set ──

def test_uncovered_active_agents_flags_routeless(monkeypatch):
    monkeypatch.setattr(cr, "_active_agents",
                        lambda: ("alpha", "bravo", "charlie", "delta", "echo", "zeta"))
    monkeypatch.setattr(cr, "TITLE_PREFIX_ROUTES", {"build:": ("alpha", 0.85, "")})
    monkeypatch.setattr(cr, "CATEGORY_ROUTES", {
        "a": ("bravo", 0.80, ""),
        "b": ("zeta", 0.80, ""),
        "c": ("echo", 0.78, ""),
    })
    monkeypatch.setattr(cr, "DESCRIPTION_HEURISTICS", [])
    # charlie + delta have no route — flagged, sorted, stable.
    assert cr._uncovered_active_agents() == ("charlie", "delta")


def test_uncovered_active_agents_empty_when_all_covered(monkeypatch):
    monkeypatch.setattr(cr, "_active_agents", lambda: ("alpha", "charlie"))
    monkeypatch.setattr(cr, "TITLE_PREFIX_ROUTES", {"build:": ("alpha", 0.85, "")})
    monkeypatch.setattr(cr, "CATEGORY_ROUTES", {"npc-evaluation": ("charlie", 0.75, "")})
    monkeypatch.setattr(cr, "DESCRIPTION_HEURISTICS", [])
    assert cr._uncovered_active_agents() == ()


def test_uncovered_active_agents_either_only_route_does_not_cover(monkeypatch):
    # An agent is only "covered" by a route that names IT — a category routing
    # to "either" does not cover any specific agent.
    monkeypatch.setattr(cr, "_active_agents", lambda: ("alpha", "delta"))
    monkeypatch.setattr(cr, "TITLE_PREFIX_ROUTES", {"build:": ("alpha", 0.85, "")})
    monkeypatch.setattr(cr, "CATEGORY_ROUTES", {"x": ("either", 0.45, "")})
    monkeypatch.setattr(cr, "DESCRIPTION_HEURISTICS", [])
    assert cr._uncovered_active_agents() == ("delta",)


# ── end-to-end routability after the world-overlay data fix ──

def test_charlie_routable_via_npc_evaluation():
    if "npc-evaluation" not in cr.CATEGORY_ROUTES:
        pytest.skip("capability-routing overlay absent (fresh deployment)")
    r = cr.evaluate("Evaluate NPC believability gap", category="npc-evaluation")
    assert r["intended_agent"] == "charlie", r


def test_delta_routable_via_roblox_integration():
    if "roblox-integration" not in cr.CATEGORY_ROUTES:
        pytest.skip("capability-routing overlay absent (fresh deployment)")
    # Category-only goal (no strong dev title-prefix to win Tier 1) -> delta.
    r = cr.evaluate("NPC in-world reaction polish", category="roblox-integration")
    assert r["intended_agent"] == "delta", r


def test_charlie_and_delta_present_in_live_route_tables():
    # Structural assertion against the live overlay: after the data fix the two
    # previously-unroutable agents now appear as route targets. Skips on a
    # fresh deployment where the overlay (and thus all category routes) is empty.
    if not cr.CATEGORY_ROUTES:
        pytest.skip("capability-routing overlay absent (fresh deployment)")
    covered = cr._route_covered_agents()
    assert "charlie" in covered, covered
    assert "delta" in covered, covered


# ── regression: pre-existing routes unaffected by the additions ──

def test_existing_routes_intact():
    if not cr.TITLE_PREFIX_ROUTES:
        pytest.skip("capability-routing overlay absent (fresh deployment)")
    assert cr.evaluate("Investigate: flaky selector")["intended_agent"] == "zeta"
    assert cr.evaluate("Build: new daemon endpoint")["intended_agent"] == "alpha"
    if "arc-agi-3" in cr.CATEGORY_ROUTES:
        assert cr.evaluate("x", category="arc-agi-3")["intended_agent"] == "echo"
