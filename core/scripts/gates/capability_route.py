"""Capability-route gate logic — daemon-safe extraction (PR 7c/3).

Distinct from `gates.capability` (which decides agent-vs-user by checking
agent-provisionable capabilities). This module decides WHICH agent should
own a goal among the active agents (alpha/bravo/zeta) based on:

  Tier 1 (strongest): goal title verb prefix (`Investigate:`, `Build:`, ...)
  Tier 2 (medium):    goal category (`npc-intelligence`, `framework-loop`, ...)
  Tier 3 (weakest):   description heuristic phrases (apply as bias deltas)

Public API:
    evaluate(title, *, category="", description="", route_to=None) -> dict

Return shape (matches the legacy CLI's stdout JSON byte-for-byte):
    {
      "intended_agent": "alpha" | "bravo" | "zeta" | "either",
      "confidence": float (0.0-1.0, rounded to 2 decimals),
      "rationale": str (pipe-joined explanation),
    }

This is a CLASSIFIER, not a refusal gate — there's no would_block field,
exit code is always 0, and uncertain cases return "either" with low
confidence. The aspirations.py caller treats the result as advisory:
when `intended_agent` is one of the active agents AND no explicit
intended_agent was set on the goal, the result is stamped onto the goal.

Daemon safety:
  - Reads no env directly.
  - No file I/O — pure classification over the three string inputs.
  - No telemetry (legacy CLI emits none; preserved for byte-equivalence).

NOTE on domain leakage: prefix/category/heuristic tables now live in
`world/config/capability-routing.yaml` (keys: `title_prefix_routes`,
`category_routes`, `description_heuristics`) and are loaded via
`_world_config.load_world_config(...)` at import time. the framework's tables were
moved to its world overlay on 2026-05-18 (Phase 2.5 of packaging plan). A
fresh deployment without that overlay file gets empty tables and the
classifier returns "either" for every goal — safe-default posture, no
wrong-domain misroute. ACTIVE_AGENTS itself is derived dynamically from
world/team-state.yaml via _agents.py, so a fresh deployment with a single
different agent works without an edit here either.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# Resolve sibling _agents.py + _world_config.py without a package-relative
# import — this module is imported by both the daemon (gates package) and
# CLI shims via sys.path manipulation, so a `from .._agents import ...`
# would fail in the CLI path.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from _agents import get_active_agents  # noqa: E402
from _world_config import load_world_config as _load_world_config  # noqa: E402


def _active_agents() -> tuple:
    """Lazy accessor — kept as a function so caller-cached results stay fresh
    if clear_cache() is called after /start adds a new agent."""
    return get_active_agents()


# Back-compat shim: callers (capability-route-gate.py CLI) may still import
# ACTIVE_AGENTS as a constant. Resolved once at import; if the agent set
# changes mid-process, callers must re-import or call _active_agents().
ACTIVE_AGENTS = _active_agents()


def _load_routing_tables() -> tuple:
    """Load routing tables from world/config/capability-routing.yaml overlay.
    Returns (title_prefix_routes, category_routes, description_heuristics).
    Each is the safe-empty default when the overlay is absent or malformed.

    YAML overlay shape:
        title_prefix_routes:
          - prefix: "investigate:"
            agent: "zeta"
            confidence: 0.88
            rationale: "..."
          - ...
        category_routes: [ {category, agent, confidence, rationale}, ... ]
        description_heuristics: [ {phrase, agent, delta, rationale}, ... ]
    """
    cfg = _load_world_config(
        "capability-routing",
        default={
            "title_prefix_routes": [],
            "category_routes": [],
            "description_heuristics": [],
        },
    )

    # Title prefix → ordered dict preserving overlay sequence (Python 3.7+
    # dicts retain insertion order). Empty/malformed entries skipped.
    tpr: dict = {}
    for entry in (cfg.get("title_prefix_routes") or []):
        if not isinstance(entry, dict):
            continue
        p = entry.get("prefix")
        a = entry.get("agent")
        c = entry.get("confidence")
        r = entry.get("rationale", "")
        if not isinstance(p, str) or not isinstance(a, str) or not isinstance(c, (int, float)):
            continue
        tpr[p] = (a, float(c), str(r))

    cr: dict = {}
    for entry in (cfg.get("category_routes") or []):
        if not isinstance(entry, dict):
            continue
        k = entry.get("category")
        a = entry.get("agent")
        c = entry.get("confidence")
        r = entry.get("rationale", "")
        if not isinstance(k, str) or not isinstance(a, str) or not isinstance(c, (int, float)):
            continue
        cr[k] = (a, float(c), str(r))

    dh: list = []
    for entry in (cfg.get("description_heuristics") or []):
        if not isinstance(entry, dict):
            continue
        p = entry.get("phrase")
        a = entry.get("agent")
        d = entry.get("delta")
        r = entry.get("rationale", "")
        if not isinstance(p, str) or not isinstance(a, str) or not isinstance(d, (int, float)):
            continue
        dh.append((p, a, float(d), str(r)))

    return tpr, cr, dh


# Resolved-once snapshots — daemon recycles on post-commit, so a hot-edit to
# the world overlay takes effect on the next iteration. For mid-process refresh,
# call _world_config.clear_cache("capability-routing") then re-invoke
# _load_routing_tables() and reassign the module-level constants.
TITLE_PREFIX_ROUTES, CATEGORY_ROUTES, DESCRIPTION_HEURISTICS = _load_routing_tables()

# Vertical-ownership override threshold (). A category route to a
# SPECIFIC active agent at confidence >= this value is treated as DOMAIN-VERTICAL
# ownership (foxtrot's npc-*/roblox, echo's arc-*, bravo's monitoring lanes) that
# overrides a DISAGREEING generic work-type title prefix (investigate:/build:/
# audit:...) — the vertical owner does ALL work-types in their vertical, so a
# generic 'investigate:'->zeta must not swallow foxtrot's npc-behavior goals.
# Weaker categories (< threshold: the 'either' framework categories, maintenance
# 0.65) keep the prior Tier-1-wins behavior. Consumed in _classify Tier 2.
CATEGORY_VERTICAL_OVERRIDE_CONF = 0.75


def _route_covered_agents() -> set:
    """Agent names appearing as a route target in ANY table (title prefix,
    category, or description heuristic). The sentinel "either" is not an agent
    and is excluded."""
    covered: set = set()
    for agent, _conf, _why in TITLE_PREFIX_ROUTES.values():
        if agent and agent != "either":
            covered.add(agent)
    for agent, _conf, _why in CATEGORY_ROUTES.values():
        if agent and agent != "either":
            covered.add(agent)
    for _phrase, agent, _delta, _why in DESCRIPTION_HEURISTICS:
        if agent and agent != "either":
            covered.add(agent)
    return covered


def _uncovered_active_agents() -> tuple:
    """Active agents (from world/team-state.yaml via _agents) that have NO
    route in any table — the classifier is structurally blind to them and can
    never be returned as intended_agent.

    Root-cause guard for the static-table vs dynamic-ACTIVE_AGENTS coverage gap
    (g-115-908 / charlie F-001): _active_agents() grows when /start adds an
    agent, but the route tables are seeded once from the world overlay. A
    newly-added agent with no route is silently unroutable until someone
    notices. Returns a sorted tuple for stable output."""
    return tuple(sorted(set(_active_agents()) - _route_covered_agents()))


# Coverage warning ( / charlie F-001): emit a loud one-time stderr
# warning at import if any active agent has no route. stderr ONLY — the legacy
# CLI's stdout JSON byte-equivalence contract is untouched. A WARNING, not an
# assert: returning "either" for an uncovered agent is already the safe-default
# posture, and a transient team-state carrying a just-added agent before its
# routes land must not hard-crash goal-stamping for every other goal. Fail-safe
# — any error here must never break import of the classifier.
try:
    _uncovered = _uncovered_active_agents()
    if _uncovered:
        sys.stderr.write(
            "[capability_route] WARNING: active agent(s) with NO route in the "
            "capability-routing overlay can never be returned as intended_agent: "
            + ", ".join(_uncovered)
            + ". Add title_prefix_routes / category_routes / description_heuristics "
            "entries (see module docstring).\n"
        )
except Exception:
    pass


def _classify(title: str, category: str, description: str) -> dict:
    """Classify goal → {intended_agent, confidence, rationale}.

    Three-tier classification:
      Tier 1 (strongest): title verb prefix
      Tier 2 (medium):    category
      Tier 3 (weakest):   description heuristic phrases (apply as bias deltas)

    When Tier 1 + Tier 2 disagree, Tier 1 wins. Description heuristics nudge
    confidence and can override 'either' from Tier 1 if the bias is unambiguous.
    """
    title_lc = (title or "").strip().lower()
    cat_lc = (category or "").strip().lower()
    desc_lc = (description or "").lower()

    best_agent = "either"
    best_conf = 0.30
    rationale_parts: list[str] = []

    # Tier 1: title prefix
    for prefix, (agent, conf, why) in TITLE_PREFIX_ROUTES.items():
        if title_lc.startswith(prefix):
            best_agent = agent
            best_conf = conf
            rationale_parts.append(f"title-prefix({prefix!r}): {why}")
            break

    active = _active_agents()

    # Tier 2: category
    if cat_lc in CATEGORY_ROUTES:
        cat_agent, cat_conf, cat_why = CATEGORY_ROUTES[cat_lc]
        rationale_parts.append(f"category({cat_lc!r}): {cat_why}")
        if best_agent == "either" and cat_agent in active:
            best_agent = cat_agent
            best_conf = max(best_conf, cat_conf)
        elif best_agent in active and cat_agent == best_agent:
            # Reinforcing — bump confidence
            best_conf = min(0.95, best_conf + 0.05)
        elif (best_agent in active and cat_agent in active
              and cat_agent != best_agent
              and cat_conf >= CATEGORY_VERTICAL_OVERRIDE_CONF):
            # Vertical-ownership override (). A high-confidence
            # domain-vertical category (foxtrot's npc-*/roblox, echo's arc-*,
            # bravo's monitoring lanes) represents DOMAIN ownership that a
            # GENERIC work-type title prefix (investigate:/build:/audit:...)
            # must not override — the vertical owner does ALL work-types in
            # their vertical. Symmetric to the Tier-3 high-delta override
            # below. Before this branch, 'investigate:'->zeta (Tier 1, 0.88)
            # silently swallowed foxtrot's npc-behavior 'Investigate:' goals,
            # causing cross_lane_refused on every foxtrot claim attempt.
            prev_agent = best_agent
            best_agent = cat_agent
            best_conf = max(cat_conf, best_conf)
            rationale_parts.append(
                f"  (OVERRIDE {prev_agent}->{cat_agent}: vertical category "
                f"conf {cat_conf} >= {CATEGORY_VERTICAL_OVERRIDE_CONF} "
                f"outranks generic title prefix)"
            )

    # Tier 3: description heuristic biases
    for phrase, agent, delta, why in DESCRIPTION_HEURISTICS:
        if phrase in desc_lc:
            rationale_parts.append(f"desc-bias({phrase!r}, +{delta}): {why}")
            if best_agent == "either" and agent in active:
                best_agent = agent
                best_conf = max(best_conf, 0.50 + delta)
            elif best_agent == agent:
                best_conf = min(0.95, best_conf + delta)
            elif best_agent in active and agent != best_agent:
                # Tier 3 high-delta override (): a high-conviction
                # description heuristic (delta >= 0.30) overrides a *low-confidence*
                # Tier 1/2 signal (best_conf < 0.85). At/above 0.85 the stronger
                # Tier 1/2 signal still wins (kept path) — without the confidence
                # ceiling a single strong title prefix could be flipped by any
                # adjacent phrase. Fixes: every 'Apply:' goal routed to the
                # title-prefix agent (conf 0.80) regardless of solver_v0 /
                # arc-agi-3 / owner-echo description content.
                if delta >= 0.30 and best_conf < 0.85 and agent in active:
                    prev_agent = best_agent
                    best_agent = agent
                    best_conf = max(0.50 + delta, best_conf)
                    rationale_parts.append(
                        f"  (OVERRIDE {prev_agent}->{agent}: Tier 3 delta {delta} "
                        f">= 0.30 and Tier 1/2 conf < 0.85)"
                    )
                else:
                    rationale_parts.append(
                        f"  (kept {best_agent} despite {agent}-bias; "
                        f"stronger Tier 1/2 signal wins)"
                    )

    if not rationale_parts:
        rationale_parts.append(
            "no recognized title prefix, category, or description signal — "
            "defaulting to 'either'"
        )

    return {
        "intended_agent": best_agent,
        "confidence": round(best_conf, 2),
        "rationale": " | ".join(rationale_parts),
    }


def evaluate(title: str, *, category: str = "", description: str = "",
             route_to: Optional[str] = None) -> dict:
    """Public entry point. See module docstring.

    When `route_to` is one of the valid agent names or "either", it bypasses
    the classifier and returns that value with confidence=1.0. Invalid
    route_to values are ignored (classifier runs normally) — matches the
    legacy CLI's argparse `choices=` constraint behavior (CLI rejects with
    SystemExit; daemon ignores silently).
    """
    if route_to in _active_agents() or route_to == "either":
        return {
            "intended_agent": route_to,
            "confidence": 1.0,
            "rationale": f"--route-to override: explicit assignment to {route_to}",
        }
    return _classify(title, category, description)
