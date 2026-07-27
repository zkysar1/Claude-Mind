"""pr_merged predicate ( / rb-3995) — branch-per-goal estate gate.

All gh probes are monkeypatched; no network. Cache isolation via a tmp agent
dir + MIND_AGENT env so the per-agent session cache never touches live state.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import predicate  # noqa: E402


PRED = {"id": "pr89", "type": "pr_merged", "repo": "acme/widget-service", "pr": 89}


@pytest.fixture()
def agent_cache(tmp_path, monkeypatch):
    """Bind a tmp agent with a session dir; return the cache file path."""
    agent_root = tmp_path / "agents" / "testag"
    (agent_root / "session").mkdir(parents=True)
    monkeypatch.setenv("MIND_AGENT", "testag")
    monkeypatch.setattr(predicate, "_agent_dir", lambda name: agent_root)
    return agent_root / "session" / "pr-merge-state-cache.json"


def _gh(state, calls=None):
    def fake_run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"state": state}), stderr="")
    return fake_run


def _gh_error(calls=None):
    def fake_run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="gh: network unreachable")
    return fake_run


def test_given_pr_merged_when_evaluated_then_passes_and_caches(agent_cache, monkeypatch):
    monkeypatch.setattr(predicate.subprocess, "run", _gh("MERGED"))
    r = predicate.evaluate(dict(PRED))
    assert r.passed is True
    assert r.observed_value["state"] == "MERGED"
    cached = json.loads(agent_cache.read_text())
    assert cached["acme/widget-service#89"]["state"] == "MERGED"


def test_given_pr_open_when_evaluated_then_fails_with_state_reason(agent_cache, monkeypatch):
    monkeypatch.setattr(predicate.subprocess, "run", _gh("OPEN"))
    r = predicate.evaluate(dict(PRED))
    assert r.passed is False
    assert "OPEN" in r.reason and "89" in r.reason


def test_given_pr_closed_unmerged_when_evaluated_then_fails_with_abandoned_warning(agent_cache, monkeypatch):
    monkeypatch.setattr(predicate.subprocess, "run", _gh("CLOSED"))
    r = predicate.evaluate(dict(PRED))
    assert r.passed is False
    assert "CLOSED without merge" in r.reason


def test_given_fresh_open_cache_when_evaluated_then_no_probe_fires(agent_cache, monkeypatch):
    calls = []
    monkeypatch.setattr(predicate.subprocess, "run", _gh("OPEN", calls))
    predicate.evaluate(dict(PRED))          # probe 1 populates cache
    r = predicate.evaluate(dict(PRED))      # within TTL -> cache hit
    assert r.passed is False
    assert r.observed_value["source"] == "cache"
    assert len(calls) == 1


def test_given_merged_cache_when_evaluated_then_terminal_no_reprobe(agent_cache, monkeypatch):
    calls = []
    monkeypatch.setattr(predicate.subprocess, "run", _gh("MERGED", calls))
    predicate.evaluate(dict(PRED))
    # Age the entry far past any TTL — MERGED must stay terminal.
    cache = json.loads(agent_cache.read_text())
    cache["acme/widget-service#89"]["checked_at"] = "2020-01-01T00:00:00"
    agent_cache.write_text(json.dumps(cache))
    r = predicate.evaluate(dict(PRED))
    assert r.passed is True
    assert r.observed_value["source"] == "cache"
    assert len(calls) == 1


def test_given_expired_open_cache_when_evaluated_then_reprobes(agent_cache, monkeypatch):
    calls = []
    monkeypatch.setattr(predicate.subprocess, "run", _gh("OPEN", calls))
    predicate.evaluate(dict(PRED))
    cache = json.loads(agent_cache.read_text())
    cache["acme/widget-service#89"]["checked_at"] = "2020-01-01T00:00:00"
    agent_cache.write_text(json.dumps(cache))
    predicate.evaluate(dict(PRED))
    assert len(calls) == 2


def test_given_probe_error_with_stale_cache_when_evaluated_then_stale_verdict_used(agent_cache, monkeypatch):
    monkeypatch.setattr(predicate.subprocess, "run", _gh("OPEN"))
    predicate.evaluate(dict(PRED))
    cache = json.loads(agent_cache.read_text())
    cache["acme/widget-service#89"]["checked_at"] = "2020-01-01T00:00:00"
    agent_cache.write_text(json.dumps(cache))
    monkeypatch.setattr(predicate.subprocess, "run", _gh_error())
    r = predicate.evaluate(dict(PRED))
    assert r.passed is False
    assert r.observed_value["source"] == "stale-cache"


def test_given_probe_error_without_cache_when_evaluated_then_fails_closed(agent_cache, monkeypatch):
    monkeypatch.setattr(predicate.subprocess, "run", _gh_error())
    r = predicate.evaluate(dict(PRED))
    assert r.passed is False
    assert "gh probe failed" in r.reason


def test_given_no_bound_agent_when_evaluated_then_uncached_but_functional(tmp_path, monkeypatch):
    monkeypatch.delenv("MIND_AGENT", raising=False)
    calls = []
    monkeypatch.setattr(predicate.subprocess, "run", _gh("MERGED", calls))
    r = predicate.evaluate(dict(PRED))
    assert r.passed is True
    r2 = predicate.evaluate(dict(PRED))
    assert r2.passed is True
    assert len(calls) == 2  # no cache without an agent — probes each time


def test_given_malformed_fields_when_evaluated_then_fail_closed_never_probe(monkeypatch):
    def boom(cmd, **kw):
        raise AssertionError("probe must not fire on malformed predicate")
    monkeypatch.setattr(predicate.subprocess, "run", boom)
    assert predicate.evaluate({"type": "pr_merged", "repo": "noslash", "pr": 1}).passed is False
    assert predicate.evaluate({"type": "pr_merged", "repo": "o/r", "pr": "89"}).passed is False
    assert predicate.evaluate({"type": "pr_merged", "repo": "o/r", "pr": True}).passed is False
