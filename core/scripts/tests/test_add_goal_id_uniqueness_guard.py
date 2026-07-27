"""test_add_goal_id_uniqueness_guard.py — regression for .

The single-goal add_goal endpoint must NEVER let a caller-supplied `id` collide
with an existing goal in the same aspiration — INCLUDING a completed one. That
collision is the cross-Mind-promotion-injection corruption class that produced
two distinct goals both id g-115-1539 in asp-115: every id-based op
(claim / update / complete-by) then targets the FIRST match and silently
corrupts the wrong record.

Three contracts pinned here:
  1. A caller-supplied id that collides with an existing goal is reassigned to a
     fresh unique id (via _allocate_goal_id = max-seq+1); the existing record is
     left untouched (exactly one goal keeps the original id).
  2. A caller-supplied id that does NOT collide is preserved as-is.
  3. A goal with no id still auto-allocates (existing behavior, regression guard).

Pattern: DaemonFixture + direct HTTP POST (bash-free, hits the patched endpoint
via the fixture's in-process daemon). Mirrors test_add_goal_filed_by_agent_stamp.py.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402

_GID_RE = re.compile(r"^g-100-\d{2,4}$")


def _make_world(tmp: Path) -> tuple[Path, Path]:
    """Tempdir world with asp-100 holding one existing goal ."""
    world = tmp / "world"
    world.mkdir()

    seed = {
        "id": "g-100-01",
        "title": "Seed goal",
        "description": "Pre-existing goal",
        "status": "completed",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    asp = {
        "id": "asp-100",
        "title": "id-uniqueness guard regression",
        "motivation": "Test add_goal id uniqueness guard",
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


def _all_goals(world: Path) -> list[dict]:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    out: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        out.extend(asp.get("goals", []))
    return out


def _find_by_title(world: Path, title: str) -> dict | None:
    for g in _all_goals(world):
        if g.get("title") == title:
            return g
    return None


def test_colliding_caller_id_is_reassigned():
    """A caller-supplied id colliding with an existing (completed) goal must be
    reassigned to a fresh unique id; the original record stays untouched."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, _agent_dir = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "id": "",  # collides with the seeded completed goal
                "title": "Collided promote goal",
                "description": "Cross-Mind promotion reused an existing id",
                "priority": "HIGH",
                "status": "pending",
                "blocked_by": [],
                "verification": {"outcomes": ["x"], "checks": [],
                                 "preconditions": []},
                "origin_signal": "investigate:recovery-gate-promotion",
                "participants": ["agent"],
            }
            status, out = _add_goal(df.port, body, agent="alpha")
            assert status == 200, f"add-goal status={status}; body={out!r}"

            g = _find_by_title(world, "Collided promote goal")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            assert g.get("id") != "g-100-01", (
                "colliding caller id must be reassigned, not honored; "
                f"got id={g.get('id')!r}")
            assert _GID_RE.match(g.get("id") or ""), (
                f"reassigned id must match g-100-NN; got {g.get('id')!r}")

            # The original completed seed must be intact and remain the SOLE
            # holder of .
            holders = [x for x in _all_goals(world) if x.get("id") == "g-100-01"]
            assert len(holders) == 1, (
                f"exactly one goal must hold g-100-01 after the guard; "
                f"got {len(holders)}")
            assert holders[0].get("title") == "Seed goal", (
                "the original completed record must be untouched; "
                f"got title={holders[0].get('title')!r}")
            assert holders[0].get("status") == "completed", (
                "the original completed record must not be corrupted")


def test_noncolliding_caller_id_is_preserved():
    """A caller-supplied id that does not collide is kept verbatim."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, _agent_dir = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "id": "",  # fresh, no collision
                "title": "Fresh-id goal",
                "description": "Caller-supplied non-colliding id",
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
            g = _find_by_title(world, "Fresh-id goal")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            assert g.get("id") == "g-100-50", (
                "a non-colliding caller-supplied id must be preserved; "
                f"got {g.get('id')!r}")


def test_missing_id_is_auto_allocated():
    """A goal with no id still auto-allocates (existing behavior preserved)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, _agent_dir = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "title": "No-id goal",
                "description": "No caller-supplied id",
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
            g = _find_by_title(world, "No-id goal")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            assert _GID_RE.match(g.get("id") or ""), (
                f"auto-allocated id must match g-100-NN; got {g.get('id')!r}")
            assert g.get("id") != "g-100-01", (
                "auto-allocated id must not collide with the existing goal")


if __name__ == "__main__":
    test_colliding_caller_id_is_reassigned()
    test_noncolliding_caller_id_is_preserved()
    test_missing_id_is_auto_allocated()
    print("ok")
