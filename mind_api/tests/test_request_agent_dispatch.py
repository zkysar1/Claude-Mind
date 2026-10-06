"""The daemon names the calling agent for everything it does in-process ().

guard-2480: the daemon inherits the env of the session that SPAWNED it, then serves every
agent on the box. g-375-41 made gate firings carry the caller's session; the agent half
stayed open. Gates that log without an agent_name or meta_dir stamped the spawner's
MIND_AGENT and wrote to the spawner's META_DIR, _fileops._agent_name() attributed changelog
and history writes to the spawner, and children the daemon spawns inherited the spawner's
MIND_AGENT.

Layers:
  1. HTTP round-trip through the real handler class: a route that logs the way those gates
     do and reports _agent_name(), on a daemon spawned as alpha and called as bravo, whose
     meta dir is its own. The firing is read back off disk. One case first deletes _fileops
     from sys.modules, as some suites do earlier in a full run, so the dispatcher imports a
     fresh copy and names the agent on it.
  2. The children: meta_backpressure's rollback board post and history.py restore, and a
     curriculum command_check, each read from the env its subprocess.run receives.

Under test: mind_api/src/server.py (the dispatcher), core/scripts/_fileops.py and
core/scripts/_gate_log.py (the request's agent and meta dir), and the child sites in
mind_api/src/meta/meta_backpressure.py and mind_api/src/endpoints/curriculum.py. Naming the
files here is also what selects this test in run-scoped-suite.py, which maps by file name.

_fileops and _gate_log are reached through _live(), the copy in sys.modules at call time,
which is the copy the dispatcher imports per request. Every env check reads one key into a
local before asserting, so a failure never prints the whole env (guard-7475).
"""
from __future__ import annotations

import importlib
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from mind_api.src import lifecycle
from mind_api.src.server import Response, Server, _Handler

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "core" / "scripts"))

SPAWNER = "alpha"
CALLER = "bravo"


def _live(name: str):
    """The module in sys.modules now, the one the dispatcher imports per request. A
    collection-time import can be a stale copy whose ContextVar the dispatcher never sets."""
    return importlib.import_module(name)


@pytest.fixture
def spawner_env(monkeypatch, tmp_path):
    """The env a daemon inherits: alpha's. META_DIR is moved off the live store, so a firing
    that fell back to it could never write there."""
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("MIND_AGENT", SPAWNER)
    monkeypatch.setenv("MIND_SID", "spawner-session")
    fallback = tmp_path / "meta-fallback"
    fallback.mkdir()
    monkeypatch.setattr(_live("_gate_log"), "META_DIR", fallback)
    return fallback


# --- 1. the dispatcher -------------------------------------------------------

def _start_daemon_with_gate_route(project_root: Path):
    """A minimal daemon whose one extra route logs a firing the way the gates that pass
    neither agent_name nor meta_dir do, and reports whom a write would be attributed to."""

    def fire_gate(ctx):
        _live("_gate_log").log("request-agent-probe", "noop", caller="test_request_agent_dispatch")
        return Response.json({"attributed_to": _live("_fileops")._agent_name()})

    server = Server(project_root=project_root, port=0)
    routes = server.routes
    routes[("GET", "/v1/test/fire-gate")] = fire_gate

    handler_cls = type(
        "_RequestAgentTestHandler", (_Handler,), {
            "routes": routes,
            "resolver": server.resolver,
            "access_log_path": lifecycle.access_log(project_root),
            "pid": 0,
            "port": 0,
        },
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = httpd.server_address[1]
    handler_cls.port = port
    handler_cls.pid = 99999
    lifecycle.write_pid_and_port_atomic(project_root, 99999, port)

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                break
        except OSError:
            time.sleep(0.02)
    return httpd, port


def _fire(port: int, agent: str | None) -> str:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/test/fire-gate")
    if agent is not None:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        return json.loads(resp.read())["attributed_to"]


def _agents(meta_dir: Path) -> list:
    out = []
    for path in _live("_gate_log").firings_paths(meta_dir):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line)["agent"])
    return out


@pytest.fixture
def daemon(project_root, tmp_path, spawner_env):
    """alpha's daemon, with bravo's local-paths.conf pointing at a meta dir of its own."""
    alpha_meta = project_root / "meta"
    bravo_meta = tmp_path / "meta-bravo"
    bravo_meta.mkdir()
    (project_root / "agents" / CALLER / "local-paths.conf").write_text(
        f"WORLD_PATH={(project_root / 'world').as_posix()}\nMETA_PATH={bravo_meta.as_posix()}\n",
        encoding="utf-8")
    httpd, port = _start_daemon_with_gate_route(project_root)
    try:
        yield port, alpha_meta, bravo_meta, spawner_env
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)


def test_a_firing_for_the_caller_carries_the_caller_and_lands_in_its_meta(daemon):
    port, alpha_meta, bravo_meta, fallback = daemon
    assert _fire(port, CALLER) == CALLER
    assert _agents(bravo_meta) == [CALLER]
    assert _agents(alpha_meta) == []
    assert _agents(fallback) == []


def test_a_request_naming_no_agent_keeps_the_spawner(daemon):
    """The positive control: with no caller named, the old attribution stands, so the test
    above can tell the two apart."""
    port, alpha_meta, bravo_meta, fallback = daemon
    assert _fire(port, None) == SPAWNER
    assert _agents(alpha_meta) == [SPAWNER]
    assert _agents(bravo_meta) == []


def test_the_agent_does_not_outlive_its_request(daemon):
    port, alpha_meta, bravo_meta, fallback = daemon
    assert _fire(port, CALLER) == CALLER
    assert _fire(port, None) == SPAWNER
    assert _agents(bravo_meta) == [CALLER]
    assert _agents(alpha_meta) == [SPAWNER]


def test_a_firing_for_the_caller_survives_a_reimported_fileops(daemon, monkeypatch):
    """The full-run order that first failed here: a suite earlier in the run deleted
    _fileops from sys.modules, so the dispatcher imports a fresh copy and names the agent
    on it. The firing and the attribution must both read that copy. monkeypatch puts the
    original back when the test ends."""
    port, alpha_meta, bravo_meta, fallback = daemon
    before = _live("_fileops")
    monkeypatch.delitem(sys.modules, "_fileops")
    assert _fire(port, CALLER) == CALLER
    assert _live("_fileops") is not before  # the dispatcher really imported a new copy
    assert _agents(bravo_meta) == [CALLER]
    assert _agents(alpha_meta) == []


# --- 2. the children ---------------------------------------------------------

def _capture_run(monkeypatch, module, rc: int, stdout: str = "") -> list:
    """Replace module.subprocess.run; return the list each call's env is appended to."""
    envs = []

    def fake_run(argv, **kwargs):
        envs.append(kwargs.get("env"))
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return envs


def _ctx(agent: str | None) -> SimpleNamespace:
    headers = {"x-mind-agent": agent} if agent is not None else {}
    return SimpleNamespace(headers=headers, paths=SimpleNamespace(project_root=REPO_ROOT))


def test_rollback_board_child_runs_as_the_caller_when_the_entry_names_no_agent(
        monkeypatch, spawner_env):
    from mind_api.src.meta import meta_backpressure as mb
    envs = _capture_run(monkeypatch, mb, 0, stdout="msg-test-1\n")
    entry = {"file_kind": "goal-selection", "file_path": "meta/x.yaml",
             "revision_id": "r2", "previous_revision_id": "r1", "reasoning": "test"}
    assert mb._post_rollback_board(_ctx(CALLER), entry) == "msg-test-1"
    child_agent = envs[0].get("MIND_AGENT")
    assert child_agent == CALLER


def test_rollback_board_child_keeps_the_entrys_own_agent(monkeypatch, spawner_env):
    from mind_api.src.meta import meta_backpressure as mb
    envs = _capture_run(monkeypatch, mb, 0, stdout="msg-test-1\n")
    entry = {"file_kind": "goal-selection", "file_path": "meta/x.yaml",
             "revision_id": "r2", "previous_revision_id": "r1",
             "agent": "echo", "reasoning": "test"}
    mb._post_rollback_board(_ctx(CALLER), entry)
    child_agent = envs[0].get("MIND_AGENT")
    assert child_agent == "echo"


def test_history_restore_child_runs_as_the_caller(monkeypatch, spawner_env):
    from mind_api.src.meta import meta_backpressure as mb
    envs = _capture_run(monkeypatch, mb, 1)
    mon = {"history_snapshot": "/nowhere/.history/x.yaml.v1", "file_path": "meta/x.yaml",
           "monitor_kind": "goal-selection", "revision_id": "r2"}
    rollback = mb._evolution_rollback(_ctx(CALLER), mon, {}, {}, {})
    assert rollback["rolled_back"] is False  # rc 1 stops before any stream or board write
    child_agent = envs[0].get("MIND_AGENT")
    assert child_agent == CALLER


def test_history_restore_child_keeps_the_env_when_no_caller_is_named(monkeypatch, spawner_env):
    from mind_api.src.meta import meta_backpressure as mb
    envs = _capture_run(monkeypatch, mb, 1)
    mon = {"history_snapshot": "/nowhere/.history/x.yaml.v1", "file_path": "meta/x.yaml",
           "monitor_kind": "goal-selection", "revision_id": "r2"}
    mb._evolution_rollback(_ctx(None), mon, {}, {}, {})
    child_agent = envs[0].get("MIND_AGENT")
    assert child_agent == SPAWNER


def test_curriculum_command_check_runs_as_the_curriculums_agent(monkeypatch, spawner_env,
                                                               tmp_path):
    from mind_api.src.endpoints import curriculum as cur
    envs = _capture_run(monkeypatch, cur, 0)
    gate = {"type": "command_check", "command": "bash core/scripts/probe-gate-check.sh"}
    passed, value = cur._evaluate_gate(gate, tmp_path, tmp_path, None, CALLER)
    assert (passed, value) == (True, 0)
    child_agent = envs[0].get("MIND_AGENT")
    assert child_agent == CALLER
