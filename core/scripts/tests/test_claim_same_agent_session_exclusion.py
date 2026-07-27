"""test_claim_same_agent_session_exclusion.py -- regression matrix for
session-scoped claim exclusion (g-115-3176).

Bug shape: claim()'s conflict test was `existing != agent_name`, so a claim from
a DIFFERENT SESSION of the SAME agent fell through as an idempotent no-op. Two
sessions both "succeeded" and neither was warned. It could not have behaved
otherwise -- aspirations-claim.sh transmitted NO session identity at all, so the
endpoint was structurally unable to tell two sessions apart.

Observed live 2026-07-25: two sessions of one agent held a single world goal 16
minutes apart; the second was still doing reconnaissance when the first
completed it, one write away from creating duplicate credentials in an external
service.

Fix: the wrapper sends `&sid=$MIND_SID`; claim() stamps `claimed_by_sid` and
REFUSES (409 same_agent_other_session) when the holder is a DIFFERENT session
positively confirmed to be the agent's LIVE autonomous runner
(running-session-id match + fresh runner-heartbeat mtime).

FAIL-OPEN is the load-bearing property and is asymmetric (mirrors
.claude/rules/check-team-state-before-silent.md): a FRESH heartbeat is positive
evidence of life; a STALE one is AMBIGUOUS (idle session vs broken heartbeat
writer -- a live agent once read 59h stale). So the REFUSAL is gated on
freshness and the ALLOW is never gated on staleness: a wrong allow merely
permits what is already possible today, while a wrong refusal would wedge the
goal for every session of the agent.

Matrix:
  1. different agent               -> refuse (pre-existing behavior, unchanged)
  2. same agent, same session      -> idempotent no-op
  3. same agent, different LIVE    -> REFUSE 409 same_agent_other_session
  4. same agent, different DORMANT -> allowed takeover
  4b. holder is runner, heartbeat STALE -> fail open, allowed
  5. legacy claim (no stored sid) / caller sends no sid -> allowed, no-op

NOTE: claim() does NOT set `status` (verified while reading the endpoint), so
none of these assert on status transitions.

Run: py -3 -m pytest core/scripts/tests/test_claim_same_agent_session_exclusion.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(SCRIPT_DIR))

from _daemon_fixture import DaemonFixture  # noqa: E402

LIVE_SID = "11111111-aaaa-bbbb-cccc-111111111111"
OTHER_SID = "22222222-dddd-eeee-ffff-222222222222"
STALE_MINUTES = 60


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _make_world(tmp: Path, *, claimed_by=None, claimed_by_sid=None) -> Path:
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": "g-300-01", "title": "Session-exclusion goal",
        "description": "Exercises same-agent cross-session claim exclusion",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
    }
    if claimed_by is not None:
        goal["claimed_by"] = claimed_by
        goal["claimed_at"] = _now()
    if claimed_by_sid is not None:
        goal["claimed_by_sid"] = claimed_by_sid
    asp = {
        "id": "asp-300", "title": "session-scoped claim exclusion regression",
        "motivation": "Test claim() same-agent cross-session refusal",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [goal],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _seed_session(project_root: Path, agent: str, *, running_sid: str | None,
                  heartbeat_age_s: float = 0.0) -> None:
    """Seed the live-runner signals the endpoint probes.

    The fixture's project_root is a bare tmp repo, so it carries NO
    core/config/aspirations.yaml -- without seeding one the endpoint's
    stale_minutes lookup fails and the probe fail-opens, which would make the
    refusal path silently untestable (a green test that proves nothing).
    """
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "aspirations.yaml").write_text(
        f"runner_heartbeat:\n  stale_minutes: {STALE_MINUTES}\n",
        encoding="utf-8")
    sess = project_root / "agents" / agent / "session"
    sess.mkdir(parents=True, exist_ok=True)
    if running_sid is None:
        return
    (sess / "running-session-id").write_text(running_sid, encoding="utf-8")
    hb = sess / "runner-heartbeat"
    hb.write_text("", encoding="utf-8")
    if heartbeat_age_s:
        past = time.time() - heartbeat_age_s
        os.utime(hb, (past, past))


def _claim(port: int, agent: str, sid: str | None = None) -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/claim"
           f"?id=g-300-01&agent={agent}")
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
            if g.get("id") == "g-300-01":
                return g
    return None


# --- 1. different agent -> refuse (pre-existing behavior preserved) ---------
def test_different_agent_still_refused():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="bravo",
                            claimed_by_sid=OTHER_SID)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_session(df.project_root, "alpha", running_sid=LIVE_SID)
            code, body = _claim(df.port, "alpha", LIVE_SID)
            assert code == 409, f"different agent must still 409; got {code}: {body}"
            assert "already_claimed" in body, body


# --- 2. same agent, SAME session -> idempotent no-op ------------------------
def test_same_agent_same_session_is_noop():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha",
                            claimed_by_sid=LIVE_SID)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_session(df.project_root, "alpha", running_sid=LIVE_SID)
            code, body = _claim(df.port, "alpha", LIVE_SID)
            assert code == 200, f"same-session re-claim must stay a no-op: {code} {body}"
            assert _goal(world).get("claimed_by") == "alpha"


# --- 3. same agent, DIFFERENT LIVE session -> REFUSE ------------------------
def test_same_agent_different_live_session_refused():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha",
                            claimed_by_sid=LIVE_SID)
        with DaemonFixture(world, agent="alpha") as df:
            # holder IS the running session with a fresh heartbeat -> live
            _seed_session(df.project_root, "alpha", running_sid=LIVE_SID,
                          heartbeat_age_s=0.0)
            code, body = _claim(df.port, "alpha", OTHER_SID)
            assert code == 409, (
                "a second LIVE session of the same agent must be refused -- this "
                f"is the g-115-3176 collision; got {code}: {body}")
            assert "same_agent_other_session" in body, body
            assert _goal(world).get("claimed_by_sid") == LIVE_SID, (
                "the holder's identity must survive a refusal")


# --- 4. same agent, DIFFERENT DORMANT session -> allowed takeover -----------
def test_same_agent_dormant_session_takeover_allowed():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha",
                            claimed_by_sid=OTHER_SID)
        with DaemonFixture(world, agent="alpha") as df:
            # holder is NOT the current running session -> dormant
            _seed_session(df.project_root, "alpha", running_sid=LIVE_SID,
                          heartbeat_age_s=0.0)
            code, body = _claim(df.port, "alpha", LIVE_SID)
            assert code == 200, (
                f"a dormant session's claim must not wedge the goal: {code} {body}")
            assert _goal(world).get("claimed_by_sid") == LIVE_SID


# --- 4b. holder IS the runner but heartbeat STALE -> fail open, allow -------
def test_stale_heartbeat_fails_open_and_allows():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha",
                            claimed_by_sid=LIVE_SID)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_session(df.project_root, "alpha", running_sid=LIVE_SID,
                          heartbeat_age_s=10 * 24 * 3600)  # 10 days
            code, body = _claim(df.port, "alpha", OTHER_SID)
            assert code == 200, (
                "a STALE heartbeat is ambiguous and must fail OPEN -- refusing on "
                f"it would wedge claims fleet-wide; got {code}: {body}")


# --- 5. legacy claim (no stored sid) / caller sends no sid -> allowed -------
def test_legacy_claim_without_sid_is_noop():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha", claimed_by_sid=None)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_session(df.project_root, "alpha", running_sid=LIVE_SID)
            code, body = _claim(df.port, "alpha", OTHER_SID)
            assert code == 200, f"legacy sid-less claim must not refuse: {code} {body}"


def _release(port: int, agent: str, sid: str | None = None) -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/release"
           f"?id=g-300-01&source=world")
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


# --- 6. release by a NON-HOLDER live session -> WARNS but still releases ----
def test_release_by_nonholder_warns_but_succeeds():
    """Outcome 5. WARN, never refuse.

    stranded-claim-sweep.py --apply releases claims left by DEAD sessions and
    therefore always runs from a non-holding session. Refusing here would break
    that sweep fleet-wide and wedge the very goals it repairs -- so the release
    must succeed and merely become visible.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha",
                            claimed_by_sid=LIVE_SID)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_session(df.project_root, "alpha", running_sid=LIVE_SID)
            code, body = _release(df.port, "alpha", OTHER_SID)
            assert code == 200, (
                f"release must NOT be refused for a non-holder: {code} {body}")
            assert "same_agent_other_session" not in body
            assert "does NOT hold the claim" in body, (
                f"non-holder release must carry a warning; got {body}")
            g = _goal(world)
            assert g.get("claimed_by") is None, "claim must actually be cleared"


# --- 7. release by the HOLDER -> no warning --------------------------------
def test_release_by_holder_emits_no_warning():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha",
                            claimed_by_sid=LIVE_SID)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_session(df.project_root, "alpha", running_sid=LIVE_SID)
            code, body = _release(df.port, "alpha", LIVE_SID)
            assert code == 200, body
            assert "does NOT hold the claim" not in body, (
                f"the holder releasing its OWN claim must not warn: {body}")


# --- 8. release must clear claimed_by_sid WITH the claim -------------------
def test_release_clears_claimed_by_sid():
    """A stamp that outlives its claim is worse than no stamp.

    If claimed_by_sid survives the release, the next claimer that sends no sid
    inherits the PREVIOUS holder's session label -- making a later collision
    less diagnosable than before the field existed.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha",
                            claimed_by_sid=LIVE_SID)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_session(df.project_root, "alpha", running_sid=LIVE_SID)
            code, _ = _release(df.port, "alpha", LIVE_SID)
            assert code == 200
            g = _goal(world)
            assert "claimed_by_sid" not in g, (
                f"claimed_by_sid outlived its claim: {g.get('claimed_by_sid')}")


def _update_goal(port: int, agent: str, field: str, value: str) -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/update-goal"
           f"?id=g-300-01&field={field}&source=world")
    # The endpoint requires a JSON *value* as the body, not a bare string.
    req = urllib.request.Request(url, data=json.dumps(value).encode("utf-8"),
                                 method="POST")
    req.add_header("X-Mind-Agent", agent)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


# --- 9. terminal status via update-goal must clear claimed_by_sid ----------
def test_terminal_status_clears_claimed_by_sid():
    """The MOST-TRAVELLED claim-clearing path, and the one a test nearly missed.

    iteration-close.sh routes RECURRING goals to complete-by but sends
    everything else to `update-goal status=<terminal>`, so this is the path
    every non-recurring closure takes. It was found on a LIVE close (g-115-3176
    closed with its own claimed_by_sid still attached) rather than by the
    matrix, because the matrix only exercised claim/release.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha",
                            claimed_by_sid=LIVE_SID)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_session(df.project_root, "alpha", running_sid=LIVE_SID)
            code, body = _update_goal(df.port, "alpha", "status", "completed")
            assert code == 200, f"update-goal failed: {code} {body}"
            g = _goal(world)
            assert g.get("status") == "completed", g.get("status")
            assert g.get("claimed_by") is None, "claim must be cleared"
            assert "claimed_by_sid" not in g, (
                f"claimed_by_sid outlived the claim on the terminal-status "
                f"path: {g.get('claimed_by_sid')}")


def test_caller_without_sid_is_noop():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha",
                            claimed_by_sid=LIVE_SID)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_session(df.project_root, "alpha", running_sid=LIVE_SID)
            code, body = _claim(df.port, "alpha", None)  # no sid transmitted
            assert code == 200, (
                f"a caller sending no sid must behave as before: {code} {body}")
            assert _goal(world).get("claimed_by_sid") == LIVE_SID, (
                "a sid-less caller must NOT erase the holder's recorded identity")
