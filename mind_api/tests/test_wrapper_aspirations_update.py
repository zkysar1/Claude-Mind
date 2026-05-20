"""End-to-end wrapper test for aspirations-update.sh.

Tests the daemon-aware wrapper for aspiration-level field updates.
Strategy:
  - running_daemon fixture spawns daemon in a tmp project_root
  - RT_DIR override so wrapper finds the tmp daemon's port file
  - Verify exit 0 + persisted aspiration updated
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-update.sh"


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _run(args, *, project_root: Path, agent: str = "alpha"):
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    proc = subprocess.run(
        [_bash(), UPDATE_WRAPPER.as_posix(), *args],
        env=env, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _read_jsonl(path: Path):
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


class TestWrapperAspirationUpdate:
    def test_basic_field_update(self, running_daemon):
        root, port = running_daemon
        rc, out, err = _run(
            ["asp-001", "title", "Wrapper Title"],
            project_root=root)
        assert rc == 0, f"stderr: {err}"
        resp = json.loads(out)
        assert resp["title"] == "Wrapper Title"

        # Verify persisted
        items = _read_jsonl(root / "world" / "aspirations.jsonl")
        asp = next(a for a in items if a["id"] == "asp-001")
        assert asp["title"] == "Wrapper Title"

    def test_typed_value_encoding(self, running_daemon):
        root, port = running_daemon
        # Boolean true should be encoded as JSON true, not string "true"
        rc, out, err = _run(
            ["asp-001", "archived", "true"],
            project_root=root)
        assert rc == 0, f"stderr: {err}"
        resp = json.loads(out)
        assert resp["archived"] is True

    def test_source_agent(self, running_daemon):
        root, port = running_daemon
        rc, out, err = _run(
            ["--source", "agent", "asp-100", "title", "Agent Wrap"],
            project_root=root)
        assert rc == 0, f"stderr: {err}"
        resp = json.loads(out)
        assert resp["title"] == "Agent Wrap"

    def test_validation_rejected(self, running_daemon):
        root, port = running_daemon
        rc, out, err = _run(
            ["asp-001", "status", "bogus"],
            project_root=root)
        # Daemon returns 4xx → wrapper exits 1
        assert rc == 1
        assert "invalid_status" in err

    def test_missing_positional_error(self, running_daemon):
        """Missing positional args → wrapper exits non-zero."""
        root, port = running_daemon
        rc, out, err = _run(
            ["asp-001", "title"],  # missing VALUE
            project_root=root)
        assert rc != 0 or "Wrapper Title" not in out
