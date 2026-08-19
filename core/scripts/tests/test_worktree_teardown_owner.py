#!/usr/bin/env python3
""" — worktree-teardown.sh must remove worktrees of FOREIGN repos.

THE BUG THIS PINS
-----------------
`worktree-teardown.sh` ran `git -C "$PROJECT_ROOT" worktree remove <path>`.
That is correct only while every worktree it is asked to remove belongs to THIS
repo — true of its originating caller (g-328-08: throwaway pytest worktrees of
the Mind repo) and false the moment a promotion plants into a worktree of a
downstream deployment clone. Run from the wrong repo, git answers

    fatal: '<path>' is not a working tree

which the script's own control flow reads as "removal failed (handle still
busy?)" — a Windows-flavoured diagnosis for what is actually a wrong-repo
error. It then exits 1 leaving BOTH the directory and the worktree
registration behind, and the registration is what wedges the next run with
`'<branch>' is already used by worktree at ...`.

The script had ZERO callers and ZERO tests when this was found. Its header
advertised composability into two scripts that never called it, so the defect
was unreachable-by-construction until first real use — which is why
`test_foreign_repo_worktree_is_removed` below is the load-bearing test in this
file rather than a completeness nicety.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

REPO = Path(__file__).resolve().parents[2].parent
TEARDOWN = REPO / "core" / "scripts" / "worktree-teardown.sh"


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


# NOT named `teardown`: a module-level function with that name is pytest's
# nose-style teardown hook, so pytest calls it with the MODULE object after
# the file's tests finish. That collision was silent while the helper did
# str(path) — the bogus call just produced an ignored non-zero rc — and only
# became visible once the path was posix-ified and TypeError'd on a module.
def run_teardown(path, *extra):
    return subprocess.run(
        [BASH, TEARDOWN.as_posix(), Path(path).as_posix(), "--force", *extra],
        capture_output=True, text=True,
    )


@pytest.fixture()
def foreign(tmp_path):
    """A repo that is NOT this project — the case the hardcode got wrong."""
    r = tmp_path / "foreign"
    r.mkdir()
    git("init", "-q", "-b", "main", cwd=r)
    git("config", "user.email", "t@example.invalid", cwd=r)
    git("config", "user.name", "t", cwd=r)
    (r / "f.txt").write_text("x\n", encoding="utf-8")
    git("add", "-A", cwd=r)
    git("commit", "-qm", "base", cwd=r)
    assert r.resolve() != REPO.resolve()
    return r


def add_worktree(owner, tmp_path, name="wt", branch="wt/topic"):
    wt = tmp_path / name
    git("worktree", "add", "-b", branch, str(wt), cwd=owner)
    return wt


def test_foreign_repo_worktree_is_removed(foreign, tmp_path):
    """The regression test. Fails against the PROJECT_ROOT hardcode."""
    wt = add_worktree(foreign, tmp_path)
    proc = run_teardown(wt)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not wt.exists()
    assert str(wt) not in git("worktree", "list", cwd=foreign)


def test_owner_flag_works_when_the_directory_is_already_gone(foreign, tmp_path):
    """Derivation needs the directory; --owner does not.

    This is the case that makes --owner more than a convenience: a crashed run
    or a hand-cleanup leaves the registration without the directory, and only
    an explicitly-named owner can still prune it.
    """
    wt = add_worktree(foreign, tmp_path)
    subprocess.run(["rm", "-rf", str(wt)], check=True)
    assert str(wt) in git("worktree", "list", cwd=foreign)

    proc = run_teardown(wt, "--owner", foreign.as_posix())
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert str(wt) not in git("worktree", "list", cwd=foreign), (
        "stale registration survived — the next run would fail with "
        "'already used by worktree'"
    )


def test_without_owner_a_vanished_foreign_worktree_cannot_be_resolved(foreign, tmp_path):
    """Negative control for the test above.

    Proves --owner is doing the work: same scenario, flag withheld, derivation
    impossible (no directory to ask), fallback lands on PROJECT_ROOT — and the
    stale registration in the FOREIGN repo is left untouched. Without this,
    the passing test above is consistent with --owner being ignored entirely.
    """
    wt = add_worktree(foreign, tmp_path, name="wt2", branch="wt/topic2")
    subprocess.run(["rm", "-rf", str(wt)], check=True)

    proc = run_teardown(wt)
    assert str(wt) in git("worktree", "list", cwd=foreign), (
        "the foreign registration was pruned without --owner; if this is a "
        "deliberate improvement, retire this control rather than deleting it"
    )
    assert proc.returncode == 1


def test_owner_is_derived_from_the_worktree_when_not_supplied(foreign, tmp_path):
    """The no-flag path must not regress to PROJECT_ROOT while the dir exists."""
    wt = add_worktree(foreign, tmp_path, name="wt3", branch="wt/topic3")
    proc = run_teardown(wt)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert f"owning repo: {foreign}" in proc.stdout, (
        f"expected the derived owner to be {foreign}; got:\n{proc.stdout}"
    )


def test_branch_and_commit_survive_teardown(foreign, tmp_path):
    """Teardown removes a checkout, never work."""
    wt = add_worktree(foreign, tmp_path, name="wt4", branch="wt/topic4")
    (wt / "new.txt").write_text("payload\n", encoding="utf-8")
    git("add", "-A", cwd=wt)
    git("commit", "-qm", "work", cwd=wt)
    sha = git("rev-parse", "HEAD", cwd=wt)

    assert run_teardown(wt).returncode == 0
    assert git("rev-parse", "wt/topic4", cwd=foreign) == sha
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=foreign) == "main"


def test_still_refuses_to_tear_down_project_root():
    """The pre-existing safety guard must survive the owner refactor."""
    proc = run_teardown(REPO)
    assert proc.returncode == 2
    assert "refusing to tear down PROJECT_ROOT" in proc.stderr


def test_usage_and_help_windows_render_completely():
    """Both sed windows are line-number ranges — a header edit can truncate them."""
    helped = subprocess.run(
        [BASH, TEARDOWN.as_posix(), "--help"], capture_output=True, text=True
    )
    assert helped.returncode == 0
    assert "Exit codes:" in helped.stdout, "help window cut off before the exit codes"
    assert "--owner" in helped.stdout

    usage = subprocess.run([BASH, TEARDOWN.as_posix()], capture_output=True, text=True)
    assert usage.returncode == 2
    assert "--owner" in usage.stderr, "usage hint omits the flag it now accepts"
