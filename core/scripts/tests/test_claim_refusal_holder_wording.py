"""Refusal DETAIL must name the holder correctly — .

THE DEFECT. `_holder_session_is_live_runner` has returned True for a live
non-reducer worker Body since g-306-132-a/g-306-140, but the claim refusal
sent ONE detail for every refusal: "That session is this agent's running
autonomous loop ... Pick a different goal, or stop the other session
first." Measured 2026-09-27 05:37Z (g-376-59): the alpha reducer claimed a
goal held by a WORKER Body (fresh in_flight_bodies row, phase 4) and read a
refusal that (a) named a holder that is not the running loop — contradicting
its own runner-identity-check — and (b) prescribed stopping that session,
which would destroy the Body's in-flight work. The only correct action in
the worker-holder case is to pick a different goal.

THE FIX. claim() now branches the detail on which case fired:
  - holder_sid == local running-session-id -> the holder IS the running loop;
    today's wording is kept VERBATIM (that case really is dangerous in the
    way the text says).
  - otherwise (same-box body heartbeat, cross-box in_flight_bodies row, or
    the absent-rsid branch a worker box always takes) -> the holder is a
    live non-reducer Body; the detail names it as a worker Body and drops
    the stop-it advice.
The 409 CODE same_agent_other_session is unchanged -- tests and callers key
on it, so these tests assert the CODE on every case and the PROSE per case.

OUTCOME 1 (this file): a worker-holder refusal names a worker Body and
contains NEITHER "running autonomous loop" NOR "stop the other session".
OUTCOME 2 (case 3): the reducer-holder case keeps its current meaning.

HERMETIC BY CONSTRUCTION: STORAGE_BACKEND=local (guard-955). Every signal is
a pure mtime/contents file under the fixture's own tmp project_root -- no S3,
no creds, no network. Production arg shape preserved (guard-920): every case
drives the real HTTP claim endpoint, not the helper in isolation.

Run: py -3 -m pytest core/scripts/tests/test_claim_refusal_holder_wording.py
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _daemon_fixture import DaemonFixture  # noqa: E402

GOAL_ID = "g-300-03"
HOLDER_SID = "88888888-aaaa-bbbb-cccc-888888888888"
CLAIMER_SID = "99999999-dddd-eeee-ffff-999999999999"
REDUCER_SID = "aaaaaaaa-1111-2222-3333-aaaaaaaaaaaa"
STALE_MINUTES = 60

# The two phrases the worker-holder refusal must NOT contain (goal outcome 1).
BANNED_PHRASES = ("running autonomous loop", "stop the other session")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _make_world(tmp: Path) -> Path:
    """World whose single goal is already claimed by alpha from HOLDER_SID."""
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": GOAL_ID, "title": "Refusal-wording goal",
        "description": "Exercises the holder-wording branch of the refusal",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
        "claimed_by": "alpha", "claimed_at": _now(),
        "claimed_by_sid": HOLDER_SID,
    }
    asp = {
        "id": "asp-300", "title": "claim refusal holder-wording regression",
        "motivation": "Test the worker-vs-reducer branch of the refusal detail",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [goal],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _seed_config(project_root: Path) -> None:
    """Without this the stale_minutes lookup returns None and the probe
    fail-opens BEFORE reaching the branch under test — a green test that
    proves nothing."""
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "aspirations.yaml").write_text(
        f"runner_heartbeat:\n  stale_minutes: {STALE_MINUTES}\n",
        encoding="utf-8")


def _seed_session(project_root: Path, agent: str, *, running_sid: str | None,
                  heartbeat_age_s: float = 0.0) -> None:
    """running_sid=None leaves running-session-id ABSENT — the normal state
    on a worker box (/start W0: a worker Body must not touch it)."""
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


def _seed_body_heartbeat(project_root: Path, agent: str, sid: str) -> None:
    """FRESH per-Body heartbeat for a live non-reducer worker Body — the
    same signal test_claim_worker_vs_worker.py pins the refusal on."""
    sdir = project_root / "agents" / agent / "sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "body-heartbeat").write_text("", encoding="utf-8")


def _claim(port: int, agent: str, sid: str) -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/claim"
           f"?id={GOAL_ID}&agent={agent}&sid={sid}")
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("X-Mind-Agent", agent)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _goal(world: Path) -> dict | None:
    with open(world / "aspirations.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            for g in (json.loads(line).get("goals") or []):
                if g.get("id") == GOAL_ID:
                    return g
    return None


def _assert_worker_wording(code: int, body: str) -> None:
    """Outcome 1's three clauses, applied to a worker-holder refusal."""
    assert code == 409, (
        "the worker-holder refusal must stay a 409 (tests and callers key "
        f"on the code); got {code}: {body}")
    assert "same_agent_other_session" in body, (
        "the 409 CODE name same_agent_other_session is unchanged -- "
        f"callers key on it; got {body}")
    assert "worker Body" in body, (
        "a worker-holder refusal must NAME a worker Body, so the caller "
        f"understands what it is yielding to; got {body}")
    for phrase in BANNED_PHRASES:
        assert phrase not in body, (
            f"the worker-holder refusal must not contain {phrase!r} -- it "
            f"would name the wrong holder or prescribe stopping a live "
            f"Body; got {body}")


# --- 1. THE FIX: live worker holder (reducer present, third sid) ------------
def test_live_worker_holder_refusal_names_worker_not_runner():
    """The  measured shape: running-session-id present and naming a
    THIRD session (the reducer), holder is a live non-reducer worker Body
    with a fresh per-Body heartbeat."""
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            _seed_config(df.project_root)
            _seed_session(df.project_root, "alpha", running_sid=REDUCER_SID,
                          heartbeat_age_s=0.0)
            _seed_body_heartbeat(df.project_root, "alpha", HOLDER_SID)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            _assert_worker_wording(code, body)
            assert _goal(world).get("claimed_by_sid") == HOLDER_SID, (
                "the live worker's claim identity must survive the refusal")


# --- 2. same wording from the absent-rsid branch a worker box takes ---------
def test_absent_rsid_worker_holder_gets_worker_wording():
    """The NORMAL state on a worker box: no running-session-id at all
    (guard-2418: unanswerable locally), holder confirmed live only by its
    per-Body heartbeat. The refusal must still name a worker Body and must
    not read as if a second runner existed on this box."""
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            _seed_config(df.project_root)
            _seed_session(df.project_root, "alpha", running_sid=None)
            _seed_body_heartbeat(df.project_root, "alpha", HOLDER_SID)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            _assert_worker_wording(code, body)
            assert _goal(world).get("claimed_by_sid") == HOLDER_SID


# --- 3. outcome 2: the REDUCER-holder case keeps its current meaning --------
def test_reducer_holder_refusal_keeps_current_wording():
    """holder_sid == local running-session-id: the holder really IS the
    running loop, and today's wording (running autonomous loop / stop the
    other session first) stays verbatim for this one case. If this test
    ever sees the worker wording, the reducer case has lost its meaning."""
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            _seed_config(df.project_root)
            _seed_session(df.project_root, "alpha", running_sid=HOLDER_SID,
                          heartbeat_age_s=0.0)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 409, (
                f"the reducer-holder refusal must still be a 409; {code} {body}")
            assert "same_agent_other_session" in body, body
            assert "running autonomous loop" in body, (
                "the reducer-holder case must KEEP today's meaning -- the "
                f"holder IS the running loop here; got {body}")
            assert "stop the other session first" in body, (
                "the reducer-holder case's stop-it advice is correct for "
                f"that case and must stay; got {body}")
            assert "worker Body" not in body, (
                "the reducer is NOT a worker Body; naming it as one would "
                f"restore the mislabeling in the other direction; got {body}")
            assert _goal(world).get("claimed_by_sid") == HOLDER_SID
