"""test_claim_terminal_status.py -- regression for the claim() terminal-status
gap (g-115-4521).

Bug shape: the daemon claim endpoint
(mind_api/src/endpoints/aspirations_write.py::claim) stamped
claimed_by/claimed_at/claimed_by_sid onto goals whose status was ALREADY
terminal. The endpoint's own success payload returns `status`, so it was handing
back the very field it should have refused on -- a missing BRANCH, not missing
data.

Measured twice on 2026-08-01, on two boxes, with two different terminal statuses:
  - bravo (cc-05): claimed g-115-4519, status `skipped` 31s earlier by zeta,
    with a full outcome_note already written.
  - echo  (cc-03): claimed g-326-78, status `completed` 23s earlier by foxtrot.
Neither is a race: 23-31s is not a race window, and the local record was already
correct and simply not consulted. Not the selector's bug either -- a selector
snapshot is stale by construction, which is what makes the claim the right
chokepoint.

Cost: a wasted claim+release cycle, a claim briefly held on a partner-resolved
goal (visible to every agent reading team-state in_flight, readable as duplicated
work), and -- if execution had proceeded -- an outcome_note overwriting the
closer's (the g-335-94 clobber class).

TWO-WAY PROOF (guard-1220): a pending goal must STILL claim, and every terminal
status must refuse. A test that only asserts the refusal cannot tell a working
gate from a gate that refuses everything, so test_pending_goal_still_claims is
load-bearing, not a courtesy.

The terminal set is DERIVED from _goal_census.TERMINAL_STATUSES rather than
re-listed here (guard-1960): a hardcoded second list would drift in lockstep with
the thing it checks and pass forever. A future terminal status auto-enrolls.

Daemon-only: aspirations-claim.sh is daemon-only (no-python-cli-fallback), so the
daemon endpoint IS the production claim path; there is no CLI mirror to keep
byte-parallel here.

Pattern: DaemonFixture + direct HTTP POST to the claim endpoint (bash-free,
exercises the LIVE daemon path) -- mirrors test_claim_staleness_takeback.py.

Run: py -3 -m pytest core/scripts/tests/test_claim_terminal_status.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402
from _goal_census import TERMINAL_STATUSES  # noqa: E402


def _make_world(tmp: Path, *, status: str, completed_by=None,
                completed_at=None, outcome_note=None) -> Path:
    """Tempdir world with asp-200:  carrying `status`.

    The bound (claiming) agent is alpha; the closer is bravo. Only the world
    queue carries the goal (no agent-queue collision)."""
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": "g-200-01", "title": "Claimable goal",
        "description": "Exercises the claim terminal-status refusal path",
        "status": status, "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
    }
    if completed_by is not None:
        goal["completed_by"] = completed_by
    if completed_at is not None:
        goal["completed_at"] = completed_at
    if outcome_note is not None:
        goal["outcome_note"] = outcome_note
    asp = {
        "id": "asp-200", "title": "claim terminal-status regression",
        "motivation": "Test claim() refuses goals that are already closed",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [goal],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


# Claims MUST carry a sid: the endpoint refuses sid-less world-goal claims
# (-b), and production always sends one -- aspirations-claim.sh appends
# &sid=$MIND_SID, which bash-agent-inject.py injects into every Bash call. A
# sid-less test call was therefore already diverging from the production arg
# shape (guard-920). Terminal-status refusal is ordered BEFORE the sid check, so
# the terminal cases here assert the same thing either way; the sid matters for
# the pending-goal positive control.
CLAIMER_SID = "55555555-aaaa-bbbb-cccc-555555555555"


def _claim(port: int, goal_id: str, agent: str) -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/claim"
           f"?id={goal_id}&agent={agent}&sid={CLAIMER_SID}")
    req = urllib.request.Request(url, data=b"", method="POST")
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


def test_pending_goal_still_claims():
    """POSITIVE CONTROL: the gate must not refuse everything.

    Without this, a gate that 409s unconditionally would pass every other test
    in this file (guard-1220)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), status="pending")
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 200, (
                f"a PENDING goal must still be claimable; got {status}; {out!r}")
            g = _find_goal(world, "g-200-01")
            assert g.get("claimed_by") == "alpha", (
                f"pending claim must stamp claimed_by; got {g.get('claimed_by')!r}")


@pytest.mark.parametrize("terminal_status", sorted(TERMINAL_STATUSES))
def test_every_terminal_status_refused(terminal_status):
    """EVERY member of the canonical terminal set is refused with 409 goal_terminal.

    Parametrized off _goal_census.TERMINAL_STATUSES so a future terminal status
    auto-enrolls instead of silently going unguarded (guard-1960)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), status=terminal_status,
                            completed_by="bravo",
                            completed_at="2026-08-01T22:40:28")
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 409, (
                f"status={terminal_status!r} must be refused; got {status}; {out!r}")
            assert "goal_terminal" in out, (
                f"expected goal_terminal error code; got {out!r}")


@pytest.mark.parametrize("terminal_status", sorted(TERMINAL_STATUSES))
def test_refusal_does_not_mutate_the_record(terminal_status):
    """A refused claim leaves the closed record byte-identical.

    The original defect's real cost was the WRITE, not the non-zero exit: it
    stamped a claimant onto a record another agent had closed."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), status=terminal_status,
                            completed_by="bravo",
                            completed_at="2026-08-01T22:40:28",
                            outcome_note="closed by bravo; do not clobber")
        before = (world / "aspirations.jsonl").read_text(encoding="utf-8")
        with DaemonFixture(world, agent="alpha") as df:
            _claim(df.port, "g-200-01", "alpha")
            g = _find_goal(world, "g-200-01")
            assert g.get("claimed_by") is None, (
                f"refused claim must not stamp claimed_by; "
                f"got {g.get('claimed_by')!r}")
            assert g.get("claimed_at") is None, (
                f"refused claim must not stamp claimed_at; "
                f"got {g.get('claimed_at')!r}")
            assert g.get("outcome_note") == "closed by bravo; do not clobber", (
                "refused claim must not disturb the closer's outcome_note")
            assert (world / "aspirations.jsonl").read_text(encoding="utf-8") == before, (
                "a refused claim must leave the store byte-identical")


def test_error_body_names_the_closer():
    """The refusal carries WHY, so the caller can journal without a second read.

    The goal spec asks for status + outcome_note in the error body specifically
    so a rebounding caller does not have to re-read the store to explain itself."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), status="completed",
                            completed_by="foxtrot",
                            completed_at="2026-08-01T23:38:13",
                            outcome_note="validated against a real measurement")
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 409
            assert "completed" in out, f"error body must name the status; {out!r}"
            assert "foxtrot" in out, f"error body must name completed_by; {out!r}"
            assert "validated against a real measurement" in out, (
                f"error body must carry outcome_note; {out!r}")
