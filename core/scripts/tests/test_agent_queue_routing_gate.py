#!/usr/bin/env python3
"""Regression: the agent-queue routing blocker (, remedy (b) of ).

A goal filed with `source=agent` into an agent's PRIVATE queue while carrying an
`intended_agent` naming a DIFFERENT agent is unclaimable BY CONSTRUCTION: the
routed agent cannot write another agent's store, and the owning agent's selector
routes it away. The row stays visible and rankable forever without ever being
executable -- measured at RANK 1 for two consecutive iterations on one box.

Contracts pinned here (all through the REAL wired Phase D.6 blocker in
mind_api/src/endpoints/aspirations_write.py::_run_add_goal_pipeline, never by
calling the predicate directly -- that is the goal's outcome 4, and guard-1943:
a passing unit test proves the function, never the wiring):
  1. source=agent + foreign intended_agent  -> REFUSED, and the refusal names
     `--source world` as the route.
  2. The SAME body with source=world        -> accepted, intended_agent intact
     (the fix redirects the QUEUE, never the routing).
  3. source=agent + intended_agent = self   -> UNAFFECTED.
  4. source=agent + intended_agent="either" -> UNAFFECTED.
  5. The bulk override header does NOT bypass it -- every case below already
     sends X-Mind-Override-All, so case 1 refusing proves the gate has no
     override slot to fan into. That is deliberate: the refusal's remedy is
     itself a legal filing, so an escape hatch would only preserve the
     stranded shape.

Pattern: DaemonFixture + direct HTTP POST, mirroring
test_forge_curriculum_precondition_gate.py (the Phase D.5 sibling).
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402

AGENT = "alpha"
FOREIGN = "bravo"


def _seed_aspiration(asp_id: str, title: str) -> dict:
    return {
        "id": asp_id,
        "title": title,
        "motivation": "agent-queue routing gate regression",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-09-06T00:00:00",
        "goals": [{
            "id": f"g-{asp_id.split('-')[1]}-01",
            "title": "Seed goal",
            "description": "Pre-existing goal",
            "status": "pending",
            "priority": "MEDIUM",
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "origin_signal": "user_directive",
            "participants": ["agent"],
        }],
    }


def _make_world(tmp: Path) -> Path:
    """Tempdir world holding asp-200."""
    world = tmp / "world"
    world.mkdir()
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(_seed_aspiration("asp-200", "world queue"),
                           ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _agent_queue(df) -> Path:
    """Seed asp-001 in the agent's PRIVATE queue and return its path.

    Must run AFTER the fixture starts: DaemonFixture builds its own project root
    (agents/<agent>/ under it), so a queue pre-seeded beside the world dir is not
    the store the daemon resolves. Seeding it is load-bearing rather than
    cosmetic -- without it every 'unaffected' case below returns 404
    aspiration_not_found, which is indistinguishable from the gate correctly
    staying silent. Measured while writing this test: cases 3 and 4 first failed
    exactly that way.
    """
    store = df.project_root / "agents" / AGENT / "aspirations.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    with open(store, "w", encoding="utf-8") as f:
        f.write(json.dumps(_seed_aspiration("asp-001", "Maintain Agent Health"),
                           ensure_ascii=False) + "\n")
    (store.parent / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return store


def _add_goal(port: int, body: dict, *, asp_id: str, source: str,
              agent: str = AGENT) -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/add-goal"
           f"?asp_id={asp_id}&source={source}")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", agent)
    # Deliberate: proves the bulk override cannot fan into this gate (case 5).
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _body(title: str, intended: str | None) -> dict:
    b = {
        "title": title,
        "description": "agent-queue routing gate regression case",
        "priority": "MEDIUM",
        "participants": ["agent"],
        "origin_signal": "user_directive",
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
    }
    if intended is not None:
        b["intended_agent"] = intended
    return b


def _find_goal_by_title(store: Path, title: str) -> dict | None:
    text = store.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("title") == title:
                return g
    return None


def test_agent_queue_foreign_routing_is_refused():
    """Case 1: source=agent + foreign intended_agent -> refused, names --source world."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            # Seeded so the refusal is proven against an EXISTING target
            # aspiration -- otherwise a 404 could masquerade as the gate firing.
            _agent_queue(df)
            status, raw = _add_goal(
                df.port, _body("Stranded by routing", FOREIGN),
                asp_id="asp-001", source="agent")
    assert status == 400, f"expected refusal, got {status}: {raw[:400]}"
    payload = json.loads(raw)
    assert payload.get("error") == "agent_queue_routing_blocked", payload
    msg = (payload.get("gate_output") or {}).get("message") or ""
    # The refusal must NAME the route, not merely reject (goal outcome 1).
    assert "--source world" in msg, msg
    assert FOREIGN in msg and AGENT in msg, msg


def test_same_body_on_world_queue_succeeds_with_routing_intact():
    """Case 2: the identical body via source=world is accepted, routing preserved.

    This is the half that proves the fix redirects the QUEUE and not the routing
    -- a gate that simply banned foreign intended_agent would also pass case 1.
    """
    title = "Routed to a partner, filed on the world queue"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            status, raw = _add_goal(
                df.port, _body(title, FOREIGN),
                asp_id="asp-200", source="world")
            assert status == 200, f"world filing rejected: {status} {raw[:400]}"
            goal = _find_goal_by_title(world / "aspirations.jsonl", title)
    assert goal is not None, "goal did not land on the world queue"
    assert goal.get("intended_agent") == FOREIGN, goal.get("intended_agent")


def test_agent_queue_self_routing_is_unaffected():
    """Case 3: intended_agent naming the queue owner is a normal filing."""
    title = "Self-routed housekeeping"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            store = _agent_queue(df)
            status, raw = _add_goal(
                df.port, _body(title, AGENT), asp_id="asp-001", source="agent")
            assert status == 200, f"self-routed agent filing refused: {status} {raw[:400]}"
            goal = _find_goal_by_title(store, title)
    assert goal is not None, "self-routed goal did not land on the agent queue"


def test_agent_queue_either_sentinel_is_unaffected():
    """Case 4: the 'either' sentinel routes away from nobody."""
    title = "Either-routed housekeeping"
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            store = _agent_queue(df)
            status, raw = _add_goal(
                df.port, _body(title, "either"), asp_id="asp-001", source="agent")
            assert status == 200, f"either-routed agent filing refused: {status} {raw[:400]}"
            goal = _find_goal_by_title(store, title)
    assert goal is not None, "either-routed goal did not land on the agent queue"


def test_gate_is_role_agnostic_in_source():
    """The verdict may not depend on BODY_ROLE (guard-2783).

    Remedy (a) -- a selector-side filter -- was forbidden precisely because a
    component both roles run may not branch on role. This asserts the property
    on the SOURCE of the shipped remedy, which is the only place it can be
    checked cheaply and the only place a future edit would break it.
    """
    src = (CORE_SCRIPTS.parent.parent / "mind_api" / "src" / "endpoints"
           / "aspirations_write.py").read_text(encoding="utf-8")
    start = src.index("def _agent_queue_routing_violation")
    end = src.index("\n# ---", start)
    whole = src[start:end]
    # Strip the docstring before scanning. It NAMES the role tokens in order to
    # state that the predicate does not read them, so scanning the prose asserts
    # the exact opposite of the property. Executable lines only -- which is also
    # the only half a future edit could break.
    q1 = whole.index('"""')
    q2 = whole.index('"""', q1 + 3)
    body = whole[:q1] + whole[q2 + 3:]
    for token in ("BODY_ROLE", "body_role", "is_worker", "reducer", "worker"):
        assert token not in body, f"role branch {token!r} introduced into the gate body"


def test_routing_audit_mint_site_reconciles_its_route_to_its_queue():
    """The SECOND producer of the stranded shape is reconciled too (guard-3448).

    `_file_routing_audit_investigate` appends a goal DIRECTLY rather than
    routing through `_run_add_goal_pipeline`, so the Phase D.6 blocker above
    structurally cannot see it -- and it selects its own store, falling back to
    the AGENT queue when the target aspiration lives there, while hardcoding a
    partner name into `intended_agent`. That is the exact unclaimable row the
    blocker refuses on the add path, minted through a door the blocker does not
    watch.

    WHAT THIS TEST PROVES AND WHAT IT DOES NOT (guard-1943, stated rather than
    implied): it is a SOURCE-level pin, not a wiring test. Driving the real
    path needs the audit module to emit an `investigate_spec`, which no cheap
    fixture produces. It therefore catches the realistic regression -- someone
    restoring the literal roster expression at the mint site, or reconciling
    with a second hand-rolled rule instead of the blocker's predicate -- and it
    would NOT catch a change that broke the store-selection flag itself. The
    HTTP cases above are the wiring evidence for the add path; this site has
    none, and that gap is deliberate rather than overlooked.
    """
    src = (CORE_SCRIPTS.parent.parent / "mind_api" / "src" / "endpoints"
           / "aspirations_write.py").read_text(encoding="utf-8")
    start = src.index("def _file_routing_audit_investigate")
    end = src.index("\ndef ", start + 1)
    fn = src[start:end]

    # The flag is set on the agent-store fallback branch, and only there.
    assert "filed_into_agent_queue = False" in fn, fn[:200]
    assert "filed_into_agent_queue = True" in fn, fn[:200]

    # The reconciliation uses the BLOCKER's predicate, not a second rule.
    assert "_routes_away_from(" in fn, "mint site does not reuse the predicate"

    # The minted goal carries the reconciled value, never the raw roster pick.
    assert '"intended_agent": invest_route,' in fn, (
        "mint site no longer files the reconciled route")
    assert '"intended_agent": (\n' not in fn, (
        "the literal roster expression is back at the mint site")
