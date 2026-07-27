"""test_goal_selector_dep_failclosed.py —  regression.

Hardens the dependency_timeout fail-open in goal-selector.py's collect_candidates
and collect_blocked. BEFORE this fix, a pending goal with unmet blocked_by was
treated as EXECUTABLE (fail-open) whenever:
  (a) blocked_since was null/unparseable  -> dep_age is None, OR
  (b) blocked_since aged past dependency_timeout_hours.
Both leaked a goal with genuinely-unmet prerequisites into the selectable pool,
letting the agent work it out of order.

AFTER the fix the block is fail-CLOSED. The goal stays blocked UNLESS the block
is genuinely stale: blocked_since is set AND aged past the timeout AND every
unmet dep is terminal-unresolvable (abandoned status or orphan ref — none still
LIVE). A still-live unmet dep (pending/in-progress/blocked) keeps the goal
blocked regardless of age — the timeout fail-open is reserved for genuinely
stuck deps (skipped/expired/superseded/orphan).

collect_candidates (selectable) and collect_blocked (blocked-diagnostics) must
stay logical complements — the SYMMETRY comment at the blocked_by check mandates
it. Every case below asserts BOTH functions agree.

Pattern: build synthetic aspiration dicts, compute global_done_ids +
global_live_ids exactly as main() does, call the two functions directly.
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
TERMINAL_GOAL_STATUSES = gs.TERMINAL_GOAL_STATUSES

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

TIMEOUT = 48.0  # dependency_timeout_hours under test


def _ts(hours_ago):
    """ISO local timestamp `hours_ago` hours in the past (None passes through)."""
    if hours_ago is None:
        return None
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _goal(goal_id, status="pending", blocked_by=None, blocked_since=None):
    g = {
        "id": goal_id,
        "title": f"test goal {goal_id}",
        "status": status,
        "priority": "MEDIUM",
        "category": "framework-architecture",
        "participants": ["agent"],
        "recurring": False,
    }
    if blocked_by is not None:
        g["blocked_by"] = blocked_by
    if blocked_since is not None:
        g["blocked_since"] = blocked_since
    return g


def _asp(goals):
    return [{"id": "asp-test", "status": "active", "priority": "MEDIUM", "goals": goals}]


def _global_ids(aspirations):
    """Mirror main(): done_ids = completed/decomposed, live_ids = non-terminal."""
    done, live = set(), set()
    for asp in aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []):
            st = g.get("status")
            if st in ("completed", "decomposed"):
                done.add(g["id"])
            if st not in TERMINAL_GOAL_STATUSES:
                live.add(g["id"])
    return done, live


def _selectable_ids(aspirations):
    done, live = _global_ids(aspirations)
    cands = collect_candidates(
        aspirations, source="world",
        global_done_ids=done, global_live_ids=live,
        dependency_timeout_hours=TIMEOUT)
    # collect_candidates returns {"goal": {...}, "aspiration": {...}, "source": ...}
    return {c["goal"]["id"] for c in cands}


def _blocked_ids(aspirations):
    done, live = _global_ids(aspirations)
    blocked = collect_blocked(
        aspirations,
        global_done_ids=done, global_live_ids=live,
        dependency_timeout_hours=TIMEOUT)
    return {b["goal_id"] for b in blocked if b.get("block_reason") == "dependency"}


def _assert_complement(aspirations, gid, *, selectable):
    """A goal-under-test must be in exactly one of {selectable, dependency-blocked}."""
    sel = _selectable_ids(aspirations)
    blk = _blocked_ids(aspirations)
    if selectable:
        assert gid in sel, f"{gid} expected selectable, got sel={sel} blk={blk}"
        assert gid not in blk, f"{gid} must not be dependency-blocked when selectable"
    else:
        assert gid not in sel, f"{gid} expected blocked, got sel={sel} blk={blk}"
        assert gid in blk, f"{gid} expected in dependency-blocked, got blk={blk}"


# ── Branch (a): null/unparseable blocked_since -> fail-CLOSED (keep blocked) ──

def test_branch_a_null_blocked_since_keeps_blocked():
    asp = _asp([
        _goal("dep-1", status="pending"),                       # live dep
        _goal("g-under", blocked_by=["dep-1"], blocked_since=None),
    ])
    _assert_complement(asp, "g-under", selectable=False)


def test_branch_a_unparseable_blocked_since_keeps_blocked():
    asp = _asp([
        _goal("dep-1", status="pending"),
        _goal("g-under", blocked_by=["dep-1"], blocked_since="not-a-timestamp"),
    ])
    _assert_complement(asp, "g-under", selectable=False)


def test_branch_a_null_blocked_since_dead_dep_still_kept_blocked():
    # Even when the dep is dead, a NULL blocked_since stays blocked (fail-closed):
    # treat as recently-blocked; a backfill sweep will stamp blocked_since and
    # THEN the aged+dead path (branch b) clears it.
    asp = _asp([
        _goal("dep-skip", status="skipped"),                    # dead dep
        _goal("g-under", blocked_by=["dep-skip"], blocked_since=None),
    ])
    _assert_complement(asp, "g-under", selectable=False)


# ── Branch (b): aged past timeout, dep still LIVE -> keep blocked ──

def test_branch_b_aged_but_live_dep_keeps_blocked():
    asp = _asp([
        _goal("dep-1", status="pending"),                       # live
        _goal("g-under", blocked_by=["dep-1"], blocked_since=_ts(100)),  # 100h > 48h
    ])
    _assert_complement(asp, "g-under", selectable=False)


def test_branch_b_aged_in_progress_dep_keeps_blocked():
    asp = _asp([
        _goal("dep-1", status="in-progress"),                   # live
        _goal("g-under", blocked_by=["dep-1"], blocked_since=_ts(100)),
    ])
    _assert_complement(asp, "g-under", selectable=False)


def test_branch_b_aged_one_live_among_dead_keeps_blocked():
    # ANY live unmet dep keeps the goal blocked, even if others are dead.
    asp = _asp([
        _goal("dep-dead", status="skipped"),
        _goal("dep-live", status="pending"),
        _goal("g-under", blocked_by=["dep-dead", "dep-live"], blocked_since=_ts(100)),
    ])
    _assert_complement(asp, "g-under", selectable=False)


# ── Branch (b) fail-open PRESERVED: aged + all deps dead/orphan -> selectable ──

def test_branch_b_aged_dead_dep_fails_open():
    asp = _asp([
        _goal("dep-skip", status="skipped"),                    # dead/abandoned
        _goal("g-under", blocked_by=["dep-skip"], blocked_since=_ts(100)),
    ])
    _assert_complement(asp, "g-under", selectable=True)


def test_branch_b_aged_expired_dep_fails_open():
    asp = _asp([
        _goal("dep-exp", status="expired"),
        _goal("g-under", blocked_by=["dep-exp"], blocked_since=_ts(100)),
    ])
    _assert_complement(asp, "g-under", selectable=True)


def test_branch_b_aged_orphan_dep_fails_open():
    # Orphan ref (dep id exists in no goal) -> unresolvable -> fail-open after timeout.
    asp = _asp([
        _goal("g-under", blocked_by=["nonexistent-dep"], blocked_since=_ts(100)),
    ])
    _assert_complement(asp, "g-under", selectable=True)


# ── Unchanged behavior: within timeout, met dep, multi-dep mixed ──

def test_within_timeout_keeps_blocked():
    asp = _asp([
        _goal("dep-1", status="pending"),
        _goal("g-under", blocked_by=["dep-1"], blocked_since=_ts(1)),  # 1h < 48h
    ])
    _assert_complement(asp, "g-under", selectable=False)


def test_met_dep_is_selectable():
    asp = _asp([
        _goal("dep-1", status="completed"),                     # satisfied -> done_ids
        _goal("g-under", blocked_by=["dep-1"], blocked_since=_ts(100)),
    ])
    _assert_complement(asp, "g-under", selectable=True)


def test_no_blocked_by_is_selectable():
    asp = _asp([_goal("g-under")])
    _assert_complement(asp, "g-under", selectable=True)


# ── Direct fail-open regression: the EXACT pre-fix leak shape ──

def test_prefix_leak_shape_now_closed():
    # g-1337..1340 carried blocked_since aged ~66h > 48h above still-pending root
    #  and leaked as executable. Reproduce: aged blocked_since + LIVE
    # root dep -> must stay blocked now.
    asp = _asp([
        _goal("g-root", status="pending"),                      # design root, live
        _goal("g-child", blocked_by=["g-root"], blocked_since=_ts(66)),
    ])
    _assert_complement(asp, "g-child", selectable=False)
