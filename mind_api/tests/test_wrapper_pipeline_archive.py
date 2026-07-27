"""End-to-end wrapper tests for pipeline-archive.sh through a running daemon.

Tests the full shell-wrapper -> daemon -> file cycle. Each test calls
the wrapper script via subprocess with RT_DIR overrides pointing at the
test daemon.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import date, timedelta
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


def _valid_rec(**kwargs) -> dict:
    base = {
        "id": "2026-05-01_wrapper-sweep",
        "title": "Wrapper sweep test hypothesis about something interesting",
        "stage": "resolved",
        "horizon": "session",
        "type": "calibration",
        "confidence": 0.6,
        "position": "YES this is a valid multi-word position claim",
        "formed_date": "2026-05-01",
        "category": "test-cat",
        "outcome": "CONFIRMED",
        "reflected": True,
    }
    base.update(kwargs)
    return base


def _old_date() -> str:
    return (date.today() - timedelta(days=5)).isoformat()


@pytest.fixture
def archive_daemon(running_daemon):
    """Seed pipeline.jsonl with a resolved record eligible for archiving."""
    project_root, port = running_daemon
    live = project_root / "world" / "pipeline.jsonl"
    archive = project_root / "world" / "pipeline-archive.jsonl"
    archive.write_text("", encoding="utf-8")

    rec = _valid_rec(outcome_date=_old_date())
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return project_root, port


def _run_wrapper(project_root: Path, port: int, args: list):
    """Run pipeline-archive.sh against the test daemon."""
    script = REPO_ROOT / "core" / "scripts" / "pipeline-archive.sh"
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    env["MIND_AGENT"] = "alpha"
    env["MIND_RUNTIME_DISABLE_SPAWN"] = "1"

    result = subprocess.run(
        [_bash(), script.as_posix()] + args,
        capture_output=True, text=True, timeout=15,
        env=env,
    )
    return result


# ---------------------------------------------------------------------------
# Test: wrapper archives eligible records and prints count
# ---------------------------------------------------------------------------

def test_wrapper_archive_sweep(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"
    archive = project_root / "world" / "pipeline-archive.jsonl"

    result = _run_wrapper(project_root, port, [])
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # stdout should be the archived count (matches legacy CLI shape)
    assert result.stdout.strip() == "1"

    # Verify file state — : the swept record stays in live as a
    # stage=archived tombstone (pruned only after PRUNE_GRACE_DAYS).
    live_items = _read_jsonl(live)
    archive_items = _read_jsonl(archive)
    assert len(live_items) == 1
    assert live_items[0]["stage"] == "archived"
    assert live_items[0].get("archived_date")
    assert len(archive_items) == 1
    assert archive_items[0]["stage"] == "archived"


# ---------------------------------------------------------------------------
# Test: wrapper reports 0 when nothing to archive
# ---------------------------------------------------------------------------

def test_wrapper_archive_sweep_nothing(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"
    # Overwrite with a record that has no outcome_date
    rec = _valid_rec()  # no outcome_date field
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    result = _run_wrapper(project_root, port, [])
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert result.stdout.strip() == "0"


# ---------------------------------------------------------------------------
# Test: wrapper handles empty pipeline
# ---------------------------------------------------------------------------

def test_wrapper_archive_sweep_empty(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"
    live.write_text("", encoding="utf-8")

    result = _run_wrapper(project_root, port, [])
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert result.stdout.strip() == "0"
