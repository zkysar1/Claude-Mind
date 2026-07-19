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

3. Skipped goals with NO attempt marker (`started`) are excluded (g-115-2175):
   a failure requires an attempt, but 96.5% of live skips are WITHDRAWN work
   (dedup/superseded/obsolete) skipped straight from pending, never claimed.
   `started` is stamped at goal-CLAIM time (aspirations_write.py) and survives
   the compact projection. A naive filter on `started` alone was reverted under
   g-115-2171 (genuine failures lacked it too, pre-writer) — making it truthful
   at claim time is the prerequisite. test_never_attempted_skips_no_cycle pins it.

Refs: g-115-1211 (audit + removal), g-115-615 (the dead synthetic exclusion),
g-001-220 (the live Unblock: exclusion), g-002-23 (migration FP discovery),
g-115-2175 (never-attempted exclusion + claim-time attempt marker),
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
        # 5: a GENUINE repeated failure was ATTEMPTED then skipped, so it
        # carries the claim-time `started` marker. A skip WITHOUT `started` is
        # WITHDRAWN work (test_never_attempted_skips_no_cycle), not failure.
        {"id": f"g-test-{i}", "status": "skipped", "title": f"Apply: something {i}",
         "started": "2026-07-14T10:00:00",
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


def test_never_attempted_skips_no_cycle():
    """5: 3 skipped NON-Unblock goals with NO `started` marker (never
    attempted — WITHDRAWN: duplicate/superseded/obsolete) → NO repeated_failure.

    Regression lock for the g-115-2175 fix: a failure requires an attempt, and
    96.5% of live skips are withdrawn work skipped straight from pending (never
    claimed → no `started`). Before the fix these tripped phantom repeated_failure
    (it hit asp-335). Contrast test_genuine_repeated_failure_still_detected, whose
    skips carry `started` (attempted-then-failed) and DO fire — the ONLY
    difference is the attempt marker, not the title.
    """
    goals = [
        # NO `started` → never attempted → withdrawn, not failure.
        {"id": f"g-test-{i}", "status": "skipped", "title": f"Apply: dedup {i}",
         "tags": ["apply"], "category": "npc-cognition"}
        for i in range(3)
    ]
    compact = _build_compact(goals)
    result = _run_cycles(compact)
    reasons = [c.get("reason") for c in result.get("cycles", [])]
    assert "repeated_failure" not in reasons, (
        f"never-attempted skips (no `started`) must NOT trip repeated_failure "
        f"(g-115-2175 withdrawn-vs-failed): {result}"
    )
    print("PASS: never-attempted skips (no `started`) → no repeated_failure (g-115-2175)")


def test_synthetic_skips_fire_post_removal():
    """1: synthetic-tagged skips are NO LONGER specially excluded.

    The dead g-115-615 tag branch was removed. 3 skipped goals fire
    repeated_failure like any non-Unblock skip — even with `tags` present in
    the input dict. Guards against a future dev re-adding the dead exclusion
    expecting it to fire (it never did on real, tags-stripped compact data).
    """
    goals = [
        {"id": f"g-test-{i}", "status": "skipped", "title": f"Wire test {i}",
         # 5: attempted-then-skipped carries `started` (this test's point
         # is that `tags` don't exclude — not the never-attempted rule).
         "started": "2026-07-14T10:00:00",
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
    assert "started" in keep, (
        "started must survive projection for the g-115-2175 never-attempted "
        "exclusion to be live — cmd_cycles reads it from the compact to tell "
        "WITHDRAWN skips from genuine attempted-then-failed work"
    )
    assert "work_class" in keep, (
        "work_class must survive projection for the g-115-2492 all-product "
        "zero_learning_velocity suppression to be live — a check on a "
        "projection-stripped field is a dead branch (g-115-1211 class)"
    )

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


def test_near_complete_same_category_no_velocity_cycle():
    """Aspiration with completion_ratio >= 0.8 and all recent goals in
    same category -> no zero_learning_velocity cycle (g-001-12 fix).

    Canonical incident: asp-006 (5/5 complete, value-proposition category)
    was flagged zero_learning_velocity while having produced 4 distinct
    knowledge tree nodes + an umbrella synthesis. The convergence shape
    of a near-complete focused aspiration is NOT unproductive cycling.

    Note: this test uses 5 completed goals in the same category. Without
    the completion-ratio gate, the detector would proceed to the trajectory
    probe (which would fail-open since no live trajectory exists in the
    minimal compact fixture). The gate at completion_ratio >= 0.8 should
    skip the probe entirely.
    """
    goals = [
        {"id": f"g-test-{i}", "status": "completed", "title": f"P{i+1} work",
         "tags": [], "category": "value-proposition"}
        for i in range(5)
    ]
    compact = _build_compact(goals)
    result = _run_cycles(compact)
    cycles = result.get("cycles", [])
    reasons = [c.get("reason") for c in cycles]
    assert "zero_learning_velocity" not in reasons, (
        f"Expected NO zero_learning_velocity for 5/5 complete aspiration, got: {cycles}"
    )
    print("PASS: 5/5 complete aspiration in same category -> no zero_learning_velocity (g-001-12)")


def _with_zero_velocity_probe(fn):
    """Run fn with the trajectory probe forced to velocity-0 and the temp-report
    suppression forced off, so the zero_learning_velocity branch is REACHABLE in
    a hermetic fixture (the real probe fails open on a nonexistent aspiration,
    making fire vs suppress indistinguishable — both yield no cycle)."""
    orig_run, orig_reports = pe._run_script, pe._has_recent_reports
    pe._run_script = lambda cmd, timeout=60: ('{"current_velocity": 0}', "", 0)
    pe._has_recent_reports = lambda *a, **k: False
    try:
        return fn()
    finally:
        pe._run_script, pe._has_recent_reports = orig_run, orig_reports


def _velocity_fixture(work_classes):
    """3 completed same-category Fix goals (work_class per element; None omits
    the field) + 2 pending pads keeping completion_ratio 3/5 = 0.6 < 0.8 so the
    g-001-12 gate does not mask the g-115-2492 branch under test."""
    goals = []
    for i, wc in enumerate(work_classes):
        g = {"id": f"g-test-{i}", "status": "completed", "title": f"Fix: thing {i}",
             "category": "product-parity"}
        if wc is not None:
            g["work_class"] = wc
        goals.append(g)
    goals += [
        {"id": f"g-test-p{i}", "status": "pending", "title": f"Fix: pending {i}",
         "category": "product-parity"}
        for i in range(2)
    ]
    return _build_compact(goals)


def test_all_product_window_no_velocity_cycle():
    """2 (rb-3820): 3 completed work_class=product goals, velocity 0
    → NO zero_learning_velocity. Product deliverables are sibling-repo commits,
    invisible to all five velocity counters — the zero is expected, not a stall.
    Canonical: asp-335 flagged during Vinheim/Lodestar Fix-close stretches."""
    result = _with_zero_velocity_probe(
        lambda: _run_cycles(_velocity_fixture(["product", "product", "product"]))
    )
    reasons = [c.get("reason") for c in result.get("cycles", [])]
    assert "zero_learning_velocity" not in reasons, (
        f"all-product window must suppress zero_learning_velocity (g-115-2492): {result}"
    )
    print("PASS: all-product window -> no zero_learning_velocity (g-115-2492)")


def test_mixed_class_window_velocity_cycle_fires():
    """Control for 2: a MIXED window (2 product + 1 framework) with
    velocity 0 still fires — strict all() keeps the detector live, and this
    proves the monkeypatched probe actually reaches the firing branch (making
    the all-product no-fire above meaningful, not a fixture artifact)."""
    result = _with_zero_velocity_probe(
        lambda: _run_cycles(_velocity_fixture(["product", "product", "framework"]))
    )
    reasons = [c.get("reason") for c in result.get("cycles", [])]
    assert "zero_learning_velocity" in reasons, (
        f"mixed-class window must still fire zero_learning_velocity: {result}"
    )
    print("PASS: mixed product/framework window -> zero_learning_velocity still fires")


def test_missing_work_class_velocity_cycle_fires():
    """Legacy goals without work_class fail the == 'product' equality → behavior
    unchanged (safe-cutover semantics per g-115-2187-t): velocity 0 still fires."""
    result = _with_zero_velocity_probe(
        lambda: _run_cycles(_velocity_fixture([None, None, None]))
    )
    reasons = [c.get("reason") for c in result.get("cycles", [])]
    assert "zero_learning_velocity" in reasons, (
        f"work_class-less legacy window must keep firing (safe cutover): {result}"
    )
    print("PASS: missing work_class -> zero_learning_velocity unchanged (legacy safe)")


if __name__ == "__main__":
    test_all_unblock_skips_no_cycle()
    test_genuine_repeated_failure_still_detected()
    test_never_attempted_skips_no_cycle()
    test_synthetic_skips_fire_post_removal()
    test_cmd_cycles_through_real_projection()
    test_near_complete_same_category_no_velocity_cycle()
    test_all_product_window_no_velocity_cycle()
    test_mixed_class_window_velocity_cycle_fires()
    test_missing_work_class_velocity_cycle_fires()
    print()
    print("ALL cmd_cycles FILTER TESTS PASS")
