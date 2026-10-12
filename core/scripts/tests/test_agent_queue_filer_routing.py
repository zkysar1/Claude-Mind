#!/usr/bin/env python3
"""Regression: an automated filer's agent-queue goal names its executor ().

recurring-starvation-check filed its rescue Unblock for a starved goal in an agent's
PRIVATE queue, and cargo-cult-detector its Idea, with no `intended_agent`. The daemon's
capability router classifies by CONTENT and stamped a different agent (a starved goal
titled like a tree-maintenance job read as the tree steward's); the agent-queue routing
gate (g-115-9234) then refused the filing as unclaimable by construction. The sweep
reported file_failures=1 on every run, for the one starved goal it most needed to reach,
and nothing retried with the remedy the gate names.

Contracts pinned here:
  1. `_file_unblock` on an agent-queue row sends intended_agent = the queue owner
     (MIND_AGENT); on a world row it sends none (the router's stamp is reachable
     there); with no owner in the environment it falls back to the gate-safe "either".
  2. THROUGH THE REAL DAEMON: the exact payload `_file_unblock` builds is ACCEPTED on
     source=agent and stored with intended_agent == the owner, read back from the
     store (guard-2980: what the filer meant and what is stored can disagree silently).
  3. CONTROL: the same payload WITHOUT the field is refused agent_queue_routing_blocked,
     so reverting the fix fails here and not in the next precheck. If the router stops
     stamping a third party for this payload the control fails by name: the defect this
     file pins may be gone, and the fix should be re-derived before the test is deleted.
  4. `cargo-cult-detector.file_idea` carries the same field on source=agent only, keeps
     a caller-set value, and does not mutate the caller's dict.

Pattern: DaemonFixture + direct HTTP POST, mirroring test_agent_queue_routing_gate.py
(the Phase D.6 gate this file is the filer-side half of).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import pytest  # noqa: E402
from _daemon_fixture import DaemonFixture  # noqa: E402
from _frontier_world import requires_frontier_world  # noqa: E402

AGENT = "alpha"
FOREIGN = "bravo"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, str(CORE_SCRIPTS / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rsc = _load("recurring_starvation_agent_queue_routing", "recurring-starvation-check.py")
cargo = _load("cargo_cult_agent_queue_routing", "cargo-cult-detector.py")


@pytest.fixture(autouse=True)
def _pinned_roster(monkeypatch):
    """Pin the active-agent roster the router and Phase D.6 read (rb-3076 class 5).

    The router only stamps FOREIGN, and the gate only treats it as routing AWAY, when
    FOREIGN is on the roster; a checkout that runs fewer agents than this file names
    would answer 200 for the control. Patched on `gates.capability_route`, the module
    object the in-process daemon imported.
    """
    import gates.capability_route as capability_route
    monkeypatch.setattr(capability_route, "get_active_agents", lambda: (AGENT, FOREIGN))


def _row(source: str = "agent", **over) -> dict:
    """One starved row in the shape scan() emits, with the measured live title:
    a per-agent tree-maintenance template whose content reads as the steward's."""
    row = {
        "goal_id": "g-001-03", "aspiration_id": "asp-001", "source": source,
        "title": "Knowledge tree maintenance", "age_hours": 358.5,
        "anchor_field": "lastAchievedAt", "anchor": "2026-09-07T20:35:30",
        "interval_hours": 72, "basis_hours": 117.45, "basis_reason": "recent_actual_p50",
        "ratio": 3.05, "declared_ratio": 4.98,
    }
    row.update(over)
    return row


def _filed_payload(monkeypatch, row: dict) -> dict:
    """The exact payload `_file_unblock` hands the daemon client."""
    captured: dict = {}
    monkeypatch.setattr(
        rsc._rt, "aspirations_add_goal",
        lambda asp_id, payload, source=None: (
            captured.update(payload) or {"id": "g-115-9001"}))
    rsc._file_unblock(row)
    return captured


def _filed_idea(monkeypatch, source: str, idea: dict):
    seen: dict = {}

    def fake(asp_id, record, source=None, overrides=None):
        seen["record"], seen["source"] = record, source
        return {"goal_id": "g-115-9002"}

    monkeypatch.setattr(cargo._rt, "aspirations_add_goal", fake)
    return cargo.file_idea("asp-001", source, idea), seen


# -- contract 1: the filer names the owner ------------------------------------

def test_agent_queue_unblock_names_the_queue_owner(monkeypatch):
    monkeypatch.setenv("MIND_AGENT", AGENT)
    assert _filed_payload(monkeypatch, _row("agent"))["intended_agent"] == AGENT


def test_world_unblock_keeps_the_routers_stamp(monkeypatch):
    monkeypatch.setenv("MIND_AGENT", AGENT)
    payload = _filed_payload(monkeypatch, _row("world", goal_id="g-353-04"))
    assert "intended_agent" not in payload, payload


def test_agent_queue_unblock_without_an_owner_falls_back_to_either(monkeypatch):
    monkeypatch.delenv("MIND_AGENT", raising=False)
    assert _filed_payload(monkeypatch, _row("agent"))["intended_agent"] == "either"


# -- contracts 2 and 3: through the real daemon -------------------------------

def _seed_aspiration(asp_id: str, title: str) -> dict:
    return {
        "id": asp_id, "title": title, "motivation": "filer routing regression",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-10-12T00:00:00",
        "goals": [{
            "id": f"g-{asp_id.split('-')[1]}-01", "title": "Seed goal",
            "description": "Pre-existing goal", "status": "pending",
            "priority": "MEDIUM", "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "origin_signal": "user_directive", "participants": ["agent"],
        }],
    }


def _make_world(tmp: Path) -> Path:
    world = tmp / "world"
    world.mkdir()
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(_seed_aspiration("asp-200", "world queue")) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _agent_queue(df) -> Path:
    """Seed asp-001 in the agent's PRIVATE queue AFTER the fixture starts (it builds
    its own project root); unseeded, every filing 404s, which is indistinguishable
    from the gate staying silent (test_agent_queue_routing_gate.py measured this)."""
    store = df.project_root / "agents" / AGENT / "aspirations.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    with open(store, "w", encoding="utf-8") as f:
        f.write(json.dumps(_seed_aspiration("asp-001", "Maintain Agent Health")) + "\n")
    (store.parent / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return store


def _post(port: int, body: dict) -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/add-goal"
           f"?asp_id=asp-001&source=agent")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", AGENT)
    # The bulk override cannot fan into the routing gate (it has no override slot);
    # it is sent so the OTHER gates (duplication, origin signal) stay out of the way.
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _stored(store: Path, title: str) -> dict | None:
    for line in store.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for g in json.loads(line).get("goals", []):
            if g.get("title") == title:
                return g
    return None


def test_agent_queue_unblock_is_accepted_and_stored_with_its_owner(monkeypatch):
    monkeypatch.setenv("MIND_AGENT", AGENT)
    payload = _filed_payload(monkeypatch, _row("agent"))
    with tempfile.TemporaryDirectory() as tmpd:
        with DaemonFixture(_make_world(Path(tmpd))) as df:
            store = _agent_queue(df)
            status, raw = _post(df.port, payload)
            goal = _stored(store, payload["title"])
    assert status == 200, f"the starvation Unblock was refused: {status} {raw[:400]}"
    assert goal is not None, "the Unblock did not land on the agent queue"
    assert goal.get("intended_agent") == AGENT, goal.get("intended_agent")


@requires_frontier_world(
    "the control needs the frontier world's capability-routing overlay to stamp a third party")
def test_same_payload_without_the_field_is_refused_by_the_gate(monkeypatch):
    """Control: the pre-fix payload. Fails by name if the router stops stamping."""
    monkeypatch.setenv("MIND_AGENT", AGENT)
    payload = _filed_payload(monkeypatch, _row("agent"))
    payload.pop("intended_agent")
    with tempfile.TemporaryDirectory() as tmpd:
        with DaemonFixture(_make_world(Path(tmpd))) as df:
            _agent_queue(df)
            status, raw = _post(df.port, payload)
    assert status == 400, (
        f"control did not reproduce the defect (got {status}): the router no longer "
        f"stamps a third party for this payload, so re-derive the fix before "
        f"deleting this test. {raw[:300]}")
    body = json.loads(raw)
    assert body.get("error") == "agent_queue_routing_blocked", body
    msg = (body.get("gate_output") or {}).get("message") or ""
    assert FOREIGN in msg and AGENT in msg, msg


# -- contract 4: the second filer ---------------------------------------------

def test_cargo_cult_idea_on_an_agent_queue_names_the_owner(monkeypatch):
    monkeypatch.setenv("MIND_AGENT", AGENT)
    idea = {"title": "Idea: Extend interval for g-001-07"}
    new_id, seen = _filed_idea(monkeypatch, "agent", idea)
    assert new_id == "g-115-9002"
    assert seen["record"]["intended_agent"] == AGENT, seen
    assert "intended_agent" not in idea, "file_idea mutated the caller's dict"


def test_cargo_cult_idea_on_the_world_queue_is_untouched(monkeypatch):
    monkeypatch.setenv("MIND_AGENT", AGENT)
    _, seen = _filed_idea(monkeypatch, "world", {"title": "Idea: x"})
    assert "intended_agent" not in seen["record"], seen


def test_cargo_cult_idea_keeps_a_caller_set_routing(monkeypatch):
    monkeypatch.setenv("MIND_AGENT", AGENT)
    _, seen = _filed_idea(
        monkeypatch, "agent", {"title": "Idea: x", "intended_agent": "either"})
    assert seen["record"]["intended_agent"] == "either", seen
