"""test_quiescence_fragmentation_downgrade.py — Magic Wand #3 regression.

Exercises the evaluate_fragmentation_downgrade helper extracted from
quiescence-gate.py cmd_check. Each case calls the helper with a different
actual_sleep_history pattern and asserts the (sleep_seconds, downgrade,
reset) tuple matches the digest's decision rule.

Decision rule:
  - len(history) < min_samples → no decision yet, return baseline + flags False
  - len(history) >= min_samples AND avg(last window) < threshold → DOWNGRADE
    (sleep_seconds = fragmented_sleep_seconds, downgrade=True)
  - len(history) >= min_samples AND avg(last window) >= threshold → RESET
    (sleep_seconds = baseline, reset=True so caller resets fragmented_count)
  - downgrade target >= baseline → no-op (never upgrade past baseline)

Pattern: import evaluate_fragmentation_downgrade and call directly. No
subprocess required — the helper is pure.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# importlib because the module name has a hyphen.
import importlib.util

GATE_PATH = CORE_SCRIPTS / "quiescence-gate.py"
spec = importlib.util.spec_from_file_location("quiescence_gate", GATE_PATH)
qg = importlib.util.module_from_spec(spec)
# Module's import-time imports (yaml, _paths) require MIND_AGENT or fall back.
# The helper does not depend on AGENT_DIR so this is safe even if the lazy
# fallback fires.
spec.loader.exec_module(qg)

evaluate = qg.evaluate_fragmentation_downgrade

DEFAULT_CFG = {
    "fragmentation_window": 5,
    "fragmentation_threshold_s": 600,
    "fragmentation_min_samples": 3,
    "fragmented_sleep_seconds": 600,
}

CASES = [
    # id, history, baseline_sleep, cfg_overrides, expected (sleep, downgrade, reset)
    {
        "id": "below-min-samples-no-decision",
        "history": [120, 60],  # len 2, below min_samples 3
        "baseline": 1800,
        "cfg_overrides": {},
        "expected": (1800, False, False),
    },
    {
        "id": "at-min-samples-fragmented-avg-100",
        "history": [120, 60, 120],  # avg 100 << 600 threshold
        "baseline": 1800,
        "cfg_overrides": {},
        "expected": (600, True, False),
    },
    {
        "id": "fragmented-window-of-5",
        "history": [200, 180, 60, 120, 100],  # avg 132 << 600
        "baseline": 1800,
        "cfg_overrides": {},
        "expected": (600, True, False),
    },
    {
        "id": "history-longer-than-window-only-recent-5-counted",
        "history": [1700, 1750, 1800, 1750, 50, 80, 100, 90, 120, 60],
        # Last 5 = [80,100,90,120,60], avg 90 << 600 — fragmented despite
        # earlier long sleeps in the history.
        "baseline": 1800,
        "cfg_overrides": {},
        "expected": (600, True, False),
    },
    {
        "id": "clean-window-resets-counter",
        "history": [1700, 1750, 1650],  # avg 1700 > 600 threshold
        "baseline": 1800,
        "cfg_overrides": {},
        "expected": (1800, False, True),
    },
    {
        "id": "borderline-just-below-threshold",
        "history": [600, 599, 600, 599, 599],  # avg 599.4 < 600
        "baseline": 1800,
        "cfg_overrides": {},
        "expected": (600, True, False),
    },
    {
        "id": "borderline-just-above-threshold",
        "history": [600, 600, 600, 600, 601],  # avg 600.2 >= 600
        "baseline": 1800,
        "cfg_overrides": {},
        "expected": (1800, False, True),
    },
    {
        "id": "downgrade-target-greater-than-baseline-noop",
        # Caller passes baseline=300 (already short), downgrade_target=600.
        # Helper must NOT upgrade — never raise sleep beyond baseline.
        "history": [60, 60, 60],
        "baseline": 300,
        "cfg_overrides": {},
        "expected": (300, False, False),
    },
    {
        "id": "empty-history",
        "history": [],
        "baseline": 1800,
        "cfg_overrides": {},
        "expected": (1800, False, False),
    },
    {
        "id": "none-history",
        "history": None,
        "baseline": 1800,
        "cfg_overrides": {},
        "expected": (1800, False, False),
    },
    # --- config-overrides cases ---
    {
        "id": "custom-threshold-300s",
        "history": [400, 350, 380],  # avg 376 < 300? No, 376 > 300 → reset
        "baseline": 1800,
        "cfg_overrides": {"fragmentation_threshold_s": 300},
        "expected": (1800, False, True),
    },
    {
        "id": "custom-threshold-300s-truly-fragmented",
        "history": [200, 250, 100],  # avg 183 < 300 → downgrade
        "baseline": 1800,
        "cfg_overrides": {"fragmentation_threshold_s": 300},
        "expected": (600, True, False),
    },
    {
        "id": "custom-min-samples-5",
        "history": [60, 60, 60, 60],  # 4 samples, below min 5
        "baseline": 1800,
        "cfg_overrides": {"fragmentation_min_samples": 5},
        "expected": (1800, False, False),
    },
]


def main() -> int:
    failures = []

    for case in CASES:
        cid = case["id"]
        cfg = dict(DEFAULT_CFG)
        cfg.update(case["cfg_overrides"])

        actual = evaluate(case["history"], case["baseline"], cfg)
        if actual != case["expected"]:
            failures.append(
                f"[FAIL] {cid}: got {actual!r}, expected {case['expected']!r}"
            )
        else:
            print(f"  [PASS] {cid}: {actual}")

    print()
    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)}/{len(CASES)} test(s) failed")
        return 1
    print(f"All {len(CASES)} fragmentation-downgrade cases verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
