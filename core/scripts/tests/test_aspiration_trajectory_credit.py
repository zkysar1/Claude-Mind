#!/usr/bin/env python3
"""test_aspiration_trajectory_credit.py - regression test ().

A worker Body never writes rb, guardrails or the tree: its learning reaches the
stores only after the reducer merges the Body's WM, runs the retrospective
(stamping the goal's retrospective marker) and drains the capture slots. Until
then every worker-closed goal counts 0 artifacts by construction, and a trailing
window of those zeros read as a plateau that armed evolve Step 1.5's pivot
(measured on asp-306: 4 of the last 5 closes were uncredited worker goals).

The fix classifies such goals as CREDIT-PENDING and runs every detector over the
SETTLED series. Because its effect is an ABSENCE (a plateau stops firing), the
positive controls below are load-bearing (guard-4166): a window of settled zeros
-- reducer-closed or worker-closed -- must still flag, and those controls must
stay green when the classification is reverted while the fix pins go red.

g-306-537 adds the NON-OWNER rule. The capture slots are read for the bound
agent only, so a marked worker goal settles only when its owner (executed_by)
is the agent whose slots were read. Every fixture below therefore declares an
owner: OWNER executes, and OWNER's slots are read, unless a test says otherwise.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

# aspiration-trajectory.py is hyphenated -> load by path
_spec_at = importlib.util.spec_from_file_location(
    "aspiration_trajectory", SCRIPT_DIR / "aspiration-trajectory.py")
at = importlib.util.module_from_spec(_spec_at)
_spec_at.loader.exec_module(at)

CONFIG = {
    "velocity_window": 5,
    "plateau_threshold": 0.2,
    "diminishing_returns_window": 5,
}
MARKER = "2026-09-27T10:00:00|alpha|worker-retrospective"
OWNER = "alpha"


def _goal(n, role=None, marker=None, outcome_class="deep", executed_by=OWNER):
    g = {
        "id": f"g-999-{n:02d}",
        "title": f"goal {n}",
        "status": "completed",
        "category": "framework-meta",
        "completed_at": f"2026-09-{n:02d}T10:00:00",
        "outcome_class": outcome_class,
    }
    if role:
        g["completed_by_role"] = role
    if marker:
        g[at._retro.MARKER_FIELD] = marker
    if executed_by:
        g["executed_by"] = executed_by
    return g


def _rb(goal_ids_with_counts):
    """{goal_id: n} -> n reasoning-bank entries attributed to each goal."""
    return [{"source_goal": gid} for gid, n in goal_ids_with_counts.items()
            for _ in range(n)]


def _asp_sources(goals):
    return [[{"id": "asp-999", "title": "test", "status": "active",
              "goals": goals}], []]


def _run(goals, rb=None, pending=frozenset(), omit_pending_key=False,
         capture_owner=OWNER):
    shared = {
        "config": dict(CONFIG),
        "reasoning_bank": _rb(rb or {}),
        "guardrails": [],
        "pattern_sigs": [],
        "tree_data": {},
        "tree_attribution": {},
        "script_convention_attribution": {},
        "pending_capture_goal_ids": None if pending is None else set(pending),
        "capture_owner": capture_owner,
        "asp_sources": _asp_sources(goals),
    }
    if omit_pending_key:
        del shared["pending_capture_goal_ids"]
    return at.build_trajectory("asp-999", shared=shared)


def _prolonged_armed(t):
    """evolve Step 1.5's prolonged (pivot) branch condition."""
    return t["plateau_detected"] and \
        t["goals_since_inflection"] >= CONFIG["velocity_window"] * 2


# ---- O1: uncredited worker closes are reported, not scored 0 ----------------

def test_uncredited_worker_window_is_pending_not_a_plateau():
    goals = [_goal(1)] + [_goal(n, role="worker") for n in range(2, 7)]
    t = _run(goals, rb={"g-999-01": 3})
    assert t["plateau_detected"] is False
    assert t["credit_pending_count"] == 5
    assert t["credit_pending_goal_ids"] == [f"g-999-{n:02d}" for n in range(2, 7)]
    reasons = {ga["goal_id"]: ga["credit_pending_reason"] for ga in t["goals"]}
    assert reasons["g-999-01"] is None
    assert all(reasons[f"g-999-{n:02d}"] == "no-retrospective-marker"
               for n in range(2, 7))
    # Velocity is over the one settled goal, not diluted by the five zeros.
    assert t["current_velocity"] == 3.0


def test_marked_but_undrained_goal_is_still_pending():
    goals = [_goal(n, role="worker", marker=MARKER) for n in range(1, 7)]
    t = _run(goals, pending={g["id"] for g in goals})
    assert t["plateau_detected"] is False
    assert {ga["credit_pending_reason"] for ga in t["goals"]} == {"capture-undrained"}


def test_unreadable_capture_slots_resolve_toward_pending():
    goals = [_goal(n, role="worker", marker=MARKER) for n in range(1, 7)]
    t = _run(goals, pending=None)
    assert t["capture_slots_readable"] is False
    assert t["plateau_detected"] is False
    assert {ga["credit_pending_reason"] for ga in t["goals"]} == {"capture-slots-unreadable"}


def test_absent_slot_data_is_treated_as_unreadable():
    t = _run([_goal(1, role="worker", marker=MARKER)], omit_pending_key=True)
    assert t["capture_slots_readable"] is False
    assert t["goals"][0]["credit_pending_reason"] == "capture-slots-unreadable"


def test_all_pending_reports_velocity_none_not_zero():
    # precheck-eval's zero_learning_velocity detector fires on == 0.
    goals = [_goal(n, role="worker") for n in range(1, 7)]
    t = _run(goals)
    assert t["current_velocity"] is None
    assert "n/a (no settled goals)" in t["summary"]
    assert t["plateau_detected"] is False
    assert t["diminishing_returns"] is False


# ---- O2: positive controls that must NOT flip -------------------------------

def test_reducer_closed_zero_window_still_flags_plateau():
    t = _run([_goal(n) for n in range(1, 7)])
    assert t["plateau_detected"] is True
    assert t["credit_pending_count"] == 0
    assert t["current_velocity"] == 0.0


def test_settled_worker_zero_window_still_flags_plateau():
    # Merged, retrospected and drained, and still zero: a real zero (guard-7059).
    goals = [_goal(n, role="worker", marker=MARKER) for n in range(1, 7)]
    t = _run(goals, pending=set())
    assert t["plateau_detected"] is True
    assert t["credit_pending_count"] == 0


def test_worker_goal_already_credited_is_settled():
    goals = [_goal(n, role="worker") for n in range(1, 7)]
    t = _run(goals, rb={g["id"]: 1 for g in goals})
    assert t["credit_pending_count"] == 0
    assert t["current_velocity"] == 1.0


# ---- O3: pending closes cannot arm the prolonged (pivot) branch -------------

def test_pending_goals_cannot_arm_the_prolonged_branch():
    # Settled: 0, 3 (inflection), then five settled zeros -> a GENUINE plateau.
    # Then ten uncredited worker closes at the tail.
    settled = [_goal(n) for n in range(1, 8)]
    pending = [_goal(n, role="worker") for n in range(8, 18)]
    t = _run(settled + pending, rb={"g-999-02": 3})
    assert t["plateau_detected"] is True          # the settled plateau still shows
    assert t["goals_since_inflection"] == 5       # counted over settled goals only
    assert _prolonged_armed(t) is False           # -> recent-plateau branch, no pivot


def test_inflection_index_is_a_position_in_the_settled_series():
    # Pending closes INTERLEAVED before the inflection: goals_since_inflection
    # subtracts the index from len(settled), so both must be in one index space.
    goals = [_goal(1)] + [_goal(n, role="worker") for n in range(2, 5)] + \
        [_goal(n) for n in range(5, 11)]
    t = _run(goals, rb={"g-999-05": 3})
    assert t["last_inflection_point"]["goal_id"] == "g-999-05"
    assert t["last_inflection_point"]["index"] == 1
    assert t["goals_since_inflection"] == 5


def test_pending_goals_do_not_fake_diminishing_returns():
    settled = [_goal(n) for n in range(1, 7)]
    pending = [_goal(n, role="worker") for n in range(7, 9)]
    t = _run(settled + pending, rb={g["id"]: 2 for g in settled})
    assert t["diminishing_returns"] is False


# ---- guard-7449: every stratum's size, summing to the population ------------

def test_strata_partition_the_population():
    goals = [
        _goal(1), _goal(2, outcome_class="routine"),                 # unstamped
        _goal(3, role="worker"),                                     # credited
        _goal(4, role="worker", marker=MARKER),                      # settled zero
        _goal(5, role="worker"), _goal(6, role="worker"), _goal(7, role="worker"),
    ]
    t = _run(goals, rb={"g-999-03": 1}, pending=set())
    s = t["credit_strata"]
    assert s["completed"] == 7
    assert s["credit_pending"] == 3
    assert s["settled_worker"] == 2
    assert s["settled_unstamped"] == 2
    assert s["completed"] == s["credit_pending"] + s["settled_worker"] + s["settled_unstamped"]
    assert s["settled_routine"] == 1
    assert s["window"] == {"size": 4, "worker": 2, "unstamped": 2, "routine": 1}


# ---- the slot loader ---------------------------------------------------------

def test_loader_unions_both_slots_and_fails_toward_none(monkeypatch):
    slots = {"spark_capture": {"g-1-01": [{}]}, "encoding_capture": {"g-1-02": [{}]}}
    monkeypatch.setattr(at._retro, "_load_capture_slot",
                        lambda root, slot: slots[slot])
    assert at.load_pending_capture_goal_ids() == {"g-1-01", "g-1-02"}

    slots["encoding_capture"] = at._retro.UNREADABLE
    assert at.load_pending_capture_goal_ids() is None


# ---- : a non-owner cannot see the owner's capture slots -------------

def test_non_owner_sees_a_marked_undrained_worker_goal_as_pending():
    # OWNER's Body executed these and bravo evaluates. bravo's slots read fine
    # and simply do not hold OWNER's entries: "drained" and "never seen" look
    # identical from here, so settling them would score false zeros.
    goals = [_goal(n, role="worker", marker=MARKER) for n in range(1, 7)]
    t = _run(goals, pending=set(), capture_owner="bravo")
    assert t["plateau_detected"] is False
    assert t["credit_pending_count"] == 6
    assert {ga["credit_pending_reason"] for ga in t["goals"]} == {"capture-slots-not-owner"}
    assert t["capture_owner"] == "bravo"


def test_unknown_owner_on_either_side_resolves_toward_pending():
    t = _run([_goal(1, role="worker", marker=MARKER)], pending=set(),
             capture_owner=None)
    assert t["goals"][0]["credit_pending_reason"] == "capture-slots-not-owner"
    t = _run([_goal(1, role="worker", marker=MARKER, executed_by=None)],
             pending=set())
    assert t["goals"][0]["credit_pending_reason"] == "capture-slots-not-owner"


def test_non_owner_rule_touches_only_marked_zero_worker_goals():
    # Reducer-closed, unmarked and credited goals classify exactly as before for
    # a non-owner: the rule sits after those early returns.
    goals = [_goal(1), _goal(2, role="worker"), _goal(3, role="worker")]
    t = _run(goals, rb={"g-999-03": 1}, pending=set(), capture_owner="bravo")
    reasons = {ga["goal_id"]: ga["credit_pending_reason"] for ga in t["goals"]}
    assert reasons == {"g-999-01": None, "g-999-02": "no-retrospective-marker",
                       "g-999-03": None}


def _shared_via_real_read(monkeypatch, tmp_path, agent, slot_rows):
    """load_shared_data() on the production call shape, hermetically.

    Every store loader is stubbed, and the capture read runs the REAL
    load_pending_capture_goal_ids -> _load_capture_slot -> bash_cmd path,
    stopped only at the subprocess boundary (_retro._run). Returns the shared
    dict and each read's (argv, MIND_AGENT the child would inherit).
    """
    monkeypatch.setenv("MIND_AGENT", agent)
    monkeypatch.setattr(at, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(at, "AGENT_DIR", None)
    monkeypatch.setattr(at, "load_config", lambda: dict(CONFIG))
    monkeypatch.setattr(at, "load_jsonl", lambda p: [])
    monkeypatch.setattr(at, "load_yaml", lambda p: {})
    monkeypatch.setattr(at, "build_tree_attribution_map", lambda p: {})
    monkeypatch.setattr(at, "build_script_convention_attribution_map", lambda: {})
    calls = []

    def fake_run(argv, timeout=90, stdin=None):
        calls.append((list(argv), os.environ.get("MIND_AGENT")))
        return 0, json.dumps(slot_rows.get(argv[-2], [])), ""

    monkeypatch.setattr(at._retro, "_run", fake_run)
    return at.load_shared_data(), calls


def test_capture_owner_is_the_agent_the_real_wm_read_addresses(monkeypatch, tmp_path):
    # bravo evaluates OWNER's goals. The read carries NO agent argument, so it is
    # keyed only by the inherited MIND_AGENT, and the owner recorded beside the
    # ids must be that same value: that pairing is what the rule trusts.
    shared, calls = _shared_via_real_read(
        monkeypatch, tmp_path, "bravo",
        {"spark_capture": [{"goal_id": "g-999-50", "observation": "bravo's own"}]})
    assert [c[0][-2:] for c in calls] == [[s, "--json"]
                                          for s in at.CREDIT_CAPTURE_SLOTS]
    assert all(c[0][-3].endswith("wm-read.sh") and c[1] == "bravo" for c in calls)
    assert shared["capture_owner"] == "bravo"
    assert shared["pending_capture_goal_ids"] == {"g-999-50"}

    shared["asp_sources"] = _asp_sources(
        [_goal(n, role="worker", marker=MARKER) for n in range(1, 7)])
    t = at.build_trajectory("asp-999", shared=shared)
    assert {ga["credit_pending_reason"] for ga in t["goals"]} == {"capture-slots-not-owner"}
    assert t["plateau_detected"] is False


def test_owner_on_the_same_call_shape_still_sees_undrained_and_still_settles(
        monkeypatch, tmp_path):
    # Positive controls (guard-4166): the owner's own evaluation, through the
    # identical read, must still catch an undrained id AND still settle drained
    # zeros. Reverting the non-owner rule must leave this test green.
    shared, _ = _shared_via_real_read(
        monkeypatch, tmp_path, OWNER,
        {"encoding_capture": [{"goal_id": "g-999-01", "fact": "undrained"}]})
    shared["asp_sources"] = _asp_sources(
        [_goal(n, role="worker", marker=MARKER) for n in range(1, 7)])
    t = at.build_trajectory("asp-999", shared=shared)
    reasons = {ga["goal_id"]: ga["credit_pending_reason"] for ga in t["goals"]}
    assert reasons["g-999-01"] == "capture-undrained"
    assert all(reasons[f"g-999-{n:02d}"] is None for n in range(2, 7))
