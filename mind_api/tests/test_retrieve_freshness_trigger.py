"""test_retrieve_freshness_trigger.py — /v1/retrieve asks the embedding-index
freshness tick to run (g-115-3684, 2026-09-29).

Before this, the tick ran only from iteration-close.sh, so a box in assistant
mode never refreshed its index. Invariants pinned here:
  1. Never under pytest — a test's tmp world must not refresh a real index.
  2. Rate-limited per process: at most one spawn per _FRESHNESS_MIN_INTERVAL.
  3. The spawn carries the REQUESTER's world and agent, detached, and a spawn
     failure never raises into the request.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "scripts"))
sys.path.insert(0, str(ROOT))

import importlib  # noqa: E402

ep = importlib.import_module("mind_api.src.endpoints.retrieve")


@pytest.fixture()
def spawns(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: calls.append((args, k)))
    monkeypatch.setattr(ep, "_freshness_last", [0.0])
    return calls


def test_never_spawns_under_pytest(spawns):
    ep._maybe_freshness_tick(Path("/w"), "alpha")
    assert spawns == []


def test_spawns_with_requester_world_and_is_rate_limited(spawns, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    ep._maybe_freshness_tick(Path("/some/world"), "bravo")
    ep._maybe_freshness_tick(Path("/some/world"), "bravo")  # inside the interval
    assert len(spawns) == 1
    args, kw = spawns[0]
    assert args[-1].endswith("embedding-index-freshness.py")
    assert kw["env"]["MIND_WORLD"] == str(Path("/some/world"))
    assert kw["env"]["MIND_AGENT"] == "bravo"


def test_interval_elapsed_allows_next_spawn(spawns, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    ep._maybe_freshness_tick(Path("/w"), "a")
    ep._freshness_last[0] -= ep._FRESHNESS_MIN_INTERVAL + 1
    ep._maybe_freshness_tick(Path("/w"), "a")
    assert len(spawns) == 2


def test_spawn_failure_never_raises(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(ep, "_freshness_last", [0.0])

    def _boom(*a, **k):
        raise OSError("no processes left")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    ep._maybe_freshness_tick(Path("/w"), "a")  # must not raise
