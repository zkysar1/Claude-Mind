#!/usr/bin/env python3
"""test_precheck_cycles_filter.py — precheck-eval.py cmd_cycles filter contract.

Pins what cmd_cycles excludes from repeated_failure detection AFTER the
g-115-1211 audit. History:

1. Skipped goals whose title starts "Unblock:" (g-001-220) are excluded —
   auto-Unblocks filed by defer-gate keyword false-positives. The agent's
   rejection of misrouted auto-Unblocks is defensive routing, not failure.
   This exclusion is LIVE: `title` survives the compact projection.

2. The g-115-615 "synthetic"-tag exclusion was REMOVED by g-115-1211. It was
   dead in production: the live compact projection (_COMPACT_GOAL_KEEP in
   mind_api/src/endpoints/aspirations.py) strips `tags`, so cmd_cycles never
   saw the tag on real compact data. The old synthetic tests passed only
   because they hand-injected `tags` that bypassed the projection
   (false-confidence, testSymmetry/g-115-744 class). repeated_failure is
   advisory and skip-by-design FPs self-resolve via lookback-window churn
   (g-002-23, rb-1320), so removal carries no production behavior change.

`test_cmd_cycles_through_real_projection` is the g-115-1211 REQUIRED
integration test: it sources the LIVE keep-set from the daemon endpoint and
runs cmd_cycles on a projection-shaped compact, not hand-injected dicts.

Refs: g-115-1211 (audit + removal), g-115-615 (the dead synthetic exclusion),
g-001-220 (the live Unblock: exclusion), g-002-23 (migration FP discovery),
rb-1320 (repeated_failure is advisory noise).
"""

import ast
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location("precheck_eval", SCRIPT_DIR / "precheck-eval.py")
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)

# Live compact projection contract, sourced from the daemon endpoint. The
# daemon module is NOT importable standalone (its `from ..jsonl_cache import
# cache` relative import needs the package context), so we extract the
# _COMPACT_GOAL_KEEP set literal via ast — this stays honest if the keep-set
# gains/loses fields, without pulling in daemon dependencies.
_DAEMON_ENDPOINT = SCRIPT_DIR.parent.parent / "mind_api" / "src" / "endpoints" / "aspirations.py"


def _real_keep_set():
    """Extract _COMPACT_GOAL_KEEP from the daemon endpoint source (live projection)."""
    tree = ast.parse(_DAEMON_ENDPOINT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_COMPACT_GOAL_KEEP":
                    return set(ast.literal_eval(node.value))
    raise AssertionError("_COMPACT_GOAL_KEEP not found in daemon endpoint")


def _project(goal, keep):
    """Apply the daemon's per-goal projection (mirrors _compact_aspiration line 59)."""
    return {k: v for k, v in goal.items() if k in keep}


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


def test_all_unblock_skips_no_cycle():
    """3 consecutive skipped Unblock: goals → no cycle (, LIVE exclusion)."""
    goals = [
        {"id": f"g-test-{i}", "status": "skipped", "title": "Unblock: do something",
         "tags": [], "category": "infrastructure"}
        for i in range(3)
    ]
    compact = _build_compact(goals)
    result = _run_cycles(compact)
    cycles = result.get("cycles", [])
    assert not cycles, f"Expected no cycle for all-Unblock skips, got: {cycles}"
    print("PASS: all-Unblock: skips → no cycle (live g-001-220 exclusion)")


def test_genuine_repeated_failure_still_detected():
    """3 skipped non-Unblock goals → repeated_failure cycle fires."""
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


def test_synthetic_skips_fire_post_removal():
    """1: synthetic-tagged skips are NO LONGER specially excluded.

    The dead g-115-615 tag branch was removed. 3 skipped goals fire
    repeated_failure like any non-Unblock skip — even with `tags` present in
    the input dict. Guards against a future dev re-adding the dead exclusion
    expecting it to fire (it never did on real, tags-stripped compact data).
    """
    goals = [
        {"id": f"g-test-{i}", "status": "skipped", "title": f"Wire test {i}",
         "tags": ["synthetic", "wire-test"], "category": "framework-self-improvement"}
        for i in range(3)
    ]
    compact = _build_compact(goals)
    result = _run_cycles(compact)
    reasons = [c.get("reason") for c in result.get("cycles", [])]
    assert "repeated_failure" in reasons, (
        f"synthetic skips should fire post-removal (no special exclusion): {result}"
    )
    print("PASS: synthetic skips → repeated_failure post-removal (no dead exclusion)")


def test_cmd_cycles_through_real_projection():
    """1 REQUIRED: cmd_cycles on a real-projection compact, not hand-injected dicts.

    Pins (1) the live projection strips `tags` (so any tag-based cmd_cycles
    exclusion is dead), and (2) the title-based Unblock: exclusion survives the
    projection and still works.
    """
    keep = _real_keep_set()
    assert "tags" not in keep, (
        "tags is now in the LIVE projection keep-set — the g-115-1211 removal "
        "rationale (synthetic exclusion dead because tags stripped) no longer "
        "holds. Re-evaluate whether cmd_cycles should regain a tag-based exclusion."
    )
    assert "outcome_note" not in keep, "outcome_note unexpectedly in keep-set (g-115-1211 assumed stripped)"
    assert "title" in keep, "title must survive projection for the Unblock: exclusion to be live"

    # A synthetic-tagged skip loses its tags through the REAL projection.
    raw = {"id": "g-syn", "status": "skipped", "title": "Wire test",
           "tags": ["synthetic"], "category": "framework-self-improvement"}
    projected = _project(raw, keep)
    assert "tags" not in projected, f"real projection should strip tags, got: {projected}"

    # 3 Unblock: skips projected through the real keep-set: title survives →
    # cmd_cycles still excludes them → no cycle.
    unblocks = [
        _project({"id": f"g-u{i}", "status": "skipped", "title": "Unblock: x",
                  "tags": [], "category": "infrastructure"}, keep)
        for i in range(3)
    ]
    result = _run_cycles(_build_compact(unblocks))
    assert not result.get("cycles"), (
        f"Unblock: skips should not cycle on real-projection compact: {result}"
    )
    print("PASS: real-projection compact — tags stripped, Unblock: exclusion live")


if __name__ == "__main__":
    test_all_unblock_skips_no_cycle()
    test_genuine_repeated_failure_still_detected()
    test_synthetic_skips_fire_post_removal()
    test_cmd_cycles_through_real_projection()
    print()
    print("ALL g-115-1211 cmd_cycles FILTER TESTS PASS")
