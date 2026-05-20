""" / rb-942: insight-trigger-gate.py dual-format dedup regression.

Verifies the gate's `_already_filed_in_aspirations()` helper catches
already-filed goals whose `origin_signal` matches EITHER:
  - `board_post:<msg_id>`     (this gate's own format)
  - `insight_trigger:<msg_id>` (insight-trigger-sweep.py's format, g-115-754)

Without this dual-format awareness, the gate writes `board_post:<msg_id>`
while the sweep writes `insight_trigger:<msg_id>`; aspirations.py
duplication-gate uses EXACT-STRING match on origin_signal and lets the
format-asymmetric duplicate land silently. Canonical incident: rb-942
(asymmetric dedup) cataloged the lesson; g-115-758 implements the fix.

Sibling: `insight-trigger-sweep.py` already dedups against both formats —
this test pins the gate side of the symmetry.

Run: py -3 -m pytest core/scripts/tests/test_insight_trigger_gate_dedup.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent

# Load insight-trigger-gate.py as a module so we can call _already_filed_in_aspirations
# directly. The script name has hyphens so we can't `import insight-trigger-gate`.
GATE_PATH = CORE_SCRIPTS / "insight-trigger-gate.py"
_spec = importlib.util.spec_from_file_location("itg_under_test", GATE_PATH)
itg = importlib.util.module_from_spec(_spec)
sys.modules["itg_under_test"] = itg
_spec.loader.exec_module(itg)


def _write_aspirations_jsonl(path: Path, records: list[dict]) -> None:
    """Write a list of aspiration records as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


@pytest.fixture
def sandbox_world(monkeypatch, tmp_path: Path):
    """Tmp WORLD + agent dir; patch the gate's PROJECT_ROOT and WORLD_PATH lookup."""
    world = tmp_path / "world"
    world.mkdir()
    agent_name = "alpha-test"
    agent_dir = tmp_path / agent_name
    agent_dir.mkdir()
    # local-paths.conf needed because _world_dir() reads it via _read_local_paths_conf
    (agent_dir / "local-paths.conf").write_text(
        f'WORLD_PATH="{world}"\nMETA_PATH="{tmp_path / "meta"}"\n', encoding="utf-8"
    )

    # Patch module-level PROJECT_ROOT so _actions_log_path and
    # _already_filed_in_aspirations resolve under our tmp tree.
    monkeypatch.setattr(itg, "PROJECT_ROOT", tmp_path)
    # Phase 2.5.C: _agent_dir now resolves via _paths.agent_dir(), not
    # PROJECT_ROOT / name, so we must also patch the imported helper.
    monkeypatch.setattr(itg, "_agent_dir", lambda name: tmp_path / name)
    # _world_dir() reads MIND_AGENT then opens
    # PROJECT_ROOT/<agent>/local-paths.conf via _read_local_paths_conf.
    # Both _read_local_paths_conf and _world_dir use the module-level
    # PROJECT_ROOT, so the monkeypatch above is sufficient.
    monkeypatch.setenv("MIND_AGENT", agent_name)
    return {"world": world, "agent": agent_name, "agent_dir": agent_dir}


def _goal(origin_signal: str | None, gid: str = "g-test-001") -> dict:
    return {
        "id": gid,
        "title": "Investigate: stub",
        "description": "stub",
        "status": "pending",
        "priority": "HIGH",
        "participants": ["agent"],
        "origin_signal": origin_signal,
    }


def _asp(asp_id: str, goals: list[dict]) -> dict:
    return {
        "id": asp_id,
        "title": "test",
        "status": "active",
        "goals": goals,
    }


def test_world_aspirations_insight_trigger_format_dedups(sandbox_world):
    """Sweep already filed `insight_trigger:<msg-X>` in world queue → gate skips."""
    msg_id = "msg-20260516-110000-bravo-001"
    _write_aspirations_jsonl(
        sandbox_world["world"] / "aspirations.jsonl",
        [_asp("asp-001", [_goal(f"insight_trigger:{msg_id}")])],
    )
    assert itg._already_filed_in_aspirations(msg_id) is True


def test_world_aspirations_board_post_format_dedups(sandbox_world):
    """Gate previously filed `board_post:<msg-X>` in world queue → still dedups."""
    msg_id = "msg-20260516-110001-alpha-002"
    _write_aspirations_jsonl(
        sandbox_world["world"] / "aspirations.jsonl",
        [_asp("asp-001", [_goal(f"board_post:{msg_id}")])],
    )
    assert itg._already_filed_in_aspirations(msg_id) is True


def test_agent_local_aspirations_dedups(sandbox_world):
    """Goal in PER-AGENT aspirations.jsonl also blocks (not just world queue)."""
    msg_id = "msg-20260516-110002-charlie-003"
    _write_aspirations_jsonl(
        sandbox_world["agent_dir"] / "aspirations.jsonl",
        [_asp("asp-002", [_goal(f"insight_trigger:{msg_id}")])],
    )
    assert itg._already_filed_in_aspirations(msg_id) is True


def test_unrelated_msg_id_passes_through(sandbox_world):
    """A different msg_id should NOT match — false-positive guard."""
    _write_aspirations_jsonl(
        sandbox_world["world"] / "aspirations.jsonl",
        [_asp("asp-001", [_goal("insight_trigger:msg-OTHER-001")])],
    )
    assert itg._already_filed_in_aspirations("msg-DIFFERENT-001") is False


def test_no_origin_signal_does_not_match(sandbox_world):
    """A goal without origin_signal must not accidentally match anything."""
    _write_aspirations_jsonl(
        sandbox_world["world"] / "aspirations.jsonl",
        [_asp("asp-001", [_goal(None), _goal("")])],
    )
    assert itg._already_filed_in_aspirations("msg-x-001") is False


def test_partial_format_does_not_match(sandbox_world):
    """`board_post:<msg-x>` must NOT match for msg_id `x` (no substring match)."""
    _write_aspirations_jsonl(
        sandbox_world["world"] / "aspirations.jsonl",
        [_asp("asp-001", [_goal("board_post:msg-EXTRA-suffix-001")])],
    )
    # Querying with the SUFFIX only should NOT match — formats are anchored
    assert itg._already_filed_in_aspirations("EXTRA-suffix-001") is False
    # Querying with the FULL msg_id should match
    assert itg._already_filed_in_aspirations("msg-EXTRA-suffix-001") is True


def test_empty_msg_id_returns_false(sandbox_world):
    """Defensive: empty / None msg_id must return False (no spurious match)."""
    _write_aspirations_jsonl(
        sandbox_world["world"] / "aspirations.jsonl",
        [_asp("asp-001", [_goal("board_post:")])],
    )
    assert itg._already_filed_in_aspirations(None) is False
    assert itg._already_filed_in_aspirations("") is False


def test_missing_aspirations_jsonl_fails_open(sandbox_world):
    """If aspirations.jsonl is absent in BOTH queues, return False (fail-open)."""
    # Do NOT write either jsonl file
    assert itg._already_filed_in_aspirations("msg-NEVER-FILED") is False


def test_malformed_jsonl_line_skipped_other_matches(sandbox_world):
    """A garbled line should be skipped; valid records still scanned for match."""
    msg_id = "msg-20260516-110003-zeta-004"
    path = sandbox_world["world"] / "aspirations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("not json garbage\n")
        f.write("\n")
        f.write(json.dumps(_asp("asp-001", [_goal(f"insight_trigger:{msg_id}")])) + "\n")
    assert itg._already_filed_in_aspirations(msg_id) is True


def test_both_world_and_agent_queues_scanned(sandbox_world):
    """When match is in agent queue but NOT in world queue, still returns True."""
    msg_world = "msg-WORLD-only-001"
    msg_agent = "msg-AGENT-only-002"
    _write_aspirations_jsonl(
        sandbox_world["world"] / "aspirations.jsonl",
        [_asp("asp-001", [_goal(f"insight_trigger:{msg_world}")])],
    )
    _write_aspirations_jsonl(
        sandbox_world["agent_dir"] / "aspirations.jsonl",
        [_asp("asp-002", [_goal(f"insight_trigger:{msg_agent}")])],
    )
    assert itg._already_filed_in_aspirations(msg_world) is True
    assert itg._already_filed_in_aspirations(msg_agent) is True
    assert itg._already_filed_in_aspirations("msg-NEITHER-003") is False
