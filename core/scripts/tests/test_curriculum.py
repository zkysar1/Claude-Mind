"""Tests for curriculum.py cross-queue graduation counting (0 / 3).

g-115-1560 added cross_queue counting to the curriculum count_check gate in BOTH
core/scripts/curriculum.py (CLI) and mind_api/src/endpoints/curriculum.py (daemon)
so a lane-specialized agent that works the shared WORLD queue accumulates
graduation credit instead of staying pinned at cur-01 forever. That close left no
test file (verified 2026-06-19); g-115-1563 adds this coverage plus a CLI/daemon
parity guard for the guard-742 daemon-mirror drift class.

Path-override approach: curriculum.py reads module globals AGENT_DIR / WORLD_DIR /
AGENT_NAME at call time, so the tests monkeypatch those attributes directly (the
conftest-documented pattern for path-dependent modules). conftest pre-locks
AGENT_DIR so the import-time assert_agent_dir("curriculum") passes.
"""
import json
import sys
from pathlib import Path

# core/scripts on path (conftest also inserts this; explicit for `py -3` direct runs).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import curriculum  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI_FILE = PROJECT_ROOT / "core" / "scripts" / "curriculum.py"
DAEMON_FILE = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "curriculum.py"


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# --- count_world_attributed ------------------------------------------------

def test_count_world_attributed_counts_only_self_completed(tmp_path, monkeypatch):
    world = tmp_path / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        {"goals": [
            {"id": "w-1", "status": "completed", "completed_by": "delta"},
            {"id": "w-2", "status": "completed", "completed_by": "delta"},
            {"id": "w-3", "status": "pending", "completed_by": "delta"},    # status mismatch
            {"id": "w-4", "status": "completed"},                            # no completed_by
        ]},
        {"goals": [
            {"id": "w-5", "status": "completed", "completed_by": "delta"},
        ]},
    ])
    monkeypatch.setattr(curriculum, "WORLD_DIR", world)
    # w-1, w-2, w-5 are completed AND completed_by==delta -> 3
    assert curriculum.count_world_attributed("goals[*].status", "completed", "delta") == 3


def test_count_world_attributed_excludes_other_agents(tmp_path, monkeypatch):
    world = tmp_path / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        {"goals": [
            {"id": "w-1", "status": "completed", "completed_by": "delta"},
            {"id": "w-2", "status": "completed", "completed_by": "alpha"},
            {"id": "w-3", "status": "completed", "completed_by": "zeta"},
        ]},
    ])
    monkeypatch.setattr(curriculum, "WORLD_DIR", world)
    assert curriculum.count_world_attributed("goals[*].status", "completed", "delta") == 1
    assert curriculum.count_world_attributed("goals[*].status", "completed", "alpha") == 1
    assert curriculum.count_world_attributed("goals[*].status", "completed", "bravo") == 0


def test_count_world_attributed_non_flatten_field_returns_zero(tmp_path, monkeypatch):
    world = tmp_path / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        {"status": "completed", "completed_by": "delta"},
    ])
    monkeypatch.setattr(curriculum, "WORLD_DIR", world)
    # Non-flatten field (no "[*].") is graduation-completion-scoped -> always 0
    assert curriculum.count_world_attributed("status", "completed", "delta") == 0


# --- evaluate_gate count_check cross_queue ---------------------------------

def _setup_gate_fixtures(tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    world = tmp_path / "world"
    _write_jsonl(agent / "aspirations.jsonl", [
        {"goals": [
            {"id": "a-1", "status": "completed"},
            {"id": "a-2", "status": "completed"},
            {"id": "a-3", "status": "pending"},
        ]},
    ])
    _write_jsonl(world / "aspirations.jsonl", [
        {"goals": [
            {"id": "w-1", "status": "completed", "completed_by": "delta"},
            {"id": "w-2", "status": "completed", "completed_by": "delta"},
            {"id": "w-3", "status": "completed", "completed_by": "delta"},
            {"id": "w-4", "status": "completed", "completed_by": "alpha"},   # other agent
        ]},
    ])
    monkeypatch.setattr(curriculum, "AGENT_DIR", agent)
    monkeypatch.setattr(curriculum, "WORLD_DIR", world)
    monkeypatch.setattr(curriculum, "AGENT_NAME", "delta")


def test_count_check_without_cross_queue_is_agent_only(tmp_path, monkeypatch):
    _setup_gate_fixtures(tmp_path, monkeypatch)
    gate = {"type": "count_check", "file": "aspirations.jsonl",
            "field": "goals[*].status", "value": "completed",
            "operator": ">=", "threshold": 3}
    # No cross_queue flag: agent-queue only = 2 completed -> 2 < 3 -> not passed.
    # Regression guard: existing count_check gates (no flag) MUST stay agent-only.
    passed, value = curriculum.evaluate_gate(gate)
    assert value == 2
    assert passed is False


def test_count_check_with_cross_queue_adds_self_attributed_world(tmp_path, monkeypatch):
    _setup_gate_fixtures(tmp_path, monkeypatch)
    gate = {"type": "count_check", "file": "aspirations.jsonl",
            "field": "goals[*].status", "value": "completed",
            "operator": ">=", "threshold": 3, "cross_queue": True}
    # 2 agent + 3 world-attributed-to-delta (w-4/alpha excluded) = 5 -> 5 >= 3 -> passed.
    passed, value = curriculum.evaluate_gate(gate)
    assert value == 5
    assert passed is True


# --- CLI/daemon parity guard (guard-742 daemon-mirror drift class) ----------

def test_cli_daemon_cross_queue_parity():
    """Both curriculum implementations MUST carry the cross_queue path in sync.

    g-115-1560 patched the CLI (core/scripts/curriculum.py) and the daemon
    reimplementation (mind_api/src/endpoints/curriculum.py) as byte-parallel
    copies. A fix to only one side is half a fix (guard-742). This guard fails if
    either side loses its cross-queue counting function, the cross_queue gate
    branch, or completed_by attribution.
    """
    assert CLI_FILE.is_file(), f"CLI curriculum missing: {CLI_FILE}"
    assert DAEMON_FILE.is_file(), f"daemon curriculum missing: {DAEMON_FILE}"
    cli = CLI_FILE.read_text(encoding="utf-8")
    daemon = DAEMON_FILE.read_text(encoding="utf-8")

    # cross-queue counting function present on both sides (CLI public, daemon private)
    assert "def count_world_attributed(" in cli
    assert "def _count_world_attributed(" in daemon
    # the count_check cross_queue branch present on both sides
    assert 'gate.get("cross_queue")' in cli
    assert 'gate.get("cross_queue")' in daemon
    # completed_by attribution discipline present on both sides (NOT claimed_by)
    assert "completed_by" in cli
    assert "completed_by" in daemon
