"""test_completed_by_stamp.py -- regression for the update-goal completed_by
stamping gap (g-115-1562).

Bug shape: the goal-completion write-path (aspirations.py cmd_update_goal and
its daemon mirror mind_api update_goal) stamped completed_at on terminal status
but NOT completed_by. Only goals closed via the explicit complete_by path
carried completed_by, so only ~11% (174/1609) of completed world goals had it --
agent-attribution audits and the cross_queue graduation count (g-115-1560) both
undercounted real output.

Fix: the completion chokepoint (field==status, value==completed) now stamps
completed_by=<executing agent> when unset, in BOTH the CLI cmd_update_goal and
the daemon update_goal mirror (guard-742 byte-parallel discipline). Scoped to
value==completed (attribution = who completed it) and idempotent (an existing
completed_by from complete-by / backfill is preserved, never overwritten).

Pattern: DaemonFixture + direct HTTP POST to the update-goal endpoint (bash-free,
exercises the LIVE daemon path) -- mirrors test_add_goal_blocked_since_stamp.py.
The behavioral daemon coverage here closes the daemon-mirror gap flagged in the
g-115-1563 fresh-eyes spark (static parity guards confirm presence, not behavior).
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI_FILE = PROJECT_ROOT / "core" / "scripts" / "aspirations.py"
DAEMON_FILE = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "aspirations_write.py"

from _daemon_fixture import DaemonFixture  # noqa: E402


def _make_world(tmp: Path) -> Path:
    """Tempdir world with asp-100:  (no completed_by) + 
    (completed_by preset to a different agent, for the idempotency test)."""
    world = tmp / "world"
    world.mkdir()
    g1 = {
        "id": "g-100-01", "title": "Completable goal",
        "description": "Closed via the update-goal completion chokepoint",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
    }
    g2 = {
        "id": "g-100-02", "title": "Pre-attributed goal",
        "description": "Already carries completed_by from an explicit complete-by",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "completed_by": "alpha",
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
    }
    asp = {
        "id": "asp-100", "title": "completed_by stamp regression",
        "motivation": "Test update-goal completed_by parity", "scope": "project",
        "priority": "MEDIUM", "status": "active",
        "created": "2026-05-01T00:00:00", "goals": [g1, g2],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "delta"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _update_goal(port: int, goal_id: str, field: str, value, agent: str) -> tuple[int, str]:
    """POST an update-goal to the daemon. value is sent as a JSON body value."""
    url = (f"http://127.0.0.1:{port}/v1/aspirations/update-goal"
           f"?id={goal_id}&field={field}&source=world")
    req = urllib.request.Request(
        url, data=json.dumps(value).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", agent)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _find_goal(world: Path, goal_id: str) -> dict | None:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                return g
    return None


def test_completion_stamps_completed_by():
    """status->completed via update-goal stamps completed_by = executing agent."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-01", "status", "completed", "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g is not None and g.get("status") == "completed", f"goal not completed; resp={out!r}"
            assert g.get("completed_by") == "delta", (
                "completed_by must be stamped with the executing agent on completion; "
                f"got {g.get('completed_by')!r}")


def test_completion_preserves_existing_completed_by():
    """Idempotent: an existing completed_by (e.g. from complete-by) is NOT overwritten."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-02", "status", "completed", "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-02")
            assert g is not None and g.get("status") == "completed"
            assert g.get("completed_by") == "alpha", (
                "pre-existing completed_by must be preserved (idempotent), not overwritten; "
                f"got {g.get('completed_by')!r}")


def test_skipped_does_not_stamp_completed_by():
    """Scope: completed_by stamps only on value==completed, not other terminal statuses."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-01", "status", "skipped", "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g is not None and g.get("status") == "skipped"
            assert not g.get("completed_by"), (
                "completed_by must NOT be stamped on a non-completed terminal status; "
                f"got {g.get('completed_by')!r}")


def _has_scoped_completed_by_stamp(src: str) -> bool:
    """True when an `if` naming value == "completed" and the completed_by guard assigns goal["completed_by"].

    Read from the AST, not from text. The seed plant scrubs "(g-NNN-NN)" from source
    comments, so a goal-id marker grep passes here and fails at every plant. A bare
    `goal["completed_by"] =` match is also satisfied by the unrelated assignment
    further down the daemon file, so the assignment must sit under that `if`.
    """
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "value == 'completed'" not in test or "completed_by" not in test:
            continue
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Assign) and any(
                        isinstance(t, ast.Subscript) and ast.unparse(t) == "goal['completed_by']"
                        for t in sub.targets):
                    return True
    return False


def test_cli_daemon_completed_by_parity():
    """Both write-path implementations carry the completion completed_by stamp (guard-742).

    The CLI (core/scripts/aspirations.py cmd_update_goal) and the daemon mirror
    (mind_api/src/endpoints/aspirations_write.py update_goal) were patched as
    byte-parallel copies. A fix to only one side is half a fix (guard-742). This
    guard fails if either side loses the completion stamp, its value==completed
    scoping, or the completed_by assignment.
    """
    assert CLI_FILE.is_file(), f"CLI aspirations missing: {CLI_FILE}"
    assert DAEMON_FILE.is_file(), f"daemon aspirations_write missing: {DAEMON_FILE}"
    cli = CLI_FILE.read_text(encoding="utf-8")
    daemon = DAEMON_FILE.read_text(encoding="utf-8")
    # one structural check per side: the stamp, its value==completed scoping (not all
    # terminal statuses) and the completed_by assignment are a single `if` + assignment
    assert _has_scoped_completed_by_stamp(cli), "CLI lost the completion completed_by stamp"
    assert _has_scoped_completed_by_stamp(daemon), "daemon lost the completion completed_by stamp"


if __name__ == "__main__":
    test_completion_stamps_completed_by()
    test_completion_preserves_existing_completed_by()
    test_skipped_does_not_stamp_completed_by()
    test_cli_daemon_completed_by_parity()
    print("ok")
