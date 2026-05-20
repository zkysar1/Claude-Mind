"""End-to-end wrapper tests for pipeline-update.sh through a running daemon.

Tests the full shell-wrapper -> daemon -> file cycle. Each test calls the
wrapper script via subprocess with RT_DIR overrides pointing at the test daemon.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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


# Seed records that pass full validation.
_SEED_ACTIVE = json.dumps({
    "id": "2026-05-12_test-active",
    "title": "Test active hypothesis for pipeline wrapper update tests",
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
    "title": "Test resolved hypothesis for pipeline wrapper update tests",
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


def _run_wrapper(project_root: Path, port: int, args: list,
                 *, stdin_data: str = ""):
    """Run pipeline-update.sh against the test daemon."""
    script = REPO_ROOT / "core" / "scripts" / "pipeline-update.sh"
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    env["MIND_AGENT"] = "alpha"
    env["MIND_RUNTIME_DISABLE_SPAWN"] = "1"

    result = subprocess.run(
        [_bash(), script.as_posix()] + args,
        capture_output=True, text=True, timeout=15,
        input=stdin_data or None,
        env=env,
    )
    return result


def _rec(**kwargs) -> dict:
    base = {
        "id": "2026-05-12_test-active",
        "title": "Wrapper-updated hypothesis title",
        "stage": "active",
        "horizon": "session",
        "type": "calibration",
        "confidence": 0.85,
        "position": "YES this is a valid multi-word position claim",
        "formed_date": "2026-05-12",
        "category": "test-cat",
        "claim": "This is a valid claim field that is longer than twenty characters",
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# pipeline-update.sh
# ---------------------------------------------------------------------------

def test_wrapper_update_replaces_record(pipeline_daemon):
    """Wrapper update replaces the existing record via daemon."""
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    replacement = _rec()
    result = _run_wrapper(project_root, port,
                          ["2026-05-12_test-active"],
                          stdin_data=json.dumps(replacement))
    assert result.returncode == 0, f"stderr: {result.stderr}"

    items = _read_jsonl(live)
    rec = next(r for r in items if r["id"] == "2026-05-12_test-active")
    assert rec["title"] == "Wrapper-updated hypothesis title"
    assert rec["confidence"] == 0.85


def test_wrapper_update_not_found_exits_nonzero(pipeline_daemon):
    """Wrapper exits non-zero when the record is not found."""
    project_root, port = pipeline_daemon

    replacement = _rec(id="2026-01-01_nonexistent")
    result = _run_wrapper(project_root, port,
                          ["2026-01-01_nonexistent"],
                          stdin_data=json.dumps(replacement))
    assert result.returncode != 0


def test_wrapper_update_preserves_sibling(pipeline_daemon):
    """Updating one record does not alter sibling records."""
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    replacement = _rec()
    _run_wrapper(project_root, port,
                 ["2026-05-12_test-active"],
                 stdin_data=json.dumps(replacement))

    items = _read_jsonl(live)
    resolved = next(r for r in items if r["id"] == "2026-05-12_test-resolved")
    assert resolved["title"] == "Test resolved hypothesis for pipeline wrapper update tests"
