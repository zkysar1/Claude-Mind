"""test_gate_log_request_agent.py — whose agent a daemon write carries, and where a gate
firing lands.

Background (guard-2480, g-375-46): the mind_api daemon is long-lived and inherits the env
of the session that SPAWNED it, then serves every agent on the box. _fileops._agent_name(),
which attributes changelog and history writes, and _gate_log.log() both took the agent
from MIND_AGENT, and log() wrote to the module-level META_DIR: the spawner's, both.
g-375-41 fixed the session half of the same defect; this file pins the agent half.

Contract pinned here:
  _fileops._agent_name():
    (a) the agent set_request_agent() named for this request wins over MIND_AGENT;
    (b) a blank name names none, so the env stands;
    (c) reset_request_agent() restores the env value, so nothing outlives its request;
    (d) outside a request it is MIND_AGENT, else "system", as before.
  _gate_log.log() with no agent_name and no meta_dir, the shape of the gate sites that
  pass neither:
    (e) the agent is the request's, else MIND_AGENT, and an explicit agent_name wins;
    (f) the firing lands in the meta dir set_request_meta_dir() names, else META_DIR,
        and an explicit meta_dir wins;
    (g) a resolver that raises leaves the firing in META_DIR rather than losing it;
    (h) once _fileops is deleted from sys.modules and imported again, as some suites do,
        a firing reads the agent from the new copy, the one the dispatcher names it on.

Every test reaches _fileops through _fo(), the copy in sys.modules at call time, for the
reason (h) exists. META_DIR is patched to a tmp dir in every test, so no case can write
the live store. The dispatcher that names the agent and the meta dir once per request is
covered by mind_api/tests/test_request_agent_dispatch.py.
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
    "_gate_log_request_agent_under_test", CORE_SCRIPTS / "_gate_log.py")
gl_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gl_mod)

SPAWNER_AGENT = "spawner-agent"


def _fo():
    """The _fileops in sys.modules now. A collection-time import can be a stale copy,
    because some suites delete _fileops from sys.modules and import it again."""
    return importlib.import_module("_fileops")


@pytest.fixture(autouse=True)
def fallback_meta(monkeypatch, tmp_path):
    """The env a daemon inherits, and a META_DIR that is not the live one."""
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("MIND_AGENT", SPAWNER_AGENT)
    meta = tmp_path / "meta-fallback"
    meta.mkdir()
    monkeypatch.setattr(gl_mod, "META_DIR", meta)
    return meta


@pytest.fixture
def in_request():
    """Name an agent, and optionally a meta-dir resolver, the way the dispatcher does for
    one request. Everything named is reset when the test ends."""
    resets = []

    def name(agent, resolve=None):
        fo = _fo()
        resets.append((fo.reset_request_agent, fo.set_request_agent(agent)))
        if resolve is not None:
            resets.append((gl_mod.reset_request_meta_dir, gl_mod.set_request_meta_dir(resolve)))

    yield name
    for reset, token in reversed(resets):
        reset(token)


def _firings(meta_dir: Path) -> list[dict]:
    out = []
    for path in gl_mod.firings_paths(meta_dir):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _fire(**kwargs) -> None:
    gl_mod.log("request-agent-probe", "noop", caller="test_gate_log_request_agent", **kwargs)


# --- _fileops._agent_name() --------------------------------------------------

def test_the_request_agent_wins_over_the_env(in_request):
    in_request("caller-agent")
    assert _fo()._agent_name() == "caller-agent"


def test_a_blank_request_agent_names_none(in_request):
    in_request("  ")
    assert _fo()._agent_name() == SPAWNER_AGENT


def test_the_request_agent_does_not_outlive_its_request():
    fo = _fo()
    token = fo.set_request_agent("caller-agent")
    fo.reset_request_agent(token)
    assert fo._agent_name() == SPAWNER_AGENT


def test_outside_a_request_the_env_and_its_default_stand(monkeypatch):
    assert _fo()._agent_name() == SPAWNER_AGENT
    monkeypatch.delenv("MIND_AGENT")
    assert _fo()._agent_name() == "system"


# --- _gate_log.log(): the agent ----------------------------------------------

def test_a_firing_with_no_agent_name_carries_the_request_agent(in_request, fallback_meta):
    in_request("caller-agent")
    _fire()
    assert [r["agent"] for r in _firings(fallback_meta)] == ["caller-agent"]


def test_outside_a_request_a_firing_carries_the_env_agent(fallback_meta):
    _fire()
    assert [r["agent"] for r in _firings(fallback_meta)] == [SPAWNER_AGENT]


def test_an_explicit_agent_name_wins_over_the_request(in_request, fallback_meta):
    in_request("caller-agent")
    _fire(agent_name="explicit-agent")
    assert [r["agent"] for r in _firings(fallback_meta)] == ["explicit-agent"]


def test_a_reimported_fileops_is_the_one_a_firing_reads(monkeypatch, in_request, fallback_meta):
    """(h): the dispatcher names the agent on the _fileops in sys.modules at request time.
    After a suite deletes _fileops and imports it again, that is a NEW copy with its own
    var, and a firing that read the copy _gate_log first imported would carry the spawner.
    monkeypatch puts the original back when the test ends."""
    before = _fo()
    monkeypatch.delitem(sys.modules, "_fileops")
    assert _fo() is not before  # the re-import really made a new copy
    in_request("caller-agent")
    _fire()
    assert [r["agent"] for r in _firings(fallback_meta)] == ["caller-agent"]


# --- _gate_log.log(): the destination ----------------------------------------

def test_a_firing_with_no_meta_dir_lands_in_the_request_agents_meta(
        in_request, fallback_meta, tmp_path):
    caller_meta = tmp_path / "meta-caller"
    caller_meta.mkdir()
    in_request("caller-agent", resolve=lambda: caller_meta)
    _fire()
    assert [r["agent"] for r in _firings(caller_meta)] == ["caller-agent"]
    assert _firings(fallback_meta) == []


def test_an_explicit_meta_dir_wins_over_the_request(in_request, fallback_meta, tmp_path):
    caller_meta = tmp_path / "meta-caller"
    explicit_meta = tmp_path / "meta-explicit"
    caller_meta.mkdir()
    explicit_meta.mkdir()
    in_request("caller-agent", resolve=lambda: caller_meta)
    _fire(meta_dir=explicit_meta)
    assert len(_firings(explicit_meta)) == 1
    assert _firings(caller_meta) == []


def test_a_resolver_that_raises_keeps_the_firing_in_meta_dir(in_request, fallback_meta):
    def unresolvable():
        raise RuntimeError("no local-paths.conf for this agent")

    in_request("unknown-agent", resolve=unresolvable)
    _fire()
    assert [r["agent"] for r in _firings(fallback_meta)] == ["unknown-agent"]
