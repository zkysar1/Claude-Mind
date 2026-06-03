"""FW-2: agent-scoped goal writes must carry an explicit X-Mind-Agent header.

When MIND_AGENT injection misses (hook cold-start / cross-session), the
wrapper omits the X-Mind-Agent header. Before FW-2, the daemon's
AgentPathResolver silently fell back to the alphabetically-first agent
(typically "alpha"), so a source=agent write targeted the WRONG agent's
queue — bravo nearly set alpha's completed g-001-240 -> pending this way
(2026-05-25). These tests assert the guard refuses agent-scoped writes
without an explicit header, while leaving world-scoped writes (shared queue,
agent-agnostic) ungated. Mirrors the store.py:_require_agent_header precedent
(g-115-957).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest


def _post(port, path, query, body, *, agent="alpha", headers=None):
    """POST helper. agent=None omits the X-Mind-Agent header entirely."""
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_update_goal_agent_source_requires_header(running_daemon):
    """source=agent + no header → 400 missing_agent_header (never alpha fallback)."""
    _project_root, port = running_daemon
    body = json.dumps("pending").encode("utf-8")
    code, resp = _post(
        port, "/v1/aspirations/update-goal",
        {"id": "g-100-01", "field": "status", "source": "agent"},
        body, agent=None,
    )
    assert code == 400, resp
    assert "missing_agent_header" in resp, resp


def test_add_goal_agent_source_requires_header(running_daemon):
    """source=agent + no header → 400 missing_agent_header on add-goal too."""
    _project_root, port = running_daemon
    goal = {"title": "x", "status": "pending", "origin_signal": "user_directive",
            "description": "x" * 100}
    body = json.dumps(goal).encode("utf-8")
    code, resp = _post(
        port, "/v1/aspirations/add-goal",
        {"asp_id": "asp-100", "source": "agent"},
        body, agent=None,
    )
    assert code == 400, resp
    assert "missing_agent_header" in resp, resp


def test_world_source_not_gated_by_agent_guard(running_daemon):
    """source=world + no header → guard does NOT fire (shared queue is agent-agnostic).

    The request may fail for other reasons (e.g. goal-not-found), but it must
    NOT be refused with missing_agent_header — world writes don't need an agent.
    """
    _project_root, port = running_daemon
    body = json.dumps("pending").encode("utf-8")
    code, resp = _post(
        port, "/v1/aspirations/update-goal",
        {"id": "g-001-99", "field": "status", "source": "world"},
        body, agent=None,
    )
    assert "missing_agent_header" not in resp, resp


def test_agent_source_with_header_passes_guard(running_daemon):
    """source=agent WITH a valid header → guard passes (no false-positive refusal)."""
    _project_root, port = running_daemon
    goal = {"title": "x", "status": "pending", "origin_signal": "user_directive",
            "description": "x" * 100}
    body = json.dumps(goal).encode("utf-8")
    code, resp = _post(
        port, "/v1/aspirations/add-goal",
        {"asp_id": "asp-100", "source": "agent"},
        body, agent="alpha",
    )
    # Guard must not fire when the header is present. (Downstream add behavior
    # is out of scope for this guard test.)
    assert "missing_agent_header" not in resp, resp


# 1: extend FW-2 coverage from add-goal/update-goal to the 11 other
# source-accepting write handlers. Each tuple is (path, extra_query, body) —
# the minimal request that reaches the agent-header gate without being
# short-circuited by an earlier validation. source=agent is added by the test.
# claim is intentionally absent: it resolves to the world queue with a
# hardcoded literal ("world"), so it never accepts source=agent and the gate
# would be dead code there.
_AGENT_GATED_WRITE_HANDLERS = [
    ("/v1/aspirations/complete", {"asp_id": "asp-100"}, b"{}"),
    ("/v1/aspirations/complete-intent", {"asp_id": "asp-100"}, b"{}"),
    # complete-by validates agent_name (the completed_by attribution) with a
    # regex BEFORE the header gate; supply a valid one so we reach the gate,
    # which then refuses on the missing routing header.
    ("/v1/aspirations/complete-by", {"goal_id": "g-100-01", "agent_name": "alpha"}, b"{}"),
    ("/v1/aspirations/retire", {"asp_id": "asp-100"}, b"{}"),
    ("/v1/aspirations/release", {"id": "g-100-01"}, b"{}"),
    ("/v1/aspirations/archive-sweep", {}, b"{}"),
    ("/v1/aspirations/meta-update", {}, b"{}"),
    ("/v1/aspirations/clear-stale-claims", {}, b"{}"),
    # add + update parse and validate the body BEFORE the gate, so the body must
    # be a valid (and for update, enum-valid + non-empty) JSON object.
    ("/v1/aspirations/add", {}, json.dumps({"title": "x"}).encode("utf-8")),
    ("/v1/aspirations/recover-recurring", {}, b"{}"),
    ("/v1/aspirations/update", {"asp_id": "asp-100"},
     json.dumps({"priority": "HIGH"}).encode("utf-8")),
]


@pytest.mark.parametrize("path,extra_query,body", _AGENT_GATED_WRITE_HANDLERS)
def test_write_handler_agent_source_requires_header(running_daemon, path, extra_query, body):
    """source=agent + no header -> 400 missing_agent_header on every write handler.

    Without the gate, an empty X-Mind-Agent header on a source=agent write
    silently falls back to the alphabetically-first agent's queue
    (AgentPathResolver). These assertions pin the gate into all 11 handlers
    wired in g-115-1201, so a future refactor cannot remove one silently.
    """
    _project_root, port = running_daemon
    query = {**extra_query, "source": "agent"}
    code, resp = _post(port, path, query, body, agent=None)
    assert code == 400, f"{path}: expected 400, got {code}: {resp}"
    assert "missing_agent_header" in resp, f"{path}: {resp}"
