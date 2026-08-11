"""Daemon aspirations-retire/release/claim endpoint tests (PR 9b).

Covers:
  retire:
    - Happy path: retire an aspiration -> archived with status=retired
    - Recurring-goals guard blocks without force
    - force=true bypasses recurring guard
    - 404 on missing aspiration
    - Missing asp_id returns 400
    - Unfinished goals produce a warning but do not block
    - Stale blockers cleared from remaining aspirations

  release:
    - Happy path: release a claimed goal -> claimed_by/claimed_at cleared
    - Release unclaimed goal -> still 200 with had_claim=false
    - 404 on missing goal
    - Missing goal_id returns 400
    - Agent-queue goal returns 400 with helpful error

  claim:
    - Happy path: claim a goal -> claimed_by/claimed_at set
    - Already claimed by same agent -> 200 (idempotent)
    - Already claimed by different agent -> 409
    - Cross-lane refused without justification -> 400
    - Cross-lane override accepted with justification + ledger written
    - Lane pin: out-of-lane claim by a pinned agent -> 400 lane_pin_refused
    - Lane pin: in-lane claim, and any claim by an unpinned agent -> 200
    - Lane pin: deleting the registry row auto-lifts the pin (no code change)
    - Lane pin: override accepted + ledger written under gate lane-pin-gate
    - Lane pin: absent registry is fail-open; pin refusal precedes cross-lane
    - 404 on missing goal
    - Missing goal_id returns 400
    - Missing agent returns 400
    - Agent-queue goal returns 400 with helpful error
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest


# Claim POSTs MUST carry a sid: the endpoint refuses sid-less world-goal claims
# (-b), and production always sends one -- aspirations-claim.sh appends
# &sid=$MIND_SID, which bash-agent-inject.py injects into every Bash call. A
# sid-less test call was therefore already diverging from the production arg
# shape (guard-920). Cases that refuse EARLIER than the sid check (missing id,
# missing agent, goal-not-found, agent-queue goal) deliberately omit it -- that
# they still refuse for their own reason is what proves the ordering.
CLAIMER_SID = "66666666-aaaa-bbbb-cccc-666666666666"


def _post(port, path, query, body=None, *, agent="alpha", headers=None):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    data = body if isinstance(body, bytes) else (body.encode("utf-8") if body else None)
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _read_jsonl(path: Path):
    items = []
    if not path.exists():
        return items
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def _seed_aspiration(world: Path, asp):
    path = world / "aspirations.jsonl"
    path.write_text(json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")


def _seed_two_aspirations(world: Path, asp1, asp2):
    path = world / "aspirations.jsonl"
    lines = [json.dumps(a, ensure_ascii=True) for a in [asp1, asp2]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# retire tests
# ---------------------------------------------------------------------------

def _make_asp_all_completed(asp_id="asp-001"):
    return {
        "id": asp_id,
        "title": "Test retire",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "goals": [
            {"id": f"g-{asp_id[4:]}-01", "title": "Done 1", "status": "completed",
             "recurring": False},
            {"id": f"g-{asp_id[4:]}-02", "title": "Done 2", "status": "skipped",
             "recurring": False},
        ],
        "progress": {"completed_goals": 1, "total_goals": 2, "recurring_goals": 0},
    }


def test_retire_happy_path(running_daemon):
    """Retire an aspiration with all goals terminal -> 200, archived."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_all_completed()
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/retire",
                         {"asp_id": "asp-001", "source": "world"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["ok"] is True
    retired_asp = resp["aspiration"]
    assert retired_asp["status"] == "retired"
    assert retired_asp["archived"] is True
    assert retired_asp["completed_at"] is None
    assert "retired_at" in retired_asp

    live = _read_jsonl(world / "aspirations.jsonl")
    assert len(live) == 0

    archive = _read_jsonl(world / "aspirations-archive.jsonl")
    assert len(archive) == 1
    assert archive[0]["id"] == "asp-001"
    assert archive[0]["status"] == "retired"


def test_retire_recurring_goals_blocked(running_daemon):
    """Aspiration with recurring goals -> 400 without force."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Has recurring", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Recurring", "status": "pending",
             "recurring": True, "interval_hours": 24},
        ],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 1},
    }
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/retire", {"asp_id": "asp-001"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "recurring_goals_present"


def test_retire_force_bypasses_recurring_guard(running_daemon):
    """force=true skips recurring guard."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Force retire", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Recurring", "status": "pending",
             "recurring": True, "interval_hours": 24},
        ],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 1},
    }
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/retire",
                         {"asp_id": "asp-001", "force": "true"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["aspiration"]["status"] == "retired"


def test_retire_not_found(running_daemon):
    """Unknown asp_id -> 404."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/retire", {"asp_id": "asp-999"})
    assert status == 404
    resp = json.loads(body)
    assert resp["error"] == "aspiration_not_found"


def test_retire_missing_asp_id(running_daemon):
    """No asp_id -> 400."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/retire", {})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "missing_asp_id"


def test_retire_unfinished_goals_warning(running_daemon):
    """Retire with unfinished goals -> 200 with warning (not blocking)."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Unfinished", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Pending", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/retire", {"asp_id": "asp-001"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    warnings = resp.get("warnings") or []
    assert any("RETIREMENT NOTE" in w for w in warnings)
    assert resp["aspiration"]["status"] == "retired"


def test_retire_clears_stale_blockers(running_daemon):
    """After retiring asp-001, blocked_by refs in asp-002 are cleaned."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp1 = _make_asp_all_completed("asp-001")
    asp2 = {
        "id": "asp-002", "title": "Other", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-002-01", "title": "Blocked one", "status": "blocked",
             "recurring": False, "blocked_by": ["g-001-01"],
             "blocked_since": "2026-01-01"},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_two_aspirations(world, asp1, asp2)

    status, body = _post(port, "/v1/aspirations/retire", {"asp_id": "asp-001"})
    assert status == 200, f"Expected 200, got {status}: {body}"

    live = _read_jsonl(world / "aspirations.jsonl")
    assert len(live) == 1
    assert live[0]["id"] == "asp-002"
    goal = live[0]["goals"][0]
    assert goal["blocked_by"] == []
    assert goal["blocked_since"] is None


# ---------------------------------------------------------------------------
# release tests
# ---------------------------------------------------------------------------

def _make_asp_with_claimed_goal():
    return {
        "id": "asp-001", "title": "Test release", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Claimed goal", "status": "in-progress",
             "recurring": False, "claimed_by": "alpha",
             "claimed_at": "2026-05-10T10:00:00"},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }


def test_release_happy_path(running_daemon):
    """Release a claimed goal -> 200, claimed_by/claimed_at cleared."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_claimed_goal())

    status, body = _post(port, "/v1/aspirations/release",
                         {"id": "g-001-01"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["had_claim"] is True
    goal = resp["goal"]
    assert "claimed_by" not in goal
    assert "claimed_at" not in goal


def test_release_unclaimed_goal(running_daemon):
    """Release an unclaimed goal -> 200 with had_claim=false."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "Test", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Unclaimed", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/release", {"id": "g-001-01"})
    assert status == 200
    resp = json.loads(body)
    assert resp["had_claim"] is False


def test_release_not_found(running_daemon):
    """Unknown goal_id -> 404."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/release", {"id": "g-999-01"})
    assert status == 404
    resp = json.loads(body)
    assert resp["error"] == "goal_not_found"


def test_release_missing_goal_id(running_daemon):
    """No id param -> 400."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/release", {})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "missing_goal_id"


def test_release_agent_queue_goal(running_daemon):
    """Goal in agent queue -> 400 with helpful error."""
    project_root, port = running_daemon
    world = project_root / "world"
    # Seed world with no matching goal
    asp = {
        "id": "asp-001", "title": "World", "status": "active",
        "priority": "LOW", "archived": False, "goals": [],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)
    # Seed agent queue with the goal
    agent_asp = {
        "id": "asp-100", "title": "Agent", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-100-01", "title": "Agent goal", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    agent_path = project_root / "agents" / "alpha" / "aspirations.jsonl"
    agent_path.write_text(json.dumps(agent_asp, ensure_ascii=True) + "\n", encoding="utf-8")

    status, body = _post(port, "/v1/aspirations/release", {"id": "g-100-01"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "agent_queue_goal"


# ---------------------------------------------------------------------------
# claim tests
# ---------------------------------------------------------------------------

def _make_asp_with_unclaimed_goal(intended_agent=None):
    goal = {
        "id": "g-001-01", "title": "Claimable goal", "status": "pending",
        "recurring": False,
    }
    if intended_agent:
        goal["intended_agent"] = intended_agent
    return {
        "id": "asp-001", "title": "Test claim", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [goal],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }


def test_claim_happy_path(running_daemon):
    """Claim a goal -> 200, claimed_by/claimed_at set."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_unclaimed_goal())

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["ok"] is True
    goal = resp["goal"]
    assert goal["claimed_by"] == "alpha"
    assert "claimed_at" in goal


def test_claim_idempotent_same_agent(running_daemon):
    """Claiming a goal already claimed by the same agent -> 200."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_with_unclaimed_goal()
    asp["goals"][0]["claimed_by"] = "alpha"
    asp["goals"][0]["claimed_at"] = "2026-05-10T10:00:00"
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID})
    assert status == 200
    resp = json.loads(body)
    assert resp["goal"]["claimed_by"] == "alpha"


def test_claim_already_claimed_different_agent(running_daemon):
    """Goal FRESHLY claimed by bravo, alpha claims -> 409 already_claimed.

    The claim must be NON-stale for this to test the conflict path: since
    g-115-1841, claim() takes back a claim whose age exceeds
    claim_timeout_hours (default 4h), returning 200 instead of 409. A
    hardcoded past claimed_at is therefore a TIME-BOMB — it silently ages
    into the take-back path and the assertion flips 409->200 (g-115-2125:
    the old "2026-05-10T10:00:00" seed had aged ~64d past the 4h timeout).
    Seed a fresh timestamp so the claim is genuinely in-flight. The
    stale-claim take-back (200) path is covered by
    test_claim_stale_claim_takeback below.
    """
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_with_unclaimed_goal()
    asp["goals"][0]["claimed_by"] = "bravo"
    asp["goals"][0]["claimed_at"] = (
        datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID})
    assert status == 409
    resp = json.loads(body)
    assert resp["error"] == "already_claimed"


def test_claim_stale_claim_takeback(running_daemon):
    """Goal claimed by bravo LONG ago (claim expired), alpha claims -> 200 take-back.

    The g-115-1841 companion to test_claim_already_claimed_different_agent:
    once a prior claim ages past the effective timeout, claim() re-assigns
    the goal (mirroring goal-selector.py's claim-visibility contract, which
    re-offers stale-claimed world goals — a hard 409 here would livelock the
    world queue). The take-back is audited to override-bypass-ledger.jsonl
    under gate 'claim-staleness-takeback'. Added by g-115-2125 to close the
    coverage gap the failing-test fix would otherwise open (the old
    time-bomb test was the ONLY accidental exercise of this path).
    """
    project_root, port = running_daemon
    world = project_root / "world"
    asp = _make_asp_with_unclaimed_goal()
    asp["goals"][0]["claimed_by"] = "bravo"
    # Ancient claim — far past the 4h claim_timeout_hours default -> expired.
    asp["goals"][0]["claimed_at"] = "2026-05-10T10:00:00"
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID})
    assert status == 200, f"Expected 200 take-back, got {status}: {body}"
    resp = json.loads(body)
    assert resp["goal"]["claimed_by"] == "alpha"

    # The take-back is audited to the bypass ledger under its own gate tag.
    ledger_path = world / "override-bypass-ledger.jsonl"
    if ledger_path.exists():
        records = _read_jsonl(ledger_path)
        takebacks = [r for r in records
                     if r.get("gate") == "claim-staleness-takeback"]
        assert len(takebacks) >= 1
        tb_ctx = takebacks[-1]["context"]
        assert tb_ctx["agent_claiming"] == "alpha"
        assert tb_ctx["prior_claimer"] == "bravo"


def test_claim_cross_lane_refused(running_daemon):
    """Goal routed to bravo, alpha claims without cross_lane -> 400."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_unclaimed_goal(intended_agent="bravo"))

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "cross_lane_refused"


def test_claim_cross_lane_override(running_daemon):
    """Goal routed to bravo, alpha claims with cross_lane -> 200 + ledger."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_unclaimed_goal(intended_agent="bravo"))

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID,
                          "cross_lane": "urgent unblock needed"})
    assert status == 200, f"Expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["goal"]["claimed_by"] == "alpha"

    # Ledger should have the override record
    ledger_path = world / "override-bypass-ledger.jsonl"
    if ledger_path.exists():
        records = _read_jsonl(ledger_path)
        assert len(records) >= 1
        rec = records[-1]
        assert rec["gate"] == "capability-route-gate"
        assert rec["context"]["agent_claiming"] == "alpha"
        assert rec["context"]["intended_agent"] == "bravo"


def test_claim_cross_lane_either_no_block(running_daemon):
    """Goal with intended_agent='either' -> alpha claims without cross_lane -> 200."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_aspiration(world, _make_asp_with_unclaimed_goal(intended_agent="either"))

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID})
    assert status == 200


def test_claim_not_found(running_daemon):
    """Unknown goal_id -> 404."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-999-01", "agent": "alpha"})
    assert status == 404
    resp = json.loads(body)
    assert resp["error"] == "goal_not_found"


def test_claim_missing_goal_id(running_daemon):
    """No id param -> 400."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/claim", {"agent": "alpha"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "missing_goal_id"


def test_claim_missing_agent(running_daemon):
    """No agent param -> 400."""
    _, port = running_daemon
    status, body = _post(port, "/v1/aspirations/claim", {"id": "g-001-01"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "missing_agent"


def test_claim_agent_queue_goal(running_daemon):
    """Goal in agent queue -> 400 with helpful error."""
    project_root, port = running_daemon
    world = project_root / "world"
    asp = {
        "id": "asp-001", "title": "World", "status": "active",
        "priority": "LOW", "archived": False, "goals": [],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 0},
    }
    _seed_aspiration(world, asp)
    agent_asp = {
        "id": "asp-100", "title": "Agent", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [
            {"id": "g-100-01", "title": "Agent goal", "status": "pending",
             "recurring": False},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    agent_path = project_root / "agents" / "alpha" / "aspirations.jsonl"
    agent_path.write_text(json.dumps(agent_asp, ensure_ascii=True) + "\n", encoding="utf-8")

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-100-01", "agent": "alpha"})
    assert status == 400
    resp = json.loads(body)
    assert resp["error"] == "agent_queue_goal"


# ---------------------------------------------------------------------------
# release + source — 
#
#  taught claim() to accept &source=agent; release() was left behind.
#  prescribed three fixes. MEASURED AGAINST PRE-FIX HEAD, only two of
# them name a real defect — recorded here because the goal's own description
# asserts otherwise, and a future reader will otherwise trust it:
#
#   REAL. The refusal text — "Agent-queue goals do not carry claims, so there
#     is nothing to release" — is FALSE since . A caller who believes
#     it abandons a live claim, and the loop digest reads any `error` field as
#     journal-abort, so nothing retries.
#   REAL. `source` was never validated, so source=agent, source=bogus and
#     no-source were BYTE-IDENTICAL from outside (all resolved world, all said
#     "world queue"). That erased the only black-box discriminator for which
#     queue release resolved — precisely the probe that
#     test_release_source_forwarding.py's docstring records being used LIVE to
#     confirm the CLAIM endpoint honors --source. The same probe against
#     release could not distinguish a working --source from one the wrapper
#     silently dropped.
#   NOT REAL. "The unconditional agent-queue fallback blocks a source=agent
#     release." It does not, and never did:
#     test_release_source_agent_actually_releases_the_agent_queue_claim below
#     PASSES against pre-fix HEAD. `_resolve_paths(ctx, "agent")` returns the
#     same file the fallback re-reads, so the fallback is unreachable whenever
#     the goal IS in the queue the caller named. The gate was still added, for
#     parity and as a structural invariant — but it fixes nothing, and the
#     mutation that removes it kills no test.
#
# The goal split into a measured DIAGNOSIS (the strings) and an inferred REMEDY
# (the gate), and only the diagnosis carried evidence — rb-5669 / guard-1719 /
# retrieve-before-deciding rule 11. The tell was ordinary: the mutation proof
# ran, and the mutation for that part SURVIVED.
#
# WHY THESE DRIVE THE DAEMON. The wrapper half is already pinned hermetically by
# test_release_source_forwarding.py, which asserts `source=agent` reaches the
# QUERY. That says nothing about what the endpoint DOES with it — the guard-2374
# split (a flag the client sends and the endpoint ignores). Only an over-the-wire
# call can falsify it, so these use the real HTTP path in the production arg
# shape (guard-920).
# ---------------------------------------------------------------------------

def _seed_world_empty_and_agent_goal(project_root: Path, goal: dict):
    """World queue with no goals; the agent queue holding `goal`."""
    world = project_root / "world"
    _seed_aspiration(world, {
        "id": "asp-001", "title": "World", "status": "active",
        "priority": "LOW", "archived": False, "goals": [],
        "progress": {"completed_goals": 0, "total_goals": 0, "recurring_goals": 0},
    })
    agent_asp = {
        "id": "asp-100", "title": "Agent", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [goal],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    agent_path = project_root / "agents" / "alpha" / "aspirations.jsonl"
    agent_path.write_text(json.dumps(agent_asp, ensure_ascii=True) + "\n",
                          encoding="utf-8")


def test_release_rejects_an_out_of_vocabulary_source(running_daemon):
    """Part 3: parity with claim()'s invalid_source 400.

    Newly REFUSED is exactly the complement of {world, agent}. Before this, a
    typo'd source silently resolved the WORLD queue and reported success —
    the failure mode where the caller believes it released something it did not.
    """
    _project_root, port = running_daemon
    status, body = _post(port, "/v1/aspirations/release",
                         {"id": "g-001-01", "source": "wrold"})
    assert status == 400, f"expected 400 for a bogus source, got {status}: {body}"
    resp = json.loads(body)
    assert resp["error"] == "invalid_source", resp


@pytest.mark.parametrize("good_source", ["world", "agent"])
def test_release_accepts_both_vocabulary_sources(running_daemon, good_source):
    """The tightening must not refuse the two values the wrapper actually sends.

    Negative control for the test above: without this, a gate that refused
    EVERYTHING would pass it. Neither call finds the goal, so both reach the
    404 — the point is only that neither is turned away as invalid_source.
    """
    _project_root, port = running_daemon
    status, body = _post(port, "/v1/aspirations/release",
                         {"id": "g-999-99", "source": good_source})
    assert "invalid_source" not in body, f"{good_source}: {body}"
    assert status == 404, f"{good_source}: expected 404, got {status}: {body}"


def test_release_source_agent_actually_releases_the_agent_queue_claim(running_daemon):
    """CHARACTERIZATION, not regression: source=agent releases — and always did.

    Labelled explicitly because it is the test most likely to be misread as
    proof of this goal's fix. It PASSES against pre-fix HEAD; no mutation of the
    g-306-257 diff kills it. Its value is twofold and neither is regression
    coverage: it is the evidence that falsified the goal's premise, and it is
    the only end-to-end pin that agent-queue release works at all — a capability
    the file's own docstring did not list before g-306-238 made it reachable.

    Keep it. A characterization test that announces what it is beats an absent
    one; the failure mode this guards is a future reader deleting it as
    redundant, or citing it as the fix.
    """
    project_root, port = running_daemon
    _seed_world_empty_and_agent_goal(project_root, {
        "id": "g-100-01", "title": "Agent goal", "status": "in-progress",
        "recurring": False, "claimed_by": "alpha",
        "claimed_at": "2026-08-07T06:00:00",
    })

    status, body = _post(port, "/v1/aspirations/release",
                         {"id": "g-100-01", "source": "agent"})
    assert status == 200, f"expected 200, got {status}: {body}"
    resp = json.loads(body)
    assert resp["ok"] is True, resp
    assert resp["had_claim"] is True, resp
    assert "claimed_by" not in resp["goal"], resp["goal"]
    assert "claimed_at" not in resp["goal"], resp["goal"]

    # Persisted, not merely reported. A success-shaped response over a store
    # that still carries the claim is the  write-loss shape.
    agent_path = project_root / "agents" / "alpha" / "aspirations.jsonl"
    stored = _read_jsonl(agent_path)[0]["goals"][0]
    assert "claimed_by" not in stored, stored
    assert "claimed_at" not in stored, stored


def test_release_404_names_the_queue_it_actually_searched(running_daemon):
    """Part 2: the 404 hardcoded "world queue" regardless of source.

    This is the discriminator, not a cosmetic string. It is the ONLY black-box
    signal distinguishing a release that honored --source from one that dropped
    it — the same technique used to LIVE-verify claim() (g-306-238).
    """
    _project_root, port = running_daemon
    status, body = _post(port, "/v1/aspirations/release",
                         {"id": "g-999-99", "source": "agent"})
    assert status == 404, body
    resp = json.loads(body)
    assert "agent queue" in resp["detail"], resp
    assert "world queue" not in resp["detail"], resp

    status, body = _post(port, "/v1/aspirations/release",
                         {"id": "g-999-99", "source": "world"})
    assert status == 404, body
    assert "world queue" in json.loads(body)["detail"], body


def test_release_world_fallback_no_longer_asserts_agent_goals_lack_claims(running_daemon):
    """Part 2b: the refusal survives, on a premise that is still true.

    source=world + goal in the agent queue is STILL a refusal — the caller did
    not name that queue. But the old text told them agent goals carry no claims,
    which g-306-238 made false; a caller who believed it would abandon a live
    claim. The corrected text must name the actionable remedy instead.
    """
    project_root, port = running_daemon
    _seed_world_empty_and_agent_goal(project_root, {
        "id": "g-100-01", "title": "Agent goal", "status": "in-progress",
        "recurring": False, "claimed_by": "alpha",
        "claimed_at": "2026-08-07T06:00:00",
    })

    status, body = _post(port, "/v1/aspirations/release", {"id": "g-100-01"})
    assert status == 400, body
    resp = json.loads(body)
    assert resp["error"] == "agent_queue_goal", resp
    msg = resp["detail"]
    # The falsified claim must be gone...
    assert "do not carry claims" not in msg, msg
    assert "nothing to release" not in msg, msg
    # ...and replaced by the call that WOULD work.
    assert "source=agent" in msg, msg
# ── Lane-pin gate () ──────────────────────────────────────────────
#
# A lane pin is a durable user-directed constraint fixing ONE agent's work
# surface, recorded in world/conventions/capability-routing.md under a
# `## Standing Lane Pins` heading. The gate parses that table LIVE at claim
# time, so deleting the row lifts the pin with no code change.
#
# The classifier's own behavior is unit-tested hermetically in
# core/scripts/tests/test_lane_pin_gate.py. THESE tests cover the half that
# file cannot see: that the daemon claim endpoint actually calls it, refuses
# with the right error, orders the refusal correctly against its siblings, and
# audits the override — the wiring, which a green classifier suite never
# certifies (guard-1943/guard-984).
#
# The fixture vocabulary is synthetic and domain-free on purpose: it exercises
# the registry's STRUCTURE without pinning these tests to whatever the live
# world's pin rows happen to say today.

_LANE_PIN_IN = (
    "RUN WIDGET SESSIONS: agent-initiated headless sessions on staging; "
    "in-session verification (clean boot, autonomous steps); RELAY/BOX-LOCAL "
    "fixes only (relay, portproxy, host plugin, host config); own agent "
    "hygiene (temp drain)."
)
_LANE_PIN_OUT = (
    "ALL CODE work: gadget scripts, client scripts, workflows, analyzers, "
    "env-server, framework scripts, doc trims. Selector score is NOT a "
    "justification for an out-of-lane claim."
)
_OUT_OF_LANE_TITLE = "Fix the retry logic in the framework scripts"
_IN_LANE_TITLE = "Run widget sessions on staging and verify a clean boot"


def _seed_lane_pin(world: Path, pinned_agent: str = "alpha", *, rows=None):
    """Write a Standing Lane Pins registry into the tmp world."""
    if rows is None:
        rows = (f"| pin-t01 | {pinned_agent} | {_LANE_PIN_IN} | {_LANE_PIN_OUT} "
                f"| 2026-08-06 | user directive | user directive only "
                f"(revoke by deleting this row) |\n")
    conv = world / "conventions"
    conv.mkdir(parents=True, exist_ok=True)
    (conv / "capability-routing.md").write_text(
        "# Capability Routing\n\nPreamble.\n\n## Standing Lane Pins\n\n"
        "| id | agent | in-lane | out-of-lane | pinned | provenance | expires |\n"
        "|----|-------|---------|-------------|--------|------------|---------|\n"
        + rows + "\n## Later Section\n\nUnrelated.\n",
        encoding="utf-8")


def _claim_goal_titled(world: Path, title: str):
    asp = _make_asp_with_unclaimed_goal()
    asp["goals"][0]["title"] = title
    _seed_aspiration(world, asp)


def test_claim_lane_pin_out_of_lane_refused(running_daemon):
    """Pinned agent claims an out-of-lane goal -> 400 lane_pin_refused."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_lane_pin(world, "alpha")
    _claim_goal_titled(world, _OUT_OF_LANE_TITLE)

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID})
    assert status == 400, f"Expected 400, got {status}: {body}"
    resp = json.loads(body)
    assert resp["error"] == "lane_pin_refused"
    # The message is the blocked agent's only guidance: it must name the pin,
    # what matched, where the row lives, and the way out.
    msg = resp.get("message") or body
    assert "pin-t01" in msg
    assert "framework scripts" in msg
    assert "--override-lane-pin" in msg

    # The refusal must not have written a claim.
    records = _read_jsonl(world / "aspirations.jsonl")
    goal = records[0]["goals"][0]
    assert goal.get("claimed_by") is None


def test_claim_lane_pin_in_lane_allowed(running_daemon):
    """Same pin, in-lane goal -> 200. The pin restricts, it does not freeze."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_lane_pin(world, "alpha")
    _claim_goal_titled(world, _IN_LANE_TITLE)

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID})
    assert status == 200, f"Expected 200, got {status}: {body}"
    assert json.loads(body)["goal"]["claimed_by"] == "alpha"


def test_claim_lane_pin_unpinned_agent_unaffected(running_daemon):
    """A pin on bravo must not constrain alpha, even on out-of-lane text."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_lane_pin(world, "bravo")
    _claim_goal_titled(world, _OUT_OF_LANE_TITLE)

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID})
    assert status == 200, f"Expected 200, got {status}: {body}"


def test_claim_lane_pin_auto_lifts_when_row_deleted(running_daemon):
    """Registry present, row gone -> the same claim that 400s now succeeds.

    This is the outcome the design turns on: retiring a pin is a registry edit
    by the user, never a code change or a redeploy.
    """
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_lane_pin(world, "alpha")
    _claim_goal_titled(world, _OUT_OF_LANE_TITLE)

    status, _ = _post(port, "/v1/aspirations/claim",
                      {"id": "g-001-01", "agent": "alpha", "sid": CLAIMER_SID})
    assert status == 400

    _seed_lane_pin(world, "alpha", rows="")
    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID})
    assert status == 200, f"Expected 200 after auto-lift, got {status}: {body}"


def test_claim_lane_pin_override_allows_and_audits(running_daemon):
    """override_lane_pin -> 200 + a ledger row under gate lane-pin-gate."""
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_lane_pin(world, "alpha")
    _claim_goal_titled(world, _OUT_OF_LANE_TITLE)

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID,
                          "override_lane_pin": "only box with the repro"})
    assert status == 200, f"Expected 200, got {status}: {body}"

    records = _read_jsonl(world / "override-bypass-ledger.jsonl")
    rows = [r for r in records if r.get("gate") == "lane-pin-gate"]
    assert len(rows) == 1, f"Expected 1 lane-pin ledger row, got {records}"
    row = rows[0]
    assert row["justification"] == "only box with the repro"
    ctx = row["context"]
    assert ctx["goal_id"] == "g-001-01"
    assert ctx["agent_claiming"] == "alpha"
    assert ctx["pin_id"] == "pin-t01"
    # The evidence survives the override so the row is readable at audit time
    # even after the registry has been re-worded.
    assert "framework scripts" in ctx["out_of_lane_evidence"]


def test_claim_lane_pin_absent_registry_is_fail_open(running_daemon):
    """No conventions/ dir at all -> claims behave exactly as before.

    This is what makes the gate safe to land: every OTHER claim test in this
    file runs against a world with no registry, so a regression here would
    surface as a fleet-wide claim outage rather than a subtle misroute.
    """
    project_root, port = running_daemon
    world = project_root / "world"
    assert not (world / "conventions").exists()
    _claim_goal_titled(world, _OUT_OF_LANE_TITLE)

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID})
    assert status == 200, f"Expected 200, got {status}: {body}"


def test_claim_lane_pin_refusal_precedes_cross_lane(running_daemon):
    """Ordering: a pinned agent claiming an out-of-lane goal routed elsewhere
    hears about the PIN, not about cross-lane.

    Both refusals apply, and supplying a cross_lane reason would not clear the
    pin — so leading with cross_lane_refused would send the agent down a path
    that ends at this same wall one call later.
    """
    project_root, port = running_daemon
    world = project_root / "world"
    _seed_lane_pin(world, "alpha")
    asp = _make_asp_with_unclaimed_goal(intended_agent="bravo")
    asp["goals"][0]["title"] = _OUT_OF_LANE_TITLE
    _seed_aspiration(world, asp)

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha",
                          "sid": CLAIMER_SID})
    assert status == 400
    assert json.loads(body)["error"] == "lane_pin_refused"
