"""test_goal_selector_past_deferred_until.py —  regression.

THE BUG (measured on the live world queue 2026-07-25T21:36, not inferred):

A goal carrying BOTH a live `defer_reason` AND a `deferred_until` that has
already PASSED fell out of BOTH selector lists — it was neither a blocked goal
nor filtered from candidacy. It became a live candidate, and
`deferred_readiness` (goal-selector.py L3005, which also keys on
`deferred_until`) then ADDED +0.9 weighted on top. So the stale field did not
merely fail to block: it BOOSTED the goal to rank #1.

The live instance was `g-115-2050` — "own-cloud legacy-prefix prune", a
DESTRUCTIVE S3 delete whose own description says "Do NOT execute
opportunistically mid-loop" and whose `defer_reason`
("precondition_unmet:fleet_quiesced_window") had been re-stamped by alpha only
2h earlier and was still TRUE (the fleet was not quiesced). It ranked #1 of 59
candidates at 9.49. Any agent obeying Scorer Sovereignty would have claimed it.

Alpha had already DIAGNOSED the surfacing an iteration earlier and wrote the
correction into `defer_reason` — the human-readable field that
probe-before-defer.md rule 4 tells you to re-probe and rewrite — while
`deferred_until` stayed at 2026-07-25T18:00:00. The re-stamp was therefore
mechanically a no-op, and the goal was back at #1 two hours later.

THE CAUSE, exactly: both collect functions guarded the defer_reason evaluation
with `if not goal.get("deferred_until")`, which treats a PAST date the same as
a FUTURE one — it hands the goal to the time gate below, and that gate only
`continue`s while `now < deferred_until`. A past date thus fell straight
through with the live defer_reason never consulted.

THE FIX: one shared predicate, `_has_future_deferred_until(goal)`, used by both
collect functions. Only a FUTURE deferred_until supersedes defer_reason.

WHY A SHARED HELPER AND NOT TWO INLINE CHECKS: collect_candidates and
collect_blocked are required to stay logical complements (the SYMMETRY comment
above the struct_pc check says so explicitly). Here they were CONSISTENT with
each other and both wrong — which is precisely why the symmetry invariant held
and no existing test caught this. One predicate makes the shared precedence
impossible to drift apart again. (guard-1280: route every sink through one
helper.)

g-241-06 IS PRESERVED. That fix existed because a prior `else: continue`
unconditionally blocked goals carrying both fields, leaving 5 goals with a past
`deferred_until` PERMANENTLY blocked. Under this fix such a goal is not
permanently blocked — it is routed to the defer_reason arm, which carries its
own `defer_reason_timeout_hours` fail-open, so a genuinely stale defer still
ages out. It is simply no longer INSTANTLY cleared by a past date.
`test_past_deferred_until_with_stale_defer_still_ages_out` pins that.

Fixture style mirrors test_goal_selector_human_blocked.py (same module, same
collect_candidates / collect_blocked entry points).
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# goal-selector.py requires MIND_AGENT to load (paths derive AGENT_DIR).
# Capture-restore around the module-level mutation so collection-time env
# pollution cannot leak to other tests (rb-1096, guard-588).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")
collect_candidates = gs.collect_candidates
collect_blocked = gs.collect_blocked

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

TTL = 120.0  # defer_reason_timeout_hours under test


def _past(hours):
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _future(hours):
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _goal(goal_id, defer_reason=None, set_at_hours_ago=None, deferred_until=None):
    g = {
        "id": goal_id,
        "title": f"test goal {goal_id}",
        "status": "pending",
        "priority": "MEDIUM",
        "category": "framework-architecture",
        "participants": ["agent"],
        "recurring": False,
    }
    if defer_reason is not None:
        g["defer_reason"] = defer_reason
        if set_at_hours_ago is not None:
            g["defer_reason_set_at"] = _past(set_at_hours_ago)
    if deferred_until is not None:
        g["deferred_until"] = deferred_until
    return g


def _asp(goals):
    return [{"id": "asp-test", "status": "active", "priority": "MEDIUM", "goals": goals}]


def _global_ids(aspirations):
    done, live = set(), set()
    for asp in aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []):
            if g.get("status") in ("completed", "decomposed"):
                done.add(g["id"])
            if g.get("status") not in gs.TERMINAL_GOAL_STATUSES:
                live.add(g["id"])
    return done, live


def _candidate_ids(aspirations):
    done, live = _global_ids(aspirations)
    return {c["goal"]["id"] for c in collect_candidates(
        aspirations, source="world", global_done_ids=done,
        global_live_ids=live, defer_reason_timeout_hours=TTL)}


def _blocked_ids(aspirations):
    done, live = _global_ids(aspirations)
    return {b["goal_id"] for b in collect_blocked(
        aspirations, global_done_ids=done, global_live_ids=live,
        defer_reason_timeout_hours=TTL)}


# ─────────────────────────── THE REGRESSION ───────────────────────────


def test_past_deferred_until_does_not_release_a_live_defer_reason():
    """THE  GUARD — the exact  shape.

    defer_reason fresh (2h old, well inside the 120h TTL) and structured;
    deferred_until 3.6h in the past. Reverting either collect site to
    `if not goal.get("deferred_until")` makes this fail.
    """
    asp = _asp([_goal(
        "g-prune",
        "precondition_unmet:fleet_quiesced_window — re-probed, fleet still active",
        set_at_hours_ago=2,
        deferred_until=_past(3.6))])
    assert "g-prune" not in _candidate_ids(asp), (
        "REGRESSION: a goal with a LIVE defer_reason was released into the "
        "candidate pool by a deferred_until already in the past. This is how a "
        "destructive S3 prune reached scorer rank #1 during a non-quiesced "
        "window (g-115-3150).")


def test_the_released_goal_is_not_silently_lost_from_both_lists():
    """Not-a-candidate is only half correct — it must be visibly BLOCKED.

    A goal in neither list is invisible to selection AND to quiescence,
    blocker-recheck, and the all-blocked ladder (the rb-4149 shape). The whole
    point of the complement invariant is that every live goal lands in exactly
    one of the two lists.
    """
    asp = _asp([_goal(
        "g-prune",
        "precondition_unmet:fleet_quiesced_window",
        set_at_hours_ago=2,
        deferred_until=_past(3.6))])
    assert "g-prune" in _blocked_ids(asp), (
        "a live defer_reason with a past deferred_until must be reported as "
        "BLOCKED, not merely absent from candidates")


def test_collect_functions_stay_logical_complements():
    """The invariant the SYMMETRY comment demands, across all four shapes.

    Both sites were CONSISTENT and both WRONG before the fix, which is exactly
    why symmetry alone never caught this. Pin complement-ness over a matrix
    that includes the failing shape.
    """
    goals = [
        _goal("g-past-live", "precondition_unmet: x", 2, _past(3)),
        _goal("g-past-stale", "precondition_unmet: x", 500, _past(3)),
        _goal("g-future", "precondition_unmet: x", 2, _future(50)),
        _goal("g-none", "precondition_unmet: x", 2, None),
        _goal("g-clean", None, None, None),
    ]
    asp = _asp(goals)
    cands, blocked = _candidate_ids(asp), _blocked_ids(asp)
    overlap = cands & blocked
    assert not overlap, f"a goal cannot be both candidate and blocked: {overlap}"
    missing = {g["id"] for g in goals} - cands - blocked
    assert not missing, (
        f"goals in NEITHER list — invisible to selection AND quiescence: {missing}")


# ────────────────────  MUST STAY FIXED ────────────────────


def test_past_deferred_until_with_stale_defer_still_ages_out():
    """ control: a past date must not resurrect PERMANENT blocking.

    The pre-g-241-06 bug was `else: continue`, which blocked such goals forever.
    Under this fix the goal is routed to the defer_reason arm — which has its
    own fail-open TTL — so a STALE defer (past 120h) still falls through to the
    candidate pool. The fix narrows WHEN the date releases a goal; it does not
    restore indefinite blocking.
    """
    asp = _asp([_goal("g-stale", "precondition_unmet: dep-x",
                      set_at_hours_ago=500, deferred_until=_past(200))])
    assert "g-stale" in _candidate_ids(asp), (
        "g-241-06 REGRESSION: a past deferred_until with an EXPIRED defer_reason "
        "must still age out to the candidate pool, not block permanently")


def test_no_defer_reason_past_deferred_until_is_a_candidate():
    """Control: the pure time gate is untouched.

    With no defer_reason there is nothing to consult, so a past deferred_until
    releases the goal exactly as before.
    """
    asp = _asp([_goal("g-timeonly", None, None, _past(10))])
    assert "g-timeonly" in _candidate_ids(asp)


# ───────────────────────── unchanged behaviors ─────────────────────────


def test_future_deferred_until_still_gates():
    asp = _asp([_goal("g-fut", "precondition_unmet: x", 2, _future(50))])
    assert "g-fut" not in _candidate_ids(asp)
    assert "g-fut" in _blocked_ids(asp)


def test_human_blocked_with_past_deferred_until_stays_blocked():
    """human_blocked: never auto-clears () — a past date must not be
    the loophole that releases it. Before the fix it was."""
    asp = _asp([_goal("g-hb", "human_blocked: awaiting user approval",
                      set_at_hours_ago=300, deferred_until=_past(48))])
    assert "g-hb" not in _candidate_ids(asp)
    assert "g-hb" in _blocked_ids(asp)


def test_corrupt_deferred_until_defers_to_defer_reason():
    """An unparseable date is not a structural gate. Route to defer_reason
    rather than letting garbage silently release the goal (fail-safe, not
    fail-open, because the defer_reason arm still has its own TTL)."""
    asp = _asp([_goal("g-corrupt", "precondition_unmet: x", 2, "not-a-date")])
    assert "g-corrupt" not in _candidate_ids(asp)


def test_no_defer_fields_is_a_normal_candidate():
    asp = _asp([_goal("g-plain")])
    assert "g-plain" in _candidate_ids(asp)


# ───────────────────── shared-predicate structural pin ─────────────────────


def test_both_collect_sites_use_the_shared_predicate():
    """Structural: neither site may reintroduce the bare presence check.

    A behavior test on one site still passes if someone "fixes" only the other,
    and the two are required to stay complements — so ban the defect shape by
    name in the source of both.
    """
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    assert 'if not goal.get("deferred_until"):' not in src, (
        "REGRESSION: the bare `if not goal.get(\"deferred_until\")` presence "
        "check is back. It treats a PAST date as a structural gate and hands "
        "the goal to a time gate that clears past dates unconditionally, so a "
        "live defer_reason is never consulted (g-115-3150). Use "
        "_has_future_deferred_until(goal).")
    assert src.count("_has_future_deferred_until(goal)") >= 2, (
        "both collect_candidates and collect_blocked must route through the "
        "shared predicate — they are required to stay logical complements")


def test_helper_only_returns_true_for_a_future_date():
    f = gs._has_future_deferred_until
    assert f({}) is False
    assert f({"deferred_until": None}) is False
    assert f({"deferred_until": ""}) is False
    assert f({"deferred_until": _past(1)}) is False
    assert f({"deferred_until": "not-a-date"}) is False
    assert f({"deferred_until": _future(1)}) is True


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
