#!/usr/bin/env python3
"""Pins for `goal-selector.sh select --top N` -- the bounded view a Body reads ().

THE DEFECT: worker-loop Phase 1 ran a bare `goal-selector.sh`, which prints every
scored row with its full breakdown. Measured 2026-09-23 on DESKTOP-O91DLK2:
2,578 rows / 6,923,586 bytes / 142 s. The zc Bodies (zakcode, 131k window)
printed ~6.5 MB the same morning. zakcode keeps 64 KB of a tool result, so a
Body paid ~16-20k tokens per selection and still saw only the first few rows.

THE CONTRACT PINNED HERE (g-375-06 outcome 2):
  1. `--top N` picks the SAME top goal as the full ranking, in the same order.
     That includes the hoisted case guard-5135 describes: index 0 can score
     LOWER than index 1, so a brief that re-sorted would pick the wrong goal.
     (The real 2026-09-23 run had exactly this shape: g-373-133 at 15.64 was
     hoisted by the strategic-focus floor over g-115-8930 at 17.53.)
  2. The brief for N=10 of a realistic 2,578-row ranking fits in 4 KB. It
     measured 3,829 bytes on the real run.
  3. Without --top, the output is byte-identical to the previous print, so the
     full-ranking consumers (iteration-open, backlog-report, priority-review,
     sprint-planning) are unaffected.
  4. `why` names the hoist, then the largest breakdown terms by magnitude.
  5. The CLI wiring works: --top reaches cmd_select, and --top 0 is refused.

Hermetic: no store is read and the 142-second scorer never runs.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

# The breakdown keys a real row carries (read off the 2026-09-23 run).
_TERMS = [
    "priority", "deadline_urgency", "agent_executable", "variety_bonus",
    "streak_momentum", "novelty_bonus", "recurring_urgency", "reward_history",
    "evidence_backing", "deferred_readiness", "context_coherence", "skill_affinity",
    "recurring_saturation", "completion_pressure", "depth_bonus", "directive_boost",
    "tail_bonus", "handoff_bonus", "per_goal_saturation", "user_signal_boost",
    "class_balance_bonus", "role_affinity", "cross_aspiration_support",
    "co_invest_alignment", "critical_blocker_surface", "opportunity_boost",
    "exploration_noise",
]


def _row(i, score, title_len=127, **extra):
    """A row with the real key set and the real bulk (breakdown + raw + tags)."""
    breakdown = {k: round(((i * 7 + j * 3) % 11) / 4.0, 2) for j, k in enumerate(_TERMS)}
    row = {
        "goal_id": f"g-{100 + i % 300}-{i:03d}",
        "aspiration_id": f"asp-{100 + i % 300}",
        "source": "world",
        "title": ("T%d " % i + "x" * title_len)[:title_len],
        "cross_world_origin": None,
        "intended_agent": None,
        "routed_to_me": False,
        "executable_by_role": None,
        "skill": None,
        "category": "framework",
        "tags": ["framework", "hardening", "zc"],
        "created_at": "2026-09-23T06:00:00",
        "pull_signal": None,
        "class_balance_penalty_waived": None,
        "class_balance_bonus_waived": None,
        "per_goal_saturation_waived": None,
        "recurring": False,
        "recurring_overdue_ratio": 0.0,
        "recurring_interval_hours": 0.0,
        "score": score,
        "breakdown": breakdown,
        "raw": {k: v / 2 for k, v in breakdown.items()},
        "exploration_params": {"epsilon": 0.85, "noise_scale": 3.0, "noise_weight": 0.35},
    }
    row.update(extra)
    return row


def _ranking(n=2578):
    """Scorer order with a HOISTED index 0 that scores lower than index 1."""
    rows = [_row(0, 15.64, title_len=366, strategic_focus_pick=True)]
    rows += [_row(i, round(17.53 - i * 0.004, 2), title_len=100 + i % 267)
             for i in range(1, n)]
    return rows


def _emit(scored, top):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        gs._emit_select(scored, top)
    return out.getvalue(), err.getvalue()


def test_top_keeps_the_scorer_pick_and_order_even_when_it_scores_lower():
    full = _ranking(50)
    assert full[0]["score"] < full[1]["score"], "fixture must carry a hoisted index 0"
    out, _ = _emit(full, 10)
    rows = json.loads(out)
    assert rows[0]["goal_id"] == full[0]["goal_id"]
    assert [r["goal_id"] for r in rows] == [r["goal_id"] for r in full[:10]]


def test_top_ten_of_a_realistic_ranking_fits_in_four_kilobytes():
    full = _ranking()
    full_bytes = len(json.dumps(full, indent=2, ensure_ascii=False).encode("utf-8"))
    out, err = _emit(full, 10)
    size = len(out.encode("utf-8"))
    assert size <= 4096, f"brief is {size} bytes"
    assert full_bytes > 1_000_000, "fixture lost its realistic bulk"
    rows = json.loads(out)
    assert len(rows) == 10 and rows[0]["goal_id"] == full[0]["goal_id"]
    assert "showing 10 of 2578" in err, "the slice must say it is a slice (guard-3211)"


def test_without_top_the_output_is_the_previous_full_print():
    full = _ranking(30)
    out, err = _emit(full, None)
    assert out == json.dumps(full, indent=2, ensure_ascii=False) + "\n"
    assert err == ""


def test_why_names_the_hoist_then_the_largest_terms_by_magnitude():
    row = _row(1, 9.0, strategic_focus_pick=True, drain_lane_pick=False)
    row["breakdown"] = {k: 0.0 for k in _TERMS}
    row["breakdown"].update({"priority": 2.0, "per_goal_saturation": -3.5,
                             "completion_pressure": 1.25, "tail_bonus": 0.5})
    (brief,) = gs._brief_rows([row], 1)
    assert brief["why"] == ("hoisted by strategic_focus; per_goal_saturation -3.5, "
                            "priority +2, completion_pressure +1.25")


def test_long_titles_are_cut_to_the_cap():
    (brief,) = gs._brief_rows([_row(1, 9.0, title_len=366)], 1)
    assert len(brief["title"]) == gs._BRIEF_TITLE_CHARS
    assert brief["title"].endswith("...")


def test_the_cli_carries_top_to_cmd_select(monkeypatch):
    seen = {}
    monkeypatch.setattr(gs, "cmd_select", lambda args: seen.setdefault("top", args.top))
    monkeypatch.setattr(sys, "argv", ["goal-selector.py", "select", "--top", "3"])
    gs.main()
    assert seen == {"top": 3}


def test_top_zero_is_refused_before_any_store_is_read():
    env = dict(os.environ, MIND_AGENT=os.environ.get("MIND_AGENT", "alpha"))
    proc = subprocess.run(
        [sys.executable, str(CORE_SCRIPTS / "goal-selector.py"), "select", "--top", "0"],
        capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 2, proc.stderr
    assert "--top must be at least 1" in proc.stderr
