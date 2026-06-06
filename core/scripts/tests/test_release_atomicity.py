"""test_release_atomicity.py — partial-failure atomicity for release.sh (omni#4).

Exercises the REAL release.sh write-path recovery, not a replica. The isolated
git-repo harness from test_release.py (which copies release.sh into a tmp repo so
PROJECT_ROOT anchors there) makes real commits/tags harmless, so we can drive the
actual EXIT trap (_release_cleanup, NEEDS_RESTORE) and the tag-failure rollback
(git reset --soft HEAD~1) by injecting deterministic failures.

Injection mechanism: git hooks written to <repo>/.git/hooks/ — which live OUTSIDE
the working tree, so they never trip release.sh's Step-1 dirty-tree guard (a hook
under a tracked path would, and the cut would abort before the write path).

  * pre-commit `exit 1`   -> `git commit` fails AFTER the version bump (NEEDS_RESTORE=1)
                             -> trap must restore __init__.py + RELEASES.json from HEAD.
  * post-commit `git tag`  -> creates a lightweight tag colliding with the annotated
                             release tag, so `git tag -a` fails AFTER the commit lands
                             (NEEDS_RESTORE=0) -> the explicit rollback resets the commit.

All cuts use --force-release + the harness's dead seed URL so the write path is
reached without a real seed feed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
for _p in (str(CORE_SCRIPTS), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _release_lib as L  # noqa: E402
# Reuse the isolated-repo harness (two consumers: section 9 of test_release.py
# and this file) — see implementation-discipline rule 3 (>=2 call sites).
from test_release import _setup_release_repo, run_release_in, _git, requires_git  # noqa: E402


def _set_git_hook(repo: Path, name: str, body: str) -> None:
    """Write an executable hook to <repo>/.git/hooks/<name>. `.git/` is never in
    the working tree, so this does NOT dirty the repo (release.sh Step 1 stays
    happy) — and `.git/hooks/` is git's DEFAULT hooksPath, so the hook fires."""
    h = repo / ".git" / "hooks" / name
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text(body, encoding="utf-8")
    os.chmod(h, 0o755)


# --- Mid-cut failure: trap restores the SSOT from HEAD (NEEDS_RESTORE=1) -------
@requires_git
def test_mid_cut_failure_restores_init_py_from_head(tmp_path):
    repo = _setup_release_repo(tmp_path)  # __version__ = 0.2.0
    orig_init = (repo / "mind_api" / "src" / "__init__.py").read_text(encoding="utf-8")
    _set_git_hook(repo, "pre-commit", "#!/bin/sh\necho 'forced pre-commit failure' >&2\nexit 1\n")
    r = run_release_in(repo, tmp_path, "patch", "--summary", "x", "--force-release", "y")
    assert r.returncode != 0, r.stdout + r.stderr
    # The commit failed AFTER the bump; the trap must have restored __init__.py.
    assert (repo / "mind_api" / "src" / "__init__.py").read_text(encoding="utf-8") == orig_init
    # No orphan tag, no orphan commit.
    assert "v0.2.1" not in _git(repo, "tag", "-l").stdout
    assert not (repo / ".release.lock").exists()


@requires_git
def test_mid_cut_failure_restores_releases_json_from_head(tmp_path):
    repo = _setup_release_repo(tmp_path)
    orig_rel = (repo / "RELEASES.json").read_text(encoding="utf-8")
    _set_git_hook(repo, "pre-commit", "#!/bin/sh\nexit 1\n")
    r = run_release_in(repo, tmp_path, "patch", "--summary", "x", "--force-release", "y")
    assert r.returncode != 0, r.stdout + r.stderr
    # The two-entry RELEASES.json the bump would have produced must be reverted to
    # the original single prepended state — exactly the HEAD content.
    assert (repo / "RELEASES.json").read_text(encoding="utf-8") == orig_rel


# --- Tag failure AFTER commit: explicit rollback (reset --soft), no trap restore
@requires_git
def test_tag_failure_rolls_back_commit(tmp_path):
    repo = _setup_release_repo(tmp_path)
    new = L.bump_version("0.2.0", "patch")  # 0.2.1 — the tag release.sh will attempt
    # A lightweight tag created by post-commit collides with the annotated tag,
    # so `git tag -a v0.2.1` fails AFTER the release commit lands.
    _set_git_hook(repo, "post-commit", f"#!/bin/sh\ngit tag v{new}\n")
    r = run_release_in(repo, tmp_path, "patch", "--summary", "x", "--force-release", "y")
    assert r.returncode != 0, r.stdout + r.stderr
    # reset --soft HEAD~1 rolled the release commit back: only the init commit remains.
    log = _git(repo, "log", "--oneline").stdout.strip().splitlines()
    assert len(log) == 1, log
    # The bump remains STAGED (not committed) per release.sh's documented contract.
    staged = _git(repo, "diff", "--cached", "--name-only").stdout
    assert "mind_api/src/__init__.py" in staged, staged
    assert "RELEASES.json" in staged, staged
    assert not (repo / ".release.lock").exists()


# --- Lock cleanup on both paths -----------------------------------------------
@requires_git
def test_lock_cleanup_on_commit_failure(tmp_path):
    repo = _setup_release_repo(tmp_path)
    _set_git_hook(repo, "pre-commit", "#!/bin/sh\nexit 1\n")
    r = run_release_in(repo, tmp_path, "patch", "--summary", "x", "--force-release", "y")
    assert r.returncode != 0
    assert not (repo / ".release.lock").exists(), "trap must release the lock on failure"


@requires_git
def test_lock_cleanup_on_success(tmp_path):
    repo = _setup_release_repo(tmp_path)
    r = run_release_in(repo, tmp_path, "patch", "--summary", "x", "--force-release", "y")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (repo / ".release.lock").exists(), "trap must release the lock on success"
    assert "v0.2.1" in _git(repo, "tag", "-l").stdout  # success path actually tagged


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
