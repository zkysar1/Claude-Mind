"""test_add_goal_handoff_intended_agent.py — regression for .

Phase D of the add-goal endpoint (capability-route mutator) stamps
`intended_agent` when the caller didn't set one. Before g-115-2577 it ran the
title-verb classifier even when the caller set an explicit `handoff_to`,
so an "Apply:" goal handed to zeta could be stamped intended_agent=alpha —
and the goal-selector's intended_agent filter then hid the goal from the very
agent the handoff named (handoff_bonus unreachable; observed live: g-336-17/18
invisible to zeta across 3 consecutive selector runs).

Three contracts pinned here:
  1. handoff_to (valid agent) + no intended_agent + no route header
     → intended_agent stamped FROM handoff_to (not from the classifier).
  2. Explicit caller-supplied intended_agent is preserved even when handoff_to
     names a different agent (caller's choice wins; gate never overrides).
  3. An invalid handoff_to never lands as intended_agent (classifier path).

Pattern: DaemonFixture + direct HTTP POST — mirrors
test_add_goal_filed_by_agent_stamp.py. The tmp world seeds team-state.yaml
with an agent_status roster so _valid_intended_agents() sees alpha+zeta.
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402


def _make_world(tmp: Path) -> Path:
    """Tempdir world with asp-100 + a team-state roster (alpha, zeta)."""
    world = tmp / "world"
    world.mkdir()

    asp = {
        "id": "asp-100",
        "title": "handoff intended_agent stamp regression",
        "motivation": "Test Phase D handoff_to-aware intended_agent stamping",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-07-01T00:00:00",
        "goals": [],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    # Roster source for _agents.get_active_agents → _valid_intended_agents().
    (world / "team-state.yaml").write_text(
        "agent_status:\n"
        "  alpha:\n    last_active: '2026-07-01T00:00:00'\n"
        "  zeta:\n    last_active: '2026-07-01T00:00:00'\n",
        encoding="utf-8",
    )

    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _add_goal(port: int, body: dict, agent: str = "alpha") -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/add-goal"
           "?asp_id=asp-100&source=world")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", agent)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _find_goal_by_title(world: Path, title: str) -> dict | None:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("title") == title:
                return g
    return None


def _base_body(title: str) -> dict:
    return {
        "title": title,
        "description": "Phase D handoff routing regression",
        "priority": "MEDIUM",
        "status": "pending",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }


def test_handoff_to_derives_intended_agent():
    """handoff_to=zeta with no explicit intended_agent → stamped zeta."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                **_base_body("Apply: build the slice (handoff regression)"),
                "handoff_to": "zeta",
                "handoff_from": "zeta",
                "handoff_created_at": "2026-07-18T00:00:00",
            }
            status, out = _add_goal(df.port, body)
            assert status == 200, f"add-goal status={status}; body={out!r}"
            g = _find_goal_by_title(
                world, "Apply: build the slice (handoff regression)")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            assert g.get("intended_agent") == "zeta", (
                "Phase D must derive intended_agent from an explicit valid "
                f"handoff_to; got {g.get('intended_agent')!r} "
                f"for goal {g.get('id')!r} (g-115-2577)")


def test_explicit_intended_agent_beats_handoff_to():
    """Caller-supplied intended_agent wins even when handoff_to differs."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                **_base_body("Apply: explicit routing wins"),
                "intended_agent": "alpha",
                "handoff_to": "zeta",
            }
            status, out = _add_goal(df.port, body)
            assert status == 200, f"add-goal status={status}; body={out!r}"
            g = _find_goal_by_title(world, "Apply: explicit routing wins")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            assert g.get("intended_agent") == "alpha", (
                "explicit caller intended_agent must be preserved; "
                f"got {g.get('intended_agent')!r}")


def test_invalid_handoff_to_falls_back_to_classifier():
    """handoff_to naming a non-roster agent never lands as intended_agent."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                **_base_body("Apply: invalid handoff target"),
                "handoff_to": "nonexistent-agent",
            }
            status, out = _add_goal(df.port, body)
            assert status == 200, f"add-goal status={status}; body={out!r}"
            g = _find_goal_by_title(world, "Apply: invalid handoff target")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            assert g.get("intended_agent") != "nonexistent-agent", (
                "an invalid handoff_to must never be stamped as "
                f"intended_agent; got {g.get('intended_agent')!r}")


if __name__ == "__main__":
    test_handoff_to_derives_intended_agent()
    test_explicit_intended_agent_beats_handoff_to()
    test_invalid_handoff_to_falls_back_to_classifier()
    print("ok")
