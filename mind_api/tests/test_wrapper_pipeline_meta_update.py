"""End-to-end wrapper tests for pipeline-meta-update.sh through a running daemon.

These test the full shell-wrapper -> daemon -> file cycle. Each test calls
the wrapper script via subprocess with RT_DIR override pointing at the
test daemon.
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


def _run_wrapper(project_root: Path, port: int, args: list):
    """Run pipeline-meta-update.sh against the test daemon."""
    script = REPO_ROOT / "core" / "scripts" / "pipeline-meta-update.sh"
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


def _read_meta(world: Path) -> dict:
    meta_path = world / "pipeline-meta.json"
    return json.loads(meta_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_wrapper_sets_field(running_daemon):
    project_root, port = running_daemon
    result = _run_wrapper(project_root, port, ["custom_note", "hello_world"])
    assert result.returncode == 0, f"stderr: {result.stderr}"

    # stdout should be pretty-printed JSON.
    out = json.loads(result.stdout)
    assert out["custom_note"] == "hello_world"

    # Verify on disk.
    on_disk = _read_meta(project_root / "world")
    assert on_disk["custom_note"] == "hello_world"


def test_wrapper_integer_coercion(running_daemon):
    project_root, port = running_daemon
    result = _run_wrapper(project_root, port, ["count", "99"])
    assert result.returncode == 0, f"stderr: {result.stderr}"

    out = json.loads(result.stdout)
    assert out["count"] == 99


def test_wrapper_boolean_coercion(running_daemon):
    project_root, port = running_daemon
    result = _run_wrapper(project_root, port, ["enabled", "true"])
    assert result.returncode == 0, f"stderr: {result.stderr}"

    out = json.loads(result.stdout)
    assert out["enabled"] is True


def test_wrapper_error_on_dotted_field(running_daemon):
    project_root, port = running_daemon
    result = _run_wrapper(project_root, port, ["a.b", "value"])
    # Should exit non-zero (daemon returns 400, wrapper maps to exit 1).
    assert result.returncode != 0
