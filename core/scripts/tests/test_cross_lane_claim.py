#!/usr/bin/env python3
"""test_cross_lane_claim.py —  regression test.

Verifies the --cross-lane override flag on aspirations.py claim:

  1. Refusal: claim with intended_agent != claimer (and != 'either') AND no
     --cross-lane → exit 2, no claim written, no ledger record.
  2. Override: same claim + --cross-lane '<reason>' → claim succeeds,
     ledger record written with gate=capability-route-gate.
  3. Same-lane claim: intended_agent == claimer → claim succeeds without
     --cross-lane, no ledger record.
  4. Either-lane claim: intended_agent == 'either' → claim succeeds without
     --cross-lane, no ledger record.

Strategy: patch WORLD_DIR + LIVE_PATH to a temp aspirations.jsonl, invoke
aspirations.cmd_claim directly with a synthetic args namespace, read back
both the goal record and the ledger.

Run: py -3 core/scripts/tests/test_cross_lane_claim.py
"""
from __future__ import annotations

import argparse
import importlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def _setup_world(tmpdir: Path):
    """Patch WORLD_DIR + LIVE_PATH to tmpdir, reload aspirations module."""
    for mod in ("_paths", "_fileops", "_override_helpers", "aspirations"):
        sys.modules.pop(mod, None)
    import _paths
    _paths.WORLD_DIR = tmpdir
    import _override_helpers
    _override_helpers.WORLD_DIR = tmpdir
    import aspirations as asp_mod
    asp_mod.WORLD_DIR = tmpdir
    asp_mod.LIVE_PATH = tmpdir / "aspirations.jsonl"
    return asp_mod


def _write_aspiration(tmpdir: Path, goal: dict) -> None:
    aspiration = {
        "id": "asp-test-282-07",
        "title": "Test aspiration",
        "status": "active",
        "priority": "MEDIUM",
        "goals": [goal],
    }
    (tmpdir / "aspirations.jsonl").write_text(
        json.dumps(aspiration) + "\n", encoding="utf-8"
    )


def _read_ledger(tmpdir: Path):
    p = tmpdir / "override-bypass-ledger.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _read_goal(tmpdir: Path):
    data = json.loads((tmpdir / "aspirations.jsonl").read_text(encoding="utf-8"))
    return data["goals"][0]


def _args(goal_id: str, agent_name: str, cross_lane: str | None = None):
    return argparse.Namespace(goal_id=goal_id, agent_name=agent_name, cross_lane=cross_lane)


def case_refusal_no_cross_lane() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="cross-lane-refusal-"))
    try:
        asp_mod = _setup_world(tmpdir)
        _write_aspiration(tmpdir, {
            "id": "g-test-1", "title": "Test cross-lane goal",
            "status": "pending", "intended_agent": "bravo",
            "verification": {"outcomes": [], "preconditions": [], "checks": []},
        })
        try:
            asp_mod.cmd_claim(_args("g-test-1", "alpha", cross_lane=None))
            print("FAIL case_refusal: cmd_claim did not raise SystemExit")
            return False
        except SystemExit as e:
            if e.code != 2:
                print(f"FAIL case_refusal: exit code {e.code} (expected 2)")
                return False
        goal = _read_goal(tmpdir)
        if goal.get("claimed_by"):
            print(f"FAIL case_refusal: goal got claimed despite refusal: {goal.get('claimed_by')}")
            return False
        records = _read_ledger(tmpdir)
        if records:
            print(f"FAIL case_refusal: ledger record written despite refusal: {records}")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_override_with_cross_lane() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="cross-lane-override-"))
    try:
        asp_mod = _setup_world(tmpdir)
        _write_aspiration(tmpdir, {
            "id": "g-test-2", "title": "Test cross-lane goal — overridden",
            "status": "pending", "intended_agent": "bravo",
            "category": "framework-self-improvement",
            "verification": {"outcomes": [], "preconditions": [], "checks": []},
        })
        try:
            asp_mod.cmd_claim(_args("g-test-2", "alpha",
                                    cross_lane="urgent — partner on PTO"))
        except SystemExit as e:
            print(f"FAIL case_override: unexpected SystemExit(code={e.code})")
            return False
        goal = _read_goal(tmpdir)
        if goal.get("claimed_by") != "alpha":
            print(f"FAIL case_override: claimed_by={goal.get('claimed_by')!r}")
            return False
        records = _read_ledger(tmpdir)
        if len(records) != 1:
            print(f"FAIL case_override: expected 1 ledger record, got {len(records)}")
            return False
        rec = records[0]
        if rec.get("gate") != "capability-route-gate":
            print(f"FAIL case_override: gate={rec.get('gate')!r}")
            return False
        ctx = rec.get("context", {})
        if ctx.get("goal_id") != "g-test-2":
            print(f"FAIL case_override: context.goal_id={ctx.get('goal_id')!r}")
            return False
        if ctx.get("intended_agent") != "bravo":
            print(f"FAIL case_override: context.intended_agent={ctx.get('intended_agent')!r}")
            return False
        if ctx.get("agent_claiming") != "alpha":
            print(f"FAIL case_override: context.agent_claiming={ctx.get('agent_claiming')!r}")
            return False
        if rec.get("justification") != "urgent — partner on PTO":
            print(f"FAIL case_override: justification={rec.get('justification')!r}")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_same_lane_no_cross_lane_needed() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="cross-lane-same-"))
    try:
        asp_mod = _setup_world(tmpdir)
        _write_aspiration(tmpdir, {
            "id": "g-test-3", "title": "Same-lane goal",
            "status": "pending", "intended_agent": "alpha",
            "verification": {"outcomes": [], "preconditions": [], "checks": []},
        })
        try:
            asp_mod.cmd_claim(_args("g-test-3", "alpha", cross_lane=None))
        except SystemExit as e:
            print(f"FAIL case_same_lane: unexpected SystemExit(code={e.code})")
            return False
        goal = _read_goal(tmpdir)
        if goal.get("claimed_by") != "alpha":
            print(f"FAIL case_same_lane: claimed_by={goal.get('claimed_by')!r}")
            return False
        if _read_ledger(tmpdir):
            print(f"FAIL case_same_lane: ledger record written for same-lane claim")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_either_lane_no_cross_lane_needed() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="cross-lane-either-"))
    try:
        asp_mod = _setup_world(tmpdir)
        _write_aspiration(tmpdir, {
            "id": "g-test-4", "title": "Either-lane goal",
            "status": "pending", "intended_agent": "either",
            "verification": {"outcomes": [], "preconditions": [], "checks": []},
        })
        try:
            asp_mod.cmd_claim(_args("g-test-4", "alpha", cross_lane=None))
        except SystemExit as e:
            print(f"FAIL case_either_lane: unexpected SystemExit(code={e.code})")
            return False
        goal = _read_goal(tmpdir)
        if goal.get("claimed_by") != "alpha":
            print(f"FAIL case_either_lane: claimed_by={goal.get('claimed_by')!r}")
            return False
        if _read_ledger(tmpdir):
            print(f"FAIL case_either_lane: ledger record written for either-lane claim")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_unset_intended_agent_no_cross_lane_needed() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="cross-lane-unset-"))
    try:
        asp_mod = _setup_world(tmpdir)
        _write_aspiration(tmpdir, {
            "id": "g-test-5", "title": "Unset intended_agent goal",
            "status": "pending",
            "verification": {"outcomes": [], "preconditions": [], "checks": []},
        })
        try:
            asp_mod.cmd_claim(_args("g-test-5", "alpha", cross_lane=None))
        except SystemExit as e:
            print(f"FAIL case_unset_intended: unexpected SystemExit(code={e.code})")
            return False
        goal = _read_goal(tmpdir)
        if goal.get("claimed_by") != "alpha":
            print(f"FAIL case_unset_intended: claimed_by={goal.get('claimed_by')!r}")
            return False
        if _read_ledger(tmpdir):
            print(f"FAIL case_unset_intended: ledger record written for unset-intended claim")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run() -> int:
    cases = [
        ("case_refusal_no_cross_lane", case_refusal_no_cross_lane),
        ("case_override_with_cross_lane", case_override_with_cross_lane),
        ("case_same_lane_no_cross_lane_needed", case_same_lane_no_cross_lane_needed),
        ("case_either_lane_no_cross_lane_needed", case_either_lane_no_cross_lane_needed),
        ("case_unset_intended_agent_no_cross_lane_needed", case_unset_intended_agent_no_cross_lane_needed),
    ]
    failures = []
    for name, fn in cases:
        if fn():
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print(f"\nAll {len(cases)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
