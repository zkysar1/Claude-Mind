"""test_goal_selector_team_state_parse_or_restore.py -- 8 / rb-2429 regression.

_load_team_state_cached must NOT crash the whole selector when it reads a
partial/truncated team-state.yaml. team-state.yaml is written by every agent via
_atomic_write_with_fallback (core/scripts/_fileops.py), which under sustained
multi-agent sync contention falls back to an in-place truncate-rewrite (~25.7%
of bursts, world/conventions/file-system-resilience.md). An unlocked reader
caught mid-fallback sees partial YAML.

team-state is ADVISORY scoring input (handoff liveness, critical_blockers) --
never correctness-critical for selection -- so a partial/unreadable read must
fail OPEN to {} after a bounded retry, not raise yaml.YAMLError up through the
selector and block goal selection every iteration until the writer finishes.

Before the fix: bare `yaml.safe_load(open(f))` -> ScannerError -> selector crash.
After the fix: bounded retry (_TEAM_STATE_READ_RETRIES), then fail-open {}.

Pattern mirrors test_goal_selector_critical_blocker_surface.py: import the
module, exercise the pure helper. WORLD_DIR is monkeypatched to a tmp dir and
the module-level read cache is reset per test.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# goal-selector.py requires MIND_AGENT to load (paths derive AGENT_DIR).
# Capture-restore around the module-level mutation so collection-time env
# pollution cannot leak to other tests (rb-1096, guard-588).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point WORLD_DIR at a tmp dir, reset the read cache, and zero the retry
    sleep so the corrupt-file path adds no real wall-clock to the suite."""
    monkeypatch.setattr(gs, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(gs, "_TEAM_STATE_CACHE", None)
    monkeypatch.setattr(gs, "_TEAM_STATE_READ_RETRY_SLEEP", 0)
    yield


def _write(tmp_path, text):
    (tmp_path / "team-state.yaml").write_text(text, encoding="utf-8")


def test_valid_yaml_returns_dict(tmp_path):
    _write(tmp_path, "agent_status:\n  alpha:\n    last_active: '2026-06-26T10:00:00'\n")
    out = gs._load_team_state_cached()
    assert isinstance(out, dict)
    assert out["agent_status"]["alpha"]["last_active"] == "2026-06-26T10:00:00"


def test_missing_file_returns_empty():
    # autouse fixture points WORLD_DIR at an empty tmp dir (no team-state.yaml).
    out = gs._load_team_state_cached()
    assert out == {}


def test_empty_file_returns_empty(tmp_path):
    _write(tmp_path, "")
    out = gs._load_team_state_cached()
    assert out == {}


def test_partial_yaml_fails_open_not_crash(tmp_path):
    # Mid-truncate-rewrite read shape: unterminated quote + flow mapping. This
    # is the exact class that raised yaml.ScannerError and crashed the selector
    # before the fix (rb-2429).
    _write(tmp_path, 'agent_status:\n  alpha: {last_active: "2026-06-26T10:00:00\n  bravo: [unterminated')
    out = gs._load_team_state_cached()  # must NOT raise
    assert out == {}


def test_unterminated_flow_sequence_fails_open(tmp_path):
    _write(tmp_path, "critical_blockers: [{goal_id: g-1, downstream_count: 9")
    out = gs._load_team_state_cached()  # must NOT raise
    assert out == {}


def test_failopen_result_is_cached(tmp_path):
    # After a fail-open the empty dict is cached for the run (read-once
    # contract) -- a persistently-broken file does not re-spin the retry per goal.
    _write(tmp_path, "foo: [1, 2, 3")
    first = gs._load_team_state_cached()
    assert first == {}
    # Even after the file becomes valid, the cached {} is returned (no re-read).
    _write(tmp_path, "agent_status: {alpha: {last_active: x}}")
    second = gs._load_team_state_cached()
    assert second == {}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
