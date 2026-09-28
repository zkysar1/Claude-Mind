"""test_gate_log_request_session.py — whose session a gate firing is stamped with.

Background (guard-2480, g-375-41): the mind_api daemon is long-lived and
inherits the env of the session that SPAWNED it, then serves every session on
the box. Gates run in-process under its handlers, so a firing that took its
`session_id` from MIND_SID named the spawner, never the caller. The same held
for the override-bypass ledger, whose `agent` was env-derived too.

Contract pinned here:
  _gate_log.log() session_id resolution, in order:
    (a) an explicit argument wins, and None writes no session;
    (b) otherwise the session set_request_session() named for this request,
        where a request that named none writes None, never the env value;
    (c) otherwise MIND_SID, so a CLI or subprocess caller is unchanged;
    (d) reset_request_session() restores (c), so nothing outlives its request.
  _override_helpers.audit_bulk_override():
    (e) explicit agent_name / session_id win over the env, None writes none;
    (f) omitted, both come from the env (the CLI caller's own).

Every case writes to tmp_path and reads back off disk through firings_paths().
The dispatcher that calls set_request_session() once per request is covered by
mind_api/tests/test_request_session_dispatch.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

spec = importlib.util.spec_from_file_location(
    "_gate_log_request_session_under_test", CORE_SCRIPTS / "_gate_log.py")
gl_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gl_mod)

import _override_helpers as oh_mod  # noqa: E402

SPAWNER_SID = "spawner-session"
SPAWNER_AGENT = "spawner-agent"


@pytest.fixture(autouse=True)
def _spawner_env(monkeypatch):
    """The env a daemon inherits: some OTHER session's identity."""
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("MIND_SID", SPAWNER_SID)
    monkeypatch.setenv("MIND_AGENT", SPAWNER_AGENT)


def _firings(meta_dir: Path) -> list[dict]:
    out = []
    for path in gl_mod.firings_paths(meta_dir):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _only_firing(meta_dir: Path) -> dict:
    recs = _firings(meta_dir)
    assert len(recs) == 1, f"expected exactly one firing, got {len(recs)}"
    return recs[0]


def _in_request(sid):
    """Run the rest of the test as if a server had named `sid` for this request."""
    return gl_mod.set_request_session(sid)


# --- _gate_log.log() ---------------------------------------------------------

def test_explicit_session_wins_over_env_and_request(tmp_path):
    """(a) An explicit session_id beats both the env and the request's session."""
    token = _in_request("request-session")
    try:
        gl_mod.log("request-session-test", "pass", meta_dir=tmp_path,
                   session_id="explicit-session")
    finally:
        gl_mod.reset_request_session(token)
    assert _only_firing(tmp_path)["session_id"] == "explicit-session"


def test_explicit_none_writes_no_session(tmp_path):
    """(a) None is a real answer: no session, not the env's."""
    gl_mod.log("request-session-test", "pass", meta_dir=tmp_path, session_id=None)
    assert _only_firing(tmp_path)["session_id"] is None


def test_omitted_takes_the_request_session(tmp_path):
    """(b) The daemon path: a gate that passes nothing is stamped with the caller."""
    token = _in_request("request-session")
    try:
        gl_mod.log("request-session-test", "block", meta_dir=tmp_path)
    finally:
        gl_mod.reset_request_session(token)
    assert _only_firing(tmp_path)["session_id"] == "request-session"


@pytest.mark.parametrize("named", [None, "", "   "], ids=["none", "empty", "blank"])
def test_request_that_named_no_session_writes_none_not_env(tmp_path, named):
    """(b) A request without an `x-mind-sid` header must not fall back to the env."""
    token = _in_request(named)
    try:
        gl_mod.log("request-session-test", "noop", meta_dir=tmp_path)
    finally:
        gl_mod.reset_request_session(token)
    assert _only_firing(tmp_path)["session_id"] is None


def test_omitted_outside_a_request_reads_env(tmp_path):
    """(c) CLI / subprocess callers are unchanged: their env is their own."""
    gl_mod.log("request-session-test", "pass", meta_dir=tmp_path)
    assert _only_firing(tmp_path)["session_id"] == SPAWNER_SID


def test_reset_restores_the_env_fallback(tmp_path):
    """(d) After the request ends, a later call reads the env again."""
    gl_mod.reset_request_session(_in_request("request-session"))
    gl_mod.log("request-session-test", "pass", meta_dir=tmp_path)
    assert _only_firing(tmp_path)["session_id"] == SPAWNER_SID


# --- _override_helpers.audit_bulk_override() --------------------------------

def _ledger(world_dir: Path) -> list[dict]:
    path = world_dir / "override-bypass-ledger.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _audit(world_dir: Path, **identity) -> dict:
    oh_mod.audit_bulk_override("tok123", "request-session test", ["override_signal"],
                               {"caller": "test"}, world_dir=world_dir, **identity)
    recs = _ledger(world_dir)
    assert len(recs) == 1, f"expected exactly one ledger row, got {len(recs)}"
    return recs[0]


def test_audit_explicit_identity_wins_over_env(tmp_path):
    """(e) The daemon passes the request's agent and session; neither is the env's."""
    rec = _audit(tmp_path, agent_name="caller-agent", session_id="request-session")
    assert (rec["agent"], rec["session_id"]) == ("caller-agent", "request-session")


def test_audit_explicit_none_session_writes_none(tmp_path):
    """(e) A request without a session writes none rather than the spawner's."""
    rec = _audit(tmp_path, agent_name="caller-agent", session_id=None)
    assert rec["session_id"] is None


def test_audit_omitted_identity_reads_env(tmp_path):
    """(f) CLI callers omit both and keep today's env-derived identity."""
    rec = _audit(tmp_path)
    assert (rec["agent"], rec["session_id"]) == (SPAWNER_AGENT, SPAWNER_SID)
