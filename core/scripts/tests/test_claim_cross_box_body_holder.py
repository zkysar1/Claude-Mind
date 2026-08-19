"""Cross-BOX *worker Body* liveness in claim() — .

The remaining cell of the holder-liveness matrix. `g-306-140` closed
worker-vs-worker on the SAME box (`_same_box_body_is_live`); `g-306-132-a`
closed the absent-`running-session-id` cross-box case
(`_cross_box_holder_is_live`). What was left unguarded is the case that matters
most under the Mind/Body split: **the reducer versus its own REMOTE workers.**

MEASURED 2026-08-18 07:20 (alpha). Worker Body ``d1aec55b`` on one box claimed
``g-115-6538`` at 07:20:10; fourteen seconds later the reducer on another box
claimed the SAME goal (07:20:24) and both executed one non-recurring Fix goal.
Mechanism, read from the source rather than inferred: in
`_holder_session_is_live_runner` the branch ``running-session-id present AND
!= holder_sid`` consults only `_same_box_body_is_live`, a per-Body heartbeat
under *this* box's ``agents/<agent>/sessions/<sid>/``. A worker Body on ANOTHER
box has no heartbeat there, so the helper returns False, the claim falls
through as a "cross-session take-over from dormant <sid>", and
``claimed_by_sid`` is overwritten.

WHY A NEW SIGNAL RATHER THAN REUSING `_cross_box_holder_is_live`. That helper
reads the AGENT-keyed ``in_flight`` row, which carries no session id, and the
target branch's docstring rejects it for exactly that reason: it "would refuse
a legitimate same-box takeover whenever the mind happened to be alive
elsewhere." The SID-keyed ``in_flight_bodies[<holder_sid>]`` row (goal_id +
claimed_at, written by the Body at claim time) answers the session-level
question the branch actually asks. Case D below is the empirical proof that the
distinction holds: it reproduces the shape of
``test_claim_cross_box_holder.py::test_same_box_dormant_holder_ignores_shard``
— a fresh agent-keyed ``in_flight`` naming this very goal — and still expects a
takeover, because no SID-keyed row exists.

IT ALSO AVOIDS A KNOWN FALSE-POSITIVE GENERATOR (guard-3604).
`_cross_box_holder_is_live` gates on ``last_active`` freshness, and
``team-state-clear-in-flight.sh`` BUMPS a peer's ``last_active`` when policing
it — so a dormant peer reads fresh for the full window and that helper would
refuse a claim it should allow. ``claimed_at`` on the per-SID row is written
once by the Body itself and no cross-agent maintenance write touches it.

KNOWN LIMIT, stated rather than discovered later: ``claimed_at`` is stamped
once at claim time and never refreshed, so a Body legitimately working one goal
for longer than ``runner_heartbeat.stale_minutes`` (60) ages out of this
protection and becomes takeable again. That is the documented fail-open
direction (a wrong False merely permits a claim already possible today), not a
silent hole — the continuously-refreshed cross-box signal is the syncable
``session/body-heartbeat-<SID>.json`` carrier, and consulting it is a strictly
larger change than this one.

HERMETIC BY CONSTRUCTION: ``STORAGE_BACKEND=local`` (guard-955) makes
`_team_state.read_shard_authoritative` take its documented local-mirror path,
so seeding ``world/team-state/agents/<agent>.yaml`` IS the stubbed
authoritative signal — no S3, no creds, no network. Production arg shape
preserved (guard-920): every case drives the real HTTP claim endpoint, which is
the only caller that passes ``goal_id`` into the helper, rather than poking the
helper in isolation.
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
# The REMOTE worker Body holding the claim (no heartbeat on this box).
HOLDER_SID = "d1aec55b-2316-426c-8ada-0fd2e80c00eb"
# The reducer attempting the takeover — it owns running-session-id here.
CLAIMER_SID = "ed59f154-1111-2222-3333-444444444444"
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
        "id": GOAL_ID, "title": "Cross-box worker Body holder goal",
        "description": "Exercises the cross-box BODY holder-liveness fallback",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
        "claimed_by": "alpha", "claimed_at": _now(),
        "claimed_by_sid": HOLDER_SID,
    }
    asp = {
        "id": "asp-301", "title": "cross-box body claim regression",
        "motivation": "Test claim() cross-box worker-Body holder liveness",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [goal],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _seed_config(project_root: Path) -> None:
    """Without aspirations.yaml the stale_minutes lookup returns None and the
    probe fail-opens BEFORE reaching the branch under test — a green test that
    proves nothing."""
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "aspirations.yaml").write_text(
        f"runner_heartbeat:\n  stale_minutes: {STALE_MINUTES}\n",
        encoding="utf-8")


def _seed_reducer_session(project_root: Path, agent: str,
                          running_sid: str) -> None:
    """This box RUNS the loop under `running_sid`.

    That is the whole point of these cases: `running-session-id` is present and
    names a session OTHER than the holder, which routes into the branch this
    goal fixes. It is NOT the absent-rsid shape covered by
    test_claim_cross_box_holder.py.
    """
    _seed_config(project_root)
    sess = project_root / "agents" / agent / "session"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "running-session-id").write_text(running_sid, encoding="utf-8")
    (sess / "runner-heartbeat").write_text("", encoding="utf-8")


def _seed_shard(world: Path, agent: str, *, last_active: str,
                in_flight_goal: str | None = None,
                body_sid: str | None = None,
                body_goal: str | None = None,
                body_claimed_at: str | None = None) -> None:
    """The stubbed authoritative signal.

    Written as YAML text rather than via yaml.dump so the file shape is visible
    in the test itself — same choice the sibling file makes. `in_flight` is the
    AGENT-keyed row; `in_flight_bodies` is the SID-keyed one this fix reads.
    """
    rows = world / "team-state" / "agents"
    rows.mkdir(parents=True, exist_ok=True)
    lines = [f"last_active: '{last_active}'"]
    if in_flight_goal is not None:
        lines += ["in_flight:",
                  f"  goal_id: '{in_flight_goal}'",
                  "  phase: '4'"]
    if body_sid is not None:
        lines += ["in_flight_bodies:",
                  f"  {body_sid}:",
                  f"    goal_id: '{body_goal}'",
                  f"    claimed_at: '{body_claimed_at}'",
                  "    phase: '4'"]
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


# --- A. THE FIX: live cross-box worker Body -> REFUSE -----------------------
def test_cross_box_live_body_holder_is_refused():
    """Reproduces 07:20's shape: running-session-id present == caller, holder
    absent locally, FRESH remote per-SID body row naming this goal."""
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, "alpha", last_active=_now(),
                    body_sid=HOLDER_SID, body_goal=GOAL_ID,
                    body_claimed_at=_now())
        with DaemonFixture(world, agent="alpha") as df:
            _seed_reducer_session(df.project_root, "alpha",
                                  running_sid=CLAIMER_SID)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 409, (
                "a LIVE worker Body holding this goal on ANOTHER box must be "
                f"refused (g-306-318); got {code}: {body}")
            assert "same_agent_other_session" in body, body
            assert HOLDER_SID in body, (
                "the 409 must name the holding sid so the caller can "
                f"coordinate; got {body}")
            assert _goal(world).get("claimed_by_sid") == HOLDER_SID, (
                "the cross-box Body's claim must survive the refusal")


# --- B. STALE per-SID row must fail OPEN ------------------------------------
def test_cross_box_stale_body_row_fails_open():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, "alpha", last_active=_now(),
                    body_sid=HOLDER_SID, body_goal=GOAL_ID,
                    body_claimed_at=_ago(STALE_MINUTES * 10))
        with DaemonFixture(world, agent="alpha") as df:
            _seed_reducer_session(df.project_root, "alpha",
                                  running_sid=CLAIMER_SID)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "a STALE claimed_at is AMBIGUOUS and must never ground a "
                "refusal (check-team-state-before-silent rule 5); "
                f"got {code}: {body}")
            assert _goal(world).get("claimed_by_sid") == CLAIMER_SID


# --- C. per-SID row naming a DIFFERENT goal -> allow ------------------------
def test_cross_box_body_row_other_goal_allows():
    """Goal-scoping, not bare Body liveness: a Body alive on an UNRELATED goal
    must not wedge this one."""
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, "alpha", last_active=_now(),
                    body_sid=HOLDER_SID, body_goal="g-999-99",
                    body_claimed_at=_now())
        with DaemonFixture(world, agent="alpha") as df:
            _seed_reducer_session(df.project_root, "alpha",
                                  running_sid=CLAIMER_SID)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "a Body working an UNRELATED goal must not wedge this one; "
                f"got {code}: {body}")
            assert _goal(world).get("claimed_by_sid") == CLAIMER_SID


# --- D. NO-REGRESSION: agent-keyed in_flight alone must not refuse ----------
def test_agent_keyed_in_flight_alone_still_allows_takeover():
    """The branch the target docstring protects, restated as an assertion.

    This is the shape of
    test_claim_cross_box_holder.py::test_same_box_dormant_holder_ignores_shard
    — the strongest possible AGENT-keyed refuse signal (fresh `last_active`,
    `in_flight` naming this very goal) — with NO SID-keyed row. It must still
    permit the takeover, which is what proves the new consult is SID-scoped
    rather than a reintroduction of the agent-keyed read that branch rejects.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, "alpha", last_active=_now(),
                    in_flight_goal=GOAL_ID)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_reducer_session(df.project_root, "alpha",
                                  running_sid=CLAIMER_SID)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "agent-keyed in_flight carries no session id and must not "
                "refuse a legitimate same-box takeover — consulting it here "
                f"is what the target branch rejects; got {code}: {body}")
            assert _goal(world).get("claimed_by_sid") == CLAIMER_SID


# --- E. per-SID row for a DIFFERENT sid -> allow ----------------------------
def test_cross_box_body_row_other_sid_allows():
    """Precision check: a fresh row for some OTHER Body must not be read as
    evidence about THIS holder."""
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        _seed_shard(world, "alpha", last_active=_now(),
                    body_sid="99999999-0000-1111-2222-999999999999",
                    body_goal=GOAL_ID, body_claimed_at=_now())
        with DaemonFixture(world, agent="alpha") as df:
            _seed_reducer_session(df.project_root, "alpha",
                                  running_sid=CLAIMER_SID)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "a fresh row for a DIFFERENT sid says nothing about this "
                f"holder and must not refuse; got {code}: {body}")
            assert _goal(world).get("claimed_by_sid") == CLAIMER_SID


TESTS = [
    test_cross_box_live_body_holder_is_refused,
    test_cross_box_stale_body_row_fails_open,
    test_cross_box_body_row_other_goal_allows,
    test_agent_keyed_in_flight_alone_still_allows_takeover,
    test_cross_box_body_row_other_sid_allows,
]


def main() -> int:
    passed = 0
    for t in TESTS:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            continue
        passed += 1
    print(f"{passed}/{len(TESTS)} passed")
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
