"""test_add_goal_blocked_since_stamp.py — regression for the single-goal
add_goal endpoint blocked_since stamping gap.

Bug shape: aspirations_write.add_goal() (single-goal add — what
aspirations-add-goal.sh POSTs to) applied status/created_at defaults but did
NOT stamp blocked_since when the payload carried blocked_by. The
full-aspiration add() endpoint and cmd_update_goal both stamp it. A goal added
with inline blocked_by therefore landed with blocked_since=None, which
goal-selector's dependency-timeout check reads as an EXPIRED dependency
(fail-open) → the genuinely-blocked goal leaks into the executable ranked list.

Observed via an HTN decomposition chain: children added with inline blocked_by
(e.g. g-115-1339/1340 blocked_by pending siblings) surfaced as top-ranked
executable candidates while their prerequisites had not run. Siblings whose
blocked_by was set via aspirations-update-goal.sh (cmd_update_goal cascade) WERE
stamped and correctly blocked — the asymmetry pinpointed the add path.

Fix: add_goal() now stamps blocked_since (parity with add() / cmd_update_goal)
when blocked_by is non-empty and blocked_since is unset.

Pattern: DaemonFixture + direct HTTP POST to the add-goal endpoint (bash-free,
hits the patched endpoint via the fixture's in-process daemon).
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


def _make_world(tmp: Path) -> tuple[Path, Path]:
    """Tempdir world with asp-100 holding one existing goal ."""
    world = tmp / "world"
    world.mkdir()

    seed = {
        "id": "g-100-01",
        "title": "Seed goal",
        "description": "Pre-existing goal to depend on",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    asp = {
        "id": "asp-100",
        "title": "blocked_since stamp regression",
        "motivation": "Test add_goal blocked_since parity",
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


def _add_goal(port: int, body: dict) -> tuple[int, str]:
    """POST a single goal under asp-100 to the daemon add-goal endpoint."""
    url = (f"http://127.0.0.1:{port}/v1/aspirations/add-goal"
           "?asp_id=asp-100&source=world")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", "alpha")
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _find_goal_by_blocked_by(world: Path, blocked_by: list) -> dict | None:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") != "g-100-01" and g.get("blocked_by") == blocked_by:
                return g
    return None


def test_add_goal_with_blocked_by_stamps_blocked_since():
    """A goal added WITH inline blocked_by must land with blocked_since set."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, _agent_dir = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "title": "Dependent goal",
                "description": "Added with inline blocked_by",
                "priority": "MEDIUM",
                "status": "pending",
                "blocked_by": ["g-100-01"],
                "verification": {"outcomes": ["x"], "checks": [],
                                 "preconditions": []},
                "origin_signal": "decomposition:g-100-01",
                "participants": ["agent"],
            }
            status, out = _add_goal(df.port, body)
            assert status == 200, f"add-goal status={status}; body={out!r}"
            g = _find_goal_by_blocked_by(world, ["g-100-01"])
            assert g is not None, f"dependent goal not found on disk; resp={out!r}"
            assert g.get("blocked_since"), (
                "blocked_since must be stamped when blocked_by is present on add; "
                f"got blocked_since={g.get('blocked_since')!r} for goal {g.get('id')!r}")


def test_add_goal_without_blocked_by_leaves_blocked_since_unset():
    """Negative control: no blocked_by → blocked_since stays unset (no spurious stamp)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, _agent_dir = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "title": "Independent goal",
                "description": "No dependencies",
                "priority": "MEDIUM",
                "status": "pending",
                "blocked_by": [],
                "verification": {"outcomes": ["x"], "checks": [],
                                 "preconditions": []},
                "origin_signal": "user_directive",
                "participants": ["agent"],
            }
            status, out = _add_goal(df.port, body)
            assert status == 200, f"add-goal status={status}; body={out!r}"
            g = _find_goal_by_blocked_by(world, [])
            assert g is not None, f"independent goal not found on disk; resp={out!r}"
            assert not g.get("blocked_since"), (
                "blocked_since must remain unset when blocked_by is empty; "
                f"got {g.get('blocked_since')!r} for goal {g.get('id')!r}")


if __name__ == "__main__":
    test_add_goal_with_blocked_by_stamps_blocked_since()
    test_add_goal_without_blocked_by_leaves_blocked_since_unset()
    print("ok")
