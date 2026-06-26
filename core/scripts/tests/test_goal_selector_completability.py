"""test_goal_selector_completability.py — 0 regression (2026-06-26).

Pins the completion_pressure COMPLETABILITY FACTOR (zeta rb-2384 decision,
implemented by alpha). Bare completion_ratio² radiated near-max selection
pressure forever from never-completable aspirations — recurring catch-alls
(asp-115: recurring goals refill `total`) and blocked tails (asp-315: gap-to-1.0
all blocked) — structurally starving achievable product lanes (asp-327 Vinheim).

The fix folds `completability` (the share of the remaining gap that is genuine
achievable terminal progress = pending/in-progress AND non-recurring) into the
ratio BEFORE squaring, so completion_pressure becomes (achievable_ratio)².

Four invariants:
  1. BYTE-IDENTICAL for a genuine all-pending tail (completability == 1.0) — the
     backward-compat anchor; the fix must not touch healthy consolidation.
  2. Recurring-dominated remaining is heavily discounted (the asp-115 class).
  3. Blocked-dominated remaining is heavily discounted (the asp-315 class).
  4. in-progress counts as achievable; remaining == 0 does not divide-by-zero.

Harness mirrors test_goal_selector_deadline_urgency.py: capture/restore
MIND_AGENT around the module import, pre-empty _ACTIVE_DIRECTIVES (no board
I/O), call score_goal directly. No subprocess.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


def _cp(goals):
    """Return raw completion_pressure for an aspiration with the given goals."""
    gs._ACTIVE_DIRECTIVES = []
    asp = {"id": "asp-x", "goals": goals}
    # The scored goal itself is one of the aspiration's goals (a pending one).
    cand = {"goal": goals[0], "aspiration": asp, "source": "world"}
    return gs.score_goal(cand, {}, [], [])["raw"]["completion_pressure"]


def _g(status, recurring=False):
    return {"status": status, "recurring": recurring}


def _legacy_cp(done, total):
    """The pre-fix formula: (done/total)**2 * 2.5, rounded to 2dp to match the
    `raw` dict's `round(v, 2)` at goal-selector.py:2375 (so byte-identical
    comparisons line up with what score_goal actually returns)."""
    return round(((done / total) ** 2) * 2.5, 2) if total else 0.0


# ── Invariant 1: byte-identical for a genuine achievable tail ──────────────

def test_all_pending_tail_is_byte_identical():
    """9 done + 1 pending (non-recurring): completability == 1.0, so cp is
    EXACTLY the legacy (done/total)**2 * 2.5. The fix must not perturb healthy
    consolidation of an achievable tail."""
    goals = [_g("pending")] + [_g("completed") for _ in range(9)]
    assert _cp(goals) == _legacy_cp(9, 10)


def test_multi_pending_tail_is_byte_identical():
    """7 done + 3 pending non-recurring: still fully achievable -> unchanged."""
    goals = [_g("pending") for _ in range(3)] + [_g("completed") for _ in range(7)]
    assert _cp(goals) == _legacy_cp(7, 10)


# ── Invariant 2: recurring-dominated remaining is discounted (asp-115 class)

def test_recurring_dominated_remaining_is_discounted():
    """90 done + 10 pending-RECURRING: completability == 0 (recurring never
    closes the gap) -> completion_pressure collapses to 0, vs legacy 2.025."""
    goals = [_g("pending", recurring=True) for _ in range(10)] + \
            [_g("completed") for _ in range(90)]
    cp = _cp(goals)
    assert cp == 0.0, f"all-recurring remaining must zero cp; got {cp}"
    assert _legacy_cp(90, 100) > 2.0  # the pressure the fix removes


def test_mixed_recurring_catchall_is_heavily_discounted():
    """asp-115 shape: 90 done + 2 pending-achievable + 8 pending-recurring.
    completability = 2/10 = 0.2 -> cp = (0.9*0.2)**2 * 2.5 = 0.081, vs legacy
    2.025 (a >25x discount that drops it below an achievable product lane)."""
    goals = ([_g("pending") for _ in range(2)] +
             [_g("pending", recurring=True) for _ in range(8)] +
             [_g("completed") for _ in range(90)])
    cp = _cp(goals)
    assert cp == round(((0.9 * 0.2) ** 2) * 2.5, 2)   # 0.08, rounded per L2375
    assert cp < _legacy_cp(90, 100) / 10


# ── Invariant 3: blocked-dominated remaining is discounted (asp-315 class) ─

def test_blocked_tail_is_discounted():
    """90 done + 10 blocked: completability == 0 (blocked can't progress now)
    -> cp 0, vs legacy 2.025. Dynamic: when blocks clear -> pending -> restored."""
    goals = [_g("blocked") for _ in range(10)] + [_g("completed") for _ in range(90)]
    assert _cp(goals) == 0.0


# ── Invariant 4: in-progress is achievable; remaining==0 is safe ───────────

def test_in_progress_counts_as_achievable():
    """in-progress goals ARE achievable progress (counted in completability)."""
    goals = ([_g("in-progress") for _ in range(2)] +
             [_g("blocked") for _ in range(8)] +
             [_g("completed") for _ in range(90)])
    # completability = 2/10 = 0.2
    assert _cp(goals) == round(((0.9 * 0.2) ** 2) * 2.5, 2)   # 0.08, rounded per L2375


def test_fully_done_aspiration_no_div_by_zero():
    """remaining == 0 -> completability defaults to 1.0, no ZeroDivisionError."""
    goals = [_g("completed") for _ in range(10)]
    # score a pending goal that belongs to a different (live) view but the asp is
    # fully done; guard is that the computation does not raise.
    gs._ACTIVE_DIRECTIVES = []
    asp = {"id": "asp-done", "goals": goals}
    cand = {"goal": {"status": "pending"}, "aspiration": asp, "source": "world"}
    cp = gs.score_goal(cand, {}, [], [])["raw"]["completion_pressure"]
    assert cp == _legacy_cp(10, 10)  # ratio 1.0, completability 1.0 -> 2.5


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
