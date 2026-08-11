"""test_completed_date_stamp.py -- regression for the update-goal completed_date
stamping gap (g-115-5069).

Bug shape: the goal-completion write-path (aspirations.py cmd_update_goal and its
daemon mirror mind_api update_goal) stamped completed_at on terminal status but
NOT completed_date, while the sibling achieve/recurring path stamped BOTH. So the
field recorded WHICH CLOSE PATH RAN, not whether the goal completed -- a goal
closed via `aspirations-update-goal.sh <id> status completed` carried
completed_at/completed_by/completed_by_sid and no completed_date.

Why it mattered: completed_date is the filter field for every windowed
measurement of goal flow (directive-lane compliance shares, fresh-eyes Phase 2.2b
close counts, fleet throughput arithmetic). A close without the field is
invisible to all of them. Measured 2026-08-10 across world+agent: 616 of 4346
completed goals (14.2%) carried no completed_date, and the gap was still
accruing (471 on 08-05 -> 556 on 08-08 -> 616 on 08-10). The loss is not
proportional between numerator and denominator, so the error is a bias, not
noise.

Why NOT "just read completed_at instead": completed_at reads 100% present only
because the terminal-goal normalizer backfills it with datetime.now() when it is
absent. 158 of those 616 goals carry the single identical second
2026-08-08T01:05:39 across 7 different aspirations -- a normalizer artifact, a
median 8 days (max 21) away from the goal's own last_modified. Swapping
consumers to completed_at would convert a VISIBLE gap (null -> excluded ->
countable) into an INVISIBLE error (a confident wrong timestamp landing in the
wrong window). Fix the stamp instead.

Fix: the completion chokepoint (field==status, value==completed) now stamps
completed_date when unset, in BOTH the CLI cmd_update_goal and the daemon
update_goal mirror (guard-2323 / guard-547: daemon-only makes a CLI-only fix
inert). Scoped to value==completed (a skipped/expired goal has no completion
date) and idempotent (a back-stamp from complete-by / backfill is preserved).

DATE-shaped, deliberately: the canonical iteration-close path writes $TODAY and
95% of the live store (3557/3743) is date-only. The 5% datetime-shaped minority
already breaks a date-string comparison; test_completed_date_is_date_shaped
exists so a future "make it a full timestamp" change cannot silently grow it.

Pattern: DaemonFixture + direct HTTP POST to the update-goal endpoint (bash-free,
exercises the LIVE daemon path) -- mirrors test_completed_by_stamp.py, the
sibling cascade this one sits beside in both files.
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

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI_FILE = PROJECT_ROOT / "core" / "scripts" / "aspirations.py"
DAEMON_FILE = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "aspirations_write.py"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

from _daemon_fixture import DaemonFixture  # noqa: E402


def _make_world(tmp: Path) -> Path:
    """Tempdir world with asp-100:  (no completed_date) + 
    (completed_date preset, for the idempotency test)."""
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
        "id": "g-100-02", "title": "Pre-dated goal",
        "description": "Already carries completed_date from an explicit complete-by",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "completed_date": "2026-01-15",
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
    }
    asp = {
        "id": "asp-100", "title": "completed_date stamp regression",
        "motivation": "Test update-goal completed_date parity", "scope": "project",
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


def test_completion_stamps_completed_date():
    """status->completed via update-goal stamps completed_date (the  gap)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-01", "status", "completed", "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g is not None and g.get("status") == "completed", (
                f"goal not completed; resp={out!r}")
            assert g.get("completed_date"), (
                "completed_date must be stamped on the completion transition -- without "
                "it the close is invisible to every window-filtered lane/compliance "
                f"measurement (g-115-5069); got {g.get('completed_date')!r}")


def test_completed_date_is_date_shaped():
    """The stamp is YYYY-MM-DD, not a full ISO datetime.

    95% of the live store (3557/3743) is date-only and the canonical
    iteration-close path writes $TODAY. The 5% datetime-shaped minority already
    breaks consumers that compare date strings -- an inclusive same-day upper
    bound (`ts <= "2026-08-10"`) excludes "2026-08-10T06:00:00" lexicographically.
    This guard stops that minority from growing through this write path.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-01", "status", "completed", "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g is not None
            cd = str(g.get("completed_date"))
            assert DATE_RE.match(cd), (
                "completed_date must be date-shaped YYYY-MM-DD (not a full ISO "
                f"datetime) to stay comparable with the 95% date-only majority; got {cd!r}")


def test_completion_preserves_existing_completed_date():
    """Idempotent: an existing completed_date (e.g. from complete-by) is NOT overwritten."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-02", "status", "completed", "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-02")
            assert g is not None and g.get("status") == "completed"
            assert g.get("completed_date") == "2026-01-15", (
                "pre-existing completed_date must be preserved (idempotent), not "
                f"overwritten with today; got {g.get('completed_date')!r}")


def test_skipped_does_not_stamp_completed_date():
    """Scope: completed_date stamps only on value==completed, not other terminal statuses.

    A skipped or expired goal has no completion date; stamping one would inject
    phantom closes into exactly the windowed measurements this fix exists to
    make trustworthy.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-01", "status", "skipped", "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g is not None and g.get("status") == "skipped"
            assert not g.get("completed_date"), (
                "completed_date must NOT be stamped on a non-completed terminal status; "
                f"got {g.get('completed_date')!r}")


def test_cli_daemon_completed_date_parity():
    """Both write-path implementations carry the  completion stamp.

    guard-2323 / guard-547: under daemon-only architecture the daemon is the ONLY
    live path, so a CLI-side-only fix is inert from the moment it lands -- and
    mind_api/tests sits in DEFERRED_TESTPATHS, so a green run-full-suite is not
    evidence of parity. This guard lives in core/scripts/tests (which DOES run in
    the normal closure suite) precisely so the parity check cannot be skipped
    along with the daemon suite.
    """
    assert CLI_FILE.is_file(), f"CLI aspirations missing: {CLI_FILE}"
    assert DAEMON_FILE.is_file(), f"daemon aspirations_write missing: {DAEMON_FILE}"
    cli = CLI_FILE.read_text(encoding="utf-8")
    daemon = DAEMON_FILE.read_text(encoding="utf-8")
    # the  completion-stamp marker present on both sides
    assert "g-115-5069" in cli, "CLI lost the g-115-5069 completed_date stamp"
    assert "g-115-5069" in daemon, "daemon lost the g-115-5069 completed_date stamp"
    # the date-shaped assignment present on both sides
    stamp = 'goal["completed_date"] = datetime.now().strftime("%Y-%m-%d")'
    assert stamp in cli, f"CLI missing the date-shaped stamp: {stamp}"
    assert stamp in daemon, f"daemon missing the date-shaped stamp: {stamp}"
    # scoped to value==completed on both sides (not all terminal statuses)
    assert 'value == "completed"' in cli
    assert 'value == "completed"' in daemon


if __name__ == "__main__":
    test_completion_stamps_completed_date()
    test_completed_date_is_date_shaped()
    test_completion_preserves_existing_completed_date()
    test_skipped_does_not_stamp_completed_date()
    test_cli_daemon_completed_date_parity()
    print("ok")
