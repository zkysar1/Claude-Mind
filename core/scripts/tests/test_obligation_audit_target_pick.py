"""Regression tests for obligation-audit._file_investigate_goal target selection ().

WHY THIS EXISTS
obligation-audit is the enforcement arm for FALSE ABBREVIATION CLAIMS (Phase 9.5d):
when it detects an agent claimed an obligation was legitimately abbreviated but the
claimed condition was not true, it files a HIGH Investigate goal. That filing path was
STRUCTURALLY DEAD for its entire life.

The bug: it read the compact via `world-cat.sh aspirations-compact.json`, which resolves
to $WORLD_DIR/aspirations-compact.json — a path that does not exist. The compact is
written by load-aspirations-compact.sh to AGENT_DIR/session/aspirations-compact.json.
world-cat.sh is a bare `cat`, so the miss returned rc=1 with empty stdout, the
`if r.returncode == 0 and r.stdout.strip()` guard failed, target_id stayed None, and the
function returned at `if not target_id` WITHOUT EVER FILING.

It was latent rather than observed-failing: iteration-close-stderr.log had zero
occurrences of both the no-target warn AND the filed-goal success line, meaning the
function had never been reached. It would have failed the first time a false claim was
detected — exactly when the enforcement is needed. Nobody noticed because this path had
no test. Hence this file.

These tests do NOT let the real `aspirations-add-goal.sh` run — subprocess.run is
patched, so no goal is ever written to a live queue.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = PROJECT_ROOT / "core" / "scripts"


def _load_module(agent_dir: Path, monkeypatch):
    """Import obligation-audit.py with AGENT_DIR pointed at a tmp agent.

    The module reads AGENT_DIR at import time (`from _paths import AGENT_DIR`), so the
    tmp value must be injected into the already-imported `_paths` before loading it.

    MUST go through monkeypatch, not a bare `_paths.AGENT_DIR = ...`. `_paths` is a
    process-global singleton imported by 36 other test files; a bare assignment leaks
    this tmp path into every test that runs after us in the same pytest process.
    monkeypatch restores the original at teardown.
    """
    sys.path.insert(0, str(SCRIPTS))
    import _paths  # type: ignore

    monkeypatch.setattr(_paths, "AGENT_DIR", agent_dir)
    spec = importlib.util.spec_from_file_location(
        "obligation_audit_undertest", SCRIPTS / "obligation-audit.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.AGENT_DIR = agent_dir
    return mod


def _seed_compact(agent_dir: Path, entries):
    sess = agent_dir / "session"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "aspirations-compact.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )


@pytest.fixture
def patched(tmp_path, monkeypatch):
    """Module with subprocess.run captured — nothing external ever executes."""
    agent_dir = tmp_path / "agents" / "testagent"
    agent_dir.mkdir(parents=True)
    mod = _load_module(agent_dir, monkeypatch)
    calls = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append([str(c) for c in cmd])
        return _R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return mod, calls, agent_dir


def _add_goal_call(calls):
    for c in calls:
        if any("aspirations-add-goal.sh" in part for part in c):
            return c
    return None


def test_picks_framework_maintenance_aspiration(patched):
    """The preferred branch: an active framework-maintenance aspiration wins."""
    mod, calls, agent_dir = patched
    _seed_compact(agent_dir, [
        {"id": "asp-900", "status": "active", "source": "world"},
        {"id": "asp-901", "status": "active", "source": "world",
         "category": "framework-maintenance"},
    ])
    mod._file_investigate_goal(3, agent_dir / "audit.jsonl")

    call = _add_goal_call(calls)
    assert call is not None, "REGRESSION: filing path dead — add-goal was never invoked"
    assert "asp-901" in call, f"expected framework-maintenance target, got {call}"


def test_falls_back_to_first_active(patched):
    """No framework-maintenance match -> first active, still a real target (not None)."""
    mod, calls, agent_dir = patched
    _seed_compact(agent_dir, [
        {"id": "asp-910", "status": "active", "source": "world"},
        {"id": "asp-911", "status": "completed", "source": "world"},
    ])
    mod._file_investigate_goal(2, agent_dir / "audit.jsonl")

    call = _add_goal_call(calls)
    assert call is not None, "REGRESSION: filing path dead on the fallback branch"
    assert "asp-910" in call


def test_reads_agent_session_path_not_world_dir(patched):
    """The  regression guard, stated as the defect itself.

    A compact present ONLY at the agent-session path must yield a target. Before the
    fix the read went to $WORLD_DIR (nonexistent) and this asserted None.
    """
    mod, calls, agent_dir = patched
    _seed_compact(agent_dir, [
        {"id": "asp-920", "status": "active", "source": "world",
         "category": "framework-maintenance"},
    ])
    mod._file_investigate_goal(1, agent_dir / "audit.jsonl")

    assert _add_goal_call(calls) is not None
    # And it must not have reached for the world-dir reader that caused the bug.
    assert not any("world-cat.sh" in part for c in calls for part in c), \
        "REGRESSION: reverted to world-cat.sh, which resolves to a nonexistent path"


def test_no_active_aspirations_does_not_file(patched):
    """Guard the opposite direction: nothing active -> no goal filed, no crash."""
    mod, calls, agent_dir = patched
    _seed_compact(agent_dir, [{"id": "asp-930", "status": "completed"}])
    mod._file_investigate_goal(4, agent_dir / "audit.jsonl")
    assert _add_goal_call(calls) is None


def test_missing_compact_does_not_crash(patched):
    """No compact on disk at all -> return quietly (fail-open, never raise)."""
    mod, calls, agent_dir = patched
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    mod._file_investigate_goal(5, agent_dir / "audit.jsonl")
    assert _add_goal_call(calls) is None
