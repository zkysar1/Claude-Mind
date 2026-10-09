"""test_worker_release_keeps_claim.py -- a worker Body never releases a claim it
does not hold (g-115-12306).

The defect. A worker Body releases at the end of every unit it did not finish
(worker-loop step 4a), and release() cleared whatever claim stood on the goal
by then. Measured on g-335-1718: zc-02 took the goal over at
2026-10-08T16:41:44 (the sanctioned 24 h hold-cap takeover of a stuck zc-06),
zc-06's unit kept running, and its end-of-unit release at 2026-10-09T05:01:34
cleared the claim zc-02 held.

The fix has two halves and this file pins both:

  * the endpoint: a release that says it comes from a worker
    (X-Mind-Body-Role) and from a session that does not hold the claim is
    SKIPPED. Nothing is written, and the answer carries `"released": false`.
  * the wrapper: aspirations-release.sh forwards BODY_ROLE as that header,
    and after a skipped release clears only the worker's own records (its
    in_flight_bodies row and its body-keyed checkpoint), never the
    agent-keyed in_flight row, which can be the holder's.

What must NOT change is pinned beside it (guard-4166): the worker's release of
its OWN claim still releases (the positive control), and a release with no
role or the reducer role -- the recovery sweeps' shape -- still clears a claim
it does not hold, because that is how a dead session's claim gets cleared
(g-115-3176).

The endpoint tests run against a real in-process daemon. The wrapper tests are
hermetic: a copy of the real wrapper runs beside a stub _runtime.sh and stub
team-state scripts that record what they were asked to do, the same staging
test_clear_in_flight_call_site_scoping.py uses. Real bytes, no network.

guard-1165: no module-level os.environ mutation, no sys.modules stubs.
Run: STORAGE_BACKEND=local python -m pytest \
     core/scripts/tests/test_worker_release_keeps_claim.py -q
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402  (guard-580: explicit bash binary)
from _daemon_fixture import DaemonFixture  # noqa: E402

GOAL_ID = "g-300-01"
HOLDER_SID = "11111111-aaaa-bbbb-cccc-111111111111"
WORKER_SID = "22222222-dddd-eeee-ffff-222222222222"
CLAIMED_AT = "2026-10-08T16:41:44"


# ── endpoint ─────────────────────────────────────────────────────────────────

def _make_world(tmp: Path, *, claimed_by: str, claimed_by_sid: str | None) -> Path:
    """One in-progress world goal, claimed by `claimed_by` (and sid, when given)."""
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": GOAL_ID, "title": "Worker release goal",
        "description": "Exercises a worker release over another session's claim",
        "status": "in-progress", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
        "claimed_by": claimed_by, "claimed_at": CLAIMED_AT,
        "last_modified": CLAIMED_AT,
    }
    if claimed_by_sid is not None:
        goal["claimed_by_sid"] = claimed_by_sid
    asp = {
        "id": "asp-300", "title": "worker release regression",
        "motivation": "Test release() for a worker that does not hold the claim",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [goal],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _goal(world: Path) -> dict:
    for line in (world / "aspirations.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            for g in json.loads(line).get("goals", []):
                if g.get("id") == GOAL_ID:
                    return g
    raise AssertionError(f"{GOAL_ID} is missing from the store")


def _release(port: int, *, sid: str, role: str | None) -> tuple[int, dict]:
    """POST a release the way aspirations-release.sh does for a worker's step 4a."""
    query = urllib.parse.urlencode({
        "id": GOAL_ID, "source": "world", "sid": sid,
        "reason": "unit ended, work remains", "reason_kind": "progress",
    })
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/aspirations/release?{query}",
        data=b"", method="POST")
    req.add_header("X-Mind-Agent", "alpha")
    req.add_header("X-Mind-Override-All", "test-fixture")
    if role is not None:
        req.add_header("X-Mind-Body-Role", role)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"http_error": e.read().decode("utf-8")}


@pytest.mark.parametrize("holder,holder_sid", [
    ("alpha", HOLDER_SID),   # the  shape: another session of this agent
    ("bravo", HOLDER_SID),   # another agent's claim is never this worker's either
], ids=["other-session", "other-agent"])
def test_worker_release_over_a_claim_it_does_not_hold_is_skipped(holder, holder_sid):
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by=holder, claimed_by_sid=holder_sid)
        with DaemonFixture(world, agent="alpha") as df:
            # Read after the daemon is up, so only the release can explain a change.
            before = (world / "aspirations.jsonl").read_bytes()
            code, resp = _release(df.port, sid=WORKER_SID, role="worker")
            after = (world / "aspirations.jsonl").read_bytes()
        assert code == 200, resp
        assert resp.get("released") is False, resp
        assert any("SKIPPED" in w for w in resp.get("warnings") or []), resp
        # Nothing was written: the claim, its sid, the status, last_modified and
        # release_negatives are exactly as the holder left them.
        assert after == before, "a skipped release must not write the store"
        g = _goal(world)
        assert (g["claimed_by"], g.get("claimed_by_sid")) == (holder, holder_sid)
        assert g["status"] == "in-progress"


def test_worker_release_of_its_own_claim_still_releases():
    # Positive control: the worker that holds the claim gives it back as before.
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha", claimed_by_sid=WORKER_SID)
        with DaemonFixture(world, agent="alpha") as df:
            code, resp = _release(df.port, sid=WORKER_SID, role="worker")
        assert code == 200, resp
        assert "released" not in resp, resp
        g = _goal(world)
        assert "claimed_by" not in g and "claimed_by_sid" not in g, g
        assert g["status"] == "pending"
        assert g["release_negatives"][-1]["kind"] == "progress"


@pytest.mark.parametrize("role", [None, "reducer"], ids=["no-role", "reducer"])
def test_a_non_worker_release_still_clears_a_claim_it_does_not_hold(role):
    # Scope control: stranded-claim-sweep posts with no role header and the
    # abandoned-claim lane runs on the reducer. Both must still clear another
    # session's claim; that path stays warn-only ().
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha", claimed_by_sid=HOLDER_SID)
        with DaemonFixture(world, agent="alpha") as df:
            code, resp = _release(df.port, sid=WORKER_SID, role=role)
        assert code == 200, resp
        assert "released" not in resp, resp
        g = _goal(world)
        assert "claimed_by" not in g, g
        assert g["status"] == "pending"


def test_worker_release_of_a_claim_with_no_sid_still_releases():
    # Absent evidence keeps the old behaviour: a same-agent claim that carries
    # no sid could be this worker's own, so the release runs as it always has.
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), claimed_by="alpha", claimed_by_sid=None)
        with DaemonFixture(world, agent="alpha") as df:
            code, resp = _release(df.port, sid=WORKER_SID, role="worker")
        assert code == 200, resp
        assert "released" not in resp, resp
        assert "claimed_by" not in _goal(world)


# ── wrapper ──────────────────────────────────────────────────────────────────

# Stub _runtime.sh: records the query and the headers rt_call was handed, then
# answers with a canned response. Test data travels by env, never into shell
# source (guard-165).
STUB_RUNTIME = """
rt_url_encode() { printf '%s' "$1"; }
rt_python_launcher() { printf '%s' "$RT_PY"; }
rt_call() {
    local q="" h=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --query) q="$2"; shift 2;;
            --header) h="${h}$2
"; shift 2;;
            *) shift;;
        esac
    done
    printf '%s' "$q" > "$QUERY_SINK"
    printf '%s' "$h" > "$HEADER_SINK"
    printf '%s' "$RESPONSE_JSON"
    return 0
}
rt_try_autospawn() { return 1; }
rt_no_daemon_error() { echo "no daemon: $1" >&2; exit 1; }
"""

# Each stub team-state script logs its name and arguments, one call per line.
STUB_CALL = 'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$CALLS_SINK"\n'
STUBS = {
    "team-state-clear-in-flight.sh": STUB_CALL,
    "team-state-clear-body-row.sh": STUB_CALL,
    "loop-state-save.sh": STUB_CALL,
    # The worker's own in_flight_bodies row names the goal it just ran.
    "team-state-read.sh": STUB_CALL + 'printf \'"%s"\\n\' "$BODY_GOAL"\n',
    "context-budget-banner.sh": 'echo "CTX: test"\n',
}

KEPT = json.dumps({"ok": True, "goal": {"id": GOAL_ID}, "had_claim": True,
                   "released": False, "warnings": ["release SKIPPED"]})
RELEASED = json.dumps({"ok": True, "goal": {"id": GOAL_ID}, "had_claim": True,
                       "warnings": None})


def _stage(tmp: Path) -> Path:
    """Copy the REAL wrapper and its sourced helpers beside the stubs."""
    scripts = tmp / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("aspirations-release.sh", "_goal-arg-normalize.sh", "_argv_strict.sh"):
        shutil.copy(CORE_SCRIPTS / name, scripts / name)
    (scripts / "_runtime.sh").write_text(STUB_RUNTIME, encoding="utf-8")
    for name, body in STUBS.items():
        (scripts / name).write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    return scripts / "aspirations-release.sh"


def _run_wrapper(tmp: Path, *, response: str, body_role: str | None):
    """Run the worker-loop's literal step-4a call (guard-920) and return what it did."""
    script = _stage(tmp)
    sinks = {k: tmp / f"{k}.txt" for k in ("query", "headers", "calls")}
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "RT_PY": sys.executable,
        "RESPONSE_JSON": response,
        "QUERY_SINK": str(sinks["query"]),
        "HEADER_SINK": str(sinks["headers"]),
        "CALLS_SINK": str(sinks["calls"]),
        "BODY_GOAL": GOAL_ID,
        "MIND_AGENT": "alpha",
        "MIND_SID": WORKER_SID,
    }
    if body_role is not None:
        env["BODY_ROLE"] = body_role
    proc = subprocess.run(
        [BASH, script.as_posix(), GOAL_ID, "--source", "world",
         "--reason", "unit ended, work remains", "--reason-kind", "progress"],
        capture_output=True, text=True, env=env, timeout=60)
    read = {k: (p.read_text(encoding="utf-8") if p.exists() else "") for k, p in sinks.items()}
    return proc, read


def test_the_wrapper_forwards_the_worker_role_as_a_header(tmp_path):
    proc, seen = _run_wrapper(tmp_path, response=RELEASED, body_role="worker")
    assert proc.returncode == 0, proc.stderr
    assert "X-Mind-Body-Role: worker" in seen["headers"].splitlines(), seen
    assert f"sid={WORKER_SID}" in seen["query"], seen


def test_without_a_role_the_wrapper_sends_no_role_header(tmp_path):
    # Every caller that is not a Body sends exactly what it sent before.
    proc, seen = _run_wrapper(tmp_path, response=RELEASED, body_role=None)
    assert proc.returncode == 0, proc.stderr
    assert "X-Mind-Body-Role" not in seen["headers"], seen


def test_a_skipped_release_clears_only_the_workers_own_records(tmp_path):
    proc, seen = _run_wrapper(tmp_path, response=KEPT, body_role="worker")
    assert proc.returncode == 0, proc.stderr
    calls = seen["calls"].splitlines()
    # The agent-keyed row can be the holder's, so it is left alone...
    assert not [c for c in calls if c.startswith("team-state-clear-in-flight.sh")], calls
    # ...while the worker's own body row and its checkpoint are still cleared.
    assert f"team-state-clear-body-row.sh --agent alpha --sid {WORKER_SID}" in calls, calls
    assert f"loop-state-save.sh clear --if-goal {GOAL_ID}" in calls, calls


def test_a_completed_release_still_clears_the_agent_row_too(tmp_path):
    # Positive control: an answer with no `released` key is a release that ran,
    # and every surface is cleared exactly as before.
    proc, seen = _run_wrapper(tmp_path, response=RELEASED, body_role="worker")
    assert proc.returncode == 0, proc.stderr
    calls = seen["calls"].splitlines()
    assert f"team-state-clear-in-flight.sh --agent alpha --if-goal {GOAL_ID}" in calls, calls
    assert f"team-state-clear-body-row.sh --agent alpha --sid {WORKER_SID}" in calls, calls
    assert f"loop-state-save.sh clear --if-goal {GOAL_ID}" in calls, calls
