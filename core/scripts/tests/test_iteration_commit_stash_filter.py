"""test_iteration_commit_stash_filter.py -  regression test.

Verifies iteration-commit.sh's stash-overlap filter: when a `git stash`
entry contains paths that also appear in the committer's staged set, those
paths are dropped from the commit and a warning names the stash SHA so the
original author can recover under their own signature.

Origin incident: rb-1127 / 3c4a61c4 (2026-05-22). Alpha stashed work at
12:15 (creating stash 92543f81); the LLM later restored files via
`git checkout 92543f81 -- <files>`; bravo's iteration-commit at 12:26 swept
the restored files under bravo's signature in commit 3c4a61c4. Net effect:
alpha's authorship lost. The filter below catches that incident shape by
probing `git stash list` and refusing to commit any path that also lives in
a stash entry.

Cases covered:
  1. Stash entry overlaps committer's staged set -> filtered with warning
  2. No stash present -> filter inactive (existing behavior preserved)
  3. Stash entry exists but no overlap with staged set -> all files included
  4. All staged files filtered by stash -> graceful "nothing to commit" exit

Pattern: subprocess + tempdir + scripted git init. Mirrors
test_iteration_commit_untracked_filter.py.

Cross-references: agents/zeta/reports/g-115-1127-iteration-commit-stash-clobber-analysis.md
Recommendation C; iteration-commit.sh lines ~880-925 (stash-overlap filter block).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
ITERATION_COMMIT_SH = CORE_SCRIPTS / "iteration-commit.sh"

PROJECT_TMP = SCRIPT_DIR / "_tmp_iteration_commit_stash_test"


def _to_bash_path(p) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _run_bash(args, env=None, cwd=None):
    cmd = [GIT_BASH] + [_to_bash_path(a) for a in args]
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd, env=full_env, cwd=cwd,
        capture_output=True, text=True, timeout=30,
    )


def _setup_repo(tmpdir: Path) -> Path:
    """Initialize a minimal git repo with alpha+echo agent dirs."""
    repo = tmpdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "agents").mkdir()
    for a in ("alpha", "echo"):
        d = repo / "agents" / a
        d.mkdir()
        (d / "self.md").write_text(f"# {a}\n")
    core_scripts = repo / "core" / "scripts"
    core_scripts.mkdir(parents=True)
    (core_scripts / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _shim_iteration_commit(tmpdir: Path) -> Path:
    """Copy iteration-commit.sh into tmpdir; the cross-agent uncommitted
    filter is sensitive to claimed_at — we don't seed a team-state shim
    because the stash-overlap filter is orthogonal to that flow. With no
    team-state shim present, team-state-read.sh sibling lookup falls back
    to "no claimed_at" which deactivates the cross-agent filter — exactly
    what we want for this isolated test.
    """
    shim_dir = tmpdir / "scripts"
    shim_dir.mkdir()
    target = shim_dir / "iteration-commit.sh"
    target.write_bytes(ITERATION_COMMIT_SH.read_bytes())
    target.chmod(0o755)
    # Mock team-state-read.sh to return null (no in_flight claimed_at) so
    # the cross-agent mtime filter is fail-open. This isolates the
    # stash-overlap filter as the only active filter for these tests.
    shim = shim_dir / "team-state-read.sh"
    shim.write_text("#!/usr/bin/env bash\necho 'null'\nexit 0\n")
    shim.chmod(0o755)
    return target


def _stash_files(repo: Path, paths: list[str]) -> str:
    """Create + stash the given files. Returns the stash SHA."""
    for p in paths:
        target = repo / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# stash content for {p}\n")
    # Track them so stash will pick them up
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    # Stash with -u (include untracked) — but we already added, so plain stash works
    subprocess.run(["git", "stash", "push", "-m", "WIP test stash"],
                   cwd=repo, check=True)
    # Get the SHA
    result = subprocess.run(
        ["git", "stash", "list", "--format=%H"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    sha = result.stdout.strip().split("\n")[0]
    assert len(sha) == 40, f"Expected 40-char SHA, got: {sha!r}"
    return sha


def _restore_from_stash(repo: Path, sha: str, paths: list[str]):
    """Restore files from a stash SHA without popping (mimics rb-1127 flow)."""
    subprocess.run(
        ["git", "checkout", sha, "--"] + paths,
        cwd=repo, check=True,
    )


def test_stash_overlap_filters_matching_files():
    """Files in stash AND in committer's staged set -> filtered; other own
    files survive and appear in the stage list."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)

        # Alpha stashes one file
        stash_paths = ["core/scripts/alpha-feature.py"]
        sha = _stash_files(repo, stash_paths)
        _restore_from_stash(repo, sha, stash_paths)

        # Echo's OWN edit on a different path — must survive the filter so the
        # dry-run actually reaches the stage-list output. Without this, the
        # script short-circuits via the "all candidate files filtered" branch.
        own = repo / "core" / "scripts" / "echo-own.py"
        own.write_text("# echo own work — not in any stash\n")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "echo"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (stash-overlap)" in combined, (
            f"Expected stash-overlap filter to catch alpha-feature.py. "
            f"stderr={result.stderr!r} stdout={result.stdout!r}"
        )
        assert "alpha-feature.py" in combined, (
            f"Filtered file path missing in output. stderr={result.stderr!r}"
        )
        assert sha[:8] in combined, (
            f"Stash SHA prefix should appear in warning for recovery. "
            f"stderr={result.stderr!r}"
        )
        # Dry-run stage list MUST be reached now (echo-own survives)
        assert "files to stage (git add):" in result.stdout, (
            f"Dry-run header missing. stdout={result.stdout!r}"
        )
        # Own file appears in the stage list
        assert "echo-own.py" in result.stdout, (
            f"Echo's own file should be staged. stdout={result.stdout!r}"
        )
        # Filtered file MUST NOT appear in the stage list (stdout, not stderr)
        assert "alpha-feature.py" not in result.stdout, (
            f"alpha-feature.py should NOT appear in stdout stage list. "
            f"stdout={result.stdout!r}"
        )


def test_no_stash_present_no_filter_fires():
    """No stash entries -> filter inactive, all files staged normally."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)

        # Just add a file (no stash)
        target = repo / "core" / "scripts" / "echo-feature.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# echo own work\n")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-02", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "echo"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (stash-overlap)" not in combined, (
            f"Filter should NOT fire when no stash present. "
            f"stderr={result.stderr!r}"
        )
        # File should appear in stage list
        assert "echo-feature.py" in result.stdout, (
            f"File should be staged. stdout={result.stdout!r}"
        )


def test_stash_present_but_no_overlap_all_included():
    """Stash exists but its paths don't overlap committer's set -> all included."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)

        # Alpha stashes work on path X
        _stash_files(repo, ["core/scripts/alpha-stashed.py"])
        # (intentionally do NOT restore — the stashed file isn't in the working tree)

        # Echo edits a DIFFERENT path
        target = repo / "core" / "scripts" / "echo-feature.py"
        target.write_text("# echo own work\n")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-03", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "echo"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (stash-overlap)" not in combined, (
            f"Filter should NOT fire when paths don't overlap. "
            f"stderr={result.stderr!r}"
        )
        assert "echo-feature.py" in result.stdout, (
            f"Echo's own file should be staged. stdout={result.stdout!r}"
        )


def test_all_staged_files_stash_filtered_exits_clean():
    """When EVERY staged file matches a stash entry, graceful no-op exit."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)

        stash_paths = ["core/scripts/only-stashed-file.py"]
        sha = _stash_files(repo, stash_paths)
        _restore_from_stash(repo, sha, stash_paths)

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-04", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "echo"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (stash-overlap)" in combined, (
            f"Expected stash filter to fire. stderr={result.stderr!r}"
        )
        assert "all candidate files filtered by stash-overlap" in combined, (
            f"Expected nothing-to-commit exit message. "
            f"stderr={result.stderr!r}"
        )
        assert result.returncode == 0, (
            f"Expected clean exit (rc=0), got rc={result.returncode}. "
            f"stderr={result.stderr!r}"
        )
