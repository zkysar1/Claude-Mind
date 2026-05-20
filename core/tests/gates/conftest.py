"""Shared fixtures for gate-extraction equivalence tests.

Every gate test compares two paths:
  1. The CLI wrapper at core/scripts/<name>-gate.py invoked as a subprocess
  2. The extracted module at core/scripts/gates/<name>.py called in-process

Both must produce byte-identical JSON payloads. The fixtures here construct
tmp git repos so the gates have something dirty/clean to scan without
touching the real repo.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"

# Make `from gates.<name> import evaluate` work for in-process direct calls.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git with the repo as CWD. Helper to set up tmp repos."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


@pytest.fixture
def empty_clean_repo(tmp_path: Path) -> Path:
    """A clean git repo with one committed framework file (CLAUDE.md).

    Gates pointed here should report would_block=False (no dirty files).
    """
    repo = tmp_path / "clean-repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "CLAUDE.md").write_text("# clean\n", encoding="utf-8")
    _git(repo, "add", "CLAUDE.md")
    _git(repo, "commit", "-m", "initial", "--quiet")
    return repo


@pytest.fixture
def dirty_framework_repo(tmp_path: Path) -> Path:
    """Repo with mixed dirty state: one framework file modified, one new
    framework file untracked, one ephemeral file modified (should be ignored).

    Gates pointed here should report would_block=True with two dirty
    framework files: CLAUDE.md and a new core/scripts/foo.py. The
    journal.jsonl mutation must NOT appear in dirty_framework_files.
    """
    repo = tmp_path / "dirty-repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    # Seed: CLAUDE.md committed, journal.jsonl committed.
    (repo / "CLAUDE.md").write_text("# original\n", encoding="utf-8")
    (repo / "alpha").mkdir()
    (repo / "alpha" / "journal.jsonl").write_text(
        '{"session":1}\n', encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial", "--quiet")

    # Dirty: modify CLAUDE.md (framework — should block), add new
    # core/scripts/foo.py (framework, untracked — should block), and
    # modify journal.jsonl (agent state churn — should be ignored).
    (repo / "CLAUDE.md").write_text("# modified\n", encoding="utf-8")
    (repo / "core").mkdir()
    (repo / "core" / "scripts").mkdir()
    (repo / "core" / "scripts" / "foo.py").write_text("# new\n", encoding="utf-8")
    (repo / "alpha" / "journal.jsonl").write_text(
        '{"session":1}\n{"session":2}\n', encoding="utf-8",
    )
    return repo


@pytest.fixture
def world_with_ledger(tmp_path: Path):
    """Tmp WORLD_DIR for override-audit-log writes. Returns (world_dir, agent)."""
    world = tmp_path / "world"
    world.mkdir()
    return world, "alpha"
