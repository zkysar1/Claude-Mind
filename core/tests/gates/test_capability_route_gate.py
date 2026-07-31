"""Equivalence + behavior tests for capability_route gate (PR 7c/3).

Classifier — always returns a dict with intended_agent + confidence +
rationale. No would_block field, no override audit. Exit code from CLI is
always 0.

Tier hierarchy (Tier 1 wins on conflict):
  Tier 1: title verb prefix (strongest)
  Tier 2: category (medium)
  Tier 3: description heuristic phrases (weakest; can break 'either' ties)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
CLI = SCRIPTS_DIR / "capability-route-gate.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from gates.capability_route import evaluate, ACTIVE_AGENTS  # noqa: E402


def _run_cli(*, title: str, category: str = "", description: str = "",
             route_to: str | None = None) -> tuple[int, dict]:
    args = [sys.executable, str(CLI), "--title", title]
    if category:
        args.extend(["--category", category])
    if description:
        args.extend(["--description", description])
    if route_to:
        args.extend(["--route-to", route_to])
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    return proc.returncode, json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Tier 1: title prefix routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected_agent,min_conf", [
    ("Investigate: Sound NullInput x6", "zeta", 0.85),
    ("Research: failure modes", "zeta", 0.80),
    ("Audit: completed Investigates", "bravo", 0.75),
    ("Decompose: g-001-99 into primitives", "zeta", 0.80),
    ("Build: tree-find-node script", "alpha", 0.80),
    ("Fix: broken state file", "alpha", 0.80),
    ("Implement: caching layer", "alpha", 0.85),
    ("Deploy: roblox-bridge update", "alpha", 0.80),
    ("Strategic: next sprint priorities", "bravo", 0.85),
    ("Prioritize: aspirations backlog", "bravo", 0.80),
    ("Monitor: infra-health.sh failures", "bravo", 0.60),
    # Ambiguous cognitive primitives → 'either' with low confidence
    ("Unblock: EFS reconnect", "either", 0.35),
    ("Idea: try a new approach", "either", 0.35),
    ("Maintain: weekly cleanup", "either", 0.40),
])
def test_title_prefix_routing(title, expected_agent, min_conf):
    out = evaluate(title)
    assert out["intended_agent"] == expected_agent, out["rationale"]
    assert out["confidence"] >= min_conf


def test_no_recognized_prefix_defaults_either():
    out = evaluate("just some title with no verb")
    assert out["intended_agent"] == "either"
    assert out["confidence"] == 0.30
    assert "defaulting to 'either'" in out["rationale"]


# ---------------------------------------------------------------------------
# Tier 2: category routing
# ---------------------------------------------------------------------------

def test_category_resolves_either_to_specific_agent():
    """Title prefix 'Unblock:' returns either; category 'infrastructure-
    monitoring' (bravo's lane) should resolve the choice."""
    out = evaluate("Unblock: monitor failures", category="infrastructure-monitoring")
    assert out["intended_agent"] == "bravo"
    # confidence: max(0.40 Tier-1, 0.80 Tier-2 cat-override) = 0.80
    assert out["confidence"] >= 0.78


def test_category_reinforces_tier_1():
    """Title says zeta (Investigate), category investigation-methodology
    also says zeta → confidence bump."""
    out = evaluate("Investigate: methodology gaps",
                   category="investigation-methodology")
    assert out["intended_agent"] == "zeta"
    # Tier 1 confidence 0.88, reinforcing bump capped at 0.95
    assert out["confidence"] >= 0.88


def test_tier_1_wins_over_tier_2_conflict():
    """Build: (alpha) + investigation-methodology (zeta) → Tier 1 wins."""
    out = evaluate("Build: investigation script",
                   category="investigation-methodology")
    assert out["intended_agent"] == "alpha"


# ---------------------------------------------------------------------------
# Tier 3: description heuristic biases
# ---------------------------------------------------------------------------

def test_description_bias_breaks_either_tie():
    """Idea: + 'cross-cutting refactor' → zeta bias should win."""
    out = evaluate("Idea: refactor session state",
                   description="this is a cross-cutting refactor across all agents")
    assert out["intended_agent"] == "zeta"


def test_description_bias_cannot_flip_strong_tier_1():
    """Build: (alpha 0.85) + 'code archaeology' (zeta bias 0.12) →
    alpha still wins, but rationale notes the disagreement."""
    out = evaluate("Build: a tool",
                   description="this is code archaeology — analyzing history")
    assert out["intended_agent"] == "alpha"
    assert "kept alpha despite zeta-bias" in out["rationale"]


def test_owner_naming_routes_explicitly():
    """'owner alpha' in description has weight 0.30 — strong enough to
    pull 'either' from Tier 1 into alpha."""
    out = evaluate("Idea: something to do",
                   description="owner alpha — they're best for this")
    assert out["intended_agent"] == "alpha"
    assert out["confidence"] >= 0.50


# ---------------------------------------------------------------------------
# Explicit override (--route-to)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("override", ["alpha", "bravo", "zeta", "either"])
def test_route_to_override_bypasses_classifier(override):
    """--route-to alpha must return alpha regardless of title/category."""
    out = evaluate("Investigate: foo",  # would normally → zeta
                   category="infrastructure-monitoring",  # would normally → bravo
                   description="cross-cutting refactor",  # would normally → zeta
                   route_to=override)
    assert out["intended_agent"] == override
    assert out["confidence"] == 1.0
    assert "--route-to override" in out["rationale"]


def test_invalid_route_to_falls_through_to_classifier():
    """route_to='garbage' must NOT be returned — classifier runs normally."""
    out = evaluate("Investigate: foo", route_to="garbage")
    # Classifier sees Investigate: → zeta
    assert out["intended_agent"] == "zeta"


# ---------------------------------------------------------------------------
# CLI vs module equivalence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,category,description", [
    ("Investigate: foo", "", ""),
    ("Build: tool", "framework-engineering", "clear requirements"),
    ("Idea: improvement", "", "code archaeology"),
    ("Unblock: EFS", "infrastructure-monitoring", ""),
    ("just a title", "", ""),
])
def test_cli_module_equivalent(title, category, description):
    rc, cli_out = _run_cli(title=title, category=category, description=description)
    mod_out = evaluate(title, category=category, description=description)
    assert rc == 0
    assert cli_out == mod_out


def test_cli_route_to_equivalent():
    rc, cli_out = _run_cli(title="Investigate: foo", route_to="alpha")
    mod_out = evaluate("Investigate: foo", route_to="alpha")
    assert rc == 0
    assert cli_out == mod_out
    assert cli_out["intended_agent"] == "alpha"


def test_cli_falls_through_invalid_route_to():
    """CLI now matches module behavior — argparse choices= was removed in
    fresh-eyes review MEDIUM M1 (2026-05-18) so a freshly-/started agent
    isn't rejected by parse-time choices snapshot. Unknown route_to falls
    through to the classifier (CLI exits 0 with classifier's verdict)."""
    proc = subprocess.run(
        [sys.executable, str(CLI), "--title", "Investigate: foo",
         "--route-to", "garbage"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    # Classifier sees Investigate: → routes per title-prefix table (zeta in
    # current deployment). The garbage route_to must NOT poison the result.
    assert out["intended_agent"] != "garbage"
    assert "intended_agent" in out
    assert "confidence" in out


# ---------------------------------------------------------------------------
# All active agents are valid outputs
# ---------------------------------------------------------------------------

def test_active_agents_tripwire():
    """Tripwire: if the active agent set changes, this test fails to remind
    the author to update the routing tables (world/config/capability-routing.yaml)
    AND the parametrized tests above. The baseline is updated by hand each
    time agents are added or removed — that handwork is the forcing function.

    Previous baseline ('alpha', 'bravo', 'zeta') was last refreshed when only
    three agents existed. Refreshed 2026-05-20 after charlie/delta/echo were
    added; new entries need title-prefix routes if they take goal classes
    that today route to 'either'.

    Refreshed 2026-07-31 (g-115-3748) after charlie+delta merged into foxtrot
    on 2026-07-07. It had been red 24 days, unseen: run-full-suite.sh collected
    only 1 of the 3 testpaths pytest.ini declares, so nothing ran this file.

    Worth recording what the red actually caught, because it is the argument
    for keeping a hand-maintained baseline. Two causes were stacked in it:
    the expected roster drift above, AND a phantom agent 'test-race-5' that
    test_team_state_race.py had leaked into the LIVE world/team-state
    (a fixture row that outlived its own cleanup, carrying a stale in_flight
    claim on a fake goal for ~5.8h while capability_route warned on every
    import that it could never be routed to). The tripwire was the only thing
    that noticed. Purge live pollution BEFORE re-baselining -- baking a test
    artifact into this tuple would have silenced the detector with the very
    defect it detected.
    """
    assert ACTIVE_AGENTS == ("alpha", "bravo", "echo", "foxtrot", "zeta")
