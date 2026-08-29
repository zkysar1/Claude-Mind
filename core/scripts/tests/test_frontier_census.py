"""test_frontier_census.py — `_frontier.py`, the claimable-frontier census.

THE NUMBER THIS PINS. A fleet of N Bodies is at most as parallel as its
claimable frontier is wide, and nothing measured the width until 2026-08-29,
when a live 8-Body deployment sat with 15 pending goals, frontier 0, all gated
on one in-progress goal — five Bodies closed for lack of work. These tests
build that shape from fresh fixtures and assert the census reads it the way the
selector would (dependencies resolve through `_dependency_graph`, the SSOT):

  1. claimable vs gated — completed and superseded-then-completed blockers
     satisfy; an in-progress one gates; a chain gates transitively.
  2. roots and fan-out — the in-progress gate is the root of everything
     behind it, counted once per gated goal, sorted by fan-out.
  3. the buckets that are NOT claimable and NOT gated: deferred, recurring,
     user-only, blocked, in-progress, and goals in non-active aspirations.
  4. an unknown blocker gates (the selector cannot find it in done_ids) but
     has no root, and is reported as a pair.
  5. a bare-string blocked_by resolves (norm_blocked_by is honoured).
  6. body census from body-manifest.yaml: active vs closed-recent vs closed-old.
  7. open_funnel_goals: pending+unclaimed funnel goals by signal prefix.
  8. parse_skipped counts an unparseable line rather than hiding it.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _frontier  # noqa: E402


def _goal(gid, status="pending", **kw):
    g = {"id": gid, "title": f"title {gid}", "status": status,
         "priority": "MEDIUM", "participants": ["agent"]}
    g.update(kw)
    return g


def _world(tmp_path: Path, aspirations: list, agent_stores: dict | None = None):
    world = tmp_path / "world"
    world.mkdir(exist_ok=True)
    with (world / "aspirations.jsonl").open("w", encoding="utf-8") as f:
        for a in aspirations:
            f.write(json.dumps(a) + "\n")
    agents = tmp_path / "agents"
    agents.mkdir(exist_ok=True)
    for name, asps in (agent_stores or {}).items():
        d = agents / name
        d.mkdir(parents=True, exist_ok=True)
        with (d / "aspirations.jsonl").open("w", encoding="utf-8") as f:
            for a in asps:
                f.write(json.dumps(a) + "\n")
    return world, agents


def _funnel_world(tmp_path: Path):
    """The measured shape: one in-progress root gating a chain."""
    asp = {"id": "asp-006", "status": "active", "goals": [
        _goal("g-006-03", "in-progress", claimed_by="coach"),           # root
        _goal("g-006-06", blocked_by=["g-006-03"]),
        _goal("g-006-07", blocked_by=["g-006-03"]),
        _goal("g-006-08", blocked_by=["g-006-03"]),
        _goal("g-006-10", blocked_by=["g-006-08"]),                       # transitive
        _goal("g-006-05", "completed"),
        _goal("g-006-22", blocked_by=["g-006-05"]),                       # satisfied
        _goal("g-006-01", "superseded", superseded_by="g-006-05"),
        _goal("g-006-23", blocked_by=["g-006-01"]),                       # satisfied via chain
        _goal("g-006-24"),                                                # free
    ]}
    return _world(tmp_path, [asp])


def test_claimable_vs_gated_and_roots(tmp_path):
    world, agents = _funnel_world(tmp_path)
    c = _frontier.frontier_census(world, agents)
    assert set(c["claimable"]) == {"g-006-22", "g-006-23", "g-006-24"}
    assert set(c["gated"]) == {"g-006-06", "g-006-07", "g-006-08", "g-006-10"}
    assert c["claimable_count"] == 3 and c["gated_count"] == 4
    assert c["pending_total"] == 7
    assert c["in_progress"] == 1
    assert c["unknown_blockers"] == []
    # The in-progress goal is the ONE root; the transitive consumer counts
    # against it too, so fan-out is the whole chain, not the direct edges.
    assert [r["id"] for r in c["roots"]] == ["g-006-03"]
    r = c["roots"][0]
    assert r["gates"] == 4
    assert r["status"] == "in-progress" and r["claimed_by"] == "coach"
    assert r["asp_id"] == "asp-006" and r["source"] == "world"


def test_the_measured_funnel_reads_as_frontier_zero(tmp_path):
    """Remove the free goals and the census must read exactly what the fleet
    lived through: frontier 0, every pending goal gated on one root."""
    asp = {"id": "asp-006", "status": "active", "goals": [
        _goal("g-006-03", "in-progress"),
        *[_goal(f"g-006-{n:02d}", blocked_by=["g-006-03"]) for n in range(6, 10)],
        _goal("g-006-10", blocked_by=["g-006-08"]),
    ]}
    world, agents = _world(tmp_path, [asp])
    c = _frontier.frontier_census(world, agents)
    assert c["claimable_count"] == 0
    assert c["gated_count"] == 5
    assert c["roots"][0]["id"] == "g-006-03" and c["roots"][0]["gates"] == 5


def test_roots_sorted_by_fanout_and_counted_once_per_goal(tmp_path):
    asp = {"id": "asp-1", "status": "active", "goals": [
        _goal("g-1-01", "in-progress"), _goal("g-1-02", "in-progress"),
        _goal("g-1-10", blocked_by=["g-1-01", "g-1-02"]),   # one goal, two roots
        _goal("g-1-11", blocked_by=["g-1-02"]),
        _goal("g-1-12", blocked_by=["g-1-02"]),
    ]}
    world, agents = _world(tmp_path, [asp])
    c = _frontier.frontier_census(world, agents)
    assert [(r["id"], r["gates"]) for r in c["roots"]] == [("g-1-02", 3), ("g-1-01", 1)]


def test_non_claimable_non_gated_buckets(tmp_path):
    asp = {"id": "asp-1", "status": "active", "goals": [
        _goal("g-1-01", defer_reason="precondition_unmet: waiting on the season"),
        _goal("g-1-02", recurring={"interval_hours": 24}),
        _goal("g-1-03", participants=["user"]),
        _goal("g-1-04", "blocked"),
        _goal("g-1-05", "in-progress"),
        _goal("g-1-06", "completed"),
        _goal("g-1-07"),
    ]}
    retired = {"id": "asp-2", "status": "retired", "goals": [_goal("g-2-01")]}
    world, agents = _world(tmp_path, [asp, retired])
    c = _frontier.frontier_census(world, agents)
    assert c["claimable"] == ["g-1-07"]
    assert c["gated"] == []
    assert (c["deferred"], c["recurring"], c["user_only"], c["blocked"], c["in_progress"]) == (1, 1, 1, 1, 1)
    assert c["active_aspirations"] == 1


def test_unknown_blocker_gates_without_a_root(tmp_path):
    asp = {"id": "asp-1", "status": "active", "goals": [
        _goal("g-1-01", blocked_by=["g-9-99"]),
    ]}
    world, agents = _world(tmp_path, [asp])
    c = _frontier.frontier_census(world, agents)
    assert c["claimable"] == [] and c["gated"] == ["g-1-01"]
    assert c["roots"] == []
    assert c["unknown_blockers"] == [("g-1-01", "g-9-99")]


def test_bare_string_blocked_by_resolves(tmp_path):
    asp = {"id": "asp-1", "status": "active", "goals": [
        _goal("g-1-01", "in-progress"),
        _goal("g-1-02", blocked_by="g-1-01"),
    ]}
    world, agents = _world(tmp_path, [asp])
    c = _frontier.frontier_census(world, agents)
    assert c["gated"] == ["g-1-02"] and c["roots"][0]["id"] == "g-1-01"
    assert c["unknown_blockers"] == []


def test_blocked_by_resolves_across_queues(tmp_path):
    """coordination.md: blocked_by resolves GLOBALLY. A world goal gated on an
    agent-queue goal must find it, and the root carries the agent as source —
    the value the retire path passes to --source."""
    world_asp = {"id": "asp-1", "status": "active", "goals": [
        _goal("g-1-01", blocked_by=["g-7-01"]),
    ]}
    agent_asp = {"id": "asp-7", "status": "active", "goals": [
        _goal("g-7-01", "in-progress"),
    ]}
    world, agents = _world(tmp_path, [world_asp], {"bravo": [agent_asp]})
    c = _frontier.frontier_census(world, agents)
    assert c["gated"] == ["g-1-01"]
    assert c["roots"][0]["id"] == "g-7-01" and c["roots"][0]["source"] == "bravo"


def test_archived_completed_dependency_satisfies(tmp_path):
    """guard-1890: a dependency on a completed-then-ARCHIVED goal is satisfied,
    not unknown. Without the archive fold this consumer reads as gated forever
    with no root — the shape that froze g-005-17 for 37 days."""
    live = {"id": "asp-1", "status": "active", "goals": [
        _goal("g-1-01", blocked_by=["g-0-77"]),
    ]}
    world, agents = _world(tmp_path, [live])
    archived = {"id": "asp-0", "status": "completed", "archived": True,
                "goals": [_goal("g-0-77", "completed")]}
    with (world / "aspirations-archive.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(archived) + "\n")
    c = _frontier.frontier_census(world, agents)
    assert c["claimable"] == ["g-1-01"] and c["gated"] == []
    assert c["unknown_blockers"] == []
    # The archived aspiration is a resolution source only, never a population.
    assert c["active_aspirations"] == 1


def _manifest(agents: Path, agent: str, sid: str, state: str, age_s: float) -> None:
    d = agents / agent / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    p = d / "body-manifest.yaml"
    p.write_text(f"unitKey: x\nmindKey: {agent}\nbody_state: {state}\nrole: worker\n",
                 encoding="utf-8")
    t = time.time() - age_s
    os.utime(p, (t, t))


def test_body_census_active_closed_recent_closed_old(tmp_path):
    """A dead session never closes its manifest (measured: 37 of 49 read `active`,
    14 fresh), so `active` requires a fresh manifest OR a fresh body-heartbeat."""
    agents = tmp_path / "agents"
    _manifest(agents, "coach", "s1", "active", 10)
    _manifest(agents, "coach", "s2", "parked", 10)
    _manifest(agents, "coach", "s3", "closed-pending-merge", 60)
    _manifest(agents, "coach", "s4", "'merged'", 60)           # quoted form
    _manifest(agents, "coach", "s5", "closed-stale", 3 * 24 * 3600)   # too old
    _manifest(agents, "other", "s6", "closed-pending-merge", 60)
    _manifest(agents, "coach", "s7", "active", 2 * 24 * 3600)   # died days ago, never closed
    # Stale manifest but a FRESH heartbeat beside it: the Body is alive (a manifest is
    # written at join and close, the heartbeat every tick).
    _manifest(agents, "coach", "s8", "active", 2 * 24 * 3600)
    hb = agents / "coach" / "sessions" / "s8" / "body-heartbeat"
    hb.write_text("beat\n", encoding="utf-8")
    # Inside the closed-recently window but past the liveness window: a session that
    # died two hours ago (measured: live heartbeats are minutes old, dead ones 2h+).
    _manifest(agents, "coach", "s9", "active", 2 * 3600)
    b = _frontier.count_bodies(agents, lookback_hours=6, liveness_hours=1)
    assert b == {"active": 3, "active_stale": 2, "closed_recent": 3, "scanned": 9}
    # The same census at the old 6h liveness counts the 2h corpse as a Body.
    assert _frontier.count_bodies(agents, lookback_hours=6, liveness_hours=6)["active"] == 4
    assert _frontier.count_bodies(tmp_path / "nope") == {
        "active": 0, "active_stale": 0, "closed_recent": 0, "scanned": 0}


def test_open_funnel_goals_by_signal_prefix(tmp_path):
    sig = _frontier.FUNNEL_SIGNAL_PREFIX
    asp = {"id": "asp-1", "status": "active", "goals": [
        _goal("g-1-01", origin_signal=f"{sig}g-9-01"),
        _goal("g-1-02", origin_signal=f"{sig}g-9-02", claimed_by="bravo"),   # in someone's hands
        _goal("g-1-03", "skipped", origin_signal=f"{sig}g-9-03"),           # already closed
        _goal("g-1-04", origin_signal="investigate:something-else"),
    ]}
    world, agents = _world(tmp_path, [asp])
    idx, _asps, _stats = _frontier.load_goal_index(world, agents)
    assert _frontier.open_funnel_goals(idx) == [
        {"id": "g-1-01", "root": "g-9-01", "source": "world"}]


def test_parse_skipped_is_counted_not_hidden(tmp_path):
    world, agents = _funnel_world(tmp_path)
    with (world / "aspirations.jsonl").open("a", encoding="utf-8") as f:
        f.write("{this is not json\n")
    c = _frontier.frontier_census(world, agents)
    assert c["parse_skipped"] == 1
    assert c["claimable_count"] == 3   # the good lines still count
