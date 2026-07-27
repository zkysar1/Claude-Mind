"""RC1 regression tests — clearing the LAST blocked_by must restore status.

Before the fix, dependent-unblock.py Step 1 removed the completed predecessor
from each dependent's `blocked_by` but never touched `status`. A goal whose
last dependency cleared stayed status="blocked" forever: free of every
predecessor, yet invisible to the goal-selector. Nothing else in the framework
performs that transition, so the leak was silent AND cumulative — every future
unblock stranded its own dependents and the backlog only grew.

These tests pin the three guard conditions. Each guarded case is a goal that
must NOT be flipped, and each has a distinct owner:
  - still blocked by others  -> the remaining predecessors own it
  - non-`blocked` status     -> terminal/in-progress states are not ours to undo
  - blocker_ref present      -> CREATE_BLOCKER + its re-probe sweep own that axis
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent

if not os.environ.get("MIND_AGENT"):
    os.environ["MIND_AGENT"] = "alpha"


def _load():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "dependent_unblock_rc1", str(SCRIPTS_DIR / "dependent-unblock.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(goal, *, completed="g-1-1"):
    """Drive main() with a single blocked_match; return the recorded _update calls."""
    mod = _load()
    calls = []

    def fake_update(source, goal_id, field, value, dry_run):
        calls.append((goal_id, field, value))
        return True, None

    with mock.patch.object(mod, "_scan", return_value=([("world", "asp-1", goal)], [])), \
         mock.patch.object(mod, "_update", side_effect=fake_update):
        rc = mod.main(["--goal", completed, "--summary", "done"])
    assert rc == 0
    return calls


def _status_writes(calls):
    return [c for c in calls if c[1] == "status"]


# ── the fix itself ───────────────────────────────────────────────────────────

def test_last_blocked_by_cleared_restores_pending():
    calls = _run({"id": "g-2-1", "status": "blocked", "blocked_by": ["g-1-1"]})
    assert ("g-2-1", "blocked_by", "[]") in calls
    assert _status_writes(calls) == [("g-2-1", "status", "pending")]


def test_string_form_blocked_by_also_restores():
    # blocked_by may be a bare string, not a list — the scalar form must not
    # silently skip the restore.
    calls = _run({"id": "g-2-2", "status": "blocked", "blocked_by": "g-1-1"})
    assert _status_writes(calls) == [("g-2-2", "status", "pending")]


def test_defer_reason_does_not_block_restore():
    # defer and status are ORTHOGONAL axes. A deferred goal returns to pending
    # and stays suppressed by its defer; gating on defer_reason would re-strand
    # exactly the goals carrying both.
    calls = _run({"id": "g-2-3", "status": "blocked", "blocked_by": ["g-1-1"],
                  "defer_reason": "precondition_unmet: waiting on X"})
    assert _status_writes(calls) == [("g-2-3", "status", "pending")]


# ── guard 1: still blocked by other predecessors ─────────────────────────────

def test_remaining_blocked_by_does_not_restore():
    calls = _run({"id": "g-2-4", "status": "blocked",
                  "blocked_by": ["g-1-1", "g-9-9"]})
    assert ("g-2-4", "blocked_by", '["g-9-9"]') in calls
    assert _status_writes(calls) == []


# ── guard 2: only `blocked` is ours to undo ──────────────────────────────────

def test_non_blocked_statuses_are_never_touched():
    for st in ("completed", "skipped", "in-progress", "expired", "pending"):
        calls = _run({"id": "g-2-5", "status": st, "blocked_by": ["g-1-1"]})
        assert _status_writes(calls) == [], f"status={st} must not be rewritten"


# ── guard 3: blocker_ref is a separate suppression axis ──────────────────────

def test_blocker_ref_present_does_not_restore():
    # blocked_by going empty says NOTHING about whether the structured blocker
    # cleared — that axis is owned by CREATE_BLOCKER and its re-probe sweep.
    calls = _run({"id": "g-2-6", "status": "blocked", "blocked_by": ["g-1-1"],
                  "blocker_ref": "blk-123"})
    assert ("g-2-6", "blocked_by", "[]") in calls
    assert _status_writes(calls) == []


# ── reporting + dry-run ──────────────────────────────────────────────────────

def test_dry_run_still_reports_the_restore_intent():
    mod = _load()
    calls = []

    def fake_update(source, goal_id, field, value, dry_run):
        calls.append((goal_id, field, value, dry_run))
        return True, None

    goal = {"id": "g-2-7", "status": "blocked", "blocked_by": ["g-1-1"]}
    with mock.patch.object(mod, "_scan", return_value=([("world", "asp-1", goal)], [])), \
         mock.patch.object(mod, "_update", side_effect=fake_update):
        rc = mod.main(["--goal", "g-1-1", "--summary", "done", "--dry-run"])
    assert rc == 0
    status = [c for c in calls if c[2] == "pending"]
    assert status and status[0][3] is True, "dry_run flag must propagate to the restore"
