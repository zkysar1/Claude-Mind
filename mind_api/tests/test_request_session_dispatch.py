"""The daemon names the calling session for everything it does in-process ().

guard-2480: the daemon inherits the env of the session that SPAWNED it, then
serves every session on the box. Gates run in-process under its handlers and
log through _gate_log.log() without a session argument, so before the
dispatcher named each request's session, every firing carried the spawner's
MIND_SID. Children the daemon spawns inherit the same env, so a child that
stamps a session (board.py on a rollback post) needs the caller's handed to it.

Layers:
  1. HTTP round-trip through the real handler class: a route that logs the way
     a gate does, read back off disk.
  2. meta_backpressure._post_rollback_board: the env its board.py child gets.
"""
from __future__ import annotations

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

import _gate_log  # noqa: E402  (the SAME module the dispatcher sets the session on)

SPAWNER_SID = "spawner-session"


@pytest.fixture
def spawner_env(monkeypatch):
    """The env a daemon inherits: some OTHER session's identity."""
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("MIND_SID", SPAWNER_SID)


def _start_daemon_with_gate_route(project_root: Path, meta_dir: Path):
    """A minimal daemon whose one extra route logs a firing the way a gate does."""

    def fire_gate(ctx):
        _gate_log.log("request-session-probe", "noop",
                      caller="test_request_session_dispatch", meta_dir=meta_dir)
        return Response.json({"ok": True})

    server = Server(project_root=project_root, port=0)
    routes = server.routes
    routes[("GET", "/v1/test/fire-gate")] = fire_gate

    handler_cls = type(
        "_RequestSessionTestHandler", (_Handler,), {
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


def _fire(port: int, sid: str | None) -> None:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/test/fire-gate")
    req.add_header("X-Mind-Agent", "alpha")
    if sid is not None:
        req.add_header("X-Mind-Sid", sid)
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200


def _sessions(meta_dir: Path) -> list:
    out = []
    for path in _gate_log.firings_paths(meta_dir):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line)["session_id"])
    return out


@pytest.fixture
def daemon(project_root, tmp_path, spawner_env):
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir()
    httpd, port = _start_daemon_with_gate_route(project_root, meta_dir)
    try:
        yield port, meta_dir
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)


# --- 1. the dispatcher -------------------------------------------------------

def test_in_process_gate_firing_carries_the_callers_session(daemon):
    port, meta_dir = daemon
    _fire(port, "caller-session")
    assert _sessions(meta_dir) == ["caller-session"]


def test_request_without_a_session_writes_none_not_the_spawners(daemon):
    port, meta_dir = daemon
    _fire(port, None)
    assert _sessions(meta_dir) == [None]


def test_a_session_does_not_outlive_its_request(daemon):
    port, meta_dir = daemon
    _fire(port, "caller-session")
    _fire(port, None)
    assert _sessions(meta_dir) == ["caller-session", None]


# --- 2. the rollback board post's child env ----------------------------------

def _rollback_board_child_env(monkeypatch, headers: dict) -> dict:
    from mind_api.src.meta import meta_backpressure as mb
    seen = {}

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout="msg-test-1\n", stderr="")

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    ctx = SimpleNamespace(headers=headers,
                          paths=SimpleNamespace(project_root=REPO_ROOT))
    entry = {"file_kind": "goal-selection", "file_path": "meta/x.yaml",
             "revision_id": "r2", "previous_revision_id": "r1",
             "agent": "alpha", "reasoning": "test"}
    assert mb._post_rollback_board(ctx, entry) == "msg-test-1"
    return seen["env"]


def test_rollback_board_child_gets_the_callers_session(monkeypatch, spawner_env):
    env = _rollback_board_child_env(monkeypatch, {"x-mind-sid": "caller-session"})
    assert env["MIND_SID"] == "caller-session"


def test_rollback_board_child_without_a_session_gets_none(monkeypatch, spawner_env):
    env = _rollback_board_child_env(monkeypatch, {})
    # A bare `not in env` would print the whole env on failure, credentials included.
    carries_sid = "MIND_SID" in env
    assert not carries_sid, "the board.py child still inherits a session id"
