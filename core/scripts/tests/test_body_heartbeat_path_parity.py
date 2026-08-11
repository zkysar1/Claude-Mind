"""Per-Body heartbeat WRITER->READER path parity — .

THE GAP THIS CLOSES. g-306-140 shipped the per-Body heartbeat as two halves
that agree only by convention, and nothing tested the join. The path is derived
INDEPENDENTLY in two places, in two languages:

  writer  core/scripts/heartbeat-tick.sh:115-116
          _HB_BODY_DIR="$AGENT_DIR/$SESSIONS_DIRNAME/$MIND_SID"
          touch "$_HB_BODY_DIR/body-heartbeat"
  reader  mind_api/src/endpoints/aspirations_write.py:3978-3982
          sess_dir = ctx.paths.session_dir(str(holder_sid))
          hb_path  = sess_dir / "body-heartbeat"

test_body_heartbeat_writer.py pins the WRITER; test_claim_worker_vs_worker.py
pins the READER but SEEDS the file itself. So neither ever runs the tick and
then drives the claim. If either derivation drifts — a rename of the filename
literal, a change to session_dir, a SESSIONS_DIRNAME flip — the writer keeps
writing to X, the reader keeps reading Y, BOTH suites stay green, and the claim
CAS silently returns to its pre-fix behaviour of reading every live worker Body
as dormant. That is guard-1943 exactly: a green suite certifies the FUNCTION,
never the WIRING.

WHY A TEST AND NOT A SHARED HELPER. The goal preferred collapsing the two
derivations to one helper "if a shared resolver already exists". It does not,
and both sites document why they deliberately avoid the nearest candidate:
  - the writer is "deliberately NOT agent_session_dir(), which re-derives from
    PROJECT_ROOT and would ignore the sanctioned _AGENT_DIR_OVERRIDE test seam
    that $AGENT_DIR honors" (heartbeat-tick.sh:107-113);
  - the reader keeps its bound-agent arm "byte-identical to the pre-g-306-148
    path rather than folded into the general form" because ctx.paths.agent is
    INJECTED into AgentPaths, not derived from agents_root, "so the two are
    equal by convention and not by construction"
    (aspirations_write.py:3962-3967).
Both rationales are load-bearing and measured, and the two halves are in
different languages, so a single shared resolver is not available without a
generated constant. The DIRECTORY derivation is already constant-routed on both
sides (SESSIONS_DIRNAME in each). What remains genuinely duplicated is the
filename literal, in exactly two executable places. A join test is the
proportionate remedy: it fails the moment either half moves.

WHAT THESE TESTS PIN:
  1. running the REAL heartbeat-tick.sh for a bound worker Body makes the REAL
     claim endpoint refuse a takeover of that SID (409). Writer and reader
     resolve the same path.
  2. the NEGATIVE CONTROL — identical setup with the tick NOT run yields 200.
     Without this, assertion 1 could pass on some unrelated refusal path and
     the test would prove nothing about the join.

THE TEST DELIBERATELY DOES NOT DERIVE THE HEARTBEAT PATH. Writing
`agent_dir / "sessions" / SID / "body-heartbeat"` here would add a THIRD
derivation and could agree with the writer while both disagree with the reader.
Pass/fail depends only on the two production halves agreeing. The path is
touched only to build a diagnostic on failure, and even then by SEARCHING
(rglob) rather than by reconstructing it.

HERMETIC: STORAGE_BACKEND=local (guard-955). DaemonFixture binds an in-process
daemon in a tmp project root, so this file needs no `daemon_integration` marker
and is safe to run beside a live own-cloud daemon (guard-672) — the same basis
on which test_claim_worker_vs_worker.py runs unmarked.

guard-2484 (BOTH seams): the agent-dir seam is the PUBLIC env var
MIND_AGENT_DIR — injecting the internal _AGENT_DIR_OVERRIDE is clobbered by
_paths.sh and silently resolves the REAL agent dir. And that seam does NOT
cover writes keyed on the agent NAME: heartbeat-tick's team-state write lands
in the real world regardless of MIND_AGENT_DIR and MIND_WORLD, so a synthetic
name would create a permanent phantom shard and a partner's name would forge
their liveness. This file therefore drives the tick as the BOUND agent, whose
row the script legitimately advances every iteration — the side effect is
identical to a normal tick.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _daemon_fixture import DaemonFixture  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402

# parents: [0]=tests [1]=scripts [2]=core [3]=repo root.
REPO = Path(__file__).resolve().parents[3]
TICK = str(REPO / "core" / "scripts" / "heartbeat-tick.sh")

GOAL_ID = "g-300-03"
# The holder is a WORKER Body — deliberately not the reducer, so the claim
# reaches the per-Body branch instead of the pre-existing runner check.
HOLDER_SID = "a1a1a1a1-2b2b-3c3c-4d4d-a1a1a1a1a1a1"
CLAIMER_SID = "b2b2b2b2-3c3c-4d4d-5e5e-b2b2b2b2b2b2"
REDUCER_SID = "c3c3c3c3-4d4d-5e5e-6f6f-c3c3c3c3c3c3"
STALE_MINUTES = 60

# The BOUND agent, never a literal and never synthetic — see guard-2484 in the
# module docstring. No fallback: an unbound run must fail loudly rather than
# forge a named agent's liveness in the shared store.
AGENT = os.environ.get("MIND_AGENT")
if not AGENT:
    import pytest

    pytest.skip(
        "MIND_AGENT unset — refusing to run unbound. This test drives the real "
        "heartbeat-tick.sh, whose team-state write is keyed on the agent NAME "
        "and lands in the SHARED world regardless of MIND_AGENT_DIR/MIND_WORLD "
        "(guard-2484). Defaulting to a literal name here would forge a "
        "partner-visible liveness heartbeat.",
        allow_module_level=True,
    )


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _make_world(tmp: Path) -> Path:
    """World whose single goal is already claimed by AGENT from HOLDER_SID."""
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": GOAL_ID, "title": "Body heartbeat path-parity goal",
        "description": "Exercises the writer->reader join for the per-Body heartbeat",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
        "claimed_by": AGENT, "claimed_at": _now(),
        "claimed_by_sid": HOLDER_SID,
    }
    asp = {
        "id": "asp-300", "title": "body heartbeat path parity",
        "motivation": "Test the per-Body heartbeat writer/reader join",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [goal],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _seed_reducer(project_root: Path, running_sid: str) -> Path:
    """running-session-id PRESENT and naming a THIRD session, so the holder is
    not the reducer and the claim reaches the per-Body liveness branch.

    Also seeds stale_minutes: without it the lookup returns None and the probe
    fail-opens BEFORE the branch under test, giving a green test that proves
    nothing (the trap test_claim_cross_box_holder.py documents).

    Returns the agent dir, which is the MIND_AGENT_DIR seam value.
    """
    cfg = project_root / "core" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "aspirations.yaml").write_text(
        f"runner_heartbeat:\n  stale_minutes: {STALE_MINUTES}\n",
        encoding="utf-8")

    adir = project_root / "agents" / AGENT
    sess = adir / "session"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "running-session-id").write_text(running_sid, encoding="utf-8")
    (sess / "runner-heartbeat").write_text("", encoding="utf-8")
    # Seeded for shape parity with a real session dir, NOT because the tick reads
    # it. session-state-get.sh is IRREDUCIBLY LOCAL (PROJECT_ROOT from $0/../..)
    # and honors no seam but MIND_AGENT, and _run_tick passes the REAL bound
    # agent name with cwd=REPO -- so the state gate reads the LIVE box's
    # agents/<agent>/session/agent-state, never this file. An earlier version of
    # this comment said "RUNNING is seeded so the tick also exits 0", which is
    # false: on an IDLE box the tick exits 2 no matter what is written here.
    # The assertions survive that regardless, and THAT is why the file is safe:
    # the per-Body write sits ABOVE the gate () and rc is never
    # asserted -- it is only interpolated into the 409 diagnostic below.
    # MEASURED in a staged tmp PROJECT_ROOT (, zeta, cc-02):
    #   IDLE    -> rc=2, body-heartbeat PRESENT, runner-heartbeat absent
    #   RUNNING -> rc=0, body-heartbeat PRESENT, runner-heartbeat PRESENT
    # Adding `assert r.returncode == 0` would make this file's colour track the
    # MACHINE rather than the diff (rb-6740, guard-2693). Do not add it.
    (sess / "agent-state").write_text("RUNNING", encoding="utf-8")
    return adir


def _make_body_session_dir(agent_dir: Path, sid: str) -> None:
    """Create the session dir the way /start does.

    The writer is guarded on the dir ALREADY EXISTING and never mkdir -p's it
    (path-resolution.md L1 refuses inventing a dir for an unbound SID). Without
    this the tick correctly writes nothing and assertion 1 would fail for a
    reason that has nothing to do with path parity.
    """
    (agent_dir / "sessions" / sid).mkdir(parents=True, exist_ok=True)


def _run_tick(agent_dir: Path, sid: str) -> subprocess.CompletedProcess:
    """Drive the REAL writer. MIND_AGENT_DIR is the PUBLIC seam name; the
    internal _AGENT_DIR_OVERRIDE is clobbered by _paths.sh and would resolve
    the live agent dir instead (guard-2484)."""
    env = os.environ.copy()
    env["STORAGE_BACKEND"] = "local"
    env["MIND_AGENT"] = AGENT
    env["MIND_AGENT_DIR"] = str(agent_dir)
    env["MIND_SID"] = sid
    return subprocess.run(bash_cmd(TICK), cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=180)


def _claim(port: int, sid: str) -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/claim"
           f"?id={GOAL_ID}&agent={AGENT}&sid={sid}")
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("X-Mind-Agent", AGENT)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _found_heartbeats(project_root: Path) -> list[str]:
    """Diagnostic ONLY, and deliberately a SEARCH rather than a derivation.

    Reconstructing the expected path here would be a third copy that could
    agree with the writer while both disagree with the reader — the exact
    failure this file exists to catch. Reporting what actually exists tells a
    failing run whether the writer wrote nothing or wrote somewhere else.

    The trailing `*` is load-bearing and was added after measuring: with the
    exact name, drifting the WRITER's literal reported `[]` — identical to
    "wrote nothing" — which is precisely the distinction this diagnostic claims
    to make, and the renamed-literal case is one of the two drifts the file
    exists to catch. The glob now surfaces the drifted name itself.
    """
    try:
        return [str(p.relative_to(project_root))
                for p in project_root.rglob("body-heartbeat*")]
    except Exception:
        return []


# --- 1. THE JOIN: real writer -> real reader -> takeover refused ------------
def test_real_tick_makes_claim_endpoint_refuse_takeover():
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            adir = _seed_reducer(df.project_root, REDUCER_SID)
            _make_body_session_dir(adir, HOLDER_SID)

            r = _run_tick(adir, HOLDER_SID)

            code, body = _claim(df.port, CLAIMER_SID)
            assert code == 409, (
                "the REAL heartbeat-tick.sh ran for the holder Body, so the "
                "REAL claim endpoint must refuse a takeover of that SID. A 200 "
                "here means the writer and the reader no longer resolve the "
                "same path — both halves' own suites stay green when that "
                "happens (guard-1943), which is why this join test exists. "
                f"tick rc={r.returncode} stderr={r.stderr[-300:]} "
                f"heartbeats found under project_root="
                f"{_found_heartbeats(df.project_root)} claim={code}: {body}")
            assert "same_agent_other_session" in body, body


# --- 2. NEGATIVE CONTROL: no tick -> takeover permitted ---------------------
def test_without_tick_takeover_is_permitted():
    """Identical setup, tick NOT run.

    This is what makes assertion 1 evidence about the JOIN rather than about
    the claim endpoint's general willingness to refuse. If this also returned
    409, test 1 would pass with the writer disconnected entirely.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=AGENT) as df:
            adir = _seed_reducer(df.project_root, REDUCER_SID)
            # Session dir present, heartbeat absent — a Body predating the
            # writer. Absence is ambiguous and must never ground a refusal.
            _make_body_session_dir(adir, HOLDER_SID)

            code, body = _claim(df.port, CLAIMER_SID)
            assert code == 200, (
                "with no per-Body heartbeat the takeover must proceed, or a "
                "transient crash becomes a permanent wedge. If this is 409 the "
                "refusal in test 1 is NOT attributable to the tick, and that "
                f"test proves nothing about path parity; got {code}: {body}")
