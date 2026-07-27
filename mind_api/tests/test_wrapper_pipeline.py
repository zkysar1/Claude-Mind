"""End-to-end wrapper tests for pipeline-move.sh, pipeline-add.sh,
pipeline-update-field.sh through a running daemon.

These test the full shell-wrapper -> daemon -> file cycle. Each test calls
the wrapper script via subprocess with RT_PORT/RT_PID_FILE overrides
pointing at the test daemon.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# Seed records that pass full validation (conftest seeds are minimal/reader-only).
_SEED_ACTIVE = json.dumps({
    "id": "2026-05-12_test-active",
    "title": "Test active hypothesis for pipeline wrapper tests",
    "stage": "active",
    "horizon": "session",
    "type": "calibration",
    "confidence": 0.6,
    "position": "YES this is a valid multi-word active hypothesis position",
    "formed_date": "2026-05-12",
    "category": "test-cat",
    "reflected": False,
})

_SEED_RESOLVED = json.dumps({
    "id": "2026-05-12_test-resolved",
    "title": "Test resolved hypothesis for pipeline wrapper tests",
    "stage": "resolved",
    "horizon": "session",
    "type": "calibration",
    "confidence": 0.7,
    "position": "YES this is a valid multi-word resolved hypothesis position",
    "formed_date": "2026-05-12",
    "category": "test-cat",
    "outcome": "CONFIRMED",
    "reflected": True,
})


@pytest.fixture
def pipeline_daemon(running_daemon):
    """Re-seed pipeline.jsonl with records that pass full validation."""
    project_root, port = running_daemon
    live = project_root / "world" / "pipeline.jsonl"
    live.write_text(_SEED_ACTIVE + "\n" + _SEED_RESOLVED + "\n",
                    encoding="utf-8")
    (project_root / "world" / "pipeline-archive.jsonl").write_text(
        "", encoding="utf-8")
    return project_root, port


def _run_wrapper(project_root: Path, port: int, script_name: str,
                 args: list, *, stdin_data: str = ""):
    """Run a pipeline wrapper script against the test daemon."""
    # Use the real repo scripts but point env at the test daemon's dirs.
    script = REPO_ROOT / "core" / "scripts" / script_name
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    # Redirect daemon discovery at the tmp daemon spawned by running_daemon.
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    env["MIND_AGENT"] = "alpha"
    # Disable auto-start -- the daemon is already running.
    env["MIND_RUNTIME_DISABLE_SPAWN"] = "1"

    result = subprocess.run(
        [_bash(), script.as_posix()] + args,
        capture_output=True, text=True, timeout=15,
        input=stdin_data or None,
        env=env,
    )
    return result


# ---------------------------------------------------------------------------
# pipeline-add.sh
# ---------------------------------------------------------------------------

def test_wrapper_add_creates_record(pipeline_daemon):
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"
    before = len(_read_jsonl(live))

    rec = json.dumps({
        "id": "2026-05-14_wrapper-add",
        "title": "Wrapper add test hypothesis",
        "stage": "discovered",
        "horizon": "session",
        "type": "calibration",
        "confidence": 0.5,
        "position": "YES this is a valid test position claim",
        "formed_date": "2026-05-14",
        "category": "test-cat",
    })

    result = _run_wrapper(project_root, port, "pipeline-add.sh", [],
                          stdin_data=rec)
    assert result.returncode == 0, f"stderr: {result.stderr}"

    after = _read_jsonl(live)
    assert len(after) == before + 1
    assert any(r["id"] == "2026-05-14_wrapper-add" for r in after)


# ---------------------------------------------------------------------------
# pipeline-move.sh
# ---------------------------------------------------------------------------

def test_wrapper_move_changes_stage(pipeline_daemon):
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    # Move the seeded active record to discovered.
    result = _run_wrapper(project_root, port, "pipeline-move.sh",
                          ["2026-05-12_test-active", "discovered"])
    assert result.returncode == 0, f"stderr: {result.stderr}"

    items = _read_jsonl(live)
    rec = next(r for r in items if r["id"] == "2026-05-12_test-active")
    assert rec["stage"] == "discovered"


def test_wrapper_move_with_merge_data(pipeline_daemon):
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    merge = json.dumps({"notes": "wrapper merge"})
    result = _run_wrapper(project_root, port, "pipeline-move.sh",
                          ["2026-05-12_test-active", "discovered"],
                          stdin_data=merge)
    assert result.returncode == 0, f"stderr: {result.stderr}"

    items = _read_jsonl(live)
    rec = next(r for r in items if r["id"] == "2026-05-12_test-active")
    assert rec["notes"] == "wrapper merge"


def test_wrapper_move_to_archived(pipeline_daemon):
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"
    archive = project_root / "world" / "pipeline-archive.jsonl"

    result = _run_wrapper(project_root, port, "pipeline-move.sh",
                          ["2026-05-12_test-resolved", "archived"])
    assert result.returncode == 0, f"stderr: {result.stderr}"

    #  tombstone-in-live archival: the record stays in live as a
    # stage=archived tombstone; the archive gains one deduped copy.
    live_items = _read_jsonl(live)
    archive_items = _read_jsonl(archive)
    tomb = next(r for r in live_items if r["id"] == "2026-05-12_test-resolved")
    assert tomb["stage"] == "archived"
    assert tomb.get("archived_date")
    assert any(r["id"] == "2026-05-12_test-resolved" for r in archive_items)


# ---------------------------------------------------------------------------
# pipeline-update-field.sh
# ---------------------------------------------------------------------------

def test_wrapper_update_field(pipeline_daemon):
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    result = _run_wrapper(project_root, port, "pipeline-update-field.sh",
                          ["2026-05-12_test-active", "confidence", "0.8"])
    assert result.returncode == 0, f"stderr: {result.stderr}"

    items = _read_jsonl(live)
    rec = next(r for r in items if r["id"] == "2026-05-12_test-active")
    assert rec["confidence"] == 0.8


def test_wrapper_update_field_reflected(pipeline_daemon):
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    result = _run_wrapper(project_root, port, "pipeline-update-field.sh",
                          ["2026-05-12_test-active", "reflected", "true"])
    assert result.returncode == 0, f"stderr: {result.stderr}"

    items = _read_jsonl(live)
    rec = next(r for r in items if r["id"] == "2026-05-12_test-active")
    assert rec["reflected"] is True
    assert rec.get("reflected_date") is not None
