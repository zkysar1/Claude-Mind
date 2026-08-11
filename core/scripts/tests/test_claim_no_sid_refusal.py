"""test_claim_no_sid_refusal.py -- sid-less world-goal claims are refused,
with an audited escape hatch (g-306-132-b).

Bug shape: claim()'s same-agent/other-session guard requires BOTH the holder's
stored `claimed_by_sid` AND the caller's `sid` query param. A claim that sends
no sid skips the guard entirely and falls through to the idempotent no-op path
-- so the g-115-3176 collision (two sessions of one agent holding one world
goal) stayed reachable by omitting a single query parameter. It also leaves
`claimed_by_sid` unstamped, which disarms the guard for the NEXT session too;
that unstamped record is the durable half of the harm, which is why the refusal
is unconditional for world goals rather than gated on current claim state.

COVERAGE AUDIT (measured 2026-08-03, alpha, cc-04, Linux 6.8.0-136-generic)
performed BEFORE the refusal was written, per guard-1562:
  * `core/scripts/aspirations-claim.sh` (L290-291) is the ONLY production
    caller and appends `&sid=$MIND_SID` whenever the var is non-empty.
    `bash-agent-inject.py` (L362-365, L478) injects MIND_SID into EVERY Bash
    tool call and its own comment forbids making that conditional, so every
    LLM-driven claim carries one. No executable script calls the wrapper (grep
    of core/ + mind_api/ returned comments and docs only), so no cron /
    background / CI path reaches it sid-less either.
  * LIVE world queue at audit time: 5008 goals, 6 currently holding a claim,
    6/6 (100%) carrying claimed_by_sid, 0 without. That is the CURRENTLY-HELD
    population (claimed_by is cleared at close), which is the right denominator
    for "who would newly be refused" -- not an all-time claim census.
  * The set the refusal DOES newly reject is 13 direct endpoint POSTs across 3
    TEST files that bypass the wrapper: test_cross_lane_claim.py and
    test_claim_staleness_takeback.py (both claim world goals), and
    mind_api/tests/test_runtime_aspirations_retire_release_claim.py (11 POSTs).
    Those were updated to send a sid rather than to use the hatch: production
    always sends one, so a test that omitted it was already diverging from the
    production arg shape (guard-920).

The hatch (MIND_CLAIM_ALLOW_NO_SID) therefore exists for a genuinely un-hooked
future caller, not for the tests. It fails OPEN on its own dependency errors
(guard-142): a gate that cannot read its env must never wedge the world queue.

Run: py -3 -m pytest core/scripts/tests/test_claim_no_sid_refusal.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(SCRIPT_DIR))

from _daemon_fixture import DaemonFixture  # noqa: E402

LIVE_SID = "33333333-aaaa-bbbb-cccc-333333333333"
NO_SID_ENV = "MIND_CLAIM_ALLOW_NO_SID"
GOAL_ID = "g-301-01"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _make_world(tmp: Path) -> Path:
    """Tempdir world holding one UNCLAIMED world goal."""
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": GOAL_ID, "title": "sid-less claim refusal goal",
        "description": "Exercises the no-sid world-goal claim refusal",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
    }
    asp = {
        "id": "asp-301", "title": "sid-less claim refusal regression",
        "motivation": "Test claim() refusal when the caller sends no sid",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-08-01T00:00:00", "goals": [goal],
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


def _claim(port: int, agent: str, sid: str | None = None) -> tuple[int, str]:
    """POST the claim endpoint. sid=None reproduces the sid-less shape."""
    url = (f"http://127.0.0.1:{port}/v1/aspirations/claim"
           f"?id={GOAL_ID}&agent={agent}")
    if sid:
        url += f"&sid={sid}"
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("X-Mind-Agent", agent)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _goal(world: Path) -> dict | None:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        for g in json.loads(line).get("goals", []):
            if g.get("id") == GOAL_ID:
                return g
    return None


def _ledger_records(world: Path, gate: str) -> list:
    path = world / "override-bypass-ledger.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("gate") == gate:
            out.append(rec)
    return out


# --- 1. no sid -> REFUSE, and the goal stays unclaimed ----------------------
def test_no_sid_world_claim_refused():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            code, body = _claim(df.port, "alpha", sid=None)
            assert code == 400, (
                "a sid-less world-goal claim must be refused -- otherwise the "
                "same-agent/other-session guard is bypassable by omitting one "
                f"query param (g-115-3176); got {code}: {body}")
            assert "missing_claim_sid" in body, body
            # The refusal must not half-apply: no claimant, no stamp.
            g = _goal(world)
            assert g.get("claimed_by") is None, (
                f"refused claim must not stamp a claimant: {g}")
            assert g.get("claimed_by_sid") is None, (
                f"refused claim must not stamp a sid: {g}")


# --- 2. escape hatch -> allowed AND audited ---------------------------------
def test_escape_hatch_allows_and_writes_ledger():
    """Both halves are asserted deliberately.

    The 200 alone would ALSO pass against the pre-change endpoint (which
    allowed every sid-less claim), so it cannot demonstrate the change. The
    ledger record can only exist with the change in place, which is what makes
    this test fail on revert.
    """
    prev = os.environ.get(NO_SID_ENV)
    os.environ[NO_SID_ENV] = "hermetic test: audited sid-less caller"
    try:
        with tempfile.TemporaryDirectory() as tmpd:
            world = _make_world(Path(tmpd))
            with DaemonFixture(world, agent="alpha") as df:
                code, body = _claim(df.port, "alpha", sid=None)
                assert code == 200, (
                    f"the escape hatch must allow the claim: {code} {body}")
                assert _goal(world).get("claimed_by") == "alpha", _goal(world)

                recs = _ledger_records(world, "claim-sid-gate")
                assert len(recs) == 1, (
                    "a hatched claim must leave exactly one audit record -- an "
                    "un-audited bypass is indistinguishable from the bug; got "
                    f"{recs}")
                assert recs[0]["context"]["goal_id"] == GOAL_ID, recs[0]
                assert recs[0]["context"]["agent_claiming"] == "alpha", recs[0]
                assert "hermetic test" in recs[0]["justification"], recs[0]
    finally:
        if prev is None:
            os.environ.pop(NO_SID_ENV, None)
        else:
            os.environ[NO_SID_ENV] = prev


# --- 3. control: a sid-bearing claim is unaffected ---------------------------
def test_sid_bearing_claim_still_succeeds():
    """Scopes the refusal to the sid-less shape.

    Without this, test 1 would also pass if the endpoint had been broken to
    refuse every claim.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            code, body = _claim(df.port, "alpha", LIVE_SID)
            assert code == 200, (
                f"a normal sid-bearing claim must be unaffected: {code} {body}")
            g = _goal(world)
            assert g.get("claimed_by") == "alpha", g
            assert g.get("claimed_by_sid") == LIVE_SID, g
            assert _ledger_records(world, "claim-sid-gate") == [], (
                "a normal claim must not write a bypass record")
