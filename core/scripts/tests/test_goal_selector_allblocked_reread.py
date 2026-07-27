"""test_goal_selector_allblocked_reread.py --  regression.

Guards the verify-before-assuming re-read guard in goal-selector cmd_select.

A transient empty FIRST collection pass (e.g., a stale/partial OneDrive snapshot
of the WORLD aspirations.jsonl during sync) must NOT cause all_blocked to be
emitted when a fresh re-read finds candidates. Discovered alpha session-77:
the selector intermittently returned all_blocked (candidates:[], blocked_count:19)
while 124 candidates demonstrably existed; read_jsonl + collect_candidates were
proven deterministic on the settled file (8/8 and 12/12 probes). cmd_select was
declaring a NEGATIVE, work-gating conclusion from a single collection pass --
verify-before-assuming.md requires 2+ independent signals.

Pattern mirrors test_goal_selector_substantive_demotion.py: capture/restore
MIND_AGENT around the module-level import, then drive cmd_select with read_jsonl
mocked to control the world reads. Scoring is isolated so the assertions key on
the guard's branch (array vs all_blocked dict), not on the scoring arithmetic.
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

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


def _world_with_pending():
    """One active aspiration with a single pending, agent-eligible goal."""
    return [{
        "id": "asp-test", "status": "active",
        "goals": [{
            "id": "g-test-01", "title": "test pending goal",
            "status": "pending", "participants": ["agent"],
            "category": "test", "priority": "MEDIUM",
        }],
    }]


def _world_with_blocked():
    """One active aspiration whose only goal is hard-blocked (deferred far future)."""
    return [{
        "id": "asp-test", "status": "active",
        "goals": [{
            "id": "g-test-09", "title": "blocked goal",
            "status": "pending", "participants": ["agent"],
            "deferred_until": "2099-01-01T00:00:00",
        }],
    }]


def _run_cmd_select(monkeypatch, world_reads, agent_list):
    """Drive cmd_select with read_jsonl mocked. Returns (stdout, world_read_count).

    world_reads is a queue of return values for successive WORLD reads; when
    exhausted, returns []. Scoring helpers are stubbed so output is deterministic
    and the assertions isolate the guard branch.
    """
    world_q = list(world_reads)
    counter = {"world": 0}

    def _rj(path):
        if path == gs.WORLD_ASP_PATH:
            counter["world"] += 1
            return world_q.pop(0) if world_q else []
        if path == gs.AGENT_ASP_PATH:
            return list(agent_list)
        return []  # pipeline / archive

    monkeypatch.setattr(gs, "read_jsonl", _rj)
    monkeypatch.setattr(gs, "read_wm", lambda: {"slots": {}})
    monkeypatch.setattr(gs, "load_recent_class_completions", lambda window_size=20: [])
    monkeypatch.setattr(gs, "load_exploration_params", lambda: (0.0, 0.0))
    monkeypatch.setattr(gs, "score_goal", lambda c, wm, resolved, sc, **kw: {
        "goal_id": c["goal"]["id"], "aspiration_id": c["aspiration"]["id"],
        "title": c["goal"].get("title", ""), "score": 1.0,
        "recurring": bool(c["goal"].get("recurring")), "breakdown": {}, "raw": {}})
    monkeypatch.setattr(gs, "apply_substantive_demotion", lambda scored, cfg: None)
    monkeypatch.setattr(gs, "_record_strategy_application", lambda *a, **k: None)
    monkeypatch.setattr(gs, "AGENT_DIR", None)  # skip cross-agent collection branch

    buf = io.StringIO()
    with redirect_stdout(buf):
        gs.cmd_select(argparse.Namespace())
    return buf.getvalue(), counter["world"]


def test_transient_stale_first_read_recovers(monkeypatch):
    """First WORLD read is a STALE snapshot (non-empty but 0 candidates -- everything
    looks blocked/done), retry finds the pending goal -> NOT all_blocked.

    This is the exact incident shape from alpha session-77: the failing runs
    returned all_blocked with blocked_count>0 (world WAS loaded, just yielded 0
    candidates), not an empty queue. The first read is non-empty so the line-2079
    'no goals at all' early-return does NOT fire; control reaches the guard."""
    out, world_calls = _run_cmd_select(
        monkeypatch, world_reads=[_world_with_blocked(), _world_with_pending()], agent_list=[])
    parsed = json.loads(out)
    assert isinstance(parsed, list), "expected recovered candidate list, got: %r" % parsed
    assert any(g["goal_id"] == "g-test-01" for g in parsed), parsed
    assert world_calls == 2, "guard must re-read the WORLD file exactly once on empty-candidate first pass"


def test_genuine_all_blocked_still_emitted(monkeypatch):
    """Both reads have only a hard-blocked goal -> retry confirms -> all_blocked dict."""
    out, world_calls = _run_cmd_select(
        monkeypatch, world_reads=[_world_with_blocked(), _world_with_blocked()], agent_list=[])
    parsed = json.loads(out)
    assert isinstance(parsed, dict), "expected all_blocked dict, got: %r" % parsed
    assert parsed.get("all_blocked") is True
    assert parsed.get("blocked_count", 0) >= 1
    assert world_calls == 2, "genuine all-blocked still pays the second-signal re-read"


def test_stable_success_no_retry(monkeypatch):
    """First read already has candidates -> array, retry NOT consulted (one WORLD read)."""
    out, world_calls = _run_cmd_select(
        monkeypatch, world_reads=[_world_with_pending()], agent_list=[])
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert any(g["goal_id"] == "g-test-01" for g in parsed)
    assert world_calls == 1, "no retry when the first pass already found candidates"


def test_empty_queue_no_goals_prints_empty_array(monkeypatch):
    """No world/agent goals at all -> '[]' (not all_blocked); retry also empty."""
    out, _ = _run_cmd_select(monkeypatch, world_reads=[[], []], agent_list=[])
    assert out.strip() == "[]", "no goals anywhere -> empty array, never all_blocked"
