"""Auto-allocated aspiration ids never collide across the world and agent stores.

Measured 2026-08-28 (coach, zc-03): the world store held ``asp-002`` ("Operating
Rhythm") and the agent-local store minted its own ``asp-002`` ("Build ... Data
Infrastructure") because the allocator only scanned the store it was writing to.
Every consumer that resolves a bare ``asp-NNN`` / ``g-NNN-NN`` (selector, claims,
board posts, the tree) then saw two aspirations — and two goal id sequences —
behind one name. The allocator now takes ``max`` over the sibling stores too.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _post(port, path, query, body):
    qs = urllib.parse.urlencode(query)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}?{qs}",
        data=body.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", "alpha")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _ids(path: Path):
    if not path.exists():
        return []
    return [json.loads(line)["id"] for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _aspiration(title: str):
    return {"id": "auto", "title": title, "status": "active", "priority": "MEDIUM",
            "archived": False, "goals": []}


def test_agent_auto_id_skips_ids_already_used_by_the_world_store(running_daemon):
    root, port = running_daemon
    status, body = _post(port, "/v1/aspirations/add", {"source": "world"},
                         json.dumps(_aspiration("World-level aspiration")))
    assert status == 200, body
    world_id = json.loads(body)["aspiration_id"]

    status, body = _post(port, "/v1/aspirations/add", {"source": "agent"},
                         json.dumps(_aspiration("Agent-level aspiration")))
    assert status == 200, body
    agent_id = json.loads(body)["aspiration_id"]

    assert agent_id != world_id
    world_ids = _ids(root / "world" / "aspirations.jsonl")
    agent_ids = _ids(root / "agents" / "alpha" / "aspirations.jsonl")
    assert world_id in world_ids and agent_id in agent_ids
    assert not set(world_ids) & set(agent_ids), (world_ids, agent_ids)


def test_world_auto_id_skips_ids_already_used_by_an_agent_store(running_daemon):
    root, port = running_daemon
    status, body = _post(port, "/v1/aspirations/add", {"source": "agent"},
                         json.dumps(_aspiration("Agent-level first")))
    assert status == 200, body
    agent_id = json.loads(body)["aspiration_id"]

    status, body = _post(port, "/v1/aspirations/add", {"source": "world"},
                         json.dumps(_aspiration("World-level second")))
    assert status == 200, body
    world_id = json.loads(body)["aspiration_id"]

    assert world_id != agent_id
    assert int(world_id[4:]) > int(agent_id[4:])
