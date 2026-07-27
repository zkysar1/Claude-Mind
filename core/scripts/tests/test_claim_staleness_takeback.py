"""test_claim_staleness_takeback.py -- regression for the claim() staleness
take-back gap (g-115-1841 part i).

Bug shape: the daemon claim endpoint
(mind_api/src/endpoints/aspirations_write.py::claim) hard-409'd `already_claimed`
for ANY goal whose claimed_by was another agent, with NO staleness take-back.
That VIOLATED goal-selector.py's claim-VISIBILITY contract (L1415-1428): the
selector makes a stale-claimed world goal visible again once claim_age exceeds
claim_timeout_hours, so the running agent is OFFERED the goal but then CANNOT
claim it -> the world queue LIVELOCKS on an abandoned claim.

Fix: claim() now mirrors the selector. A claim is stale when
claim_age > effective_timeout, where effective_timeout = claim_timeout_hours
(default 4) capped at 2x interval_hours for recurring goals. A stale claim is
taken back (claimed_by overwritten) and the steal is audited to
override-bypass-ledger.jsonl with gate=claim-staleness-takeback. Selector-parity
edge cases: claim_timeout_hours is None -> never take back; claimed_at
missing/unparseable (claim_age None) -> treat as expired and take back.

Daemon-only: aspirations-claim.sh is daemon-only (no-python-cli-fallback), so
the daemon endpoint IS the production claim path; there is no CLI mirror to keep
byte-parallel here.

Pattern: DaemonFixture + direct HTTP POST to the claim endpoint (bash-free,
exercises the LIVE daemon path) -- mirrors test_completed_by_stamp.py.

Run: py -3 -m pytest core/scripts/tests/test_claim_staleness_takeback.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402


def _ago(hours: float) -> str:
    """Local ISO-8601 timestamp `hours` in the past (matches claimed_at format)."""
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _make_world(tmp: Path, *, claimed_by=None, claimed_at=None,
                recurring=False, interval_hours=None, started=None) -> Path:
    """Tempdir world with asp-200:  claimed per the args.

    The bound (claiming) agent is alpha; the prior claimer is bravo. Only the
    world queue carries the goal (no agent-queue collision)."""
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": "g-200-01", "title": "Claimable goal",
        "description": "Exercises the claim staleness take-back path",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
    }
    if claimed_by is not None:
        goal["claimed_by"] = claimed_by
    if claimed_at is not None:
        goal["claimed_at"] = claimed_at
    if recurring:
        goal["recurring"] = True
    if interval_hours is not None:
        goal["interval_hours"] = interval_hours
    if started is not None:
        goal["started"] = started
    asp = {
        "id": "asp-200", "title": "claim staleness take-back regression",
        "motivation": "Test claim() take-back parity with goal-selector",
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


def _claim(port: int, goal_id: str, agent: str) -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/claim"
           f"?id={goal_id}&agent={agent}")
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


def _read_ledger(world: Path) -> list:
    p = world / "override-bypass-ledger.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()
            if x.strip()]


def test_fresh_claim_by_other_agent_409s():
    """A FRESH claim by another agent is protected: alpha gets 409, no take-back."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="bravo", claimed_at=_ago(0.5))
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 409, f"expected 409 for fresh claim; got {status}; {out!r}"
            g = _find_goal(world, "g-200-01")
            assert g.get("claimed_by") == "bravo", (
                f"fresh claim must NOT be stolen; claimed_by={g.get('claimed_by')!r}")
            assert _read_ledger(world) == [], "no ledger record for a refused claim"


def test_stale_claim_taken_back():
    """A STALE claim (age > default 4h timeout) is taken back by alpha (200)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="bravo", claimed_at=_ago(10))
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 200, f"expected take-back 200; got {status}; {out!r}"
            g = _find_goal(world, "g-200-01")
            assert g.get("claimed_by") == "alpha", (
                f"stale claim must be taken back; claimed_by={g.get('claimed_by')!r}")
            recs = _read_ledger(world)
            assert len(recs) == 1, f"expected 1 take-back ledger record; got {len(recs)}"
            assert recs[0].get("gate") == "claim-staleness-takeback"


def test_missing_claimed_at_taken_back():
    """Selector-parity: claimed_by set but claimed_at MISSING (age unknown) ->
    treated as expired, taken back (mirrors selector fall-through-to-include)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="bravo", claimed_at=None)
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 200, f"expected take-back 200; got {status}; {out!r}"
            g = _find_goal(world, "g-200-01")
            assert g.get("claimed_by") == "alpha"
            recs = _read_ledger(world)
            assert len(recs) == 1 and recs[0]["context"].get("claim_age_hours") is None


def test_recurring_2x_interval_cap_takes_back_earlier():
    """Recurring interval=1h -> effective_timeout=min(4, 2*1)=2h. A claim aged 3h
    is STALE under the cap even though it would be fresh under the 4h default."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="bravo", claimed_at=_ago(3),
                            recurring=True, interval_hours=1)
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 200, f"expected cap take-back 200; got {status}; {out!r}"
            g = _find_goal(world, "g-200-01")
            assert g.get("claimed_by") == "alpha"
            recs = _read_ledger(world)
            assert len(recs) == 1
            assert recs[0]["context"].get("effective_timeout_hours") == 2.0, (
                "effective_timeout must be capped at 2x interval (2h), got "
                f"{recs[0]['context'].get('effective_timeout_hours')!r}")


def test_recurring_within_cap_still_409s():
    """Recurring interval=1h, claim aged 1.5h (< 2h cap) -> still valid, 409."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="bravo", claimed_at=_ago(1.5),
                            recurring=True, interval_hours=1)
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 409, f"expected 409 within cap; got {status}; {out!r}"
            g = _find_goal(world, "g-200-01")
            assert g.get("claimed_by") == "bravo"
            assert _read_ledger(world) == []


def test_own_reclaim_no_ledger():
    """Idempotent re-claim by the SAME agent is not a take-back: 200, no ledger."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha", claimed_at=_ago(0.1))
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 200, f"expected own-reclaim 200; got {status}; {out!r}"
            g = _find_goal(world, "g-200-01")
            assert g.get("claimed_by") == "alpha"
            assert _read_ledger(world) == [], "own-agent reclaim must not audit a take-back"


def test_takeback_ledger_context_shape():
    """The take-back ledger record carries the debug context: goal_id,
    agent_claiming, prior_claimer, claim_age_hours, effective_timeout_hours."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="bravo", claimed_at=_ago(10))
        with DaemonFixture(world, agent="alpha") as df:
            status, _ = _claim(df.port, "g-200-01", "alpha")
            assert status == 200
            recs = _read_ledger(world)
            assert len(recs) == 1
            ctx = recs[0].get("context", {})
            assert ctx.get("goal_id") == "g-200-01"
            assert ctx.get("agent_claiming") == "alpha"
            assert ctx.get("prior_claimer") == "bravo"
            assert isinstance(ctx.get("claim_age_hours"), (int, float))
            assert ctx.get("effective_timeout_hours") == 4.0


# --- `started` attempt-marker at claim (-t / ) ---------
# The daemon claim chokepoint stamps `started` via setdefault(), so it is the
# FIRST-attempt time: set once when a never-attempted goal is claimed, and
# preserved across every later re-claim or stale take-back. cmd_cycles reads
# `started` to tell a genuine attempted-then-failed skip from a withdrawn
# (never-claimed, dedup) skip; clobbering it would corrupt that signal. These
# lock the setdefault (not assignment) choice against regression.


def test_claim_stamps_started_when_absent():
    """No `started` + claimed -> started == claimed_at (attempt marker stamped)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))  # unclaimed, no started
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 200, f"expected fresh claim 200; got {status}; {out!r}"
            g = _find_goal(world, "g-200-01")
            assert g.get("claimed_by") == "alpha"
            assert g.get("started") is not None, "claim must stamp the attempt marker"
            assert g.get("started") == g.get("claimed_at"), (
                f"started must equal the claim timestamp on first claim; "
                f"started={g.get('started')!r} claimed_at={g.get('claimed_at')!r}")


def test_stale_takeback_preserves_started():
    """Has `started` + STALE take-back -> started UNCHANGED (first-attempt
    semantics). This is the load-bearing setdefault-vs-assignment lock: an
    assignment would reset the attempt time to the take-back moment."""
    original_started = _ago(24)  # attempted a day before the take-back
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="bravo", claimed_at=_ago(10),
                            started=original_started)
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 200, f"expected take-back 200; got {status}; {out!r}"
            g = _find_goal(world, "g-200-01")
            assert g.get("claimed_by") == "alpha", "stale claim must be taken back"
            assert g.get("started") == original_started, (
                f"take-back must PRESERVE the original attempt marker; "
                f"started={g.get('started')!r} expected={original_started!r}")
            # claimed_at IS overwritten (assignment), started is NOT (setdefault):
            # proves the two fields carry deliberately different write semantics.
            assert g.get("claimed_at") != original_started


def test_own_reclaim_preserves_started():
    """Has `started` + idempotent same-agent re-claim -> started UNCHANGED
    (setdefault no-ops when present; survives an autocompact-driven re-claim)."""
    original_started = _ago(2)
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha", claimed_at=_ago(0.1),
                            started=original_started)
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 200, f"expected own-reclaim 200; got {status}; {out!r}"
            g = _find_goal(world, "g-200-01")
            assert g.get("started") == original_started, (
                f"own re-claim must preserve started; got {g.get('started')!r}")


def test_reclaim_after_release_preserves_started():
    """Has `started` + claimed_by CLEARED (released) + re-claim -> started
    UNCHANGED. g-115-2192 criterion (2) verbatim: `started` is the FIRST-attempt
    marker and must survive the release -> re-claim lifecycle (a goal released by
    aspirations-release.sh, then re-selected + claimed). release does not touch
    started (only claim's setdefault does), so the re-claim preserves the existing
    value. Distinct seed from test_own_reclaim_preserves_started (claimed_by
    present): here claimed_by is None — the exact RELEASED state."""
    original_started = _ago(6)  # attempted 6h ago, then released back to pending
    with tempfile.TemporaryDirectory() as tmpd:
        # Released state: no claimer, but the first-attempt marker survives release.
        world = _make_world(Path(tmpd), claimed_by=None, claimed_at=None,
                            started=original_started)
        with DaemonFixture(world, agent="alpha") as df:
            status, out = _claim(df.port, "g-200-01", "alpha")
            assert status == 200, f"expected re-claim 200; got {status}; {out!r}"
            g = _find_goal(world, "g-200-01")
            assert g.get("claimed_by") == "alpha", "released goal must be claimable"
            assert g.get("started") == original_started, (
                f"re-claim after release must PRESERVE the first-attempt marker; "
                f"started={g.get('started')!r} expected={original_started!r}")
            # A fresh claimed_at is stamped (new claim); started is the ORIGINAL.
            assert g.get("claimed_at") != original_started


if __name__ == "__main__":
    test_fresh_claim_by_other_agent_409s()
    test_stale_claim_taken_back()
    test_missing_claimed_at_taken_back()
    test_recurring_2x_interval_cap_takes_back_earlier()
    test_recurring_within_cap_still_409s()
    test_own_reclaim_no_ledger()
    test_takeback_ledger_context_shape()
    test_claim_stamps_started_when_absent()
    test_stale_takeback_preserves_started()
    test_own_reclaim_preserves_started()
    test_reclaim_after_release_preserves_started()
    print("ok")
