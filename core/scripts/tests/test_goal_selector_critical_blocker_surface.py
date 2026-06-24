"""test_goal_selector_critical_blocker_surface.py --  regression.

Exercises compute_critical_blocker_surface (extracted from goal-selector.py)
AND the WEIGHTS / config wiring for the critical_blocker_surface criterion
(bravo US-07: surface high-downstream-unlock bottleneck goals recorded in
team-state.critical_blockers[] so they don't get out-ranked indefinitely --
"break one, unlock five").

Decision rule (compute_critical_blocker_surface):
  - goal_id falsy OR critical_blockers not a list                  -> 0.0
  - downstream_cap non-numeric OR <= 0                             -> 0.0
  - first entry whose goal_id matches:
        downstream_count numeric (non-bool) AND >= min_downstream
            -> min(downstream_count, cap) / cap   (in [0, 1])
        else                                                       -> 0.0
        (terminal on first id match -- ids are unique by contract)
  - no matching entry                                              -> 0.0

Wiring invariants:
  - WEIGHTS (loaded from meta/goal-selection-strategy.yaml) contains the key,
    so score_goal's `for k in WEIGHTS` breakdown/total never KeyErrors.
  - load_critical_blocker_surface_config() returns the three-key config shape
    from core/config/aspirations.yaml (or fail-open defaults).

Pattern mirrors test_goal_selector_role_affinity.py: import the pure helper
and call directly. No subprocess, no file I/O.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# goal-selector.py requires MIND_AGENT to load (paths derive AGENT_DIR).
# Capture-restore around the module-level mutation so collection-time env
# pollution cannot leak to other tests (rb-1096, guard-588). Tests don't
# depend on which agent is bound.
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")
compute_critical_blocker_surface = gs.compute_critical_blocker_surface

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

# Representative live-shape critical_blockers[] (matches the 2026-06 team-state:
# top-3 bottlenecks written by aspirations-consolidate Step 8.87).
_CBS = [
    {"goal_id": "g-308-02", "downstream_count": 9, "cause": "ready-unclaimed"},
    {"goal_id": "g-309-01", "downstream_count": 7, "cause": "ready-unclaimed"},
]

# id, goal_id, critical_blockers, min_downstream, downstream_cap, expected
CASES = [
    # --- matches: boost == min(ds, cap) / cap ---
    ("match-ds9-cap10",         "g-308-02", _CBS, 3, 10, 0.9),
    ("match-ds7-cap10",         "g-309-01", _CBS, 3, 10, 0.7),
    ("cap-clamp-ds-over-cap",   "g-x", [{"goal_id": "g-x", "downstream_count": 15}], 3, 10, 1.0),
    ("ds-equals-cap",           "g-x", [{"goal_id": "g-x", "downstream_count": 10}], 3, 10, 1.0),
    ("ds-equals-floor",         "g-x", [{"goal_id": "g-x", "downstream_count": 3}], 3, 10, 0.3),
    ("float-ds",                "g-x", [{"goal_id": "g-x", "downstream_count": 4.0}], 3, 10, 0.4),
    # --- below floor / no match: 0.0 ---
    ("below-floor",             "g-y", [{"goal_id": "g-y", "downstream_count": 2}], 3, 10, 0.0),
    ("no-matching-id",          "g-zzz", _CBS, 3, 10, 0.0),
    ("empty-list",              "g-308-02", [], 3, 10, 0.0),
    # --- fail-open paths: 0.0, never raise ---
    ("none-list",               "g-x", None, 3, 10, 0.0),
    ("nonlist-dict",            "g-x", {"goal_id": "g-x"}, 3, 10, 0.0),
    ("empty-goal-id",           "", _CBS, 3, 10, 0.0),
    ("none-goal-id",            None, _CBS, 3, 10, 0.0),
    ("zero-cap",                "g-308-02", _CBS, 3, 0, 0.0),
    ("negative-cap",            "g-308-02", _CBS, 3, -5, 0.0),
    ("noncoercible-cap",        "g-308-02", _CBS, 3, "abc", 0.0),
    ("nondict-entry-skipped",   "g-x", ["garbage", {"goal_id": "g-x", "downstream_count": 9}], 3, 10, 0.9),
    ("missing-downstream",      "g-m", [{"goal_id": "g-m"}], 3, 10, 0.0),
    ("none-downstream",         "g-m", [{"goal_id": "g-m", "downstream_count": None}], 3, 10, 0.0),
    ("string-downstream",       "g-m", [{"goal_id": "g-m", "downstream_count": "9"}], 3, 10, 0.0),
    ("bool-downstream-rejected","g-b", [{"goal_id": "g-b", "downstream_count": True}], 1, 10, 0.0),
    # Terminal-on-first-id-match: a below-floor first match does NOT fall through
    # to a later duplicate id (ids unique by contract; proves the `return 0.0`
    # after the matched-but-below branch is reached, not a `continue`).
    ("first-id-match-terminal", "g-d", [{"goal_id": "g-d", "downstream_count": 1},
                                        {"goal_id": "g-d", "downstream_count": 9}], 3, 10, 0.0),
]


def test_compute_critical_blocker_surface_cases():
    failures = []
    for cid, goal_id, cbs, min_ds, cap, expected in CASES:
        try:
            actual = compute_critical_blocker_surface(goal_id, cbs, min_ds, cap)
        except Exception as e:  # fail-open contract: must never raise
            failures.append(f"{cid}: raised {type(e).__name__}: {e}")
            continue
        if actual != expected:
            failures.append(f"{cid}: got {actual!r}, expected {expected!r}")
    assert not failures, (
        "compute_critical_blocker_surface mismatches:\n" + "\n".join(failures))


def test_weights_contains_key():
    """WEIGHTS (meta SSOT) must carry the key so score_goal's `for k in WEIGHTS`
    breakdown/total never KeyErrors on the new criterion (g-305-07)."""
    assert "critical_blocker_surface" in gs.WEIGHTS, (
        "critical_blocker_surface missing from WEIGHTS -- meta/goal-selection-"
        "strategy.yaml weights block out of sync (g-305-07)")
    assert gs.WEIGHTS["critical_blocker_surface"] >= 0.0


def test_config_loader_shape():
    """Config loader returns the three expected keys with sane types (or
    fail-open defaults of the same shape)."""
    cfg = gs.load_critical_blocker_surface_config()
    assert set(cfg) == {"enabled", "min_downstream", "downstream_cap"}
    assert isinstance(cfg["enabled"], bool)
    assert isinstance(cfg["min_downstream"], int)
    assert isinstance(cfg["downstream_cap"], int)


if __name__ == "__main__":
    # Standalone runnable (mirrors role_affinity's pattern); pytest collects the
    # test_* functions above directly.
    test_compute_critical_blocker_surface_cases()
    test_weights_contains_key()
    test_config_loader_shape()
    print("All critical_blocker_surface cases verified.")
