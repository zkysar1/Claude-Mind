#!/usr/bin/env python3
"""test_precheck_cycles_filter.py —  regression test.

Pins the precheck-eval.py cmd_cycles filter contract. Two skip-by-design
classes are excluded from repeated_failure detection:

1. Skipped goals whose title starts "Unblock:" (g-001-220) — auto-Unblocks
   filed by defer-gate keyword false-positives. The agent's rejection of
   misrouted auto-Unblocks is defensive routing, not failure.

2. Skipped goals carrying "synthetic" in tags (g-115-615) — wire-test
   stubs from g-282-04/05 infrastructure. Their skipped status is
   structural, not behavioral.

Genuine repeated failures of non-primitive non-synthetic work still
surface (third assertion below).

Refs: g-115-615 (this fix), g-115-612 (the FP investigation), g-001-220
(the original Unblock: exclusion).
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location("precheck_eval", SCRIPT_DIR / "precheck-eval.py")
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)


def _build_compact(recent_goals):
    """Wrap a goal list inside the minimal compact shape cmd_cycles expects."""
    return {
        "aspirations": [
            {
                "id": "asp-test",
                "status": "active",
                "goals": recent_goals,
            }
        ]
    }


def _run_cycles(compact, lookback=3):
    """Invoke cmd_cycles with a synthetic args namespace + config."""
    class Args:
        pass

    args = Args()
    config = {"cycle_detection": {"lookback_window": lookback, "report_signal_age_days": 7}}
    result = pe.cmd_cycles(args, config, compact)
    return result


def test_all_synthetic_skips_no_cycle():
    """3 consecutive skipped synthetic-tagged goals → no cycle ()."""
    goals = [
        {"id": f"g-test-{i}", "status": "skipped", "title": f"Wire test {i}",
         "tags": ["synthetic", "wire-test"], "category": "framework-self-improvement"}
        for i in range(3)
    ]
    compact = _build_compact(goals)
    result = _run_cycles(compact)
    cycles = result.get("cycles", [])
    assert not cycles, f"Expected no cycle for all-synthetic skips, got: {cycles}"
    print("PASS: all-synthetic skips → no cycle")


def test_all_unblock_skips_no_cycle():
    """3 consecutive skipped Unblock: goals → no cycle (, regression)."""
    goals = [
        {"id": f"g-test-{i}", "status": "skipped", "title": "Unblock: do something",
         "tags": [], "category": "infrastructure"}
        for i in range(3)
    ]
    compact = _build_compact(goals)
    result = _run_cycles(compact)
    cycles = result.get("cycles", [])
    assert not cycles, f"Expected no cycle for all-Unblock skips, got: {cycles}"
    print("PASS: all-Unblock: skips → no cycle (existing g-001-220 case)")


def test_mixed_synthetic_and_unblock_no_cycle():
    """3 skips spanning synthetic and Unblock: → no cycle."""
    goals = [
        {"id": "g-test-0", "status": "skipped", "title": "Unblock: x",
         "tags": [], "category": "infrastructure"},
        {"id": "g-test-1", "status": "skipped", "title": "Wire test",
         "tags": ["synthetic"], "category": "framework-self-improvement"},
        {"id": "g-test-2", "status": "skipped", "title": "Unblock: y",
         "tags": [], "category": "infrastructure"},
    ]
    compact = _build_compact(goals)
    result = _run_cycles(compact)
    cycles = result.get("cycles", [])
    assert not cycles, f"Expected no cycle for mixed exclusion-eligible skips, got: {cycles}"
    print("PASS: mixed Unblock + synthetic skips → no cycle")


def test_genuine_repeated_failure_still_detected():
    """3 skipped non-exempt goals → repeated_failure cycle fires."""
    goals = [
        {"id": f"g-test-{i}", "status": "skipped", "title": f"Apply: something {i}",
         "tags": ["apply"], "category": "npc-cognition"}
        for i in range(3)
    ]
    compact = _build_compact(goals)
    result = _run_cycles(compact)
    cycles = result.get("cycles", [])
    reasons = [c.get("reason") for c in cycles]
    assert any(r == "repeated_failure" for r in reasons), (
        f"Expected repeated_failure cycle for 3 non-exempt Apply: skips, got: {cycles}"
    )
    print("PASS: 3 Apply: skips → repeated_failure (signal NOT suppressed)")


def test_synthetic_mixed_with_completed_no_cycle():
    """1 synthetic skip + 2 completed → no cycle (insufficient skip count)."""
    goals = [
        {"id": "g-test-0", "status": "completed", "title": "Work A",
         "tags": [], "category": "framework-self-improvement"},
        {"id": "g-test-1", "status": "skipped", "title": "Wire test",
         "tags": ["synthetic"], "category": "framework-self-improvement"},
        {"id": "g-test-2", "status": "completed", "title": "Work B",
         "tags": [], "category": "framework-self-improvement"},
    ]
    compact = _build_compact(goals)
    result = _run_cycles(compact)
    cycles = result.get("cycles", [])
    reasons = [c.get("reason") for c in cycles]
    assert "repeated_failure" not in reasons, (
        f"Did not expect repeated_failure for 1 synthetic skip + 2 completed: {cycles}"
    )
    print("PASS: 1 synthetic skip + 2 completed → no repeated_failure")


if __name__ == "__main__":
    test_all_synthetic_skips_no_cycle()
    test_all_unblock_skips_no_cycle()
    test_mixed_synthetic_and_unblock_no_cycle()
    test_genuine_repeated_failure_still_detected()
    test_synthetic_mixed_with_completed_no_cycle()
    print()
    print("ALL 5 g-115-615 FILTER TESTS PASS")
