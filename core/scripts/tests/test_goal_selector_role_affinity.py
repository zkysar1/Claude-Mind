"""test_goal_selector_role_affinity.py — Magic Wand 4 regression.

Exercises the compute_role_affinity helper extracted from goal-selector.py.
Tests the decision rule: agent role × goal work_class → multiplier.

Decision rule:
  - empty agent_name OR empty goal_class OR goal_class == "unclassified"
    → return 0.0 (no contribution)
  - agent not in multipliers dict → return 0.0
  - agent's entry not a dict → return 0.0
  - goal_class not in agent's entry → return 0.0
  - value not coercible to float → return 0.0
  - otherwise → float(multipliers[agent][goal_class])

Pattern: import compute_role_affinity and call directly. Pure function;
no subprocess, no file I/O.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# goal-selector.py requires MIND_AGENT to load (paths derive AGENT_DIR).
# Capture-restore around the module-level mutation so collection-time env
# pollution cannot leak to other tests (Layer 1 test-pollution defense —
# rb-1096, guard-588, tree node test-pollution-defense). Tests don't
# actually depend on which agent is bound.
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")
compute_role_affinity = gs.compute_role_affinity

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

# Canonical multiplier table mirrors meta/goal-selection-strategy.yaml
# initial values (Day 1 of Magic Wand 4 rollout).
CANONICAL = {
    "alpha": {
        "product": 1.5, "framework": 1.3,
        "hygiene": 1.0, "research": 0.5, "unclassified": 0.0,
    },
    "bravo": {
        "product": 0.4, "framework": 0.4,
        "hygiene": 1.0, "research": 1.5, "unclassified": 0.0,
    },
}

CASES = [
    # id, agent, goal_class, multipliers, expected
    ("alpha-product-boost",        "alpha", "product",   CANONICAL, 1.5),
    ("alpha-framework-boost",      "alpha", "framework", CANONICAL, 1.3),
    ("alpha-hygiene-neutral",      "alpha", "hygiene",   CANONICAL, 1.0),
    ("alpha-research-dampen",      "alpha", "research",  CANONICAL, 0.5),
    ("bravo-product-dampen",       "bravo", "product",   CANONICAL, 0.4),
    ("bravo-framework-dampen",     "bravo", "framework", CANONICAL, 0.4),
    ("bravo-hygiene-neutral",      "bravo", "hygiene",   CANONICAL, 1.0),
    ("bravo-research-boost",       "bravo", "research",  CANONICAL, 1.5),
    # Differential check — research differs by 1.0 in alpha's favor inverted to bravo's favor
    # (alpha 0.5 → bravo 1.5; alpha gives up 1.0 score on research, bravo gains 1.0)
    # Verified case-by-case above.
    # Edge cases — fail-open paths
    ("empty-agent-name-zero",      "",      "product",   CANONICAL, 0.0),
    ("none-agent-name-zero",       None,    "product",   CANONICAL, 0.0),
    ("empty-goal-class-zero",      "alpha", "",          CANONICAL, 0.0),
    ("none-goal-class-zero",       "alpha", None,        CANONICAL, 0.0),
    ("unclassified-zero",          "alpha", "unclassified", CANONICAL, 0.0),
    ("unknown-agent-zero",         "charlie", "product", CANONICAL, 0.0),
    ("unknown-class-zero",         "alpha", "exotic",    CANONICAL, 0.0),
    ("empty-multipliers-zero",     "alpha", "product",   {},         0.0),
    ("none-multipliers-zero",      "alpha", "product",   None,       0.0),
    ("agent-entry-not-dict-zero",  "alpha", "product",   {"alpha": "garbage"}, 0.0),
    ("agent-entry-list-zero",      "alpha", "product",   {"alpha": [1, 2]},    0.0),
    # Coercion + edge cases
    ("string-multiplier-coerced",  "alpha", "product",   {"alpha": {"product": "1.5"}}, 1.5),
    ("int-multiplier-coerced",     "alpha", "product",   {"alpha": {"product": 2}},      2.0),
    ("bad-string-zero",            "alpha", "product",   {"alpha": {"product": "abc"}}, 0.0),
    ("none-value-zero",            "alpha", "product",   {"alpha": {"product": None}},  0.0),
    ("zero-multiplier-passthrough","alpha", "product",   {"alpha": {"product": 0.0}},   0.0),
    ("negative-multiplier-passthrough", "alpha", "product", {"alpha": {"product": -0.5}}, -0.5),
]


def main() -> int:
    failures = []

    for case in CASES:
        cid, agent, goal_class, multipliers, expected = case
        try:
            actual = compute_role_affinity(agent, goal_class, multipliers)
        except Exception as e:
            failures.append(f"[FAIL] {cid}: raised {type(e).__name__}: {e}")
            continue

        if actual != expected:
            failures.append(
                f"[FAIL] {cid}: got {actual!r}, expected {expected!r}"
            )
        else:
            print(f"  [PASS] {cid}: {actual}")

    print()
    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)}/{len(CASES)} test(s) failed")
        return 1
    print(f"All {len(CASES)} role-affinity cases verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
