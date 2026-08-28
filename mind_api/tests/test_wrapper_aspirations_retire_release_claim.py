"""End-to-end wrapper tests for aspirations-retire/release/claim (PR 9b).

Test strategy:
  - running_daemon fixture spawns a daemon in a tmp project_root
  - We override RT_DIR so the wrapper finds the tmp daemon's port file
  - We seed aspirations.jsonl with test data before each call
  - Claim tests additionally pin the scorer-verdict sidecar (see
    `_seed_scorer_verdict`) -- RT_DIR does NOT reach it
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_RETIRE = REPO_ROOT / "core" / "scripts" / "aspirations-retire.sh"
WRAPPER_RELEASE = REPO_ROOT / "core" / "scripts" / "aspirations-release.sh"
WRAPPER_CLAIM = REPO_ROOT / "core" / "scripts" / "aspirations-claim.sh"


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _seed_aspiration(world: Path, asp):
    path = world / "aspirations.jsonl"
    path.write_text(json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")


def _seed_scorer_verdict(project_root: Path, top_goal_id: str,
                         agent: str = "alpha") -> Path:
    """Fixture-scoped scorer-verdict sidecar; returns the path to pass through.

    THE CLAIM WRAPPER IS THE ONLY ONE OF THE THREE THAT NEEDS THIS, and neither
    of the other two knobs reaches it. `project_root` and `RT_DIR` pin the
    daemon; the Scorer Sovereignty gate resolves its sidecar independently, as
    `agent_state_dir(<agent>) / scorer-verdict.json` through `_paths` -- rooted
    at the REAL repo, not at the tmp tree. Un-pinned, these tests read whatever
    verdict the LIVE selector wrote minutes ago and the claim is refused for
    diverging from a top pick that exists only in the live queue (g-115-5492).

    WHY IT LOOKED INTERMITTENT: the gate is fail-open and denies only on a FRESH
    verdict, so in a quiet window the live verdict is stale or absent and these
    tests pass. A green run was evidence of TIMING, not of isolation.

    `ts` is computed relative to now for the same reason (guard-566): a
    hardcoded timestamp would go stale, the gate would fail open, and the test
    would pass without the gate ever reading this file -- an isolation that
    only looks like one. `test_wrapper_claim_sovereignty_refuses_...` below is
    the positive control that this file is genuinely being read.

    The path mirrors the production layout inside the tmp root rather than
    inventing a flat one, so the fixture stays recognizable as the thing it
    stands in for (guard-920).
    """
    path = (project_root / "agents" / agent / "session" / "scorer-verdict.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "top_goal_id": top_goal_id,
        "top_score": 1.0,
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "top_5": [{"goal_id": top_goal_id, "score": 1.0}],
    }), encoding="utf-8")
    return path


def _run(wrapper, args, *, project_root: Path, agent: str = "alpha"):
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    # Production shape ALWAYS carries a sid (bash-agent-inject injects
    # MIND_SID into every hooked Bash call, and the claim endpoint's
    # missing_claim_sid gate refuses without one). The inject hook fails OPEN
    # on timeout and is absent in un-hooked launch contexts (background
    # tasks, cron, CI), so inheriting the session env makes these tests
    # flake on exactly those runs. Pin a deterministic sid instead of
    # depending on inheritance — setdefault keeps a test's own override.
    env.setdefault("MIND_SID", "pytest-wrapper-harness-sid")
    proc = subprocess.run(
        [_bash(), wrapper.as_posix(), *args],
        env=env, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# retire wrapper tests
# ---------------------------------------------------------------------------

def test_wrapper_retire_happy_path(running_daemon):
    """Daemon path: prints retired aspiration JSON to stdout, exits 0."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Retire me", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Done", "status": "completed",
             "recurring": False},
        ],
        "progress": {"completed_goals": 1, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    rc, out, err = _run(WRAPPER_RETIRE, ["asp-001"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["status"] == "retired"
    assert parsed["archived"] is True


def test_wrapper_retire_guard_block(running_daemon):
    """Aspiration with recurring goals -> wrapper exit 1."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Has recurring", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Recurring", "status": "pending",
             "recurring": True, "interval_hours": 24},
        ],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 1},
    }
    _seed_aspiration(world, asp)

    rc, out, err = _run(WRAPPER_RETIRE, ["asp-001"], project_root=project_root)
    assert rc == 1, f"expected exit 1, got {rc}"
    assert "recurring_goals_present" in err


# ---------------------------------------------------------------------------
# release wrapper tests
# ---------------------------------------------------------------------------

def test_wrapper_release_happy_path(running_daemon):
    """Daemon path: prints released goal JSON to stdout, exits 0."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Test", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Claimed", "status": "in-progress",
             "recurring": False, "claimed_by": "alpha",
             "claimed_at": "2026-05-10T10:00:00"},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    rc, out, err = _run(WRAPPER_RELEASE, ["g-001-01"], project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert "claimed_by" not in parsed
    assert "claimed_at" not in parsed


def test_wrapper_release_not_found(running_daemon):
    """Unknown goal -> wrapper exit 1."""
    project_root, _ = running_daemon
    rc, out, err = _run(WRAPPER_RELEASE, ["g-999-01"], project_root=project_root)
    assert rc == 1, f"expected exit 1, got {rc}"
    assert "goal_not_found" in err


# ---------------------------------------------------------------------------
# claim wrapper tests
# ---------------------------------------------------------------------------

def test_wrapper_claim_happy_path(running_daemon):
    """Daemon path: prints claimed goal JSON to stdout, exits 0."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Test", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Claimable", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)
    verdict = _seed_scorer_verdict(project_root, "g-001-01")

    rc, out, err = _run(WRAPPER_CLAIM,
                        ["g-001-01", "alpha", "--verdict-file", str(verdict)],
                        project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["claimed_by"] == "alpha"
    assert "claimed_at" in parsed


def test_wrapper_claim_cross_lane_refused(running_daemon):
    """Goal routed to bravo, alpha claims without --cross-lane -> exit 2 (T2.2)."""
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Test", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Routed", "status": "pending",
             "recurring": False, "intended_agent": "bravo"},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)
    # Sovereignty must ALLOW here so the cross-lane refusal is what we measure:
    # both gates exit 2, and only the stderr text tells them apart.
    verdict = _seed_scorer_verdict(project_root, "g-001-01")

    rc, out, err = _run(WRAPPER_CLAIM,
                        ["g-001-01", "alpha", "--verdict-file", str(verdict)],
                        project_root=project_root)
    assert rc == 2, f"expected exit 2, got {rc}"
    assert "cross_lane_refused" in err
    assert "scorer-sovereignty" not in err, (
        "sovereignty gate refused first -- the fixture verdict was not honored, "
        f"so this test is measuring the wrong gate: {err}")


def test_wrapper_claim_sovereignty_refuses_with_fixture_verdict(running_daemon):
    """Positive control for the `--verdict-file` seam ().

    The two tests above pass when the gate ALLOWS, and the gate allows in three
    materially different situations: the fixture verdict was read and named this
    goal as top pick (what we intend), the flag was silently dropped and the LIVE
    verdict happened to be stale (fail-open), or the fixture verdict was written
    unparseably (fail-open again). All three look identical from a green run, so
    a passing suite above is NOT by itself evidence that the seam works.

    This test makes them distinguishable: the fixture verdict names a DIFFERENT
    top pick, so the gate must refuse -- and must name the fixture's goal-id in
    the refusal. A refusal quoting anything else means the gate read some other
    verdict; no refusal at all means it read no verdict.

    It also pins the half the fix must NOT break: threading a path argument
    exposes the gate to tests, it does not disarm it.
    """
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Test", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Claimable", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)
    verdict = _seed_scorer_verdict(project_root, "g-001-99")

    rc, out, err = _run(WRAPPER_CLAIM,
                        ["g-001-01", "alpha", "--verdict-file", str(verdict)],
                        project_root=project_root)
    assert rc == 2, f"expected sovereignty refusal (exit 2), got {rc}: {err}"
    assert "scorer-sovereignty" in err, err
    assert "g-001-99" in err, (
        "refusal did not name the FIXTURE's top pick -- the gate read a "
        f"different verdict than the one threaded in: {err}")


def test_wrapper_claim_sanctioned_deviation_clears_sovereignty(running_daemon):
    """The refusal above is not a dead end: a deviation code clears it.

    Without this, the control test alone would be satisfied by a gate that
    refuses unconditionally. Same fixture verdict, same divergent claim, one
    added `--deviation` from the closed enum -- and the claim must land.
    """
    project_root, _ = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Test", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Claimable", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)
    verdict = _seed_scorer_verdict(project_root, "g-001-99")

    rc, out, err = _run(WRAPPER_CLAIM,
                        ["g-001-01", "alpha", "--verdict-file", str(verdict),
                         "--deviation", "force-override"],
                        project_root=project_root)
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    assert json.loads(out)["claimed_by"] == "alpha"
