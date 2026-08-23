"""test_goal_selector_blocked_by_reason.py — selection-stack review fix 2.

cmd_blocked's by_reason tally hard-coded 5 reasons while collect_blocked
emits 7 (precondition_unmet, not_my_lane missing). Measured 2026-08-21
(cc-09): 9 of 250 live blocked rows present in blocked_goals[] and
summary.total_blocked but absent from by_reason — a reader tallying
by_reason concluded those classes were empty. _blocked_reason_counts is now
the single source for the reason set, shared with cmd_select's all-blocked
summary.

Invariants pinned here:
  1. every reason collect_blocked emits appears in cmd_blocked's by_reason
  2. sum(by_reason counts) == summary.total_blocked
  3. the 5 preset keys are always present (consumer contract: verify-learning
     q3386 key-presence assert; aspirations-all-blocked iterates them)

Harness mirrors test_goal_selector_capability_filter.py (monkeypatch
read_jsonl/read_wm/_get_runner_capabilities, redirect stdout).
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta
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

PRESET = {"infrastructure", "dependency", "deferred", "hypothesis_gate",
          "explicit_status"}


def _fixture_aspirations():
    """One aspiration holding one goal per blocked class we exercise:
    explicit_status, deferred (future deferred_until), not_my_lane
    (requires_capability the pinned runner lacks)."""
    future = (datetime.now() + timedelta(hours=6)).isoformat(timespec="seconds")
    return [{
        "id": "asp-901", "status": "active", "priority": "MEDIUM",
        "goals": [
            {"id": "g-901-01", "title": "explicitly blocked", "status": "blocked",
             "priority": "MEDIUM", "block_reason": "waiting on external"},
            {"id": "g-901-02", "title": "deferred goal", "status": "pending",
             "priority": "MEDIUM", "deferred_until": future,
             "defer_reason": "precondition_unmet:window"},
            {"id": "g-901-03", "title": "ml-only goal", "status": "pending",
             "priority": "MEDIUM", "requires_capability": ["ml-deps"]},
        ],
    }]


def _run_cmd_blocked(monkeypatch):
    fixture = _fixture_aspirations()

    def _rj(path):
        if str(path) == str(gs.WORLD_ASP_PATH):
            return fixture
        return []

    monkeypatch.setattr(gs, "read_jsonl", _rj)
    monkeypatch.setattr(gs, "read_wm", lambda: {"slots": {}})
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: {"git-push"})

    buf = io.StringIO()
    with redirect_stdout(buf):
        gs.cmd_blocked(argparse.Namespace())
    return json.loads(buf.getvalue())


def test_by_reason_covers_every_emitted_reason(monkeypatch):
    parsed = _run_cmd_blocked(monkeypatch)
    emitted = {b["block_reason"] for b in parsed["blocked_goals"]}
    assert "not_my_lane" in emitted, parsed["blocked_goals"]
    for reason in emitted:
        assert reason in parsed["by_reason"], \
            "reason %r in blocked_goals but missing from by_reason" % reason
    assert parsed["by_reason"]["not_my_lane"]["count"] == 1
    assert parsed["by_reason"]["not_my_lane"]["goal_ids"] == ["g-901-03"]


def test_by_reason_sums_to_total_blocked(monkeypatch):
    parsed = _run_cmd_blocked(monkeypatch)
    total = parsed["summary"]["total_blocked"]
    tallied = sum(v["count"] for v in parsed["by_reason"].values())
    assert tallied == total, \
        "sum(by_reason)=%d != total_blocked=%d" % (tallied, total)
    assert total == 3, parsed["summary"]


def test_preset_keys_always_present(monkeypatch):
    parsed = _run_cmd_blocked(monkeypatch)
    for r in PRESET:
        assert r in parsed["by_reason"], "preset key %r missing" % r
    # infrastructure/dependency/hypothesis_gate have no rows in the fixture —
    # they must still be present as zero counts (consumer iteration contract).
    assert parsed["by_reason"]["infrastructure"]["count"] == 0
    assert parsed["by_reason"]["dependency"]["head_count"] == 0


def test_shared_counter_matches_select_summary_shape():
    """cmd_select's all-blocked by_reason and cmd_blocked's tally now derive
    the reason SET from the same helper — assert the helper itself covers an
    arbitrary novel reason so a future 8th predicate is visible on day 1."""
    counts = gs._blocked_reason_counts([
        {"block_reason": "explicit_status"},
        {"block_reason": "some_future_predicate"},
    ])
    assert counts["explicit_status"] == 1
    assert counts["some_future_predicate"] == 1
    for r in PRESET:
        assert r in counts
