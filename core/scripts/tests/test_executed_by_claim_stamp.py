"""executed_by is stamped at CLAIM time and survives completion — .

THE DEFECT. All three writers of `completed_by` derive it from the CALLER, not
from the claim record: core/scripts/aspirations.py (env MIND_AGENT),
aspirations_write.py `update-goal` (_agent_name(ctx)), and the complete-by
endpoint (unconditional — it clobbers a correct prior value). So the field
records who ISSUED THE CLOSE. That coincides with the executor on the normal
path and diverges on every sweep, review, bulk close, and Mind/Body split.

WHY IT WAS UNFALSIFIABLE, and why this file exists rather than an audit. Of
4178 completed world goals, 3872 carry completed_by and ZERO also carry
claimed_by, because guard-151 pops the claim on every terminal transition. So
the obvious audit — "flag goals where completed_by != claimed_by" — returns 0,
and will return 0 forever on every box no matter how wrong the field is. The
SID pair is popped identically (633 carry completed_by_sid, 0 carry both), so
there is no SID-shaped way out either.

THE FIX SHAPE IS CONSTRAINED, and two of the constraints are what these tests
actually protect:
  - Do NOT repair the pop. guard-151 is a designed, convention-backed,
    anchor-commented invariant. `test_guard_151_pop_still_intact` fails if a
    future "fix" preserves claimed_by through completion to make the comparison
    work — that would trade this defect for a worse one.
  - Do NOT stamp from the caller. `executed_by` is written inside the claim,
    under the same lock, never by a caller-side follow-on update-goal
    (guard-2793 / guard-2309).

THE ACCEPT AXIS — read this before changing any assertion below. A test that
merely shows executed_by EXISTS, or is non-null, or equals completed_by on a
normal close, is satisfied by a field stamped from the caller at CLOSE time,
i.e. by a change that fixes nothing. The discriminating case is DIVERGENCE:
agent X claims, agent Y completes, and the record must read executed_by=X AND
completed_by=Y. `test_executed_by_survives_a_foreign_close` is that test and is
the load-bearing one; the others guard its flanks.

MUTATION-PROVEN, not assumed (guard-1099 — this class has been re-authored
inert three times in one session). Moving the stamp from the claim path to the
close path makes `test_executed_by_survives_a_foreign_close` fail on its
executed_by assertion, because executed_by would then equal the closer. Deleting
the stamp entirely fails the same assertion on a None. Both were run.

MIND/BODY IS THE POINT, NOT AN EDGE CASE. The ordinary division has a WORKER
execute and a REDUCER close the same goal, so completed_by != executed_by is the
CORRECT steady state, not an alarm. Any audit built on this field must treat
that shape as normal and flag only a closer that never touched the goal. An
audit firing on every reducer close is worse than no audit.

HERMETIC BY CONSTRUCTION: STORAGE_BACKEND=local (guard-955) — a tmp world plus a
tmp project_root, no S3, no creds, no network. Production arg shape preserved
(guard-920): every case drives the real HTTP endpoints over the wire with the
same query shape aspirations-claim.sh and aspirations-complete-by.sh send,
rather than calling helpers in isolation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _daemon_fixture import DaemonFixture  # noqa: E402

EXECUTOR = "alpha"          # claims and does the work
CLOSER = "bravo"            # issues the close, never claimed
GOAL_ID = "g-900-01"
ASP_ID = "asp-900"
EXECUTOR_SID = "aaaaaaaa-1111-2222-3333-aaaaaaaaaaaa"
CLOSER_SID = "bbbbbbbb-4444-5555-6666-bbbbbbbbbbbb"


def _make_world(tmp: Path) -> Path:
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": GOAL_ID,
        "title": "a goal one agent executes and another closes",
        "description": "Mind/Body steady state: worker executes, reducer closes",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "test", "participants": ["agent"],
        "recurring": False,
    }
    asp = {
        "id": ASP_ID, "title": "executed-by fixture",
        "motivation": "Pin the claim-time executor stamp",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [goal],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _post(port: int, route: str, query: str, agent: str) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}{route}?{query}"
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("X-Mind-Agent", agent)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _claim(port: int, agent: str = EXECUTOR, sid: str = EXECUTOR_SID):
    return _post(port, "/v1/aspirations/claim",
                 f"id={GOAL_ID}&agent={agent}&sid={sid}&source=world", agent)


def _complete_by(port: int, agent: str = CLOSER, sid: str = CLOSER_SID):
    return _post(port, "/v1/aspirations/complete-by",
                 f"goal_id={GOAL_ID}&source=world&agent_name={agent}&sid={sid}",
                 agent)


def _reset_to_pending(world: Path) -> None:
    """Simulate the recurrence reset: terminal -> pending, claim fields cleared.

    Deliberately does NOT clear completed_by — that is the point of scope item 2
    (no code path anywhere clears it), and the assertion downstream depends on
    the stale value still being present.
    """
    path = world / "aspirations.jsonl"
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            asp = json.loads(line)
            for g in (asp.get("goals") or []):
                if g.get("id") == GOAL_ID:
                    g["status"] = "pending"
                    for k in ("claimed_by", "claimed_at", "claimed_by_sid"):
                        g.pop(k, None)
            rows.append(asp)
    with open(path, "w", encoding="utf-8") as f:
        for asp in rows:
            f.write(json.dumps(asp, ensure_ascii=False) + "\n")


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


# --- 1. THE ACCEPT AXIS (load-bearing) -------------------------------------
def test_executed_by_survives_a_foreign_close():
    """X claims, Y closes: executed_by=X, completed_by=Y.

    This is the ONLY assertion in the file that a close-time stamp cannot
    satisfy. Everything else here would stay green against the unfixed source.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=EXECUTOR) as df:
            code, body = _claim(df.port)
            assert code == 200, f"claim failed {code}: {body}"

            code, body = _complete_by(df.port, agent=CLOSER)
            assert code == 200, f"complete-by failed {code}: {body}"

            g = _goal(world)
            assert g is not None, "goal vanished from the queue"
            assert g.get("executed_by") == EXECUTOR, (
                "executed_by must name the agent that CLAIMED the goal, not the "
                "one that issued the close. Got %r (closer=%r). If this equals "
                "the closer, the stamp is on the close path and records exactly "
                "the caller-derived value completed_by already records — the "
                "change fixes nothing (g-115-5365)."
                % (g.get("executed_by"), CLOSER))
            assert g.get("completed_by") == CLOSER, (
                "completed_by must still record the CLOSER — this change adds a "
                "sibling field, it does not alter completed_by (rb-2148): %r" % g)
            assert g.get("executed_by") != g.get("completed_by"), (
                "the divergent case is the whole point: a worker executes and a "
                "reducer closes, and the record must be able to SAY so")


# --- 2. THE FIELD SURVIVES THE POP (why a sibling was needed) --------------
def test_guard_151_pop_still_intact():
    """claimed_by/claimed_at/claimed_by_sid are still popped on completion.

    Fails if someone "fixes" the audit by preserving the claim through
    completion instead of adding a sibling. That would violate guard-151 at two
    anchor-commented sites and break the aspirations.md Rule 3 invariant.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=EXECUTOR) as df:
            assert _claim(df.port)[0] == 200
            mid = _goal(world)
            assert mid.get("claimed_by") == EXECUTOR, mid
            assert mid.get("executed_by") == EXECUTOR, (
                "executed_by must be stamped by the CLAIM itself, visible "
                "before any close happens: %r" % mid)

            assert _complete_by(df.port)[0] == 200
            g = _goal(world)
            for popped in ("claimed_by", "claimed_at", "claimed_by_sid"):
                assert g.get(popped) is None, (
                    "guard-151: %s must be popped on the terminal transition. "
                    "Preserving it to make the audit work is the wrong fix — "
                    "that is precisely why executed_by is a SIBLING field: %r"
                    % (popped, g))
            assert g.get("executed_by") == EXECUTOR, (
                "executed_by must SURVIVE the pop — it is the replacement for "
                "the comparison field guard-151 removes: %r" % g)


# --- 3. THE SID HALF -------------------------------------------------------
def test_executed_by_sid_stamped_and_survives():
    """The session half survives too — completed_by_sid is popped identically.

    Measured: 633 completed goals carry completed_by_sid and 0 carry both sids,
    so a reader reaching for the SID pair as a way around the name problem finds
    the same wall. executed_by_sid is what actually survives.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=EXECUTOR) as df:
            assert _claim(df.port)[0] == 200
            assert _complete_by(df.port)[0] == 200
            g = _goal(world)
            assert g.get("executed_by_sid") == EXECUTOR_SID, (
                "the EXECUTING session must survive completion: %r" % g)
            assert g.get("claimed_by_sid") is None, (
                "claimed_by_sid is still popped (guard-151) — that is the "
                "reason executed_by_sid has to exist: %r" % g)


# --- 4. RECURRENCE: a re-claim re-stamps (scope item 2) --------------------
def test_reclaim_overwrites_executed_by():
    """A second claim by a different agent re-stamps executed_by.

    THIS TEST PINS THE ONE JUDGMENT CALL IN THE CHANGE. `executed_by` is
    assigned unconditionally, mirroring `claimed_by`, and deliberately NOT with
    `setdefault` like the `started` marker on the adjacent line. Flip it to
    setdefault and this test goes red.

    Why unconditional is right: `started` answers "when was this FIRST
    attempted" and must keep the original; `executed_by` answers "who did the
    work", and the goal only becomes re-claimable after the previous holder went
    dormant or the goal recurred. setdefault would durably record an ABANDONER,
    or — on a recurring goal — pin cycle 1's executor onto every later cycle.

    That second case is scope item 2 of g-115-5365: `completed_by` is never
    cleared by any code path (a grep for a pop/del/None-assign across
    core/scripts/*.py and mind_api/src/endpoints/*.py returns nothing), so a
    recurring goal carries its previous closer's name forward indefinitely —
    measured live on g-335-09, completed_by=foxtrot while a DIFFERENT agent held
    it in-progress. executed_by does not inherit that bug, because every cycle's
    claim re-stamps it. The staleness of completed_by is unchanged, but it is
    now DETECTABLE: a fresh executed_by beside a stale completed_by is visible,
    where before there was nothing to compare against.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=EXECUTOR) as df:
            assert _claim(df.port, agent=EXECUTOR, sid=EXECUTOR_SID)[0] == 200
            assert _goal(world).get("executed_by") == EXECUTOR

            # Complete, then RESET to pending — that is what the recurrence
            # machinery actually does, and it is load-bearing here: the daemon
            # refuses a claim on a terminal goal (409 goal_terminal), so a
            # complete-then-reclaim sequence never reaches the assertion below.
            # Modelling the cycle as "complete, immediately re-claim" makes this
            # test fail in SETUP, which under a mutation run is indistinguishable
            # from the mutation being caught — the per-assertion trap this file's
            # header warns about, hit while writing the file itself.
            assert _complete_by(df.port)[0] == 200
            _reset_to_pending(world)
            code, body = _claim(df.port, agent=CLOSER, sid=CLOSER_SID)
            assert code == 200, f"re-claim failed {code}: {body}"

            g = _goal(world)
            assert g.get("executed_by") == CLOSER, (
                "a re-claim must re-stamp executed_by to the agent that is "
                "actually going to do this cycle's work. If this is still %r, "
                "the stamp was written with setdefault and now pins the FIRST "
                "executor onto every later cycle — the same never-cleared bug "
                "completed_by has (g-115-5365 scope item 2): %r"
                % (EXECUTOR, g))
            assert g.get("executed_by_sid") == CLOSER_SID, (
                "the session half must re-stamp with it: %r" % g)


# --- 4b. THE SID PAIR NEVER DISAGREES --------------------------------------
def test_reclaim_without_sid_clears_executed_by_sid():
    """A no-sid reclaim must CLEAR executed_by_sid, not leave the old holder's.

    Found by /fresh-eyes-code on this goal's own diff
    (echo-fec-executed-by-sid-can-go-stale-202608081700), after the first four
    tests were already green — none of them exercises this path, because
    test_reclaim_overwrites_executed_by passes a sid on BOTH claims.

    The defect was a copied write-guard. `claimed_by_sid` above is written only
    `if claim_sid`, and its comment justifies that: "never clobber a prior SID
    with a None from a caller that does not send it." That reasoning is sound
    for `claimed_by_sid` ONLY because guard-151 pops the whole claim triple at
    every terminal transition, so a stale pair lives at most until close.
    `executed_by` is designed to SURVIVE completion, so the identical shape
    makes the divergence PERMANENT — the new agent's name sitting beside the
    previous holder's sid, forever, in the one field pair that exists to make
    attribution auditable. A permanently contradictory pair is worse than a
    missing one: absent reads honestly as "not recorded", stale reads as a
    confident wrong answer. (guard-3116: derive the write-guard from the
    question the field answers, never by mirroring the adjacent field.)

    Reachability is narrow and real: the endpoint refuses a sid-less claim with
    400 missing_claim_sid unless `_no_sid_bypass()` returns a justification,
    which is what MIND_CLAIM_ALLOW_NO_SID supplies here — the same hatch the
    endpoint deliberately retains for legacy and un-hooked callers.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=EXECUTOR) as df:
            assert _claim(df.port, agent=EXECUTOR, sid=EXECUTOR_SID)[0] == 200
            assert _goal(world).get("executed_by_sid") == EXECUTOR_SID

            assert _complete_by(df.port)[0] == 200
            _reset_to_pending(world)

            prev = os.environ.get("MIND_CLAIM_ALLOW_NO_SID")
            os.environ["MIND_CLAIM_ALLOW_NO_SID"] = (
                "test: exercising the legacy sid-less claim shape")
            try:
                # sid="" -> ctx.query.get("sid") is "" -> .strip() or None -> None
                code, body = _claim(df.port, agent=CLOSER, sid="")
            finally:
                if prev is None:
                    os.environ.pop("MIND_CLAIM_ALLOW_NO_SID", None)
                else:
                    os.environ["MIND_CLAIM_ALLOW_NO_SID"] = prev

            assert code == 200, f"no-sid re-claim failed {code}: {body}"

            g = _goal(world)
            assert g.get("executed_by") == CLOSER, (
                "the agent half still re-stamps on a no-sid claim: %r" % g)
            assert "executed_by_sid" not in g, (
                "executed_by_sid must be CLEARED, not left holding %r's sid "
                "while executed_by says %r. Leaving it is the permanent "
                "contradictory pair this test exists to forbid — and it is what "
                "you get by copying the `if claim_sid:` guard from "
                "claimed_by_sid without an else branch: %r"
                % (EXECUTOR, CLOSER, g))


# --- 5. NO CALLER-SIDE WRITE (guard-2793 / guard-2309) ---------------------
def test_stamp_lands_from_the_claim_alone():
    """A bare claim, with no follow-on write of any kind, is sufficient.

    guard-2793 forbids chaining aspirations-claim.sh with a follow-on goal
    write, so the stamp cannot be a caller-side update-goal. This test issues
    ONE request and nothing else.
    """
    os.environ["STORAGE_BACKEND"] = "local"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent=EXECUTOR) as df:
            assert _claim(df.port)[0] == 200
            g = _goal(world)
            assert g.get("executed_by") == EXECUTOR, (
                "one claim call, no follow-on write, must be enough — the "
                "stamp belongs inside the claim under the same lock: %r" % g)
