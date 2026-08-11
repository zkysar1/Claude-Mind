"""The agent-queue RELEASE path, end to end through the real wrapper — .

WHAT WAS UNCOVERED. g-306-249 taught `aspirations-release.sh` to say
`--source agent` and dropped the loop digest's `IF source==world` release guard,
making a new production path reachable:

    aspirations-release.sh --source agent
      -> POST /v1/aspirations/release?source=agent
        -> the AGENT queue record loses its claim
          -> team-state in_flight clears

Its regression suite (`test_release_source_forwarding.py`) is hermetic by
construction — no daemon, no world writes — so it pins ARG PARSING and QUERY
CONSTRUCTION and stops at the wrapper's edge. The sibling
`test_claim_agent_queue_source.py` covers the CLAIM half but drives the HTTP
endpoint directly, so it never exercises a wrapper either. Between them every
LINK was tested and the CHAIN was not: a query string that says `source=agent`
is not evidence that a record in the agent queue moved, and the in_flight clear
lives in the wrapper AFTER the daemon call, outside the endpoint entirely.

WHAT THESE TESTS PIN:

  1. the round trip: an agent-queue goal claimed and then released through the
     REAL wrapper loses its claim identity in the AGENT queue.
  2. WHICH QUEUE — the property the goal names and the one arg-parsing tests
     structurally cannot reach. A same-id decoy sits in the world queue, claimed;
     after an agent-queue release it must be UNTOUCHED. Without the decoy a
     wrapper that regressed to the `source=world` literal would resolve nothing,
     404, and leave the agent record untouched — which reads as a wrong-queue
     failure only by accident.
  3. backward compatibility, end to end: release WITHOUT `--source` must not
     touch the agent record. Same safety argument test_claim_agent_queue_source
     makes for the claim half, carried through the wrapper rather than asserted
     about a query string.
  4. the BODY in_flight clear (g-115-5143) — wrapper-side, and specifically the
     `in_flight_bodies.<sid>` surface. The agent-keyed `in_flight` row is
     reducer-owned and structurally unreachable from a fixture; case 4's
     docstring explains why, and it is the branch with no CAS behind it.
  5. a CHARACTERIZATION pin on the measured gap below — read its docstring
     before "fixing" a failure there.

THE GAP THIS COVERAGE MEASURED (g-306-260). The filing asked for a test that
"asserts the record returned to pending". Release does not do that: `release()`
pops `claimed_by` / `claimed_at` / `claimed_by_sid` and never writes `status`.
So a goal released while `in-progress` keeps that status, and
`goal-selector.py:107` has `SKIP_STATUSES = TERMINAL_GOAL_STATUSES |
{"in-progress"}` — it is NOT selectable. Releasing the claim alone does not
return a goal to the pool. `stranded-claim-sweep.py` already compensates by
doing both ("releases the claim, flips status to pending"), which is the
corroboration that the wrapper path is the incomplete one. Case 5 pins the
CURRENT behavior so a future fix flips it deliberately rather than silently.

WHY THE CLAIM HALF DRIVES THE ENDPOINT AND THE RELEASE HALF DRIVES THE WRAPPER.
Not an inconsistency — a blast-radius decision. `aspirations-release.sh` shells
out only to daemon-only scripts (team-state-*), so RT_DIR reaches all of them.
`aspirations-claim.sh` also calls `loop-state-save.sh init`, which is NOT
daemon-routed and resolves an agent dir locally, so driving it here risks
writing `iteration-checkpoint.json` into the LIVE agent session dir of whoever
runs the suite. The wrapper under test is the release one; the claim is setup,
and setup does not justify that surface.

ISOLATION — and one seam that does NOT hold, which is worth knowing:

  * RT_DIR points the wrapper (and every team-state script it shells out to) at
    the FIXTURE daemon. Without it they resolve
    PROJECT_ROOT/mind_api/state/daemon.port and drive the LIVE fleet daemon
    while the test reports success (guard-2484 seam 1).
  * MIND_AGENT_DIR does NOT isolate everything, and guard-2484 names it without
    this caveat. `_paths.py` applies it to the module-level `AGENT_DIR` constant
    ONLY (L324-331); the `agent_dir(name)` / `agent_state_dir(name)` FUNCTION
    family derives from `agents_root()` = `PROJECT_ROOT/agents` and ignores it.
    Measured while writing this file: with MIND_AGENT_DIR set to the fixture,
    `scorer-verdict-gate.py` still read the LIVE `agents/alpha/session/
    scorer-verdict.json` and refused the claim naming this session's real top
    pick. Read-only there, but any script resolving a path through those
    FUNCTIONS is unisolated by that env var.
  * The agent NAME is the BOUND agent (`alpha`), per guard-2484: a synthetic name
    creates a permanent team-state shard that own-cloud read-through resurrects.
    The GOAL ID is synthetic and verified absent from every live queue — that is
    the bounded-blast-radius half, so a leaked release 404s on an id that exists
    nowhere instead of releasing a live claim. Both halves are needed: the name
    must be real so the store is not polluted, the id fake so nothing real moves.

  STORAGE_BACKEND=local is pinned by DaemonFixture and again here (guard-955).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])
from _daemon_fixture import DaemonFixture  # noqa: E402

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
RELEASE = CORE_SCRIPTS / "aspirations-release.sh"
IN_FLIGHT = CORE_SCRIPTS / "team-state-in-flight.sh"
TEAM_STATE_READ = CORE_SCRIPTS / "team-state-read.sh"

AGENT = "alpha"
# Synthetic, and verified absent from every live queue before this file was
# written. Do NOT swap it for a real cadence id like : the sibling claim
# test can use one safely because it never leaves 127.0.0.1:<fixture port>, and
# these tests drive a wrapper that resolves its daemon from the environment.
GOAL_ID = "g-001-9901"
ASP_ID = "asp-001"
SID = "eeeeeeee-1111-2222-3333-eeeeeeeeeeee"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _goal_record(status: str, claimed_sid: str | None) -> dict:
    g = {
        "id": GOAL_ID,
        "title": "Agent-queue cadence goal (fixture)",
        "description": "seeded by test_agent_queue_release_e2e",
        "status": status,
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "cadence",
        "participants": ["agent"],
        "recurring": True,
        "interval_hours": 24,
    }
    if claimed_sid:
        g["claimed_by"] = AGENT
        g["claimed_at"] = _now()
        g["claimed_by_sid"] = claimed_sid
    return g


def _write_queue(path: Path, asp_id: str, goals: list) -> None:
    asp = {
        "id": asp_id, "title": "fixture aspiration",
        "motivation": "hold the fixture goal", "scope": "project",
        "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": goals,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")


def _make_world(tmp: Path, *, decoy: bool = False) -> Path:
    """The world queue. With ``decoy``, it carries a SAME-ID claimed goal."""
    world = tmp / "world"
    world.mkdir()
    goals = [_goal_record("in-progress", SID)] if decoy else []
    _write_queue(world / "aspirations.jsonl", "asp-900", goals)
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _seed_agent_queue(project_root: Path, *, status: str = "pending",
                      claimed_sid: str | None = None) -> Path:
    path = project_root / "agents" / AGENT / "aspirations.jsonl"
    _write_queue(path, ASP_ID, [_goal_record(status, claimed_sid)])
    return path


def _seed_config(project_root: Path) -> None:
    """Without a runner_heartbeat block the liveness probe fail-opens BEFORE the
    branch under test — the green-but-vacuous trap test_claim_cross_box_holder.py
    documents."""
    cfg = project_root / "core" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "aspirations.yaml").write_text(
        "runner_heartbeat:\n  stale_minutes: 60\n", encoding="utf-8")


def _env(df: DaemonFixture) -> dict:
    env = os.environ.copy()
    env["RT_DIR"] = str(df.runtime_dir)          # the seam that actually holds
    env["MIND_AGENT"] = AGENT
    env["MIND_SID"] = SID
    env["STORAGE_BACKEND"] = "local"             # guard-955
    env["MIND_WORLD"] = str(df.world)
    env["MIND_META"] = str(df.project_root / "meta")
    env["MIND_AGENT_DIR"] = str(df.project_root / "agents" / AGENT)
    return env


def _run(script: Path, *args: str, env: dict):
    # .as_posix(), never str(Path) — bash silently strips a WindowsPath's
    # backslashes (guard-581).
    p = subprocess.run(
        [BASH, script.as_posix(), *args],
        capture_output=True, text=True, timeout=120, env=env,
    )
    return p.returncode, (p.stdout + p.stderr)


def _claim_via_endpoint(port: int) -> tuple:
    """Setup, not the subject. Same shape as test_claim_agent_queue_source."""
    url = (f"http://127.0.0.1:{port}/v1/aspirations/claim"
           f"?id={GOAL_ID}&agent={AGENT}&sid={SID}&source=agent")
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("X-Mind-Agent", AGENT)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _read_goal(path: Path) -> dict | None:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            for g in (json.loads(line).get("goals") or []):
                if g.get("id") == GOAL_ID:
                    return g
    return None


def _claim_fields(g: dict) -> dict:
    return {k: g.get(k) for k in ("claimed_by", "claimed_at", "claimed_by_sid")}


# --- 1. THE ROUND TRIP -----------------------------------------------------
def test_claim_then_release_clears_the_agent_queue_claim():
    """The whole chain the goal exists for, with the wrapper doing the release."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            _seed_config(df.project_root)
            path = _seed_agent_queue(df.project_root)

            code, body = _claim_via_endpoint(df.port)
            assert code == 200, f"setup claim failed {code}: {body}"
            claimed = _read_goal(path)
            assert claimed.get("claimed_by_sid") == SID, (
                "the claiming SESSION must be stamped, or the release below has "
                f"no holder to clear: {claimed}")

            rc, out = _run(RELEASE, GOAL_ID, "--source", "agent", env=_env(df))
            assert rc == 0, f"release failed rc={rc}: {out}"

            released = _read_goal(path)
            assert released is not None, "the record must survive a release"
            assert _claim_fields(released) == {
                "claimed_by": None, "claimed_at": None, "claimed_by_sid": None
            }, f"every claim field must be cleared together: {released}"


# --- 2. WHICH QUEUE — the property arg-parsing tests cannot reach -----------
def test_release_clears_the_agent_claim_and_leaves_the_world_twin_untouched():
    """A same-id decoy in the world queue is the discriminator.

    If the wrapper ever regresses to the `source=world` literal the original
    defect had, THIS is the assertion that fails: the decoy loses its claim and
    the agent record keeps one.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), decoy=True)
        with DaemonFixture(world, agent=AGENT) as df:
            _seed_config(df.project_root)
            apath = _seed_agent_queue(df.project_root, status="in-progress",
                                      claimed_sid=SID)
            wpath = world / "aspirations.jsonl"

            # Positive control on the fixture itself: both queues really do hold
            # a CLAIMED same-id record before the release. Without it, a broken
            # seed makes "the world twin is untouched" vacuously true.
            assert _read_goal(apath).get("claimed_by_sid") == SID
            assert _read_goal(wpath).get("claimed_by_sid") == SID

            rc, out = _run(RELEASE, GOAL_ID, "--source", "agent", env=_env(df))
            assert rc == 0, f"release failed rc={rc}: {out}"

            assert _read_goal(apath).get("claimed_by_sid") is None, (
                "the AGENT record is the one named by --source agent")
            wg = _read_goal(wpath)
            assert wg.get("claimed_by_sid") == SID, (
                "the world twin must be untouched — a release that cleared it "
                f"went to the wrong queue: {wg}")
            assert wg.get("status") == "in-progress", wg


# --- 3. BACKWARD COMPATIBILITY, through the wrapper ------------------------
def test_default_source_does_not_touch_the_agent_record():
    """No `--source` -> the wrapper's `world` default -> the agent claim stands.

    Every caller predating g-306-249 omits the flag; this is the end-to-end form
    of the guarantee `test_default_source_is_world` makes about the query string.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            _seed_config(df.project_root)
            path = _seed_agent_queue(df.project_root, status="in-progress",
                                     claimed_sid=SID)

            rc, out = _run(RELEASE, GOAL_ID, env=_env(df))

            # POSITIVE CONTROL (guard-1906), added by the fresh-eyes pass on this
            # file. Asserting ONLY that the agent record is untouched is satisfied
            # by any wrapper that never reached the daemon at all. Measured with a
            # counterfactual: `--bogus` exits 2 at arg-parse, before `_runtime.sh`
            # is even sourced, and leaves the claim standing — so the case below
            # stayed green against a wrapper that was never invoked. Pin the REASON
            # the record survived, not just that it did.
            assert rc != 0, f"the world-source release is expected to refuse: {out}"
            assert "agent_queue_goal" in out, (
                "the release must have REACHED the daemon and been refused for "
                f"living in the agent queue; got rc={rc}: {out}")

            g = _read_goal(path)
            assert g.get("claimed_by_sid") == SID, (
                "a source-less release must not reach the agent queue, or every "
                f"pre-g-306-249 caller silently changed behavior: {g}")


# --- 4. THE BODY-ROW CLEAR — wrapper-side, outside the endpoint ------------
def test_release_clears_the_body_in_flight_row():
    """'s second surface, and the one that was measured stranded.

    A stale busy row is not untidiness: aspirations-select DROPS an agent's
    in_flight goal_id from its candidates, so it suppresses the released goal
    from the very partner the release was meant to hand it to. The wrapper's own
    comment records a worker row for g-306-227 left ~30h stale on cc-08 because
    only the agent-keyed surface was being cleared.

    WHY THE BODY ROW AND NOT THE AGENT-KEYED `in_flight`. The agent-keyed stamp
    is REDUCER-OWNED: `team-state-in-flight.sh` writes it only when the caller's
    MIND_SID equals `agent_dir(<agent>)/session/running-session-id`, and BOTH
    `agent_dir()` implementations (`_paths.sh:139`, `_paths.py:40`) build that
    path from PROJECT_ROOT and ignore MIND_AGENT_DIR — so the reducer test reads
    the LIVE repo no matter what the fixture sets. A fixture SID is therefore
    always a non-reducer, and the agent-keyed branch is unreachable from here
    without coupling the test to whichever session currently owns the live
    runner. The body branch is the one a fixture can honestly reach, and it is
    also the branch with no CAS behind it, so it carries the larger risk.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            _seed_config(df.project_root)
            _seed_agent_queue(df.project_root)
            env = _env(df)

            code, body = _claim_via_endpoint(df.port)
            assert code == 200, f"setup claim failed {code}: {body}"
            rc, out = _run(IN_FLIGHT, "--agent", AGENT, "--goal-id", GOAL_ID,
                           "--title", "fixture", "--phase", "4", env=env)
            assert rc == 0, f"setup in_flight write failed rc={rc}: {out}"

            field = f"agent_status.{AGENT}.in_flight_bodies.{SID}.goal_id"
            _, before = _run(TEAM_STATE_READ, "--field", field, env=env)
            assert GOAL_ID in before, (
                "the busy signal must be SET first, or the clear below is "
                f"vacuously satisfied by a row that never existed: {before!r}")

            rc, out = _run(RELEASE, GOAL_ID, "--source", "agent", env=env)
            assert rc == 0, f"release failed rc={rc}: {out}"

            _, after = _run(TEAM_STATE_READ, "--field", field, env=env)
            assert GOAL_ID not in after, (
                "the body row still names the released goal; a reader of it "
                f"drops that goal from its own candidates: {after!r}")


# --- 5. CHARACTERIZATION: release does not return a goal to the POOL --------
def test_release_returns_in_progress_work_to_the_pool():
    """FIXED BEHAVIOR as of  — this case was flipped deliberately.

    It previously asserted `status == "in-progress"` and documented itself as
    "MEASURED CURRENT BEHAVIOR, not an endorsement", with an instruction to
    update it deliberately if it ever went red because status had become
    `pending`. That is exactly what happened, so it now pins the fix instead of
    the defect.

    Before: `release()` popped the three claim fields and never wrote `status`.
    With `goal-selector.py:107` treating `in-progress` as a SKIP status, a goal
    released while in-progress was unclaimed AND unselectable — released from
    its holder without returning to the pool, invisible to every agent
    including the one that released it. `stranded-claim-sweep.py` compensated
    by doing both, which was the corroboration that release was the incomplete
    half.

    The reset is guarded on `status == "in-progress"` so the other callers of
    release (stop, retire, take-back) cannot launder a `blocked` or terminal
    goal into `pending`. Note the general claim that ownership and lifecycle
    are orthogonal still holds — a claimed goal sits at `pending` for its whole
    execution — so this is not a mirror of claim(); it reverses the separate
    lifecycle flip at the one transition where dropping ownership implies it.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            _seed_config(df.project_root)
            path = _seed_agent_queue(df.project_root, status="in-progress",
                                     claimed_sid=SID)

            rc, out = _run(RELEASE, GOAL_ID, "--source", "agent", env=_env(df))
            assert rc == 0, f"release failed rc={rc}: {out}"

            g = _read_goal(path)
            assert g.get("claimed_by_sid") is None, "the claim IS cleared"
            assert g.get("status") == "pending", (
                "release must return in-progress work to the pool (g-306-260). "
                "A goal left at in-progress with no claim is unselectable, so "
                "the release strands it instead of freeing it. See this test's "
                f"docstring before changing it. got: {g}")


# --- 6. THE GUARD: release must not launder a non-in-progress status --------
@pytest.mark.parametrize("status", ["blocked", "pending", "completed", "skipped"])
def test_release_leaves_every_other_status_alone(status):
    """The half of  that is easy to lose in a later simplification.

    The reset in release() is guarded on `status == "in-progress"` precisely
    because release is shared by stop, retire and take-back — the filing's
    explicit caution was that a `blocked` goal must not be laundered into
    `pending`, which would hand it straight back to the selector as ordinary
    work and erase the fact that something was wrong with it.

    Case 5 above would stay green under an UNGUARDED `goal["status"] =
    "pending"`, so it cannot detect that simplification. This case is the only
    thing standing between the guard and a plausible cleanup.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            _seed_config(df.project_root)
            path = _seed_agent_queue(df.project_root, status=status,
                                     claimed_sid=SID)

            rc, out = _run(RELEASE, GOAL_ID, "--source", "agent", env=_env(df))
            assert rc == 0, f"release failed rc={rc}: {out}"

            g = _read_goal(path)
            assert g.get("claimed_by_sid") is None, "the claim IS cleared"
            assert g.get("status") == status, (
                f"release must not rewrite status={status!r} — only in-progress "
                f"work is returned to the pool (g-306-260). Laundering a blocked "
                f"goal to pending hands it back to the selector as ordinary work. "
                f"got: {g}")
