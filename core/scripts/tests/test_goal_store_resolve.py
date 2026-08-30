"""goal-store-resolve.py + iteration-close.sh `--source` derivation (2026-08-30, coach@zc-03).

A small-model Body tried verify --status blocked four times with `--source external`,
then `agent`, then `world`; every recovery hint parroted the invalid value back. Now
`--source` is derived from the goal id (decide() below, branch-tested), a value that
is not a store is refused at entry with SOURCE cleared so no hint can echo it, and a
`--status blocked` without blocker evidence is refused with the shell-level remedy.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE_SCRIPTS))
from _runtime_bash import BASH  # noqa: E402

_spec = importlib.util.spec_from_file_location("goal_store_resolve", CORE_SCRIPTS / "goal-store-resolve.py")
_gsr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gsr)  # type: ignore[union-attr]
decide, has_blocker_evidence = _gsr.decide, _gsr.has_blocker_evidence

SCRIPT = CORE_SCRIPTS / "iteration-close.sh"
FAKE_AGENT_DIR = CORE_SCRIPTS.parents[1] / "agents" / "ic-recovery-test-agent"


@pytest.fixture(autouse=True, scope="module")
def _cleanup_fake_agent_dir():
    """Same fake agent + same residue discipline as test_iteration_close_recovery_probe."""
    try:
        from conftest import _retire_phantom_shard as _retire
        from _paths import WORLD_DIR
        shard = Path(WORLD_DIR) / "team-state" / "agents" / "ic-recovery-test-agent.yaml"
    except Exception:  # noqa: BLE001
        _retire, shard = None, None

    def _clean():
        shutil.rmtree(FAKE_AGENT_DIR, ignore_errors=True)
        if _retire is not None and shard is not None and shard.exists():
            _retire(shard)

    _clean()
    yield
    _clean()


# ------------------------------------------------------------------ decide()

G = "g-006-22"


def test_valid_source_that_holds_the_goal_is_kept():
    assert decide(G, "world", ["world"]) == ("world", None, None)


def test_valid_source_that_does_not_hold_the_goal_is_corrected_with_a_note():
    store, note, err = decide(G, "agent", ["world"])
    assert (store, err) == ("world", None)
    assert "--source agent does not hold g-006-22" in note and "using --source world" in note


def test_valid_source_with_probe_unavailable_is_kept_fail_open():
    assert decide(G, "agent", None) == ("agent", None, None)


def test_valid_source_goal_in_neither_queue_is_refused():
    store, note, err = decide(G, "world", [])
    assert store is None and "found in neither queue" in err


def test_invalid_source_is_resolved_from_the_goal_id():
    store, note, err = decide(G, "external", ["world"])
    assert (store, err) == ("world", None)
    assert "'external' is not a store" in note


def test_omitted_source_is_resolved_from_the_goal_id():
    store, note, err = decide(G, "", ["agent"])
    assert (store, err) == ("agent", None)
    assert "--source omitted" in note


def test_invalid_source_that_cannot_be_resolved_is_refused_and_names_the_value():
    store, note, err = decide(G, "external", None)
    assert store is None and "got 'external'" in err and "could not resolve" in err
    store, note, err = decide(G, "external", [])
    assert store is None and "got 'external'" in err and "neither queue" in err


def test_omitted_source_that_cannot_be_resolved_is_refused():
    assert decide(G, "", None)[0] is None
    assert decide(G, None, [])[0] is None


def test_world_wins_when_both_queues_hold_the_id():
    assert decide(G, "", ["agent", "world"])[0] == "world"


# ------------------------------------------------------------------ blocker evidence

@pytest.mark.parametrize("rec,expected", [
    ({"blocker_ref": "blk-001"}, True),
    ({"blocked_by": ["g-115-1"]}, True),
    ({"blocker_ref": "", "blocked_by": []}, False),
    ({"defer_reason": "human_blocked: re-authorize the app"}, False),
    ({}, False),
])
def test_has_blocker_evidence(rec, expected):
    assert has_blocker_evidence(rec) is expected


# ------------------------------------------------------------------ iteration-close entry (production arg shape)

def _run(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["STORAGE_BACKEND"] = "local"  # guard-955
    env["MIND_AGENT"] = "ic-recovery-test-agent"
    return subprocess.run([BASH, SCRIPT.as_posix(), *args], capture_output=True, text=True,
                          timeout=180, env=env)


def test_verify_refuses_a_source_that_is_not_a_store_and_never_echoes_it():
    proc = _run("--phase", "verify", "--goal", "g-999999-99", "--status", "blocked",
                "--source", "external", "--outcome", "routine")
    assert proc.returncode == 2, proc.stderr
    assert "--source must be world or agent (got 'external')" in proc.stderr
    for line in proc.stderr.splitlines():
        if line.strip().startswith(("Retry:", "Probe first:", "Revert")):
            assert "--source external" not in line, line


def test_state_update_and_learning_gate_refuse_a_non_store_source_too():
    for phase in ("state-update", "learning-gate"):
        proc = _run("--phase", phase, "--goal", "g-999999-99", "--source", "external",
                    "--outcome", "routine")
        assert proc.returncode == 2, proc.stderr
        assert "--source must be world or agent (got 'external')" in proc.stderr


def test_verify_source_help_text_names_the_blocked_remedy():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "needs blocker evidence" in src
    assert 'defer_reason \\"human_blocked:' in src
    assert "aspirations-release.sh $GOAL_ID --source $SOURCE" in src
    assert "create-blocker.sh --help" in src
