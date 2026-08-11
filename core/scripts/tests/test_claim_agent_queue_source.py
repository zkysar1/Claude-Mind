"""Agent-queue claims are no longer exempt from the claim protocol — .

Sibling of test_claim_worker_vs_worker.py (g-306-140) and
test_claim_cross_box_holder.py (g-306-132-a). Those pin the per-Body liveness
branches for WORLD goals. This one pins that the AGENT queue can reach those
branches at all.

THE DEFECT. `claim()` hardcoded `_resolve_paths(ctx, "world")` and answered any
agent-queue goal with 400 `agent_queue_goal`, whose stated justification was
"SINGLE-AGENT ACCESS ... Proceed directly to execution." That premise held
before asp-306 and is false under the Mind/Body split: a reducer and N worker
Bodies are all executors of ONE agent selecting from one pool. So every guard
sitting ~150 lines below the exemption — `same_agent_other_session`,
`_holder_session_is_live_runner`, `_same_box_body_is_live`,
`_cross_box_holder_is_live`, the SID-less refusal — was unreachable for the one
queue that holds the recurring cadence (g-001-01..g-001-10).

MEASURED (foxtrot, 2026-08-06): worker Body cc-08 and the reducer both executed
g-001-10 from ONE cadence fire, forming five hypotheses between them. The
worker's claim was REFUSED and the refusal told it to proceed — so the refusal
is what produced the double execution, not what prevented it.

WHAT THESE TESTS PIN:

  1. the capability: `&source=agent` claims an agent-queue goal and stamps it.
  2. BACKWARD COMPATIBILITY, which is the whole safety argument for shipping
     this ahead of the loop-digest change — a claim that does NOT name the
     queue still gets 400 `agent_queue_goal`, byte-identical to before. The
     loop digest still guards the claim with `IF source==world`, so production
     behavior is unchanged until that guard is dropped in a later change.
  3. THE FIX ITSELF: a live holder in a DIFFERENT session of the same agent is
     refused 409 on an agent-queue goal — the exact reducer-vs-worker shape
     that was measured double-executing.
  4. fail-open is preserved in the same direction as the world path: a CRASHED
     Body's stale heartbeat must never wedge the goal permanently.
  5. the SID-less refusal reaches agent goals. Session identity is the ONLY
     mechanism distinguishing a reducer from its worker Bodies, so a sid-less
     agent-queue claim would reinstate the defect while appearing to fix it.
  6. an unknown `source` is refused rather than silently treated as world.

HERMETIC BY CONSTRUCTION: STORAGE_BACKEND=local (guard-955) — a tmp world plus
a tmp project_root, no S3, no creds, no network. Production arg shape preserved
(guard-920): every case drives the real HTTP claim endpoint over the wire, with
the same headers aspirations-claim.sh sends, rather than calling the helper in
isolation.
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

AGENT = "alpha"
GOAL_ID = "g-001-10"          # the goal that actually double-executed
ASP_ID = "asp-001"
HOLDER_SID = "aaaaaaaa-1111-2222-3333-aaaaaaaaaaaa"
CLAIMER_SID = "bbbbbbbb-4444-5555-6666-bbbbbbbbbbbb"
REDUCER_SID = "cccccccc-7777-8888-9999-cccccccccccc"
STALE_MINUTES = 60


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _make_world(tmp: Path) -> Path:
    """A world queue that does NOT contain the goal.

    Load-bearing: it forces resolution through the agent queue rather than
    letting a same-id world goal satisfy the claim, which would make every
    assertion below pass without the change under test.
    """
    world = tmp / "world"
    world.mkdir()
    asp = {
        "id": "asp-900", "title": "unrelated world work",
        "motivation": "Ensure the world queue cannot satisfy this claim",
        "scope": "project", "priority": "LOW", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _seed_agent_queue(project_root: Path, *, claimed_by_sid: str | None = None,
                      recurring: bool = True) -> Path:
    """Put the goal in agents/<AGENT>/aspirations.jsonl.

    Recurring by default because that is the population at risk: the drain-lane
    promotes an OVERDUE recurring agent-queue goal to rank 1 for every Body
    that selects, and overdue-ness is exactly the condition that makes
    simultaneous pickup likely.
    """
    goal = {
        "id": GOAL_ID, "title": "Generate hypotheses from recent work",
        "description": "Agent-queue cadence goal",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "cadence", "participants": ["agent"],
        "recurring": recurring, "interval_hours": 24,
    }
    if claimed_by_sid:
        goal["claimed_by"] = AGENT
        goal["claimed_at"] = _now()
        goal["claimed_by_sid"] = claimed_by_sid
    asp = {
        "id": ASP_ID, "title": "agent cadence", "motivation": "recurring work",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [goal],
    }
    adir = project_root / "agents" / AGENT
    adir.mkdir(parents=True, exist_ok=True)
    path = adir / "aspirations.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    return path


def _seed_config(project_root: Path) -> None:
    """Without this the stale_minutes lookup returns None and the liveness
    probe fail-opens BEFORE reaching the branch under test — a green test that
    proves nothing (the trap test_claim_cross_box_holder.py documents)."""
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "aspirations.yaml").write_text(
        f"runner_heartbeat:\n  stale_minutes: {STALE_MINUTES}\n",
        encoding="utf-8")


def _seed_reducer(project_root: Path, running_sid: str) -> None:
    _seed_config(project_root)
    sess = project_root / "agents" / AGENT / "session"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "running-session-id").write_text(running_sid, encoding="utf-8")
    (sess / "runner-heartbeat").write_text("", encoding="utf-8")


def _seed_body_heartbeat(project_root: Path, sid: str,
                         age_minutes: float | None) -> None:
    sdir = project_root / "agents" / AGENT / "sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    if age_minutes is None:
        return
    hb = sdir / "body-heartbeat"
    hb.write_text("", encoding="utf-8")
    when = time.time() - (age_minutes * 60.0)
    os.utime(hb, (when, when))


def _claim(port: int, *, sid: str | None = CLAIMER_SID,
           source: str | None = "agent") -> tuple[int, str]:
    """Drive the real endpoint with the header shape aspirations-claim.sh sends."""
    url = (f"http://127.0.0.1:{port}/v1/aspirations/claim"
           f"?id={GOAL_ID}&agent={AGENT}")
    if sid:
        url += f"&sid={sid}"
    if source is not None:
        url += f"&source={source}"
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("X-Mind-Agent", AGENT)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _goal(path: Path) -> dict | None:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            for g in (json.loads(line).get("goals") or []):
                if g.get("id") == GOAL_ID:
                    return g
    return None


# --- 1. THE CAPABILITY -----------------------------------------------------
def test_agent_queue_claim_with_source_agent_succeeds():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            _seed_config(df.project_root)
            path = _seed_agent_queue(df.project_root)
            code, body = _claim(df.port)
            assert code == 200, (
                "an agent-queue goal named explicitly with &source=agent must "
                f"be claimable; got {code}: {body}")
            g = _goal(path)
            assert g.get("claimed_by") == AGENT, g
            assert g.get("claimed_by_sid") == CLAIMER_SID, (
                "the claiming SESSION must be stamped — it is the only thing "
                f"that distinguishes a reducer from its worker Bodies: {g}")
            assert g.get("claimed_at"), g


# --- 2. BACKWARD COMPATIBILITY (the safety argument) -----------------------
def test_agent_queue_claim_without_source_still_refused():
    """No `source` -> the endpoint's "world" default -> 400, exactly as before.

    This is what makes shipping the endpoint ahead of the loop-digest change
    safe: the digest still guards the claim with `IF source==world`, so no
    production caller sends `source=agent` yet and nothing changes underneath
    the running fleet.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            _seed_config(df.project_root)
            path = _seed_agent_queue(df.project_root)
            code, body = _claim(df.port, source=None)
            assert code == 400, f"expected the legacy refusal; got {code}: {body}"
            assert "agent_queue_goal" in body, body
            assert _goal(path).get("claimed_by") is None, (
                "the legacy path must not stamp a claim")


# --- 3. THE FIX: reducer-vs-worker on an AGENT goal ------------------------
def test_agent_queue_live_other_session_is_refused():
    """The measured double-execution shape, now refused.

    A live holder in a different session of the same agent (here: the reducer,
    which owns running-session-id) must refuse a second Body's claim. Pre-fix
    this goal could not be claimed AT ALL, so both Bodies proceeded.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            _seed_reducer(df.project_root, HOLDER_SID)
            path = _seed_agent_queue(df.project_root,
                                     claimed_by_sid=HOLDER_SID)
            _seed_body_heartbeat(df.project_root, HOLDER_SID, age_minutes=0)
            code, body = _claim(df.port)
            assert code == 409, (
                "a LIVE different session of this agent holds the goal — the "
                "reducer-vs-worker shape measured double-executing g-001-10; "
                f"got {code}: {body}")
            assert "same_agent_other_session" in body, body
            assert _goal(path).get("claimed_by_sid") == HOLDER_SID, (
                "the live holder's claim identity must survive the refusal")


# --- 4. fail-open preserved: a crashed Body must not wedge the goal --------
def test_agent_queue_crashed_body_falls_open():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            _seed_reducer(df.project_root, REDUCER_SID)
            path = _seed_agent_queue(df.project_root,
                                     claimed_by_sid=HOLDER_SID)
            _seed_body_heartbeat(df.project_root, HOLDER_SID,
                                 age_minutes=STALE_MINUTES * 10)
            code, body = _claim(df.port)
            assert code == 200, (
                "a crashed Body's stale heartbeat is AMBIGUOUS and must never "
                "ground a refusal, or a transient crash wedges the recurring "
                f"cadence permanently; got {code}: {body}")
            assert _goal(path).get("claimed_by_sid") == CLAIMER_SID


# --- 5. the SID-less refusal reaches agent goals ---------------------------
def test_agent_queue_claim_without_sid_is_refused():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            _seed_config(df.project_root)
            path = _seed_agent_queue(df.project_root)
            code, body = _claim(df.port, sid=None)
            assert code == 400, (
                "a sid-less agent-queue claim leaves claimed_by_sid unstamped, "
                "which reinstates the exact blindness this change closes; "
                f"got {code}: {body}")
            assert "missing_claim_sid" in body, body
            assert _goal(path).get("claimed_by") is None, (
                "an unstamped claim must not be recorded")


# --- 6. an unknown source is refused, never silently treated as world ------
def test_unknown_source_is_refused():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            _seed_config(df.project_root)
            _seed_agent_queue(df.project_root)
            code, body = _claim(df.port, source="agnet")   # typo on purpose
            assert code == 400, f"got {code}: {body}"
            assert "invalid_source" in body, (
                "a misspelled source must refuse loudly — silently falling "
                f"back to world is how a typo becomes a wrong-queue write: {body}")
