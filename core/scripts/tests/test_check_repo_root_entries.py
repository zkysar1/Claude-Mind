"""test_check_repo_root_entries.py — pre-commit Gate 14: no invented top-level entry.

Measured 2026-08-30 on coach@zc-03: a goal titled "Build yahoo/transactions.py"
created `yahoo/` and `tests/` at the repo ROOT although the world already carried
the package under `<world>/scripts/yahoo`; two copies then diverged for a day
(six modules differ, each side holds tests the other lacks) while the Bodies
spent goals syncing them by hand. Eight new top-level entries in total, six of
them cruft. The L1 hook cannot see a Bash mkdir; the commit can.

Real git repos in tmp_path (guard-1276: never under agents/<agent>/temp, which is
gitignored inside the live repo so `git init` there walks UP to the real one).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE = CORE_SCRIPTS / "check-repo-root-entries.py"
_GIT_ID = ("-c", "user.email=gate@example.invalid", "-c", "user.name=gate")


def _run(cwd: Path, *args: str) -> None:
    proc = subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"


def _gate(repo: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("REPO_ROOT_ENTRY_OVERRIDE", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(GATE), str(repo)],
                          capture_output=True, text=True, timeout=60, env=env, cwd=str(repo))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose HEAD carries the framework-shaped top level: core/, agents/, README."""
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)],
                   capture_output=True, text=True, timeout=60, check=True)
    (work / "core" / "scripts").mkdir(parents=True)
    (work / "core" / "scripts" / "x.sh").write_text("echo x\n", encoding="utf-8")
    (work / "agents" / "a").mkdir(parents=True)
    (work / "agents" / "a" / "aspirations.jsonl").write_text("", encoding="utf-8")
    (work / "README.md").write_text("r\n", encoding="utf-8")
    _run(work, "add", "-A")
    _run(work, *_GIT_ID, "commit", "-q", "-m", "seed")
    return work


def test_positive_control_new_root_dir_is_refused_with_routing(repo: Path):
    """The coach shape verbatim: a domain package invented at the repo root."""
    (repo / "yahoo").mkdir()
    (repo / "yahoo" / "transactions.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_yahoo_transactions.py").write_text("def test_x(): pass\n",
                                                                encoding="utf-8")
    _run(repo, "add", "-A")
    proc = _gate(repo)
    assert proc.returncode == 1, proc.stderr
    assert "REFUSED" in proc.stderr
    assert "yahoo/" in proc.stderr and "tests/" in proc.stderr
    assert "scripts" in proc.stderr and "agents/<agent>/temp/" in proc.stderr
    assert "REPO_ROOT_ENTRY_OVERRIDE" in proc.stderr


def test_a_new_root_file_is_refused_too(repo: Path):
    """`wind` — an empty file at the root — was one of the eight."""
    (repo / "wind").write_text("", encoding="utf-8")
    _run(repo, "add", "wind")
    proc = _gate(repo)
    assert proc.returncode == 1
    assert "wind/" in proc.stderr


def test_additions_inside_existing_top_level_entries_pass(repo: Path):
    (repo / "core" / "scripts" / "new-gate.py").write_text("pass\n", encoding="utf-8")
    (repo / "agents" / "b").mkdir()
    (repo / "agents" / "b" / "self.md").write_text("# b\n", encoding="utf-8")
    _run(repo, "add", "-A")
    proc = _gate(repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""


def test_modifications_deletions_and_renames_never_trip_it(repo: Path):
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    _run(repo, "rm", "-q", "core/scripts/x.sh")
    _run(repo, "add", "README.md")
    assert _gate(repo).returncode == 0
    _run(repo, "mv", "README.md", "agents/README.md")  # rename INTO an existing entry
    assert _gate(repo).returncode == 0


def test_override_passes_and_says_so(repo: Path):
    (repo / "RELEASES.json").write_text("[]\n", encoding="utf-8")
    _run(repo, "add", "RELEASES.json")
    assert _gate(repo).returncode == 1
    proc = _gate(repo, {"REPO_ROOT_ENTRY_OVERRIDE": "release ledger, framework-owned"})
    assert proc.returncode == 0
    assert "OVERRIDDEN" in proc.stderr and "release ledger" in proc.stderr
    # Whitespace is not a justification.
    assert _gate(repo, {"REPO_ROOT_ENTRY_OVERRIDE": "   "}).returncode == 1


def test_initial_commit_has_no_head_and_is_exempt(tmp_path: Path):
    work = tmp_path / "fresh"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)],
                   capture_output=True, text=True, timeout=60, check=True)
    (work / "anything").mkdir()
    (work / "anything" / "f.txt").write_text("f\n", encoding="utf-8")
    _run(work, "add", "-A")
    assert _gate(work).returncode == 0


def test_nothing_staged_passes(repo: Path):
    (repo / "yahoo").mkdir()
    (repo / "yahoo" / "unstaged.py").write_text("x = 1\n", encoding="utf-8")
    assert _gate(repo).returncode == 0, "an unstaged invention is not this gate's business"


def test_non_git_directory_fails_open(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _gate(plain).returncode == 0


def test_wired_into_the_pre_commit_hook():
    hook = (CORE_SCRIPTS.parent / "githooks" / "pre-commit").read_text(encoding="utf-8")
    assert "check-repo-root-entries.py" in hook
    assert "_gate check-repo-root-entries" in hook
