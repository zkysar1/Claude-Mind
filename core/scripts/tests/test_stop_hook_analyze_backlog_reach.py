"""Regression pin for --and-file's REACH ().

WHY THIS FILE EXISTS. `stop-hook-analyze.sh --and-file` invoked stall-goal-filer
only for agents named by a WROTE_AGENT marker — agents the analyzer wrote a NEW
warning row for on THIS run. `stall-goal-filer.py` skips a rate-limited entry
WITHOUT setting `goal_filed` (L296-298), so that row stays unprocessed; if the
agent never stalls again, --and-file never names it again and the row sits
unconverted indefinitely. The recurring 4h sweep bought nothing for that
population because it only ever reached the filer through the WROTE_AGENT path.

`reclaim-routed-work.md` rule 7: a reclaim predicate must not be narrower than
the gate that creates the population, or the gate's CORRECT operation fills the
blind spot and the sweep reports clean forever.

The fix widens REACH ONLY — the filer's 24h per-agent rate limit is untouched.

WHY THE PREDICATE IS A MODULE, NOT A HEREDOC. AGENTS_PARENT_DIR is an
unconditional constant in _paths.sh (line 141) and a pre-set env value is
overwritten, so a shell-embedded scanner is only reachable by planting a fake
agent dir in the LIVE agents/ tree — which is the fixture-pollution class the
roster tripwire in test_capability_route_gate.py exists to catch. Taking
agents_root as a parameter makes the predicate testable with zero live writes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stall_backlog_agents import backlog_agents  # noqa: E402

UNPROCESSED = {"type": "loop_stall", "sid": "S-OLD", "first_block_ts": "2026-08-28T00:00:00"}
PROCESSED = dict(UNPROCESSED, goal_filed=True, goal_id="g-000-00")


def _plant(root, agent, rows):
    d = root / agent / "session"
    d.mkdir(parents=True, exist_ok=True)
    (d / "loop-stall-warnings.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_unprocessed_row_is_reached(tmp_path):
    """THE REGRESSION: a rate-limited row is found with no new stall involved."""
    _plant(tmp_path, "agentwithbacklog", [UNPROCESSED])
    assert backlog_agents(str(tmp_path)) == ["agentwithbacklog"]


def test_processed_row_is_not_reached(tmp_path):
    """Positive control: the predicate is falsy `goal_filed`, not file-existence.

    Without this, the test above passes against a scanner that returns every
    agent holding a warnings FILE — much broader behaviour, same output.
    """
    _plant(tmp_path, "agentalldone", [PROCESSED])
    assert backlog_agents(str(tmp_path)) == []


def test_mixed_file_is_reached_on_the_unprocessed_row(tmp_path):
    """One unprocessed row among processed ones still counts — the real shape.

    stall #1 files and marks; stall #2 within 24h is rate-limited and left
    unmarked, so the live file holds BOTH.
    """
    _plant(tmp_path, "agentmixed", [PROCESSED, UNPROCESSED])
    assert backlog_agents(str(tmp_path)) == ["agentmixed"]


def test_no_agents_and_no_files_return_empty(tmp_path):
    assert backlog_agents(str(tmp_path)) == []
    (tmp_path / "bare" / "session").mkdir(parents=True)
    assert backlog_agents(str(tmp_path)) == []


def test_malformed_and_unreadable_rows_fail_open(tmp_path):
    """A corrupt row must not strand every OTHER agent's backlog."""
    d = tmp_path / "agentcorrupt" / "session"
    d.mkdir(parents=True)
    (d / "loop-stall-warnings.jsonl").write_text(
        "not json\n[]\n\n" + json.dumps(UNPROCESSED) + "\n", encoding="utf-8")
    _plant(tmp_path, "agentclean", [UNPROCESSED])
    assert backlog_agents(str(tmp_path)) == ["agentclean", "agentcorrupt"]


def test_result_is_sorted_and_deduped_per_agent(tmp_path):
    """One agent appears at most once however many unprocessed rows it holds."""
    _plant(tmp_path, "zeta9", [UNPROCESSED, UNPROCESSED, UNPROCESSED])
    _plant(tmp_path, "alpha9", [UNPROCESSED])
    assert backlog_agents(str(tmp_path)) == ["alpha9", "zeta9"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
