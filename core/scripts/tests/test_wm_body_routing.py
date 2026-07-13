"""Body-tier WM path routing tests (Phase 1A, ).

Covers BOTH sides of the Mind/Body working-memory routing seam:

  CLI    core/scripts/wm.py            wm_path() / wm_lock_path() / WM_PATH
                                       (PEP 562 __getattr__) honor the
                                       BODY_WM_PATH env injected by
                                       bash-agent-inject.py when the bound
                                       session has a body-manifest.
  Daemon mind_api/src/agent_paths.py   AgentPaths.wm_path(unit_key) routes to
                                       the per-Body file ONLY when the forked
                                       body-WM-FILE exists (reducer-aware,
                                       g-306-62 — a manifest alone is NOT
                                       enough), else the agent-wide WM.

The load-bearing property is BACKWARD-COMPATIBILITY: with no Body (BODY_WM_PATH
unset / no manifest) every path collapses to today's agent-wide location, so the
~7 CLI direct-writers converted to `wm.wm_path()` in g-306-61
(consolidation-precheck, tree-encoding-drift-gate, loop-state-bump-counters,
recurring-loop-state-mutate, stale-sentinel-canary, session_artifacts_count,
reflect-bookkeeping) are byte-identical to the prior hardcoded path today.

Daemon-safe (no daemon_integration marker — pure path arithmetic, no subprocess
daemon, no live mind_api/state).

Run:
  python -m pytest core/scripts/tests/test_wm_body_routing.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

CORE_ROOT = Path(__file__).resolve().parent.parent.parent  # the core/ dir
PROJECT_ROOT = CORE_ROOT.parent                              # framework repo root
_SD = CORE_ROOT / "scripts"
if str(_SD) not in sys.path:
    sys.path.insert(0, str(_SD))

# wm.py asserts an agent at import time () — bind one before import.
# _SAVED_AGENT capture per guard-588/rb-1096 module-level env hygiene: fill
# only when absent (setdefault semantics); conftest env isolation re-seeds
# between test modules, so no destructive override needs restoring here.
_SAVED_AGENT = os.environ.get("MIND_AGENT")
if _SAVED_AGENT is None:
    os.environ["MIND_AGENT"] = "alpha"

import wm  # noqa: E402  — core/scripts/wm.py (the module the importers now call)
from _paths import AGENT_DIR  # noqa: E402


def _load_agent_paths():
    """Import mind_api/src/agent_paths lazily, skipping the daemon-side tests if
    unavailable. Appended (not inserted) so core/scripts modules keep priority
    in the shared suite's global sys.path."""
    ms = PROJECT_ROOT / "mind_api" / "src"
    if str(ms) not in sys.path:
        sys.path.append(str(ms))
    return pytest.importorskip("agent_paths").AgentPaths


# ─────────────────────────── CLI side (core/scripts/wm.py) ───────────────────────────

def test_cli_wm_path_default_when_body_env_unset(monkeypatch):
    if AGENT_DIR is None:
        pytest.skip("no agent bound in test env")
    monkeypatch.delenv("BODY_WM_PATH", raising=False)
    assert wm.wm_path() == AGENT_DIR / "session" / "working-memory.yaml"


def test_cli_wm_path_routes_to_body_env(monkeypatch, tmp_path):
    body = tmp_path / "sessions" / "u1" / "working-memory.yaml"
    monkeypatch.setenv("BODY_WM_PATH", str(body))
    assert wm.wm_path() == body


def test_cli_wm_path_blank_body_env_falls_back(monkeypatch):
    if AGENT_DIR is None:
        pytest.skip("no agent bound in test env")
    # Whitespace/empty BODY_WM_PATH must be treated as unset (.strip() falsy).
    monkeypatch.setenv("BODY_WM_PATH", "   ")
    assert wm.wm_path() == AGENT_DIR / "session" / "working-memory.yaml"


def test_cli_wm_lock_path_is_body_aware(monkeypatch, tmp_path):
    body = tmp_path / "working-memory.yaml"
    monkeypatch.setenv("BODY_WM_PATH", str(body))
    assert wm.wm_lock_path() == body.with_suffix(".lock")


def test_cli_wm_path_getattr_shim_honors_body(monkeypatch, tmp_path):
    # PEP 562 module __getattr__: `wm.WM_PATH` / `wm.WM_LOCK_PATH` resolve through
    # the per-Body-aware functions at access time (keeps the legacy
    # `from wm import WM_PATH` importers working post-routing).
    body = tmp_path / "b" / "working-memory.yaml"
    monkeypatch.setenv("BODY_WM_PATH", str(body))
    assert wm.WM_PATH == body
    assert wm.WM_LOCK_PATH == body.with_suffix(".lock")


# ─────────────────────────── Daemon side (mind_api/src/agent_paths.py) ───────────

def _paths(tmp_path):
    AgentPaths = _load_agent_paths()
    return AgentPaths(
        agent_name="alpha",
        world=tmp_path / "world",
        meta=tmp_path / "meta",
        agent=tmp_path / "agents" / "alpha",
        project_root=tmp_path,
    )


def test_daemon_wm_path_agent_wide_without_unit_key(tmp_path):
    p = _paths(tmp_path)
    assert p.wm_path() == p.state_dir / "working-memory.yaml"


def test_daemon_wm_path_dormant_without_body_wm_file(tmp_path):
    # unit_key supplied but no forked body-WM-file => still agent-wide (dormant
    # routing, the one-Body case that holds until a 2nd Body forks).
    p = _paths(tmp_path)
    assert p.wm_path("u1") == p.state_dir / "working-memory.yaml"


def test_daemon_wm_path_reducer_manifest_only_stays_agent_wide(tmp_path):
    # REDUCER backward-compat keystone (): a manifest WITHOUT a forked
    # body-WM-file (what /start FORK-BODY writes for the reducer) must NOT flip
    # routing — the reducer stays on the agent-wide WM.
    p = _paths(tmp_path)
    sd = p.session_dir("u1")
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "body-manifest.yaml").write_text(
        "unitKey: u1\nmindKey: alpha\nforked_wm_hash: null\n", encoding="utf-8")
    assert p.wm_path("u1") == p.state_dir / "working-memory.yaml"


def test_daemon_wm_path_routes_to_body_with_forked_wm_file(tmp_path):
    # NON-reducer Body: the forked body-WM-file exists => routing flips per-Body.
    p = _paths(tmp_path)
    sd = p.session_dir("u1")
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "working-memory.yaml").write_text("slot: forked\n", encoding="utf-8")
    assert p.wm_path("u1") == p.body_wm_path("u1") == sd / "working-memory.yaml"


# ─── retrieval-session manifest routing (Phase 1D, ) ───
# Same reducer-aware activation signal (the forked body-WM-file) as wm_path —
# these pin that the retrieve endpoint's utilization manifest routes per-Body
# identically, so a reducer/observer stays agent-wide (dormant single-runner).

def test_daemon_retrieval_session_agent_wide_without_unit_key(tmp_path):
    p = _paths(tmp_path)
    assert p.retrieval_session_path() == p.state_dir / "retrieval-session.json"


def test_daemon_retrieval_session_dormant_without_body_wm_file(tmp_path):
    # unit_key supplied but no forked body-WM-file => agent-wide (dormant).
    p = _paths(tmp_path)
    assert p.retrieval_session_path("u1") == p.state_dir / "retrieval-session.json"


def test_daemon_retrieval_session_reducer_manifest_only_stays_agent_wide(tmp_path):
    # REDUCER keystone (matches wm_path): a manifest WITHOUT a forked body-WM-file
    # must NOT flip routing — the reducer stays on the agent-wide manifest.
    p = _paths(tmp_path)
    sd = p.session_dir("u1")
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "body-manifest.yaml").write_text(
        "unitKey: u1\nmindKey: alpha\nforked_wm_hash: null\n", encoding="utf-8")
    assert p.retrieval_session_path("u1") == p.state_dir / "retrieval-session.json"


def test_daemon_retrieval_session_routes_to_body_with_forked_wm_file(tmp_path):
    # NON-reducer Body: the forked body-WM-file exists => per-Body manifest.
    p = _paths(tmp_path)
    sd = p.session_dir("u1")
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "working-memory.yaml").write_text("slot: forked\n", encoding="utf-8")
    assert p.retrieval_session_path("u1") == sd / "body-retrieval-session.json"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
