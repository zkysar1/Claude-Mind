"""Worker-vs-worker liveness in claim() —  (the CAS side).

Sibling of test_claim_cross_box_holder.py (g-306-132-a, the cross-BOX side).
That one covers `running-session-id` ABSENT; this one covers it PRESENT and
naming a DIFFERENT session than the holder.

THE DEFECT. `_holder_session_is_live_runner` treated "present and != holder"
as "positively a dormant PRIOR session" and allowed the takeover. That reading
is correct under one-Body-per-box and FALSE under the Mind/Body split, because
`running-session-id` names only the REDUCER (body-manifest.py: "the reducer is
the worker Body holding running-session-id"). So:

  - worker claims a goal the REDUCER holds  -> holder IS the runner -> 409. Guarded.
  - worker claims a goal ANOTHER WORKER holds -> holder != runner -> takeover.
    WRONG: that worker is alive, and the takeover logged as benign recovery.

Worker-vs-reducer was guarded and worker-vs-worker was not — the double
execution the fork-Body design's exit criteria call out, hiding behind a log
line that reads like correct behavior.

WHAT THESE TESTS PIN:

  1. the fix: a live non-reducer worker Body (FRESH per-Body heartbeat) is
     refused with 409, even though it does not own running-session-id.
  2. outcome 2 of the goal — a CRASHED worker must not wedge the goal. Its
     heartbeat goes stale on its own and the takeover proceeds. This is why the
     signal is a heartbeat and not body-manifest.yaml's `body_state`, which
     nothing clears on crash (rb-4081, the stale-status class).
  3. the same fail-open direction for a Body with NO heartbeat file at all —
     any session predating the writer in heartbeat-tick.sh. Absence is
     ambiguous, never grounds to refuse.
  4. the pre-existing worker-vs-REDUCER guard is untouched: holder owns
     running-session-id + fresh runner-heartbeat -> still 409.

HERMETIC BY CONSTRUCTION: STORAGE_BACKEND=local (guard-955). The per-Body
heartbeat is a pure mtime signal on a file under the fixture's own tmp
project_root — no S3, no creds, no network, no daemon of its own. Production
arg shape preserved (guard-920): every case drives the real HTTP claim
endpoint, not the helper in isolation.
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

GOAL_ID = "g-300-02"
# The holder is a WORKER Body — deliberately NOT the reducer.
HOLDER_SID = "55555555-aaaa-bbbb-cccc-555555555555"
# A second worker Body attempting the claim.
CLAIMER_SID = "66666666-dddd-eeee-ffff-666666666666"
# The REDUCER — owns running-session-id, and is neither of the two above.
REDUCER_SID = "77777777-1111-2222-3333-777777777777"
STALE_MINUTES = 60


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _make_world(tmp: Path) -> Path:
    """World whose single goal is already claimed by alpha from HOLDER_SID."""
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": GOAL_ID, "title": "Worker-vs-worker claim goal",
        "description": "Exercises the per-Body holder-liveness branch",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
        "claimed_by": "alpha", "claimed_at": _now(),
        "claimed_by_sid": HOLDER_SID,
    }
    asp = {
        "id": "asp-300", "title": "worker-vs-worker claim regression",
        "motivation": "Test claim() per-Body holder liveness",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [goal],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _seed_config(project_root: Path) -> None:
    """Without this the stale_minutes lookup returns None and the probe
    fail-opens BEFORE reaching the branch under test — a green test that proves
    nothing (the trap test_claim_cross_box_holder.py documents)."""
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "aspirations.yaml").write_text(
        f"runner_heartbeat:\n  stale_minutes: {STALE_MINUTES}\n",
        encoding="utf-8")


def _seed_reducer(project_root: Path, agent: str, running_sid: str) -> None:
    """running-session-id PRESENT, naming `running_sid`. This is the branch
    under test — contrast the cross-box sibling, which leaves it ABSENT."""
    _seed_config(project_root)
    sess = project_root / "agents" / agent / "session"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "running-session-id").write_text(running_sid, encoding="utf-8")
    (sess / "runner-heartbeat").write_text("", encoding="utf-8")


def _seed_body_heartbeat(project_root: Path, agent: str, sid: str,
                         age_minutes: float | None) -> None:
    """Write agents/<agent>/sessions/<sid>/body-heartbeat at a controlled age.

    age_minutes=None leaves the file ABSENT while still creating the session
    dir, which is the shape of a Body that predates the writer in
    heartbeat-tick.sh (case 3) — distinct from "no session dir at all", so the
    test cannot pass merely because the directory is missing.
    """
    sdir = project_root / "agents" / agent / "sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    if age_minutes is None:
        return
    hb = sdir / "body-heartbeat"
    hb.write_text("", encoding="utf-8")
    when = time.time() - (age_minutes * 60.0)
    os.utime(hb, (when, when))


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


# --- 1. THE FIX: live non-reducer worker Body -> REFUSE ---------------------
def test_live_worker_holder_is_refused():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            # The REDUCER owns running-session-id and is a third session, so
            # running_sid != holder_sid -- pre-fix this returned False flatly.
            _seed_reducer(df.project_root, "alpha", REDUCER_SID)
            _seed_body_heartbeat(df.project_root, "alpha", HOLDER_SID,
                                 age_minutes=0)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 409, (
                "a LIVE non-reducer worker Body holding this goal must be "
                f"refused -- it does not own running-session-id, which is "
                f"exactly the worker-vs-worker blindness; got {code}: {body}")
            assert "same_agent_other_session" in body, body
            assert _goal(world).get("claimed_by_sid") == HOLDER_SID, (
                "the live worker's claim identity must survive the refusal")


# --- 2. outcome 2: a CRASHED worker must not wedge the goal -----------------
def test_crashed_worker_heartbeat_stale_falls_open():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            _seed_reducer(df.project_root, "alpha", REDUCER_SID)
            # Heartbeat far beyond stale_minutes: the Body stopped ticking.
            _seed_body_heartbeat(df.project_root, "alpha", HOLDER_SID,
                                 age_minutes=STALE_MINUTES * 10)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "a crashed worker's stale heartbeat is AMBIGUOUS and must "
                "never ground a refusal, or a transient crash becomes a "
                f"permanent wedge; got {code}: {body}")
            assert _goal(world).get("claimed_by_sid") == CLAIMER_SID


# --- 3. same direction when the Body has NO heartbeat file at all -----------
def test_worker_without_heartbeat_file_falls_open():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            _seed_reducer(df.project_root, "alpha", REDUCER_SID)
            # Session dir exists, heartbeat absent -- a Body predating the
            # writer. Absence is ambiguous, never grounds to refuse.
            _seed_body_heartbeat(df.project_root, "alpha", HOLDER_SID,
                                 age_minutes=None)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "a holder with no per-Body heartbeat predates the signal and "
                f"must still be takeable; got {code}: {body}")
            assert _goal(world).get("claimed_by_sid") == CLAIMER_SID


# --- 4. the pre-existing worker-vs-REDUCER guard is untouched ---------------
def test_reducer_holder_still_refused():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            # running-session-id names the HOLDER: the holder IS the reducer.
            # This path never reaches the new helper; the fresh local
            # runner-heartbeat decides, exactly as before.
            _seed_reducer(df.project_root, "alpha", HOLDER_SID)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 409, (
                "the pre-existing worker-vs-reducer refusal must be "
                f"unchanged by this fix; got {code}: {body}")
            assert "same_agent_other_session" in body, body
