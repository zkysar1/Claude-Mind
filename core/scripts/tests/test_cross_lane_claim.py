#!/usr/bin/env python3
"""test_cross_lane_claim.py —  regression test.

Verifies the cross-lane override on the CLAIM path:

  1. Refusal: claim with intended_agent != claimer (and != 'either') AND no
     cross_lane → 400 cross_lane_refused, no claim written, no ledger record.
  2. Override: same claim + cross_lane '<reason>' → claim succeeds,
     ledger record written with gate=capability-route-gate.
  3. Same-lane claim: intended_agent == claimer → claim succeeds without
     cross_lane, no ledger record.
  4. Either-lane claim: intended_agent == 'either' → claim succeeds without
     cross_lane, no ledger record.
  5. Unset intended_agent → claim succeeds without cross_lane, no ledger record.

REWRITTEN 2026-07-16 (g-115-2352): the original strategy invoked
aspirations.cmd_claim with a synthetic argparse namespace, but cmd_claim was
removed in the daemon-only migration — the production claim path IS the daemon
endpoint (mind_api/src/endpoints/aspirations_write.py::claim, reached via
aspirations-claim.sh; no CLI mirror exists to keep byte-parallel). The
cross-lane guard lives at that endpoint (~L3207: intended_agent vs claimer,
cross_lane query param, _audit_cross_lane_claim_inline ledger write). Pattern:
DaemonFixture + direct HTTP POST, mirroring test_claim_staleness_takeback.py —
hermetic (tmp project root, thread-local daemon), no env-pin leakage
(daemon-era test-hermeticity rule: env pins never cross the daemon boundary,
so the test drives the REAL endpoint against a fixture root instead).

Run: py -3 core/scripts/tests/test_cross_lane_claim.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402


def _make_world(tmp: Path, goal: dict) -> Path:
    """Tempdir world holding one aspiration with `goal`; claiming agent alpha."""
    world = tmp / "world"
    world.mkdir()
    aspiration = {
        "id": "asp-test-282-07",
        "title": "Test aspiration",
        "status": "active",
        "priority": "MEDIUM",
        "goals": [goal],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(aspiration, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _claim(port: int, goal_id: str, agent: str,
           cross_lane: str | None = None) -> tuple[int, str]:
    params = {"id": goal_id, "agent": agent}
    if cross_lane is not None:
        params["cross_lane"] = cross_lane
    url = (f"http://127.0.0.1:{port}/v1/aspirations/claim?"
           + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _read_ledger(world: Path):
    p = world / "override-bypass-ledger.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def _read_goal(world: Path):
    data = json.loads(
        (world / "aspirations.jsonl").read_text(encoding="utf-8"))
    return data["goals"][0]


def _goal_fixture(goal_id: str, title: str, **extra) -> dict:
    g = {
        "id": goal_id, "title": title,
        "status": "pending", "priority": "MEDIUM",
        "verification": {"outcomes": [], "preconditions": [], "checks": []},
    }
    g.update(extra)
    return g


def case_refusal_no_cross_lane() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="cross-lane-refusal-"))
    try:
        world = _make_world(tmpdir, _goal_fixture(
            "g-test-1", "Test cross-lane goal", intended_agent="bravo"))
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-test-1", "alpha")
        if status != 400 or "cross_lane_refused" not in out:
            print(f"FAIL case_refusal: expected 400 cross_lane_refused, "
                  f"got {status}: {out[:200]}")
            return False
        goal = _read_goal(world)
        if goal.get("claimed_by"):
            print(f"FAIL case_refusal: goal got claimed despite refusal: "
                  f"{goal.get('claimed_by')}")
            return False
        records = _read_ledger(world)
        if records:
            print(f"FAIL case_refusal: ledger record written despite refusal: "
                  f"{records}")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_override_with_cross_lane() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="cross-lane-override-"))
    try:
        world = _make_world(tmpdir, _goal_fixture(
            "g-test-2", "Test cross-lane goal — overridden",
            intended_agent="bravo", category="framework-self-improvement"))
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-test-2", "alpha",
                                 cross_lane="urgent — partner on PTO")
        if status != 200:
            print(f"FAIL case_override: expected 200, got {status}: {out[:200]}")
            return False
        goal = _read_goal(world)
        if goal.get("claimed_by") != "alpha":
            print(f"FAIL case_override: claimed_by={goal.get('claimed_by')!r}")
            return False
        records = _read_ledger(world)
        if len(records) != 1:
            print(f"FAIL case_override: expected 1 ledger record, "
                  f"got {len(records)}")
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
            print(f"FAIL case_override: "
                  f"context.intended_agent={ctx.get('intended_agent')!r}")
            return False
        if ctx.get("agent_claiming") != "alpha":
            print(f"FAIL case_override: "
                  f"context.agent_claiming={ctx.get('agent_claiming')!r}")
            return False
        if rec.get("justification") != "urgent — partner on PTO":
            print(f"FAIL case_override: "
                  f"justification={rec.get('justification')!r}")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_same_lane_no_cross_lane_needed() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="cross-lane-same-"))
    try:
        world = _make_world(tmpdir, _goal_fixture(
            "g-test-3", "Same-lane goal", intended_agent="alpha"))
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-test-3", "alpha")
        if status != 200:
            print(f"FAIL case_same_lane: expected 200, got {status}: {out[:200]}")
            return False
        goal = _read_goal(world)
        if goal.get("claimed_by") != "alpha":
            print(f"FAIL case_same_lane: claimed_by={goal.get('claimed_by')!r}")
            return False
        if _read_ledger(world):
            print("FAIL case_same_lane: ledger record written for same-lane claim")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_either_lane_no_cross_lane_needed() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="cross-lane-either-"))
    try:
        world = _make_world(tmpdir, _goal_fixture(
            "g-test-4", "Either-lane goal", intended_agent="either"))
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-test-4", "alpha")
        if status != 200:
            print(f"FAIL case_either_lane: expected 200, got {status}: {out[:200]}")
            return False
        goal = _read_goal(world)
        if goal.get("claimed_by") != "alpha":
            print(f"FAIL case_either_lane: claimed_by={goal.get('claimed_by')!r}")
            return False
        if _read_ledger(world):
            print("FAIL case_either_lane: ledger record written for either-lane claim")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_unset_intended_agent_no_cross_lane_needed() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="cross-lane-unset-"))
    try:
        world = _make_world(tmpdir, _goal_fixture(
            "g-test-5", "Unset intended_agent goal"))
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-test-5", "alpha")
        if status != 200:
            print(f"FAIL case_unset_intended: expected 200, got {status}: {out[:200]}")
            return False
        goal = _read_goal(world)
        if goal.get("claimed_by") != "alpha":
            print(f"FAIL case_unset_intended: claimed_by={goal.get('claimed_by')!r}")
            return False
        if _read_ledger(world):
            print("FAIL case_unset_intended: ledger record written for "
                  "unset-intended claim")
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
