"""POST /v1/aspirations/clear-stale-claims endpoint tests (PR 50).

Mirrors cmd_clear_stale_claims behavior exactly:
  - terminal goals with claimed_by/claimed_at → cleared
  - non-terminal goals with claims → preserved
  - dry_run mode → report-only, no writes
  - goals without claims → untouched
  - source=agent
  - invalid source → 400
  - idempotency
  - mixed aspirations with various goal states
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest


def _post(port: int, source: str = "world", dry_run: bool = False,
          agent: str = "alpha"):
    """POST /v1/aspirations/clear-stale-claims and return parsed JSON."""
    import urllib.request
    dr = "true" if dry_run else "false"
    url = (f"http://127.0.0.1:{port}/v1/aspirations/clear-stale-claims"
           f"?source={source}&dry_run={dr}")
    req = urllib.request.Request(
        url, method="POST",
        headers={
            "X-Mind-Agent": agent,
            "X-Runtime-Client": "test",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _post_raw(port: int, source: str = "world", agent: str = "alpha"):
    """POST and return (status_code, body_dict)."""
    import urllib.request
    import urllib.error
    url = (f"http://127.0.0.1:{port}/v1/aspirations/clear-stale-claims"
           f"?source={source}")
    req = urllib.request.Request(
        url, method="POST",
        headers={
            "X-Mind-Agent": agent,
            "X-Runtime-Client": "test",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def _write_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=True) + "\n")


def _make_asp(asp_id: str, status: str = "active", goals=None) -> Dict[str, Any]:
    return {
        "id": asp_id,
        "title": f"Test {asp_id}",
        "status": status,
        "priority": "LOW",
        "goals": goals or [],
        "progress": {"completed_goals": 0, "total_goals": 0},
    }


def _make_goal(goal_id: str, status: str = "completed", **kwargs) -> Dict[str, Any]:
    g = {"id": goal_id, "title": f"Goal {goal_id}", "status": status}
    g.update(kwargs)
    return g


# ---- Tests ----

def test_no_claims(running_daemon):
    """No goals with claims → cleared_count=0."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed"),
        ]),
    ])

    resp = _post(port)
    assert resp["ok"] is True
    assert resp["cleared_count"] == 0
    assert resp["cleared_ids"] == []
    assert resp["dry_run"] is False


def test_stale_claims_cleared(running_daemon):
    """Terminal goal with claimed_by + claimed_at → both removed."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       claimed_by="alpha", claimed_at="2026-05-01T00:00:00"),
        ]),
    ])

    resp = _post(port)
    assert resp["ok"] is True
    assert resp["cleared_count"] == 1
    assert resp["cleared_ids"] == ["g-001-01"]

    live = _read_jsonl(world / "aspirations.jsonl")
    goal = live[0]["goals"][0]
    assert "claimed_by" not in goal
    assert "claimed_at" not in goal


def test_fresh_claims_preserved(running_daemon):
    """Non-terminal (pending) goal with claims → NOT cleared."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "pending",
                       claimed_by="alpha", claimed_at="2026-05-01T00:00:00"),
        ]),
    ])

    resp = _post(port)
    assert resp["cleared_count"] == 0

    live = _read_jsonl(world / "aspirations.jsonl")
    goal = live[0]["goals"][0]
    assert goal["claimed_by"] == "alpha"
    assert goal["claimed_at"] == "2026-05-01T00:00:00"


def test_dry_run(running_daemon):
    """dry_run=true → reports what would be cleared but does not write."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       claimed_by="alpha", claimed_at="2026-05-01T00:00:00"),
        ]),
    ])

    resp = _post(port, dry_run=True)
    assert resp["ok"] is True
    assert resp["cleared_count"] == 1
    assert resp["cleared_ids"] == ["g-001-01"]
    assert resp["dry_run"] is True

    # File should be unchanged
    live = _read_jsonl(world / "aspirations.jsonl")
    goal = live[0]["goals"][0]
    assert goal["claimed_by"] == "alpha"


def test_invalid_source(running_daemon):
    """source=invalid → 400."""
    _, port = running_daemon
    status, body = _post_raw(port, source="invalid")
    assert status == 400
    assert body["error"] == "invalid_source"


def test_multiple_stale_claims(running_daemon):
    """Multiple terminal goals across aspirations → all cleared."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       claimed_by="alpha", claimed_at="2026-05-01T00:00:00"),
            _make_goal("g-001-02", "skipped",
                       claimed_by="bravo"),
        ]),
        _make_asp("asp-002", "active", goals=[
            _make_goal("g-002-01", "expired",
                       claimed_at="2026-05-01T00:00:00"),
        ]),
    ])

    resp = _post(port)
    assert resp["cleared_count"] == 3
    assert set(resp["cleared_ids"]) == {"g-001-01", "g-001-02", "g-002-01"}

    live = _read_jsonl(world / "aspirations.jsonl")
    for asp in live:
        for goal in asp["goals"]:
            assert "claimed_by" not in goal
            assert "claimed_at" not in goal


def test_source_agent(running_daemon):
    """source=agent reads/writes agent-local aspirations."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _write_jsonl(agent_dir / "aspirations.jsonl", [
        _make_asp("asp-100", "active", goals=[
            _make_goal("g-100-01", "completed",
                       claimed_by="alpha", claimed_at="2026-05-01T00:00:00"),
        ]),
    ])

    resp = _post(port, source="agent")
    assert resp["cleared_count"] == 1

    live = _read_jsonl(agent_dir / "aspirations.jsonl")
    goal = live[0]["goals"][0]
    assert "claimed_by" not in goal


def test_mixed_terminal_and_nonterminal(running_daemon):
    """Mix of terminal (with claims) and non-terminal (with claims) → only terminal cleared."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       claimed_by="alpha", claimed_at="2026-05-01T00:00:00"),
            _make_goal("g-001-02", "in-progress",
                       claimed_by="alpha", claimed_at="2026-05-02T00:00:00"),
            _make_goal("g-001-03", "pending",
                       claimed_by="bravo"),
        ]),
    ])

    resp = _post(port)
    assert resp["cleared_count"] == 1
    assert resp["cleared_ids"] == ["g-001-01"]

    live = _read_jsonl(world / "aspirations.jsonl")
    goals = live[0]["goals"]
    # Terminal goal: claims removed
    assert "claimed_by" not in goals[0]
    # Non-terminal goals: claims preserved
    assert goals[1]["claimed_by"] == "alpha"
    assert goals[2]["claimed_by"] == "bravo"


def test_idempotency(running_daemon):
    """Two consecutive clears → second returns 0."""
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       claimed_by="alpha", claimed_at="2026-05-01T00:00:00"),
        ]),
    ])

    resp1 = _post(port)
    assert resp1["cleared_count"] == 1

    resp2 = _post(port)
    assert resp2["cleared_count"] == 0


# ---- claimed_by_sid is part of the claim TRIPLE () ----
#
#  added claimed_by_sid and paired it at four of the five pop sites
# in aspirations_write.py; this sweeper was the unpaired one. Two independent
# halves are pinned below, and they fail for DIFFERENT reasons — the body half
# leaves residue behind, the predicate half never sees the residue at all.


def test_sid_cleared_with_the_pair(running_daemon):
    """Terminal goal with the full triple → all three removed, not just two.

    Fails without the `goal.pop("claimed_by_sid", None)` line: claimed_by and
    claimed_at go, and the sid is left behind on a now-unclaimed goal.
    """
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed",
                       claimed_by="alpha", claimed_at="2026-05-01T00:00:00",
                       claimed_by_sid="SID-ALPHA"),
        ]),
    ])

    resp = _post(port)
    assert resp["cleared_count"] == 1

    goal = _read_jsonl(world / "aspirations.jsonl")[0]["goals"][0]
    assert "claimed_by" not in goal
    assert "claimed_at" not in goal
    assert "claimed_by_sid" not in goal, (
        "the sid must clear WITH the pair — a claim is a triple, and a "
        "leftover sid on an unclaimed goal is read by no consumer-side guard, "
        "so it accumulates unnoticed")


def test_orphaned_sid_only_is_visible_to_the_sweeper(running_daemon):
    """A terminal goal carrying ONLY an orphaned sid must be found and cleared.

    This is the PREDICATE half and it is the retroactive one. Without the
    `or "claimed_by_sid" in goal` disjunct the goal never matches, so the
    sweeper that exists to clean this residue up can never select it — the
    orphan is permanently invisible and `cleared_count` reads 0, which is
    indistinguishable from a clean store.
    """
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "completed", claimed_by_sid="SID-ORPHAN"),
        ]),
    ])

    resp = _post(port)
    assert resp["cleared_count"] == 1, (
        "a terminal goal whose only claim residue is an orphaned sid must be "
        "selectable by the sweeper; 0 here means the predicate cannot see it")
    assert resp["cleared_ids"] == ["g-001-01"]

    goal = _read_jsonl(world / "aspirations.jsonl")[0]["goals"][0]
    assert "claimed_by_sid" not in goal


def test_fresh_claim_triple_preserved(running_daemon):
    """Non-terminal goal with the full triple → nothing cleared.

    Guards the widened predicate against over-clearing: adding the sid
    disjunct must not make a LIVE claim sweepable. The status test is still
    the gate; the disjunct only widens what counts as residue on a goal that
    is already terminal.
    """
    project_root, port = running_daemon
    world = project_root / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        _make_asp("asp-001", "active", goals=[
            _make_goal("g-001-01", "in-progress",
                       claimed_by="alpha", claimed_at="2026-05-01T00:00:00",
                       claimed_by_sid="SID-ALPHA"),
        ]),
    ])

    resp = _post(port)
    assert resp["cleared_count"] == 0

    goal = _read_jsonl(world / "aspirations.jsonl")[0]["goals"][0]
    assert goal["claimed_by"] == "alpha"
    assert goal["claimed_by_sid"] == "SID-ALPHA"
