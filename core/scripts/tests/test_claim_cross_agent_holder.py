"""Cross-BOX holder liveness on the CROSS-AGENT warn path — .

Sibling of test_claim_cross_box_holder.py, one function down. That file pinned
`_holder_session_is_live_runner` (SAME agent, different session, refuse-side).
This one pins `_cross_agent_holder_is_live` (DIFFERENT agent, warn-side), which
carried the IDENTICAL guard-2418 defect and was simply not in that goal's trace.

THE DEFECT. `_cross_agent_holder_is_live` probed
`agents_root/<holder>/session/running-session-id`, which
core/config/session-manifest.yaml registers as ``sync_tier: machine_local``. For
a FOREIGN holder that file is absent on every box except the one running that
agent's loop — the normal case, not the exception. The old code folded that
absence into the same `return False` as a genuine SID mismatch, so
`_nonholder_claim_warning` emitted NO warning at all.

Measured before the fix: 4 of 4 foreign agents inert on cc-05 (the review that
filed the goal) and independently 4 of 4 on cc-04 (hostname cc-04, uname -r
6.8.0-136-generic) — i.e. the warning was dead for 100% of foreign holders on
both boxes. The incident its docstring cites is exactly this shape: foxtrot runs
LAPTOP-3IOFCNEO, bravo runs cc-05, so the warning was silent for the one
scenario it was built to catch.

WHY THE WARN SIDE AND NOT THE CLAIM SIDE: claim() refuses cross-agent claims
outright, so the foreign-holder collision can only surface on complete/release.
These cases therefore drive the real release endpoint and assert on the
`warnings` field of its response (guard-920 — production arg shape, not the
helper in isolation).

WHAT EACH CASE PINS:
  1. the fix: absent local rsid + FRESH goal-matching foreign shard -> WARN.
     Revert the fallback and this fails.
  2. a foreign mind working a DIFFERENT goal -> QUIET. This is why the fallback
     is goal-SCOPED; keying on bare `last_active` would nag every recovery sweep
     whenever the peer was alive at all.
  3. a STALE shard -> QUIET. Stale is ambiguous and must never manufacture a
     warning (check-team-state-before-silent rule 5).
  4. local rsid PRESENT and != holder_sid, with NOTHING corroborating (no Body
     heartbeat, shard on an unrelated goal) -> QUIET. NARROWED by g-306-148 —
     see below.
  5. no shard at all -> QUIET.
  6. shard present but no in_flight -> QUIET (guard-997: ABSENCE is the
     unreliable direction).
  7. the pre-existing same-box path still works: local rsid PRESENT, == holder,
     fresh heartbeat -> WARN. Pins that the rewrite did not regress it.
  8. g-306-148 case (b): local rsid PRESENT and != holder_sid, but the goal-
     scoped shard is FRESH and matching -> WARN. `running-session-id` is
     machine_local and PERSISTS after an agent moves boxes, so a mismatch can
     mean "ran here yesterday, alive elsewhere today" rather than "dormant".
  9. g-306-148 case (a): local rsid mismatched, no shard, but a FRESH per-Body
     heartbeat under the HOLDER's own session dir -> WARN; the same heartbeat
     under the CALLER's dir -> QUIET. The pair is the mutation proof for the
     required `agent_name` parameter: without it the probe resolves the caller's
     dir, so the first half goes silent and the second half is what it would
     have been reading.

WHY CASE 4 WAS NARROWED (g-306-148). It used to seed the STRONGEST warn signal —
a fresh, goal-matching shard — and assert silence, on the premise that a
mismatched local rsid positively means "dormant prior session". That premise is
the defect: it is the same inference `_holder_session_is_live_runner` already
retired for worker Bodies (g-306-140), and it ignores that the file is
machine_local. When the authoritative goal-scoped row says the peer is working
THIS goal right now, silence is the wrong answer on a warn-only path — that is
the g-115-4232 incident, where a live claim was completed over with no signal.
What case 4 was really protecting is that ordinary dormant-session cleanup is not
nagged, so it now pins exactly that and nothing more: a mismatch with no
corroborating live signal stays quiet. Case 2 continues to pin the
peer-alive-on-unrelated-work half.

HERMETIC BY CONSTRUCTION: STORAGE_BACKEND=local (guard-955) makes
`_team_state.read_shard_authoritative` take its documented local-mirror path, so
seeding `world/team-state/agents/<agent>.yaml` IS the stubbed authoritative
signal — no S3, no creds, no network.
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

GOAL_ID = "g-301-01"
HOLDER = "bravo"            # the FOREIGN agent holding the claim
CALLER = "alpha"            # the agent invoking release
HOLDER_SID = "55555555-aaaa-bbbb-cccc-555555555555"
OTHER_SID = "66666666-dddd-eeee-ffff-666666666666"
STALE_MINUTES = 60


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _ago(minutes: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _make_world(tmp: Path) -> Path:
    """World whose single goal is already claimed by HOLDER from HOLDER_SID."""
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": GOAL_ID, "title": "Cross-agent holder goal",
        "description": "Exercises the cross-agent holder-liveness warn path",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
        "claimed_by": HOLDER, "claimed_at": _now(),
        "claimed_by_sid": HOLDER_SID,
    }
    asp = {
        "id": "asp-301", "title": "cross-agent warn regression",
        "motivation": "Test the cross-agent holder-liveness warn path",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [goal],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _seed_config(project_root: Path) -> None:
    """Without core/config/aspirations.yaml the stale_minutes lookup returns
    None and the probe fail-opens BEFORE reaching the branch under test — a
    green test that proves nothing."""
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "aspirations.yaml").write_text(
        f"runner_heartbeat:\n  stale_minutes: {STALE_MINUTES}\n",
        encoding="utf-8")


def _seed_holder_session(project_root: Path, running_sid: str | None,
                         *, heartbeat: bool = True) -> None:
    """Seeds the FOREIGN holder's session dir. running_sid=None leaves
    running-session-id ABSENT — the cross-box shape, and the whole defect."""
    _seed_config(project_root)
    sess = project_root / "agents" / HOLDER / "session"
    sess.mkdir(parents=True, exist_ok=True)
    if running_sid is None:
        return
    (sess / "running-session-id").write_text(running_sid, encoding="utf-8")
    if heartbeat:
        (sess / "runner-heartbeat").write_text("", encoding="utf-8")


def _seed_shard(world: Path, agent: str, *, last_active: str,
                in_flight_goal: str | None) -> None:
    """The stubbed authoritative signal, written as YAML text so the file shape
    is visible in the test itself."""
    rows = world / "team-state" / "agents"
    rows.mkdir(parents=True, exist_ok=True)
    lines = [f"last_active: '{last_active}'"]
    if in_flight_goal is not None:
        lines += ["in_flight:",
                  f"  goal_id: '{in_flight_goal}'",
                  "  phase: '4'"]
    (rows / f"{agent}.yaml").write_text("\n".join(lines) + "\n",
                                        encoding="utf-8")


def _seed_body_heartbeat(project_root: Path, agent: str, sid: str) -> None:
    """Seeds a FRESH per-Body heartbeat under `agent`'s own session dir.

    The path is `agents/<agent>/sessions/<sid>/body-heartbeat`, written by
    core/scripts/heartbeat-tick.sh once per iteration for every Body including
    the reducer. `agent` is a parameter and not hardcoded to the holder on
    purpose: case 9 seeds it under the CALLER to prove the probe is rooted at
    the HOLDER (g-306-148)."""
    d = project_root / "agents" / agent / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "body-heartbeat").write_text("", encoding="utf-8")


def _release(port: int, agent: str) -> tuple[int, dict]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/release"
           f"?id={GOAL_ID}&source=world")
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("X-Mind-Agent", agent)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode("utf-8")}


def _warnings(body: dict) -> list:
    return [w for w in (body.get("warnings") or []) if w]


# --- 1. THE FIX: live foreign holder on another box -> WARN -----------------
def test_cross_agent_live_holder_on_other_box_warns():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, HOLDER, last_active=_now(), in_flight_goal=GOAL_ID)
        with DaemonFixture(world, agent=CALLER) as df:
            # No local running-session-id for bravo => this box does not run
            # bravo's loop. Pre-fix this returned False and NOTHING was said.
            _seed_holder_session(df.project_root, running_sid=None)
            code, body = _release(df.port, CALLER)
            assert code == 200, body
            warns = _warnings(body)
            assert warns, (
                "a LIVE foreign holder working THIS goal on ANOTHER box must "
                f"produce the g-115-4232 warning; got warnings={warns} {body}")
            assert "DIFFERENT AGENT" in warns[0], warns[0]
            assert HOLDER in warns[0], warns[0]


# --- 2. foreign mind working a DIFFERENT goal -> QUIET ----------------------
def test_cross_agent_holder_on_other_goal_is_quiet():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, HOLDER, last_active=_now(),
                    in_flight_goal="g-999-99")
        with DaemonFixture(world, agent=CALLER) as df:
            _seed_holder_session(df.project_root, running_sid=None)
            code, body = _release(df.port, CALLER)
            assert code == 200, body
            assert not _warnings(body), (
                "a peer alive on an UNRELATED goal must not be nagged — "
                "goal-scoping is what keeps the recovery sweeps quiet; "
                f"got {_warnings(body)}")


# --- 3. STALE shard must fail OPEN (stay quiet) -----------------------------
def test_cross_agent_stale_shard_is_quiet():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, HOLDER, last_active=_ago(STALE_MINUTES * 10),
                    in_flight_goal=GOAL_ID)
        with DaemonFixture(world, agent=CALLER) as df:
            _seed_holder_session(df.project_root, running_sid=None)
            code, body = _release(df.port, CALLER)
            assert code == 200, body
            assert not _warnings(body), (
                "a STALE signal is AMBIGUOUS and must never manufacture a "
                f"warning (rule 5); got {_warnings(body)}")


# --- 4. mismatched rsid with NOTHING corroborating -> QUIET -----------------
def test_cross_agent_mismatched_rsid_without_corroboration_is_quiet():
    """NARROWED by  — see the module docstring for why.

    A mismatched local rsid no longer means "positively dormant", so the
    property worth pinning is the one this case was really protecting: an
    ordinary dormant-session cleanup must not be nagged. Nothing here
    corroborates life — no per-Body heartbeat, and the peer's row names a
    DIFFERENT goal — so both new consults decline and the branch stays silent.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, HOLDER, last_active=_now(),
                    in_flight_goal="g-999-99")
        with DaemonFixture(world, agent=CALLER) as df:
            _seed_holder_session(df.project_root, running_sid=OTHER_SID)
            code, body = _release(df.port, CALLER)
            assert code == 200, body
            assert not _warnings(body), (
                "a mismatched local rsid with NO corroborating live signal is "
                "ordinary dormant-session cleanup and must stay quiet; "
                f"got {_warnings(body)}")


# --- 5. no shard anywhere -> nothing to confirm -> QUIET --------------------
def test_cross_agent_absent_shard_is_quiet():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=CALLER) as df:
            _seed_holder_session(df.project_root, running_sid=None)
            code, body = _release(df.port, CALLER)
            assert code == 200, body
            assert not _warnings(body), (
                "an absent shard is not positive confirmation of life; "
                f"got {_warnings(body)}")


# --- 6. shard present but carrying NO in_flight -> QUIET --------------------
def test_cross_agent_shard_without_in_flight_is_quiet():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, HOLDER, last_active=_now(), in_flight_goal=None)
        with DaemonFixture(world, agent=CALLER) as df:
            _seed_holder_session(df.project_root, running_sid=None)
            code, body = _release(df.port, CALLER)
            assert code == 200, body
            assert not _warnings(body), (
                "guard-997: in_flight ABSENCE is the unreliable direction and "
                f"must never ground a warning; got {_warnings(body)}")


# --- 7. the pre-existing SAME-BOX live path still warns (no regression) -----
def test_cross_agent_same_box_live_holder_still_warns():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        # Deliberately NO shard: this must be answered by the LOCAL files
        # alone, proving the rewrite did not quietly route the same-box case
        # through the new fallback.
        with DaemonFixture(world, agent=CALLER) as df:
            _seed_holder_session(df.project_root, running_sid=HOLDER_SID)
            code, body = _release(df.port, CALLER)
            assert code == 200, body
            warns = _warnings(body)
            assert warns, (
                "the pre-existing same-box path (local rsid == holder, fresh "
                f"heartbeat) must still warn; got {warns} {body}")
            assert "DIFFERENT AGENT" in warns[0], warns[0]


# --- 8.  case (b): stale machine_local rsid, alive on another box ---
def test_cross_agent_stale_local_rsid_with_live_shard_warns():
    """PRESENT-AND-STALE is not an answer.

    `running-session-id` is `sync_tier: machine_local` and PERSISTS after an
    agent moves boxes, so "present and != holder_sid" can mean the holder ran
    HERE yesterday and is alive ELSEWHERE today. Pre-g-306-148 this returned a
    bare False and the goal-scoped shard was never consulted — the exact
    silence g-306-141 was filed to fix, reached through the "answered" branch
    instead of the absent one. Revert the fall-through and this fails.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, HOLDER, last_active=_now(), in_flight_goal=GOAL_ID)
        with DaemonFixture(world, agent=CALLER) as df:
            # Mismatched: a leftover from when bravo last ran on this box.
            _seed_holder_session(df.project_root, running_sid=OTHER_SID)
            code, body = _release(df.port, CALLER)
            assert code == 200, body
            warns = _warnings(body)
            assert warns, (
                "a stale machine_local rsid must not out-rank a FRESH "
                "goal-scoped authoritative row saying the peer is working "
                f"THIS goal; got warnings={warns} {body}")
            assert "DIFFERENT AGENT" in warns[0], warns[0]


# --- 9.  case (a): live worker Body, and the rooting mutation proof -
def test_cross_agent_live_worker_body_warns_and_is_rooted_at_holder():
    """A live non-reducer Body of the FOREIGN agent must warn — and the probe
    must read the HOLDER's session dir, not the caller's.

    Deliberately NO shard: the warning must come from the per-Body heartbeat
    alone, so this cannot pass by accidentally routing through the case-(b)
    fall-through. The second half is the mutation proof for the required
    `agent_name` parameter — `ctx.paths.session_dir` roots at the BOUND agent,
    so an implementation that omits the parameter reads
    `agents/<CALLER>/sessions/<holder-sid>/` and would go silent on the first
    half while the second half is precisely what it was reading.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    # 9a. heartbeat under the HOLDER's dir -> WARN
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=CALLER) as df:
            _seed_holder_session(df.project_root, running_sid=OTHER_SID)
            _seed_body_heartbeat(df.project_root, HOLDER, HOLDER_SID)
            code, body = _release(df.port, CALLER)
            assert code == 200, body
            warns = _warnings(body)
            assert warns, (
                "a FRESH per-Body heartbeat for the holder's own SID is "
                "positive evidence of a live non-reducer Body (g-306-140 "
                f"applied cross-agent); got warnings={warns} {body}")
            assert "DIFFERENT AGENT" in warns[0], warns[0]

    # 9b. same heartbeat under the CALLER's dir -> QUIET
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=CALLER) as df:
            _seed_holder_session(df.project_root, running_sid=OTHER_SID)
            _seed_body_heartbeat(df.project_root, CALLER, HOLDER_SID)
            code, body = _release(df.port, CALLER)
            assert code == 200, body
            assert not _warnings(body), (
                "the Body probe must be rooted at the HOLDER's dir; a "
                "heartbeat sitting under the CALLER's dir says nothing about "
                f"the holder and must not warn; got {_warnings(body)}")
