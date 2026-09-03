"""Two worker Bodies racing ONE goal from a WORKER box —  item 1.

THE GAP THIS PINS. `_holder_session_is_live_runner` branches on three shapes of
`agents/<agent>/session/running-session-id`, and the three SID-keyed Body probes
(`_same_box_body_is_live` g-306-140, `_cross_box_body_is_live` g-306-318, and its
carrier escalation g-306-328) were wired into exactly ONE of them: the
`present-and-different` branch. That branch is reachable only where the file
EXISTS — i.e. on a box running the reducer.

A worker box never writes it. `/start`'s worker activation sequence (W0) says so
verbatim: the worker variant "MUST NOT touch `running-session-id` or
`latest-session-id`; those are reducer-owned". So on every worker box the file is
ABSENT, control takes the `not running_sid` branch, and that branch used to
consult ONE signal: `_cross_box_holder_is_live`, which reads the AGENT-keyed
`in_flight` row. That row is written for the REDUCER Body only
(`coordination.md`:1065) and carries no session id at all — its own docstring
says the shard "cannot answer the session-level question directly". Two worker
Bodies are therefore invisible to it BY CONSTRUCTION, and the claim fell through
as a benign "dormant" take-over.

MEASURED 2026-09-03. Two alpha worker Bodies claimed g-368-77 32 s apart and both
built the same IAM role in a live product backend. The world override-bypass
ledger carries the fall-through twice, in both directions (00:31:12
94c0ad1f -> 2fda1f3e, and 00:52:42 the exact reverse). Corroboration that control
came through the absent-rsid branch and not the guarded one: the
`cross-box-body-liveness` gate logs a firing on EVERY outcome of
`_cross_box_body_is_live`, and it recorded ZERO firings that day.

WHY THIS FILE IS NOT A DUPLICATE OF ITS SIBLINGS. `test_claim_worker_vs_worker.py`
seeds `running-session-id` naming a third session and
`test_claim_cross_box_body_holder.py` seeds it naming the caller, so both
exercise the `present-and-different` branch, which was already guarded.
`test_claim_cross_box_holder.py` DOES leave it absent — but seeds only the
AGENT-keyed `in_flight` row, so it never asked whether a SID-keyed signal is
consulted there. The absent-rsid + per-Body-signal cell is the one nothing
covered, and it is the cell every worker box lives in.

WHAT THESE TESTS PIN

  A. same-box: a LIVE sibling Body (fresh per-Body heartbeat) is refused, and
     the refusal NAMES the holder — the goal's outcome-1 wording.
  B. cross-box: a LIVE remote Body (fresh SID-keyed shard row naming this goal)
     is refused.
  C. FAIL-OPEN IS UNCHANGED on genuine no-evidence: no heartbeat, no body row,
     no agent row -> the take-over still succeeds. This is the half that must
     NOT change; a fix that refused here would convert a crashed Body into a
     wedge lasting until the claim expires (rb-4081 / the guard-1562 direction).
  D. STALE signals are still ambiguous and still permit the claim.
  E. the pre-existing agent-keyed last resort (g-306-132-a) still refuses.
  F. FIXTURE CONTROL: `running-session-id` really is absent. Without this a
     future edit could seed it, move every case into the sibling branch, and
     leave the file green while covering nothing.
  G. SOURCE CONTROL: `/start` still forbids the worker from writing that file.
     The branch under test only matters because of that instruction, so if it
     ever changes this file must fail loudly rather than quietly go moot.

HERMETIC BY CONSTRUCTION: STORAGE_BACKEND=local (guard-955). Every case drives
the real HTTP claim endpoint with the production arg shape (guard-920), never a
helper in isolation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _daemon_fixture import DaemonFixture  # noqa: E402

# parents[2] is `core/` (what the sibling test files call REPO_ROOT); the
# project root is one further up, and `.claude/` lives there.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

GOAL_ID = "g-302-01"
# Body A — holds the claim. A WORKER; no box here runs the reducer.
HOLDER_SID = "94949494-aaaa-bbbb-cccc-949494949494"
# Body B — the second worker Body racing the same goal.
CLAIMER_SID = "2f2f2f2f-dddd-eeee-ffff-2f2f2f2f2f2f"
STALE_MINUTES = 60


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _ago(minutes: float) -> str:
    return (datetime.now()
            - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")


def _make_world(tmp: Path) -> Path:
    """World whose single goal is already claimed by alpha from HOLDER_SID."""
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": GOAL_ID, "title": "Two-SID worker-box claim race",
        "description": "Exercises the absent-running-session-id branch",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
        "claimed_by": "alpha", "claimed_at": _now(),
        "claimed_by_sid": HOLDER_SID,
    }
    asp = {
        "id": "asp-302", "title": "worker-box two-SID claim race",
        "motivation": "Test claim() holder liveness from a worker box",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [goal],
    }
    queue = world / "aspirations.jsonl"
    with open(queue, "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _seed_worker_box(project_root: Path, agent: str) -> None:
    """A WORKER box: session dir present, `running-session-id` ABSENT.

    The dir is created deliberately, so no case can pass merely because the
    directory is missing — the absence under test is of the FILE.

    aspirations.yaml is mandatory here for the reason the sibling files record:
    without it the `stale_minutes` lookup returns None and the probe fail-opens
    BEFORE reaching the branch under test, producing a green test that proves
    nothing.
    """
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "aspirations.yaml").write_text(
        f"runner_heartbeat:\n  stale_minutes: {STALE_MINUTES}\n",
        encoding="utf-8")
    sess = project_root / "agents" / agent / "session"
    sess.mkdir(parents=True, exist_ok=True)


def _seed_body_heartbeat(project_root: Path, agent: str, sid: str,
                         age_minutes: float | None) -> None:
    """agents/<agent>/sessions/<sid>/body-heartbeat at a controlled age.

    age_minutes=None leaves the file ABSENT while still creating the session
    dir — the shape of a Body predating the writer in heartbeat-tick.sh.
    """
    sdir = project_root / "agents" / agent / "sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    if age_minutes is None:
        return
    hb = sdir / "body-heartbeat"
    hb.write_text("", encoding="utf-8")
    when = time.time() - (age_minutes * 60.0)
    os.utime(hb, (when, when))


def _seed_shard(world: Path, agent: str, *, last_active: str,
                in_flight_goal: str | None = None,
                body_sid: str | None = None,
                body_goal: str | None = None,
                body_claimed_at: str | None = None) -> None:
    """The stubbed authoritative team-state shard.

    Written as literal YAML text rather than via yaml.dump so the shape is
    visible in the test — the same choice both sibling files make.
    `in_flight` is the AGENT-keyed row; `in_flight_bodies` is the SID-keyed one.
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


# --- A. THE FIX: a live SAME-BOX sibling Body -> REFUSE, holder named -------
def test_second_worker_body_same_box_is_refused_and_holder_named():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            _seed_worker_box(df.project_root, "alpha")
            _seed_body_heartbeat(df.project_root, "alpha", HOLDER_SID,
                                 age_minutes=0)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 409, (
                "a second worker Body must be refused while a LIVE sibling "
                "Body holds the goal -- on a worker box there is no "
                f"running-session-id, which is the whole gap; got {code}: "
                f"{body}")
            assert "same_agent_other_session" in body, body
            assert HOLDER_SID in body, (
                "outcome 1 requires the refusal to NAME the current holder; "
                f"got {body}")
            assert _goal(world).get("claimed_by_sid") == HOLDER_SID, (
                "the live holder's claim identity must survive the refusal")


# --- B. THE FIX: a live CROSS-BOX Body (SID-keyed shard row) -> REFUSE ------
def test_second_worker_body_cross_box_is_refused():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            _seed_worker_box(df.project_root, "alpha")
            # No local heartbeat for the holder: it runs on ANOTHER box, so the
            # same-box probe must return False and the SID-keyed shard row is
            # the only thing that can answer.
            _seed_body_heartbeat(df.project_root, "alpha", HOLDER_SID,
                                 age_minutes=None)
            _seed_shard(world, "alpha", last_active=_now(),
                        body_sid=HOLDER_SID, body_goal=GOAL_ID,
                        body_claimed_at=_now())
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 409, (
                "a FRESH in_flight_bodies row naming this goal is positive "
                f"evidence the remote Body is on it; got {code}: {body}")
            assert HOLDER_SID in body, body
            assert _goal(world).get("claimed_by_sid") == HOLDER_SID


# --- C. FAIL-OPEN UNCHANGED: no evidence anywhere -> take-over succeeds -----
def test_no_evidence_anywhere_still_allows_takeover():
    """The half that must NOT change.

    A Body whose heartbeat writer died, or one predating the writer entirely,
    leaves exactly this shape. Refusing here would turn a transient crash into
    a wedge lasting until the claim expires — the direction guard-1562 and the
    caller's own documented asymmetry both forbid.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            _seed_worker_box(df.project_root, "alpha")
            _seed_body_heartbeat(df.project_root, "alpha", HOLDER_SID,
                                 age_minutes=None)
            # Shard exists and is fresh, but says nothing about any Body.
            _seed_shard(world, "alpha", last_active=_now())
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "absence of every liveness signal is AMBIGUOUS and must never "
                f"ground a refusal; got {code}: {body}")
            assert _goal(world).get("claimed_by_sid") == CLAIMER_SID


# --- D. STALE signals stay ambiguous ---------------------------------------
def test_stale_body_signals_still_allow_takeover():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            _seed_worker_box(df.project_root, "alpha")
            _seed_body_heartbeat(df.project_root, "alpha", HOLDER_SID,
                                 age_minutes=STALE_MINUTES * 10)
            _seed_shard(world, "alpha", last_active=_now(),
                        body_sid=HOLDER_SID, body_goal=GOAL_ID,
                        body_claimed_at=_ago(STALE_MINUTES * 10))
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 200, (
                "a stale heartbeat and a stale body row are both ambiguous; "
                f"got {code}: {body}")
            assert _goal(world).get("claimed_by_sid") == CLAIMER_SID


# --- E. the agent-keyed last resort (-a) is untouched --------------
def test_agent_keyed_in_flight_still_refuses_as_last_resort():
    """The signal this branch consulted BEFORE the fix must keep working.

    Reordering a fallback chain is exactly where a still-correct earlier answer
    gets lost, so it is pinned rather than assumed.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            _seed_worker_box(df.project_root, "alpha")
            _seed_body_heartbeat(df.project_root, "alpha", HOLDER_SID,
                                 age_minutes=None)
            _seed_shard(world, "alpha", last_active=_now(),
                        in_flight_goal=GOAL_ID)
            code, body = _claim(df.port, "alpha", CLAIMER_SID)
            assert code == 409, (
                "the agent-keyed cross-box probe must still refuse a live "
                f"mind working THIS goal; got {code}: {body}")
            assert _goal(world).get("claimed_by_sid") == HOLDER_SID


# --- F. FIXTURE CONTROL ----------------------------------------------------
def test_fixture_really_leaves_running_session_id_absent():
    """Guards this whole file against silently becoming a sibling duplicate.

    Every case above is only about the absent-rsid branch. If a future edit
    seeds `running-session-id`, all of them move into the already-guarded
    branch and stay green while covering nothing.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        root = Path(tmpd)
        _seed_worker_box(root, "alpha")
        sess = root / "agents" / "alpha" / "session"
        assert sess.is_dir(), "the session dir must exist"
        assert not (sess / "running-session-id").exists(), (
            "the fixture must leave running-session-id ABSENT -- that absence "
            "IS the worker-box shape under test")


# --- G. SOURCE CONTROL -----------------------------------------------------
def test_start_still_forbids_a_worker_writing_running_session_id():
    """The branch under test matters only because /start says this.

    If the worker activation sequence ever starts writing the file, the
    absent-rsid branch stops being the worker path and this file's premise is
    void — which must fail loudly here, not be discovered later.
    """
    skill = (PROJECT_ROOT / ".claude" / "skills" / "start"
             / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    assert "MUST NOT touch `running-session-id`" in skill, (
        "/start's worker activation no longer carries the "
        "reducer-owned-file prohibition this file's premise rests on; "
        "re-derive the worker-box shape before trusting these cases")
