#!/usr/bin/env python3
"""capability-route-gate.py — thin shim over gates.capability_route.evaluate().

Distinct from `capability-gate.py` (which decides agent-vs-user by checking
agent-provisionable capabilities). This script decides WHICH agent should
own a goal among the active agents (alpha/bravo/zeta). Logic lives in
`core/scripts/gates/capability_route.py` (PR 7c/3 extraction); this file
is a CLI adapter.

Outputs JSON `{intended_agent, confidence, rationale}` where intended_agent ∈
{alpha, bravo, zeta, either}. Always exits 0 — this is a classifier, not
a refusal gate.

Usage:
  py -3 core/scripts/capability-route-gate.py \
      --title "Investigate Sound NullInput x6" \
      --category ayoai-game-integration \
      --description "..."
  # Optional explicit override:
  py -3 core/scripts/capability-route-gate.py --title "..." --route-to zeta

Origin: g-282-03 (zeta session 64, 2026-05-10). Companion to g-282-01
(agent-lanes.md taxonomy) and g-282-02 (intended_agent JSON schema field).
"""
import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from gates.capability_route import evaluate  # noqa: E402
from _agents import get_active_agents  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Suggest intended_agent for a goal.")
    p.add_argument("--title", required=True,
                   help="Goal title (string, with conventional verb prefix)")
    p.add_argument("--category", default="",
                   help="Goal category (string, e.g. npc-intelligence)")
    p.add_argument("--description", default="",
                   help="Goal description (string)")
    # NOTE: choices= NOT used here. The set of valid agents is resolved at
    # CALL time inside evaluate() — argparse choices= would be evaluated at
    # parse time and could reject a freshly-/started agent the classifier
    # knows about (fresh-eyes review MEDIUM M1, 2026-05-18).
    p.add_argument("--route-to", default=None,
                   help="Explicit override — bypasses classifier, "
                        "returns this value with confidence=1.0. "
                        "Valid: any active agent name or 'either'.")
    args = p.parse_args()

    out = evaluate(
        args.title,
        category=args.category,
        description=args.description,
        route_to=args.route_to,
    )
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
