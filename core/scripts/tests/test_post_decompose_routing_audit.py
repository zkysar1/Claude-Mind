"""test_post_decompose_routing_audit.py — regression tests for 5.

Covers the 5 acceptance cases enumerated in the goal description:
  (1) solver_v0 stamped alpha but echo matches better -> file
  (2) framework-edit stamped bravo + bravo matches -> no-file
  (3) generic Maintain no clear domain -> no-file (gap below threshold)
  (4) echo abstained recently on same goal -> no-file (already routed)
  (5) fail-open on unreadable Self.md -> no-file

Pattern: import the audit module dynamically (hyphenated filename → can't
`from post-decompose-routing-audit import ...`), build a synthetic
project_root with per-agent Self.md, monkeypatch the agent-list resolver,
call audit() and assert the returned decision.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
AUDIT_PATH = CORE_SCRIPTS / "post-decompose-routing-audit.py"


def _load_audit_module():
    """Dynamically import the audit script as `audit_mod`."""
    spec = importlib.util.spec_from_file_location(
        "post_decompose_routing_audit_module", AUDIT_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Synthetic Self.md content — concentrated domain tokens that the tests
# rely on for predictable scoring. Real Self.md content is more verbose;
# these stubs surface ONLY the tokens each test needs.

_ALPHA_SELFMD = """---
created: "2026-05-15"
---

# Self

I am Alpha — the server/backend developer.

## What I Do

I own the server side: `Ayoai-Environment-Processor`,
`Ayoai-Environment-Server`, `Ayoai-Operator`, BitNet server, processor
pipelines, gateway lambdas, database migrations.

## Boundaries With My Teammates

Delta owns the client/runtime side (Roblox/Lua/luau). Echo owns the
ARC-AGI-3 vertical end-to-end (solver, integration, deployment). I do
NOT touch their lanes unless overflow.
"""

_BRAVO_SELFMD = """---
created: "2026-05-15"
---

# Self

I am Bravo — Product Manager, Business Analyst, and Knowledge-Tree Steward.

## What I Do

Find the right work across product+framework. Audit quality. Research
breadth-first. Edit framework conventions, knowledge-tree nodes,
hygiene goals, decomposition routes, Self.md edits. Maintain
capability-routing tables.

## Boundaries With My Teammates

Alpha/Delta do the heavy code. I do the planning, auditing, and tree
stewardship.
"""

_DELTA_SELFMD = """---
created: "2026-05-15"
---

# Self

I am Delta — the client/runtime developer (Roblox / Lua / luau / NPC).

## Lane Split With Alpha

Roblox-Integration, NPC behavior, Lua, luau, client-facing surfaces.

## What I Don't Do

Server / Processor / BitNet — Alpha's lane.
"""

_ECHO_SELFMD = """---
created: "2026-05-16"
---

# Self

I am Echo — ARC-AGI-3 Vertical Owner. I own the solver, the integration,
the deployment, the game-testing.

## What I Do

Build the solver_v0 — a hand-built neural tree for ARC-AGI-3 environments.
Build the Ayoai-ARC-AGI-3-Integration client adapter. Run game sessions
against the ARC API. Score solvers against the ARC benchmark.

## Boundaries With My Teammates

Alpha owns Environment-Server/Processor. Delta owns Roblox/NPC. I do NOT
touch their lanes unless ARC-vertical overflow forces it.
"""


def _seed_project_root(tmp_path: Path, agents: dict) -> Path:
    """Create a synthetic project_root/agents/<name>/self.md tree."""
    agents_root = tmp_path / "agents"
    agents_root.mkdir(parents=True, exist_ok=True)
    for name, body in agents.items():
        agent_dir = agents_root / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "self.md").write_text(body, encoding="utf-8")
    return tmp_path


@pytest.fixture
def audit_mod(monkeypatch):
    """Load the audit module and freeze the active-agents resolver to a
    fixed tuple, so test outcomes don't depend on the live team-state.yaml."""
    mod = _load_audit_module()
    monkeypatch.setattr(mod, "_active_agents", lambda _root: (
        "alpha", "bravo", "delta", "echo",
    ))
    return mod


# ---------------------------------------------------------------------------
# Case 1: solver_v0 stamped alpha but echo matches better → file
# ---------------------------------------------------------------------------

def test_case1_solver_v0_stamped_alpha_echo_better_matches_files(
    tmp_path, audit_mod,
):
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-315-99",
        "title": "Apply: solver_v0 client adapter — neural tree wiring",
        "description": (
            "Wire the solver_v0 hand-built neural tree into the "
            "Ayoai-ARC-AGI-3-Integration client adapter so ARC game "
            "sessions route per-tick frames through the the framework server."
        ),
        "intended_agent": "alpha",
    }
    decision = audit_mod.audit(goal, project_root=project_root)
    assert decision["decision"] == "file", (
        f"Expected file, got {decision['decision']}: reason={decision['reason']} "
        f"scores={decision['scores']}"
    )
    assert decision["best_agent"] == "echo", decision
    # 0: metric is now Jaccard (float in [0,1]); echo's stand-out gap
    # (~0.26) clears the default min_gap 0.015. Was `>= 5` on the raw scale.
    assert decision["gap"] >= 0.015, decision
    assert decision["investigate_spec"] is not None
    spec = decision["investigate_spec"]
    assert spec["origin_signal"] == "routing-mismatch:g-315-99"
    assert spec["priority"] == "MEDIUM"
    assert spec["participants"] == ["agent"]
    assert "g-315-99" in spec["title"]
    assert "alpha" in spec["title"]
    assert "echo" in spec["title"]


# ---------------------------------------------------------------------------
# Case 2: framework-edit stamped bravo + bravo matches → no-file
# ---------------------------------------------------------------------------

def test_case2_framework_edit_stamped_bravo_matches_no_file(
    tmp_path, audit_mod,
):
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-115-2000",
        "title": "Maintain: extend capability-routing tables and audit hygiene",
        "description": (
            "Edit framework conventions and capability-routing.yaml to "
            "include the latest decomposition routes. Audit the quality "
            "of the planning lane."
        ),
        "intended_agent": "bravo",
    }
    decision = audit_mod.audit(goal, project_root=project_root)
    assert decision["decision"] == "no_file", decision
    # Stamped agent should be the best match (or tied for best).
    assert decision["best_agent"] == "bravo", decision
    assert decision["investigate_spec"] is None


# ---------------------------------------------------------------------------
# Case 3: generic Maintain no clear domain → no-file (gap below threshold)
# ---------------------------------------------------------------------------

def test_case3_generic_maintain_no_clear_domain_no_file(
    tmp_path, audit_mod,
):
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-115-2001",
        "title": "Maintain: refresh meta-strategy timestamps",
        "description": (
            "Update last_updated stamps on the meta strategy files. Nothing "
            "domain-specific; routine touch."
        ),
        "intended_agent": "bravo",
    }
    decision = audit_mod.audit(goal, project_root=project_root)
    assert decision["decision"] == "no_file", decision
    # 0: a routine timestamp-refresh has no clear domain owner, so the
    # best-vs-stamped Jaccard gap is tiny (~0.003) and sits below the default
    # min_gap 0.015 → no_file. Was `< 5` on the raw set-intersection scale.
    assert decision["gap"] < 0.015, decision
    assert decision["investigate_spec"] is None


# ---------------------------------------------------------------------------
# 5: specific-agent gap of exactly 3 is now below min_gap=5 → no-file
# ---------------------------------------------------------------------------

def test_g1245_specific_agent_weak_gap_below_min_gap_no_file(tmp_path, audit_mod):
    """A weak specific-agent Jaccard gap must NOT file — a fuzzy vocabulary
    overlap must not second-guess capability_route's curated stamp.

    g-115-1200 normalized the metric to Jaccard (was raw set-intersection in
    the g-115-1245 era, where this fixture's gap read as an integer +3). The
    goal is stamped alpha (Jaccard 0.0) but echo shares score/solvers/benchmark
    tokens (Jaccard ~0.091, gap ~0.091). On the synthetic-stub scale that gap
    sits below an explicit 0.15 floor, so the audit stays quiet. (Production
    agents carry ~400-660 domain tokens, so the same 3-token overlap yields a
    far smaller Jaccard (~0.006) that the production default min_gap=0.015
    suppresses directly — the synthetic stub's tiny vocab inflates the ratio,
    hence the explicit threshold here.)
    """
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-999-01",
        "title": "Apply: refresh score tracking",
        "description": "Update solvers benchmark tracking.",
        "intended_agent": "alpha",
    }
    # min_best_score=0.0 disables the orthogonal absolute-floor (1):
    # echo's synthetic Jaccard (~0.091) sits below the production floor 0.10, so
    # without this the floor would suppress with a "min_best_score" reason and
    # this test (which isolates the GAP-threshold mechanism) would never reach
    # the gap branch. The floor has its own dedicated tests below.
    decision = audit_mod.audit(goal, project_root=project_root,
                               min_gap=0.15, min_best_score=0.0)
    assert decision["decision"] == "no_file", decision
    assert decision["best_agent"] == "echo", decision
    assert decision["gap"] < 0.15, decision
    assert "min_gap" in decision["reason"], decision
    assert decision["investigate_spec"] is None
    # Sanity: with an explicit lower threshold the SAME fixture files --
    # proving the gap is real and only the threshold suppresses it.
    legacy = audit_mod.audit(goal, project_root=project_root,
                             min_gap=0.05, min_best_score=0.0)
    assert legacy["decision"] == "file", legacy
    assert legacy["best_agent"] == "echo", legacy


# ---------------------------------------------------------------------------
# Case 4: echo abstained recently on same goal → no-file (already routed)
# ---------------------------------------------------------------------------

def test_case4_already_abstained_no_file(tmp_path, audit_mod):
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-315-100",
        "title": "Apply: solver_v0 follow-up — second module integration",
        "description": "ARC-AGI-3 solver second module — wire neural tree.",
        "intended_agent": "alpha",
        "abstained_by": "echo",   # already routed signal
    }
    decision = audit_mod.audit(goal, project_root=project_root)
    assert decision["decision"] == "no_file", decision
    assert "abstained" in decision["reason"], decision


# Bonus case 4c: origin_signal routing-mismatch: short-circuits (recursion guard).
def test_case4c_recursion_guard_no_file(tmp_path, audit_mod):
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-115-2099",
        "title": "Investigate: routing-mismatch g-315-99 intended_agent=alpha but echo's Self.md domains match",
        "description": "Audit detected a routing mismatch on a solver_v0 goal.",
        "intended_agent": "bravo",
        "origin_signal": "routing-mismatch:g-315-99",
    }
    decision = audit_mod.audit(goal, project_root=project_root)
    assert decision["decision"] == "no_file", decision
    assert "recursion" in decision["reason"].lower(), decision


# Bonus case 4b: handoff_to also short-circuits.
def test_case4b_already_handoff_to_no_file(tmp_path, audit_mod):
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-115-2002",
        "title": "Apply: solver_v0 perception layer for echo",
        "description": "solver_v0 perception layer for ARC-AGI-3 integration.",
        "intended_agent": "alpha",
        "handoff_to": "echo",   # already routed signal
    }
    decision = audit_mod.audit(goal, project_root=project_root)
    assert decision["decision"] == "no_file", decision
    assert "handoff_to" in decision["reason"], decision


# ---------------------------------------------------------------------------
# Case 5: fail-open on unreadable Self.md → no-file
# ---------------------------------------------------------------------------

def test_case5_unreadable_selfmd_fail_open_no_file(tmp_path, audit_mod):
    """One agent's self.md is a directory (read fails). audit must not crash."""
    # Seed three agents with proper self.md and ONE (delta) with a directory
    # in its place so .read_text raises IsADirectoryError on Linux /
    # PermissionError on Windows.
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "echo": _ECHO_SELFMD,
    })
    delta_self_md_path = project_root / "agents" / "delta" / "self.md"
    delta_self_md_path.mkdir(parents=True, exist_ok=True)   # not a file!

    goal = {
        "id": "g-115-2003",
        "title": "Apply: client/runtime Roblox NPC behavior tweak",
        "description": "Edit luau NPC behavior tree in Roblox-Integration.",
        "intended_agent": "alpha",
    }
    # Must not raise; delta's score collapses to 0; alpha may or may not be
    # the best, but the function must complete cleanly.
    decision = audit_mod.audit(goal, project_root=project_root)
    # Just enforce the fail-open contract — no exception, and a valid schema.
    assert decision["decision"] in ("file", "no_file"), decision
    assert "scores" in decision and isinstance(decision["scores"], dict)
    # Delta's score must be 0 because its self.md is unreadable.
    assert decision["scores"].get("delta", -1) == 0, decision


# ---------------------------------------------------------------------------
# 2: either-case stand-out path
# ---------------------------------------------------------------------------

def test_g1122_either_strong_match_fires_file(tmp_path, audit_mod):
    """either + clear stand-out (Jaccard gap >= min_gap_either=0.015) fires Investigate.

    Goal text packs echo-domain tokens (solver_v0, neural, tree, benchmark,
    Ayoai-ARC-AGI-3-Integration, game sessions, adapter, deployment) so
    echo's Jaccard overlap is strongly dominant over second-best
    (stand-out gap ~0.26, well above the default 0.015; g-115-1200).
    """
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-115-2200",
        "title": "Apply: solver_v0 neural tree wiring",
        "description": (
            "Wire the solver_v0 hand-built neural tree benchmark into "
            "the Ayoai-ARC-AGI-3-Integration client adapter for ARC game "
            "sessions deployment."
        ),
        "intended_agent": "either",
    }
    decision = audit_mod.audit(goal, project_root=project_root)
    assert decision["decision"] == "file", (
        f"Expected file, got {decision['decision']}: reason="
        f"{decision['reason']} scores={decision['scores']}"
    )
    assert decision["best_agent"] == "echo", decision
    assert decision["gap"] >= 0.015, decision  # 0 Jaccard scale (~0.26)
    assert decision["stamped_score"] == 0, (
        "either-case stamped_score is 0 by convention: "
        f"{decision['stamped_score']}"
    )
    assert decision["investigate_spec"] is not None
    spec = decision["investigate_spec"]
    assert spec["origin_signal"] == "routing-either-resolve:g-115-2200"
    assert spec["priority"] == "MEDIUM"
    assert spec["participants"] == ["agent"]
    assert "g-115-2200" in spec["title"]
    assert "either" in spec["title"]
    assert "echo" in spec["title"]


def test_g1122_either_weak_match_below_threshold_no_file(tmp_path, audit_mod):
    """either + Jaccard gap below min_gap_either must noop (signal-ambiguous case).

    Goal text matches echo by a small margin only (score, solvers, benchmark
    overlap), and second-best is 0 — synthetic-stub Jaccard gap ~0.091. Under
    an explicit 0.15 floor that gap is suppressed. (g-115-1200: production
    agents' larger vocab would make the same overlap ~0.006, below the default
    min_gap_either 0.015; the synthetic stub's tiny vocab inflates the ratio,
    so the test passes an explicit fixture-scale threshold.) The whole point of
    the threshold is to keep weak-signal either-cases quiet.
    """
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-115-2201",
        "title": "Apply: refresh score tracking",
        "description": "Update solvers benchmark tracking.",
        "intended_agent": "either",
    }
    # min_best_score=0.0 disables the orthogonal absolute-floor (1) so
    # this test isolates the either-case GAP-threshold mechanism (echo's
    # synthetic Jaccard ~0.091 is below the production floor 0.10; the floor has
    # its own dedicated tests below).
    decision = audit_mod.audit(goal, project_root=project_root,
                               min_gap_either=0.15, min_best_score=0.0)
    assert decision["decision"] == "no_file", decision
    assert decision["gap"] < 0.15, decision
    assert "min_gap_either" in decision["reason"], decision
    assert decision["investigate_spec"] is None


def test_g1122_either_zero_signal_no_file(tmp_path, audit_mod):
    """either + no domain tokens at all must noop (signal-empty case).

    Title 'x' + description 'y' have no tokens >= 4 chars. Falls through to
    the existing "no signal tokens" path BEFORE the either-case scoring
    runs — defends the cheap exit even when the new code path is active.
    """
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-115-2202",
        "title": "x",
        "description": "y",
        "intended_agent": "either",
    }
    decision = audit_mod.audit(goal, project_root=project_root)
    assert decision["decision"] == "no_file", decision
    assert "signal tokens" in decision["reason"], decision
    assert decision["investigate_spec"] is None


def test_g1122_either_resolve_recursion_guard_no_file(tmp_path, audit_mod):
    """Goal carrying origin_signal=routing-either-resolve:* short-circuits.

    Without this guard, an audit-filed either-resolve Investigate (which
    aspirations_write.py stamps intended_agent=bravo) would re-trigger the
    audit on itself if its description happened to mention a different
    agent's Self.md domain tokens — producing an infinite filing chain.
    The recursion guard applies to BOTH "routing-mismatch:" (existing) and
    "routing-either-resolve:" (new, g-115-1122) prefixes.
    """
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-115-2203",
        "title": (
            "Investigate: routing-either-resolve g-115-2200 "
            "intended_agent=either but echo's Self.md domains strongly match"
        ),
        "description": (
            "Audit detected an either-case stand-out on a solver_v0 goal."
        ),
        "intended_agent": "bravo",
        "origin_signal": "routing-either-resolve:g-115-2200",
    }
    decision = audit_mod.audit(goal, project_root=project_root)
    assert decision["decision"] == "no_file", decision
    assert "recursion" in decision["reason"].lower(), decision
    assert decision["investigate_spec"] is None


# ---------------------------------------------------------------------------
# Schema contract: every decision dict has the documented keys
# ---------------------------------------------------------------------------

def test_decision_schema_complete(tmp_path, audit_mod):
    """Every code path must return a dict with the documented key set."""
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD,
    })
    expected_keys = {
        "decision", "reason", "stamped_agent", "best_agent",
        "best_score", "stamped_score", "gap", "scores", "goal_id",
        "investigate_spec",
    }
    # bad input
    d = audit_mod.audit("not a dict", project_root=project_root)
    assert set(d.keys()) == expected_keys
    # no intended_agent
    d = audit_mod.audit({"id": "g-115-1", "title": "x"},
                        project_root=project_root)
    assert set(d.keys()) == expected_keys
    # 'either' sentinel
    d = audit_mod.audit({"id": "g-115-2", "intended_agent": "either",
                         "title": "x", "description": "y"},
                        project_root=project_root)
    assert set(d.keys()) == expected_keys


# ---------------------------------------------------------------------------
# 1 / rb-1488: absolute min_best_score floor + insight-trigger guard
# ---------------------------------------------------------------------------

def test_g1351_floor_suppresses_nearzero_specific_agent(tmp_path, audit_mod):
    """A near-zero absolute best_score with a wide GAP must be suppressed by the
    min_best_score floor even though the gap clears min_gap. This is the FP
    shape (best ~0.073-0.081 production / ~0.091 synthetic) that grew the
    routing-mismatch FP cluster to an 82% all-time skip rate (rb-1478)."""
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-999-floor",
        "title": "Apply: refresh score tracking",
        "description": "Update solvers benchmark tracking.",
        "intended_agent": "alpha",
    }
    # Gap (~0.091) clears min_gap=0.05, so WITHOUT the floor this files --
    # proving the gap is real and only the floor suppresses it.
    filed = audit_mod.audit(goal, project_root=project_root,
                            min_gap=0.05, min_best_score=0.0)
    assert filed["decision"] == "file", filed
    assert filed["best_agent"] == "echo", filed
    # WITH the production floor (0.10), echo's near-zero absolute Jaccard
    # (~0.091 < 0.10) means no real ownership signal -> suppressed.
    suppressed = audit_mod.audit(goal, project_root=project_root,
                                 min_gap=0.05, min_best_score=0.10)
    assert suppressed["decision"] == "no_file", suppressed
    assert "min_best_score" in suppressed["reason"], suppressed
    assert suppressed["investigate_spec"] is None
    assert suppressed["best_score"] < 0.10, suppressed
    # The gap itself is unchanged in the returned dict — only the floor fired.
    assert suppressed["gap"] == filed["gap"], (suppressed, filed)


def test_g1351_floor_preserves_genuine_standout(tmp_path, audit_mod):
    """The floor must NOT silence a genuine high-absolute-score stand-out.
    echo's strong solver_v0 match (best_score well above 0.10) still files
    under the default floor — the floor only kills near-zero contamination."""
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-315-floor-ok",
        "title": "Apply: solver_v0 client adapter — neural tree wiring",
        "description": (
            "Wire the solver_v0 hand-built neural tree into the "
            "Ayoai-ARC-AGI-3-Integration client adapter so ARC game "
            "sessions route per-tick frames through the the framework server."
        ),
        "intended_agent": "alpha",
    }
    decision = audit_mod.audit(goal, project_root=project_root)  # default 0.10
    assert decision["decision"] == "file", decision
    assert decision["best_agent"] == "echo", decision
    assert decision["best_score"] >= 0.10, decision  # genuinely above the floor
    assert decision["investigate_spec"] is not None


def test_g1351_floor_suppresses_nearzero_either_case(tmp_path, audit_mod):
    """The min_best_score floor applies to the either-case path too: an
    either-stamped goal whose best agent scores below the floor is contamination
    noise, not a re-route signal — even when the stand-out gap clears
    min_gap_either."""
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-999-either-floor",
        "title": "Apply: refresh score tracking",
        "description": "Update solvers benchmark tracking.",
        "intended_agent": "either",
    }
    # gap clears min_gap_either=0.0; floor off -> file
    filed = audit_mod.audit(goal, project_root=project_root,
                            min_gap_either=0.0, min_best_score=0.0)
    assert filed["decision"] == "file", filed
    assert filed["best_agent"] == "echo", filed
    # floor on (0.10) -> near-zero best_score suppressed on the either path
    suppressed = audit_mod.audit(goal, project_root=project_root,
                                 min_gap_either=0.0, min_best_score=0.10)
    assert suppressed["decision"] == "no_file", suppressed
    assert "min_best_score" in suppressed["reason"], suppressed
    assert suppressed["investigate_spec"] is None


def test_g1351_insight_trigger_recursion_guard_no_file(tmp_path, audit_mod):
    """insight-trigger-derived goals carry author-curated routing (intended_agent
    set from the finding's requires_action_by tag), so the routing audit must
    NOT re-audit them — doing so spawns routing-mismatch FPs that the
    insight-trigger sweep re-files (self-perpetuation chain g-1346 -> g-1329 ->
    g-1351/1353 -> g-1348/1352/1354). The guard fires before scoring, so even a
    goal whose text strongly matches another agent is suppressed."""
    project_root = _seed_project_root(tmp_path, {
        "alpha": _ALPHA_SELFMD, "bravo": _BRAVO_SELFMD,
        "delta": _DELTA_SELFMD, "echo": _ECHO_SELFMD,
    })
    goal = {
        "id": "g-115-1351",
        "title": "Apply: post-decompose-routing-audit FP root fix",
        "description": (
            "Routing-mismatch FP root fix: add an absolute min_best_score "
            "floor in the delta Jaccard solver benchmark scoring path."
        ),
        "intended_agent": "zeta",
        "origin_signal": "insight_trigger:msg-20260607-072428-bravo-1795",
    }
    decision = audit_mod.audit(goal, project_root=project_root)
    assert decision["decision"] == "no_file", decision
    assert "recursion" in decision["reason"].lower(), decision
    assert decision["investigate_spec"] is None
