"""test_add_goal_filed_by_agent_stamp.py — regression for .

The single-goal add_goal endpoint (what aspirations-add-goal.sh POSTs to) must
stamp `filed_by_agent` = the filing agent at add time, so the per-agent
contribution-vs-harm scorecard can attribute churn ("who filed the goal that
later expired?") without git-blame / which-queue heuristics. Prereq for T2
(g-318-02).

Three contracts pinned here:
  1. add_goal stamps filed_by_agent from the requesting agent (X-Mind-Agent).
  2. add_goal PRESERVES an explicit caller-supplied filed_by_agent (setdefault
     semantics — a goal may be filed on behalf of another agent).
  3. aspirations.py::validate_goal accepts null/string and rejects non-string
     (the canonical validator; backward-compat = missing field is fine).

Pattern: DaemonFixture + direct HTTP POST (bash-free, hits the patched endpoint
via the fixture's in-process daemon) for (1)/(2); direct import for (3).
Mirrors test_add_goal_blocked_since_stamp.py.
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
import aspirations  # noqa: E402


def _make_world(tmp: Path) -> tuple[Path, Path]:
    """Tempdir world with asp-100 holding one existing goal ."""
    world = tmp / "world"
    world.mkdir()

    seed = {
        "id": "g-100-01",
        "title": "Seed goal",
        "description": "Pre-existing goal",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    asp = {
        "id": "asp-100",
        "title": "filed_by_agent stamp regression",
        "motivation": "Test add_goal filed_by_agent stamping",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-01T00:00:00",
        "goals": [seed],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world, agent_dir


def _add_goal(port: int, body: dict, agent: str = "alpha") -> tuple[int, str]:
    """POST a single goal under asp-100 to the daemon add-goal endpoint."""
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


def test_add_goal_stamps_filed_by_agent():
    """A goal added by agent alpha must land with filed_by_agent='alpha'."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, _agent_dir = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "title": "Goal from alpha",
                "description": "Should be attributed to alpha",
                "priority": "MEDIUM",
                "status": "pending",
                "blocked_by": [],
                "verification": {"outcomes": ["x"], "checks": [],
                                 "preconditions": []},
                "origin_signal": "user_directive",
                "participants": ["agent"],
            }
            status, out = _add_goal(df.port, body, agent="alpha")
            assert status == 200, f"add-goal status={status}; body={out!r}"
            g = _find_goal_by_title(world, "Goal from alpha")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            assert g.get("filed_by_agent") == "alpha", (
                "filed_by_agent must be stamped with the requesting agent; "
                f"got {g.get('filed_by_agent')!r} for goal {g.get('id')!r}")


def test_add_goal_preserves_explicit_filed_by_agent():
    """setdefault semantics: an explicit filed_by_agent in the body is kept."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, _agent_dir = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "title": "Filed on behalf of bravo",
                "description": "Explicit attribution preserved",
                "priority": "MEDIUM",
                "status": "pending",
                "blocked_by": [],
                "filed_by_agent": "bravo",
                "verification": {"outcomes": ["x"], "checks": [],
                                 "preconditions": []},
                "origin_signal": "user_directive",
                "participants": ["agent"],
            }
            # Request agent is alpha, but the body names bravo as the filer.
            status, out = _add_goal(df.port, body, agent="alpha")
            assert status == 200, f"add-goal status={status}; body={out!r}"
            g = _find_goal_by_title(world, "Filed on behalf of bravo")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            assert g.get("filed_by_agent") == "bravo", (
                "explicit filed_by_agent must be preserved (setdefault); "
                f"got {g.get('filed_by_agent')!r} for goal {g.get('id')!r}")


def test_validate_goal_filed_by_agent_type():
    """Canonical validator: null/string accepted, non-string rejected, absent OK."""
    base = {
        "id": "g-100-02",
        "title": "v",
        "status": "pending",
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
    }

    # Absent field — backward-compat, must not raise.
    aspirations.validate_goal(dict(base))

    # null — accepted.
    aspirations.validate_goal({**base, "filed_by_agent": None})

    # string — accepted.
    aspirations.validate_goal({**base, "filed_by_agent": "delta"})

    # non-string — rejected.
    bad = {**base, "filed_by_agent": 123}
    raised = False
    try:
        aspirations.validate_goal(bad)
    except ValueError as e:
        raised = True
        assert "filed_by_agent" in str(e), f"wrong error: {e}"
    assert raised, "validate_goal must reject a non-string filed_by_agent"


if __name__ == "__main__":
    test_add_goal_stamps_filed_by_agent()
    test_add_goal_preserves_explicit_filed_by_agent()
    test_validate_goal_filed_by_agent_type()
    print("ok")
