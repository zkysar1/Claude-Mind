"""Cross-BOX holder liveness in claim() — -a (fix set A(1)).

Trace T2 of the cross-box-two-bodies design. `_holder_session_is_live_runner`
resolved liveness ONLY from `running-session-id`, which is
``sync_tier: machine_local``. On a box that is NOT running the agent's loop the
file is ABSENT — not stale, UNANSWERABLE (guard-2418) — so the old code fell
straight through to False and a LIVE reducer's claim on another box was
silently taken over.

WHAT THESE TESTS PIN, and why each one is here rather than merely nice to have:

  1. the fix itself: an absent local running-session-id + a FRESH, goal-matching
     authoritative shard -> 409. Remove the fallback and this test fails.
  2. outcome 2 of the goal: a genuinely dormant cross-box holder is STILL taken
     over. This is why the fallback is goal-SCOPED rather than keyed on bare
     `last_active` freshness — the shard carries no session id (measured: no
     SID-bearing key exists on any fleet shard), so "is the mind alive at all"
     would refuse whenever the agent was alive ANYWHERE, including while working
     an unrelated goal.
  3. outcome 3: the fresh-evidence/stale-ambiguity asymmetry survives. A stale
     shard must fail OPEN. Nothing may refuse on a STALE signal alone.
  4. the branch that must NOT change: when the local running-session-id IS
     present and differs from the holder, local evidence has ANSWERED (this box
     runs the loop under another SID => the holder is a dormant prior session).
     The shard must not be consulted at all, or a legitimate same-box takeover
     would be refused whenever the mind happened to be alive.

HERMETIC BY CONSTRUCTION: STORAGE_BACKEND=local (guard-955) makes
`_team_state.read_shard_authoritative` take its documented local-mirror path, so
seeding `world/team-state/agents/<agent>.yaml` IS the stubbed authoritative
signal — no S3, no creds, no network. Production arg shape preserved (guard-920):
every case drives the real HTTP claim endpoint, not the helper in isolation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _daemon_fixture import DaemonFixture  # noqa: E402

GOAL_ID = "g-300-01"
HOLDER_SID = "33333333-aaaa-bbbb-cccc-333333333333"
CLAIMER_SID = "44444444-dddd-eeee-ffff-444444444444"
STALE_MINUTES = 60


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _ago(minutes: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _make_world(tmp: Path) -> Path:
    """World whose single goal is already claimed by alpha from HOLDER_SID."""
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": GOAL_ID, "title": "Cross-box holder goal",
        "description": "Exercises the cross-box holder-liveness fallback",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
        "claimed_by": "alpha", "claimed_at": _now(),
        "claimed_by_sid": HOLDER_SID,
    }
    asp = {
        "id": "asp-300", "title": "cross-box claim regression",
        "motivation": "Test claim() cross-box holder liveness",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [goal],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _seed_config(project_root: Path) -> None:
    """The fixture's project_root is a bare tmp repo with no
    core/config/aspirations.yaml. Without it the stale_minutes lookup returns
    None and the probe fail-opens BEFORE reaching the branch under test — a
    green test that proves nothing."""
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "aspirations.yaml").write_text(
        f"runner_heartbeat:\n  stale_minutes: {STALE_MINUTES}\n",
        encoding="utf-8")


def _seed_local_session(project_root: Path, agent: str,
                        running_sid: str | None) -> None:
    """running_sid=None leaves running-session-id ABSENT — the cross-box shape
    (this box does not run that agent's loop)."""
    _seed_config(project_root)
    sess = project_root / "agents" / agent / "session"
    sess.mkdir(parents=True, exist_ok=True)
    if running_sid is None:
        return
    (sess / "running-session-id").write_text(running_sid, encoding="utf-8")
    (sess / "runner-heartbeat").write_text("", encoding="utf-8")


def _seed_shard(world: Path, agent: str, *, last_active: str,
                in_flight_goal: str | None) -> None:
    """The stubbed authoritative signal. Written as YAML text rather than via
    yaml.dump so the file shape is visible in the test itself."""
    rows = world / "team-state" / "agents"
    rows.mkdir(parents=True, exist_ok=True)
    lines = [f"last_active: '{last_active}'"]
    if in_flight_goal is not None:
        lines += ["in_flight:",
                  f"  goal_id: '{in_flight_goal}'",
                  "  phase: '4'"]
    (rows / f"{agent}.yaml").write_text("\n".join(lines) + "\n",
                                        encoding="utf-8")


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


# --- 1. THE FIX: live reducer on another box -> REFUSE ----------------------
def test_cross_box_live_holder_is_refused():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, "alpha", last_active=_now(),
                    in_flight_goal=GOAL_ID)
        with DaemonFixture(world, agent="alpha") as df:
            # No local running-session-id => this box does not run alpha's
            # loop. Pre-fix this returned False and the claim was taken.
            _seed_local_session(df.project_root, "alpha", running_sid=None)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 409, (
                "a LIVE reducer holding this goal on ANOTHER box must be "
                f"refused (trace T2); got {code}: {body}")
            assert "same_agent_other_session" in body, body
            assert _goal(world).get("claimed_by_sid") == HOLDER_SID, (
                "the cross-box holder's identity must survive the refusal")


# --- 2. outcome 2: dormant cross-box holder is STILL taken over -------------
def test_cross_box_dormant_holder_takeover_allowed():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        # Mind is alive, but working a DIFFERENT goal -> this holder is dormant.
        _seed_shard(world, "alpha", last_active=_now(),
                    in_flight_goal="g-999-99")
        with DaemonFixture(world, agent="alpha") as df:
            _seed_local_session(df.project_root, "alpha", running_sid=None)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "a live mind working an UNRELATED goal must not wedge this "
                f"one -- goal-scoping is what preserves takeover; {code} {body}")
            assert _goal(world).get("claimed_by_sid") == CLAIMER_SID


# --- 3. outcome 3: STALE authoritative signal must fail OPEN ----------------
def test_cross_box_stale_shard_fails_open():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        # Matching goal, but last_active far beyond stale_minutes.
        _seed_shard(world, "alpha", last_active=_ago(STALE_MINUTES * 10),
                    in_flight_goal=GOAL_ID)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_local_session(df.project_root, "alpha", running_sid=None)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "a STALE signal is AMBIGUOUS and must never ground a refusal "
                f"(check-team-state-before-silent rule 5); got {code}: {body}")


# --- 4. the branch that must NOT change: local evidence ANSWERED ------------
def test_same_box_dormant_holder_ignores_shard():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        # Shard says the mind is live AND on this very goal -- the strongest
        # possible refuse signal. It must be ignored, because the LOCAL
        # running-session-id already answered: this box runs the loop under a
        # different SID, so the holder is positively a dormant prior session.
        _seed_shard(world, "alpha", last_active=_now(),
                    in_flight_goal=GOAL_ID)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_local_session(df.project_root, "alpha",
                                running_sid=CLAIMER_SID)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "when the local running-session-id ANSWERS, the cross-box "
                "fallback must not fire -- consulting the shard here would "
                f"refuse a legitimate same-box takeover; got {code}: {body}")
            assert _goal(world).get("claimed_by_sid") == CLAIMER_SID


# --- 5. no shard anywhere -> nothing to confirm -> allow --------------------
def test_cross_box_absent_shard_allows():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            _seed_local_session(df.project_root, "alpha", running_sid=None)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "an unreadable/absent shard is not positive confirmation of "
                f"life and must permit the claim; got {code}: {body}")


# --- 6. shard present but carrying NO in_flight -> allow --------------------
def test_cross_box_shard_without_in_flight_allows():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, "alpha", last_active=_now(), in_flight_goal=None)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_local_session(df.project_root, "alpha", running_sid=None)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "guard-997: in_flight ABSENCE is the unreliable direction and "
                f"must never ground a refusal; got {code}: {body}")
