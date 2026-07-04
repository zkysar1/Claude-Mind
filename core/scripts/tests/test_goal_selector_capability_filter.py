"""test_goal_selector_capability_filter.py -- 0 collection-time filter.

Proves the per-runner capability filter works through the LIVE goal-selector
selection path (collection-time skip + not_my_lane classification), the
architecture the goal's not-my-lane-quiescence intent requires:

  collect_candidates SKIPS locally-unexecutable goals (kept out of ranking) and
  collect_blocked CLASSIFIES the same goals not_my_lane with a synth blocker_ref
  (the exact inverse -- a goal is a candidate XOR not_my_lane-blocked). A fully
  capability-constrained box therefore returns 0 candidates -> the re-read guard
  confirms empty -> all_blocked emits not_my_lane -> aspirations-select routes to
  quiescence sleep instead of the box hot-looping on goals it cannot run.

Coverage:
  * unit: collect_candidates skip (out of ranking) + untagged sibling kept.
  * unit: collect_blocked not_my_lane + synth blocker_ref (type resource, future
    expires_at, synthesized) -- the quiescence-critical half cmd_select stdout
    (which omits blocker_ref) cannot show.
  * E2E: cmd_select mixed-queue drop / fully-constrained -> not_my_lane all_blocked
    / tagged goal kept when the runner HAS the capability.

Fixture mirrors test_goal_selector_allblocked_reread.py: capture/restore
MIND_AGENT around the module import, drive cmd_select with read_jsonl + scoring
stubbed, and monkeypatch _get_runner_capabilities to pin the runner's caps
(collect_candidates AND collect_blocked both self-derive from that accessor).
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


def _goal(gid, req=None):
    """A minimal pending, agent-eligible goal; optionally capability-tagged."""
    g = {
        "id": gid, "title": "goal %s" % gid, "status": "pending",
        "participants": ["agent"], "category": "test", "priority": "MEDIUM",
    }
    if req is not None:
        g["requires_capability"] = req
    return g


def _asps(goals):
    return [{"id": "asp-test", "status": "active", "goals": goals}]


# ── unit: the collection-time skip + its blocked inverse ──────────────────────

def test_collect_candidates_skips_unexecutable(monkeypatch):
    """collect_candidates skips a capability-unexecutable goal (kept out of
    ranking) but keeps the untagged sibling (untagged = universally executable)."""
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: {"git-push"})
    cands = gs.collect_candidates(
        _asps([_goal("g-ml", ["ml-deps"]), _goal("g-fw")]), source="world")
    ids = {c["goal"]["id"] for c in cands}
    assert "g-fw" in ids and "g-ml" not in ids, cands


def test_collect_blocked_classifies_not_my_lane_with_blocker_ref(monkeypatch):
    """collect_blocked classifies a capability-unexecutable goal not_my_lane with
    a synth blocker_ref (type resource, future expires_at, synthesized) so
    quiescence-gate C2/C3 accept it. The untagged sibling is NOT blocked -- the
    exact inverse of the collect_candidates skip above."""
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: {"git-push"})
    blocked = gs.collect_blocked(
        _asps([_goal("g-ml", ["ml-deps"]), _goal("g-fw")]))
    nml = [b for b in blocked if b["block_reason"] == "not_my_lane"]
    assert len(nml) == 1 and nml[0]["goal_id"] == "g-ml", blocked
    assert "g-fw" not in {b["goal_id"] for b in blocked}, "untagged goal must NOT be blocked"
    assert nml[0]["missing_capabilities"] == ["ml-deps"]
    ref = nml[0]["blocker_ref"]
    assert isinstance(ref, dict) and ref["type"] == "resource", ref
    assert ref.get("expires_at") and ref.get("synthesized") is True, ref


def test_collect_blocked_empty_caps_classifies_nothing(monkeypatch):
    """Derivation-failure guard: an EMPTY runner_caps set (probe crash + no config)
    must NOT classify any tagged goal not_my_lane -- conservative fall-through so a
    mis-derivation never wrongful-sleeps a box (matches the collect_candidates
    skip guard `if runner_caps`)."""
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: set())
    blocked = gs.collect_blocked(_asps([_goal("g-ml", ["ml-deps"])]))
    assert not any(b["block_reason"] == "not_my_lane" for b in blocked), blocked


# ── E2E: the live cmd_select path ─────────────────────────────────────────────

def _run_cmd_select(monkeypatch, world_goals, runner_caps):
    """Drive cmd_select with read_jsonl + scoring stubbed and the runner's
    capability set pinned. read_jsonl returns the world on EVERY read (the
    re-read guard reads twice on an empty first pass). Returns parsed stdout
    (a ranked list, or an all_blocked dict)."""
    def _rj(path):
        if path == gs.WORLD_ASP_PATH:
            return _asps(world_goals)
        return []  # agent queue / pipeline / archive

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
    # Pin the runner's capabilities -- bypass probe + config + process cache.
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: set(runner_caps))

    buf = io.StringIO()
    with redirect_stdout(buf):
        gs.cmd_select(argparse.Namespace())
    return json.loads(buf.getvalue())


def test_mixed_queue_drops_unexecutable_keeps_executable(monkeypatch):
    """Runner lacks ml-deps: the ml-deps goal is skipped from ranking, the
    untagged one ranks (the mixed-queue case -- executable work surfaces)."""
    parsed = _run_cmd_select(
        monkeypatch,
        world_goals=[_goal("g-test-fw"), _goal("g-test-ml", ["ml-deps"])],
        runner_caps={"git-push"})
    assert isinstance(parsed, list), "expected ranked list, got: %r" % parsed
    ids = {g["goal_id"] for g in parsed}
    assert "g-test-fw" in ids, parsed
    assert "g-test-ml" not in ids, "ml-deps goal must be skipped on a runner lacking it"


def test_fully_constrained_routes_not_my_lane_all_blocked(monkeypatch):
    """Every candidate is capability-unexecutable -> collect_candidates skips all,
    the re-read confirms empty, collect_blocked classifies them not_my_lane ->
    all_blocked dict (so aspirations-select routes to quiescence sleep instead of
    the box hot-looping on goals it cannot run)."""
    parsed = _run_cmd_select(
        monkeypatch,
        world_goals=[_goal("g-test-ml", ["ml-deps"])],
        runner_caps={"git-push"})
    assert isinstance(parsed, dict), \
        "fully capability-constrained -> all_blocked dict, got: %r" % parsed
    assert parsed.get("all_blocked") is True, parsed
    assert parsed.get("by_reason", {}).get("not_my_lane", 0) >= 1, parsed
    ids = {b["goal_id"] for b in parsed.get("blocked_goals", [])}
    assert "g-test-ml" in ids, parsed


def test_executable_when_runner_has_capability(monkeypatch):
    """Runner HAS ml-deps: the tagged goal is kept alongside the untagged one."""
    parsed = _run_cmd_select(
        monkeypatch,
        world_goals=[_goal("g-test-fw"), _goal("g-test-ml", ["ml-deps"])],
        runner_caps={"git-push", "ml-deps"})
    assert isinstance(parsed, list), parsed
    ids = {g["goal_id"] for g in parsed}
    assert "g-test-fw" in ids and "g-test-ml" in ids, \
        "both goals executable when runner has ml-deps: %r" % parsed
