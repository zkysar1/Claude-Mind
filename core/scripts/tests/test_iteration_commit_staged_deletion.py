"""test_iteration_commit_staged_deletion.py —  regression test.

Verifies iteration-commit.sh handles already-staged deletions (porcelain "D " —
index column D, clean worktree) WITHOUT aborting the whole commit.

The bug: a "D " path is absent from BOTH the worktree and the index, so
including it in the batched `git add -A -- "${staged_files[@]}"` pathspec at the
stage step makes git abort the ENTIRE batch with
`fatal: pathspec '<path>' did not match any files` (rc=128 -> iteration-commit
exits 2). Every legitimate file in that commit is dropped. Confirmed empirically:
a mixed `git add -A -- good staged_del also_good` stages NOTHING on the abort —
the index keeps only the pre-existing staged deletion, the good modifications are
lost.

The fix: the staging loop routes "D " entries to a dedicated `staged_del_files`
set (kept OUT of the failing add pathspec); a separate block re-runs
`git rm --cached --ignore-unmatch` (a safe rc=0 no-op that preserves the
already-staged deletion) and appends the paths to `staged_files` for JSON output.
The empty-changes guards also count `staged_del_files`, so a deletion-only commit
still proceeds.

Intentionally NOT caught: " D" (worktree deletion, parent dir present) stages
fine via `git add -A` (the path is still in the index); the orphan-deletion
sibling (g-280-08) handles only " D" + missing-parent via git rm --cached.

Cross-references: g-115-1620 (this fix), g-280-08 (orphan-deletion handling).
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

PROJECT_TMP = SCRIPT_DIR / "_tmp_iteration_commit_staged_deletion_test"

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _to_bash_path(p) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _run_bash(args, env=None):
    cmd = [GIT_BASH] + [_to_bash_path(a) for a in args]
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(cmd, env=full_env, capture_output=True, text=True, timeout=30)


def _mock_team_state_read_null(dir_: Path) -> None:
    shim = dir_ / "team-state-read.sh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "# null-everywhere shim for test_iteration_commit_staged_deletion.py\n"
        "echo null\n"
        "exit 0\n"
    )
    shim.chmod(0o755)


def _setup_repo(tmpdir: Path, agents=("alpha", "zeta")) -> Path:
    repo = tmpdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "agents").mkdir()
    for a in agents:
        d = repo / "agents" / a
        d.mkdir()
        (d / "self.md").write_text(f"# {a}\n")
        (d / "session").mkdir()
    core_scripts = repo / "core" / "scripts"
    core_scripts.mkdir(parents=True)
    (core_scripts / ".gitkeep").write_text("")
    # The attribution snapshots import _normalize_rel_path from these helpers;
    # copy them so the harness exercises real normalization (mirrors the sibling
    # filter tests). Harmless for the deletion path but keeps the env faithful.
    for _mod in ("_cross_agent_attribution_filter.py", "_stdio.py"):
        src = CORE_SCRIPTS / _mod
        if src.exists():
            (core_scripts / _mod).write_bytes(src.read_bytes())
    (repo / ".gitignore").write_text("__pycache__/\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _shim_iteration_commit(tmpdir: Path) -> Path:
    shim_dir = tmpdir / "scripts"
    shim_dir.mkdir()
    target = shim_dir / "iteration-commit.sh"
    target.write_bytes(ITERATION_COMMIT_SH.read_bytes())
    target.chmod(0o755)
    _mock_team_state_read_null(shim_dir)
    return target


def _git(repo: Path, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, timeout=10)


def _committed_name_status(repo: Path) -> str:
    out = subprocess.run(
        ["git", "log", "-1", "--name-status", "--format="],
        cwd=repo, capture_output=True, text=True, timeout=10,
    )
    return out.stdout


def test_staged_deletion_alongside_work_commits_cleanly():
    """A staged deletion ("D ") mixed with legitimate work must NOT abort the
    commit: both the deletion and the new file land in the commit. This is the
    core "drops legitimate commits" regression — without the fix the batched
    `git add -A` aborts (rc=128) and the new file is lost."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)

        # Track a file, then `git rm` it -> staged deletion (porcelain "D ").
        deleted = repo / "core" / "scripts" / "to-delete.py"
        deleted.write_text("# retired script\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add to-delete")
        _git(repo, "rm", "-q", "core/scripts/to-delete.py")

        # Legitimate alongside-work (untracked neutral-path file).
        keep = repo / "core" / "scripts" / "keep.py"
        keep.write_text("# legitimate deep-close work\n")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-1620-del", "--title", "Apply: alpha goal",
             "--outcome", "deep", "--repo", str(repo)],
            env={"MIND_AGENT": "alpha"},
        )

        assert result.returncode == 0, \
            f"commit ABORTED on staged deletion (the bug). stdout={result.stdout!r} stderr={result.stderr!r}"
        combined = result.stderr + result.stdout
        assert "already-staged deletion" in combined, \
            f"staged-deletion INFO marker missing. combined={combined!r}"

        names = _committed_name_status(repo)
        assert "to-delete.py" in names, \
            f"staged deletion was NOT committed. name-status={names!r}"
        assert "keep.py" in names, \
            f"legitimate work was dropped by the abort. name-status={names!r}"


def test_staged_deletion_only_still_commits():
    """A commit whose ONLY change is a staged deletion must still proceed — the
    empty-changes guard now counts staged_del_files, so the deletion is not
    mistaken for "nothing to commit"."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)

        lone = repo / "core" / "scripts" / "lone-delete.py"
        lone.write_text("# only change this iteration\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add lone-delete")
        _git(repo, "rm", "-q", "core/scripts/lone-delete.py")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-1620-lone", "--title", "Apply: alpha goal",
             "--outcome", "deep", "--repo", str(repo)],
            env={"MIND_AGENT": "alpha"},
        )

        assert result.returncode == 0, \
            f"deletion-only commit failed. stdout={result.stdout!r} stderr={result.stderr!r}"
        names = _committed_name_status(repo)
        assert "lone-delete.py" in names, \
            f"deletion-only commit did not include the deletion. name-status={names!r}"


def test_dry_run_lists_staged_deletion():
    """--dry-run surfaces staged deletions in their dedicated section without
    attempting the failing add."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)

        deleted = repo / "core" / "scripts" / "dry-delete.py"
        deleted.write_text("# x\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add dry-delete")
        _git(repo, "rm", "-q", "core/scripts/dry-delete.py")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-1620-dry", "--title", "Apply: alpha goal",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        assert result.returncode == 0, \
            f"dry-run failed. stdout={result.stdout!r} stderr={result.stderr!r}"
        combined = result.stderr + result.stdout
        assert "already staged for deletion" in combined, \
            f"dry-run staged-deletion section missing. combined={combined!r}"
        assert "dry-delete.py" in combined, \
            f"staged deletion path missing from dry-run. combined={combined!r}"


def test_worktree_deletion_parent_present_unaffected():
    """Control: a WORKTREE deletion (" D") with parent dir PRESENT must still
    stage via `git add -A` (the path is in the index) — it is intentionally NOT
    routed to the staged-deletion path, confirming the fix is scoped to "D "."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)

        # Tracked file whose parent dir still exists; delete from worktree only
        # (NOT git rm) -> porcelain " D".
        wt = repo / "core" / "scripts" / "wt-delete.py"
        wt.write_text("# worktree-deleted\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add wt-delete")
        os.remove(wt)  # worktree deletion, parent core/scripts/ still present

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-1620-wt", "--title", "Apply: alpha goal",
             "--outcome", "deep", "--repo", str(repo)],
            env={"MIND_AGENT": "alpha"},
        )

        assert result.returncode == 0, \
            f"worktree-deletion commit failed. stdout={result.stdout!r} stderr={result.stderr!r}"
        names = _committed_name_status(repo)
        assert "wt-delete.py" in names, \
            f"worktree deletion was not committed via add -A. name-status={names!r}"
