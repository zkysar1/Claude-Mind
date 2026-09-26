"""close-review-queue.py — the post-hoc review lane for uncalibrated closers ().

Pins the two properties that make the lane honest rather than merely present:

  * `list` offers a reviewer ONLY what it may independently review — a same-mind
    closure (completed_by == reviewer) is partitioned out and COUNTED, never ranked
    (coordination.md: independence is the agent name, not the session), and closures
    already carrying a verdict, resting recurring goals, non-completed rows and closures
    older than the window never reach the candidate list;
  * `stats` reads the verdict artifacts through the gate's own reader (last entry wins,
    list- and dict-shaped files both live) and applies the written relax rule exactly.

CLOSE_REVIEW_LEDGER_DIR is pinned to tmp_path in every case that touches artifacts,
so nothing here can read or write the real world ledger (the g-357-40 lesson), and
STORAGE_BACKEND=local (guard-955) on the one subprocess.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "close-review-queue.py"


def _mod():
    spec = importlib.util.spec_from_file_location("close_review_queue", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


crq = _mod()
NOW = datetime(2026, 9, 24, 21, 0, 0)


def closure(gid: str, **kw) -> dict:
    base = {
        "id": gid, "asp_id": "asp-375", "title": f"Fix: {gid} does a thing",
        "description": "plain prose, no named entities", "priority": "MEDIUM",
        "status": "completed", "completed_by": "alpha", "completed_by_role": "worker",
        "completed_by_sid": "72554e53-d4de-41cf-b350-4f4b2aff98dd",
        "completed_at": "2026-09-24T20:00:00", "participants": ["agent"],
    }
    base.update(kw)
    return base


def select(goals, **kw):
    args = dict(reviewed=set(), now=NOW, since_hours=72.0, reviewer="bravo", cap=3)
    args.update(kw)
    return crq.select_candidates(goals, **args)


# ─── list ─────────────────────────────────────────────────────────────────────

def test_a_same_mind_closure_is_partitioned_out_and_counted_never_ranked():
    res = select([closure("g-1-1", completed_by="alpha"), closure("g-1-2", completed_by="alpha")],
                 reviewer="alpha")
    assert res["candidates"] == []
    assert res["same_mind"] == ["g-1-1", "g-1-2"]
    assert res["eligible_total"] == 0
    # the same rows ARE offered to a different mind, case-insensitively
    res2 = select([closure("g-1-1"), closure("g-1-2")], reviewer="Bravo")
    assert [r["goal_id"] for r in res2["candidates"]] == ["g-1-1", "g-1-2"]
    assert res2["same_mind"] == []


def test_reviewed_recurring_pending_and_old_closures_never_become_candidates():
    goals = [
        closure("g-2-1"),                                            # eligible
        closure("g-2-2"),                                            # already reviewed
        closure("g-2-3", recurring=True, status="pending"),          # resting recurring
        closure("g-2-4", status="in-progress"),                      # not closed
        closure("g-2-5", completed_at="2026-09-20T09:00:00"),        # outside the window
        closure("g-2-6", completed_at="2026-09-24", completed_by_sid=None),  # bare date, today
    ]
    res = select(goals, reviewed={"g-2-2"})
    # g-2-6's bare date reads as that day's midnight, so it ranks OLDER than g-2-1
    assert [r["goal_id"] for r in res["candidates"]] == ["g-2-1", "g-2-6"]
    # the resting recurring goal is counted where its status puts it (not completed)
    assert res["skipped"] == {"not_completed": 2, "recurring": 0, "too_old": 1, "reviewed": 1}


def test_ranking_puts_tier_2_first_then_HIGH_then_newest_and_the_cap_holds():
    goals = [
        closure("g-3-1", priority="LOW", completed_at="2026-09-24T20:30:00"),
        closure("g-3-2", priority="MEDIUM", completed_at="2026-09-24T19:00:00"),
        closure("g-3-3", priority="MEDIUM", completed_at="2026-09-24T20:00:00"),
        # HIGH + non-recurring is a tier-2 trigger in the gate's classifier
        closure("g-3-4", priority="HIGH", completed_at="2026-09-24T18:00:00"),
    ]
    res = select(goals, cap=3)
    ids = [r["goal_id"] for r in res["candidates"]]
    assert ids == ["g-3-4", "g-3-3", "g-3-2"], ids
    assert res["candidates"][0]["tier"] == 2
    assert any("high_prio" in r for r in res["candidates"][0]["tier_reasons"])
    assert res["eligible_total"] == 4 and res["cap"] == 3


def test_candidate_rows_carry_what_the_reviewer_hands_to_the_producer():
    row = select([closure("g-4-1", commit_sha="abc123")])["candidates"][0]
    assert row["completed_by"] == "alpha"            # --closer for close-review-verdict.py
    assert row["completed_by_sid"].startswith("72554e53")
    assert row["completed_by_role"] == "worker"
    assert row["commit_sha"] == "abc123"
    assert row["asp_id"] == "asp-375"


# ─── stats ────────────────────────────────────────────────────────────────────

def _write_artifact(directory: Path, gid: str, entries, shape: str = "list") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = entries if shape == "list" else entries[-1]
    (directory / f"{gid}.json").write_text(json.dumps(payload), encoding="utf-8")


def _verdict(gid, verdict, at, reviewer="bravo"):
    return {"goal_id": gid, "verdict": verdict, "reviewer": reviewer, "reviewed_at": at}


def test_stats_group_by_the_closer_role_through_the_goal_record(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    d = tmp_path / "audit-reports" / "close-reviews"
    _write_artifact(d, "g-5-1", [_verdict("g-5-1", "REJECT", "2026-09-24T10:00:00"),
                                 _verdict("g-5-1", "APPROVE", "2026-09-24T12:00:00")])  # last wins
    _write_artifact(d, "g-5-2", [_verdict("g-5-2", "APPROVE_WITH_NOTES", "2026-09-24T13:00:00")],
                    shape="dict")                                                     # dict shape
    _write_artifact(d, "g-5-3", [_verdict("g-5-3", "REJECT", "2026-09-24T14:00:00")])
    _write_artifact(d, "g-9-9", [_verdict("g-9-9", "APPROVE", "2026-09-24T15:00:00")])  # a Mind's own
    goals = {g["id"]: g for g in (closure("g-5-1"), closure("g-5-2"), closure("g-5-3"),
                                  closure("g-9-9", completed_by_role="reducer"))}
    verdicts = crq.read_all_verdicts(crq.artifacts_dir())
    assert crq.artifacts_dir() == d
    stats = crq.role_stats(verdicts, goals, ["worker"])
    w = stats["roles"]["worker"]
    assert (w["reviewed"], w["approved"], w["rejected"], w["other"]) == (3, 2, 1, 0)
    assert w["approve_rate"] == round(2 / 3, 3)
    assert w["recent"] == ["APPROVE", "APPROVE_WITH_NOTES", "REJECT"]   # reviewed_at order
    assert w["relax_ok"] is False
    assert stats["unmatched_artifacts"] == 1                            # the reducer's close


def test_the_relax_rule_needs_ten_reviews_an_80_percent_rate_and_a_clean_recent_five():
    ok = crq.relax_rule(10, 0.8, ["APPROVE"] * 10)
    assert ok["relax_ok"] is True and ok["blocking_reasons"] == []
    few = crq.relax_rule(9, 1.0, ["APPROVE"] * 9)
    assert few["relax_ok"] is False and "only 9 reviewed" in few["blocking_reasons"][0]
    low = crq.relax_rule(12, 0.75, ["APPROVE"] * 12)
    assert low["relax_ok"] is False and any("approve rate" in r for r in low["blocking_reasons"])
    recent = crq.relax_rule(20, 0.95, ["APPROVE"] * 19 + ["REJECT"])
    assert recent["relax_ok"] is False and any("last 5" in r for r in recent["blocking_reasons"])
    old_reject = crq.relax_rule(20, 0.95, ["REJECT"] + ["APPROVE"] * 19)
    assert old_reject["relax_ok"] is True


def test_the_roles_come_from_the_gate_config_section():
    roles = crq.roles_from_config()
    assert roles == ["worker"], roles


def test_cli_help_runs_from_a_subprocess():
    env = dict(os.environ, STORAGE_BACKEND="local")
    res = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True,
                         text=True, env=env, timeout=60)
    assert res.returncode == 0 and "post-hoc" in res.stdout.lower() or "list" in res.stdout
