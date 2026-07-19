#!/usr/bin/env python3
"""test_precheck_eval_zombies.py — precheck-eval.py cmd_zombies contract (4).

Pins the two-kind zombie predicate:

  - `blocked_stale` (pre-existing class): completion_ratio >= threshold with
    ONLY blocked-and-stale goals remaining; aspirations carrying any recurring
    goal are skipped for this class (original behavior preserved).
  - `all_terminal` (g-115-2584): every non-recurring goal terminal yet the
    aspiration is still active — the in-loop closer (complete-review at the
    completing goal's iteration close) is a moment-in-time trigger, so
    sweep-completions / cross-box closes / autocompact at the closing moment
    left fully-done aspirations active forever (census 2026-07-18: 10 such,
    oldest ~2 months). A recurring-carrying aspiration with
    functionally_complete_at set is the sanctioned documented-hold and is NOT
    flagged; without the stamp it IS flagged (complete-review owns the
    stamp-vs-close judgment).
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location("precheck_eval", SCRIPT_DIR / "precheck-eval.py")
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)

CONFIG = {"intent_satisfaction": {"zombie_completion_ratio": 0.8,
                                  "phase_7_4_min_blocked_hours": 24}}

STALE_ISO = "2026-01-01T00:00:00"  # far past any 24h threshold


class _Args:
    pass


def _asp(asp_id, goals, **extra):
    return {"id": asp_id, "title": asp_id, "status": "active",
            "source": "world", "goals": goals, **extra}


def _g(gid, status, recurring=False, **extra):
    return {"id": gid, "status": status, "recurring": recurring, **extra}


def _run(aspirations):
    return pe.cmd_zombies(_Args(), CONFIG, {"aspirations": aspirations})


def test_all_terminal_flagged():
    """Fully-completed non-recurring aspiration → all_terminal entry."""
    out = _run([_asp("asp-a", [_g("g-1", "completed"), _g("g-2", "completed")])])
    assert out["flags"] == ["needs_complete_review"]
    [z] = out["zombies"]
    assert z["kind"] == "all_terminal"
    assert z["aspiration_id"] == "asp-a"
    assert z["blocked_goal_ids"] == []
    assert z["has_recurring"] is False
    assert z["completion_ratio"] == 1.0


def test_all_terminal_skipped_only_still_flagged():
    """The asp-xw-20260718T022848 shape: sole goal skipped (duplicate) — still
    needs closure review; ratio 0.0 is informational, not a gate."""
    out = _run([_asp("asp-b", [_g("g-1", "skipped")])])
    [z] = out["zombies"]
    assert z["kind"] == "all_terminal"
    assert z["completion_ratio"] == 0.0


def test_functionally_complete_stamp_is_documented_hold():
    """Recurring rider + functionally_complete_at → sanctioned hold, NOT flagged
    (the asp-249 shape; re-flagging a stamped host would 0-action loop forever)."""
    out = _run([_asp("asp-c",
                     [_g("g-1", "completed"), _g("g-r", "pending", recurring=True)],
                     functionally_complete_at="2026-05-11T12:51:00")])
    assert out["zombies"] == []
    assert out["flags"] == []


def test_recurring_rider_without_stamp_flagged():
    """All non-recurring terminal + recurring rider + NO stamp → flagged so
    complete-review can apply the functionally-complete stamp path."""
    out = _run([_asp("asp-d",
                     [_g("g-1", "completed"), _g("g-r", "pending", recurring=True)])])
    [z] = out["zombies"]
    assert z["kind"] == "all_terminal"
    assert z["has_recurring"] is True


def test_open_work_not_flagged():
    """Pending non-recurring goal → neither class fires."""
    out = _run([_asp("asp-e", [_g("g-1", "completed"), _g("g-2", "pending")])])
    assert out["zombies"] == []


def test_blocked_stale_class_preserved():
    """Regression pin: the pre-existing blocked-stale class still fires and now
    carries kind=blocked_stale."""
    goals = [_g(f"g-{i}", "completed") for i in range(4)]
    goals.append(_g("g-b", "blocked", blocked_since=STALE_ISO))
    out = _run([_asp("asp-f", goals)])
    [z] = out["zombies"]
    assert z["kind"] == "blocked_stale"
    assert z["blocked_goal_ids"] == ["g-b"]


def test_blocked_stale_recurring_skip_preserved():
    """Regression pin: a recurring rider still exempts the blocked-stale class
    (original line-179 behavior — only the all-terminal class sees recurring)."""
    goals = [_g(f"g-{i}", "completed") for i in range(4)]
    goals.append(_g("g-b", "blocked", blocked_since=STALE_ISO))
    goals.append(_g("g-r", "pending", recurring=True))
    out = _run([_asp("asp-g", goals)])
    assert out["zombies"] == []


def test_fresh_blocked_not_stale_not_flagged():
    """Blocked but within the 24h staleness window → not a zombie yet."""
    from datetime import datetime
    now_iso = datetime.now().replace(microsecond=0).isoformat()
    goals = [_g(f"g-{i}", "completed") for i in range(4)]
    goals.append(_g("g-b", "blocked", blocked_since=now_iso))
    out = _run([_asp("asp-h", goals)])
    assert out["zombies"] == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
