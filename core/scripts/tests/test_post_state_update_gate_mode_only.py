"""test_post_state_update_gate_mode_only.py — regression test for 2.

Exercises the mode-only exclusion added to core/scripts/post-state-update-gate.sh.
A file whose ONLY change is a mode bit (exec-bit normalization, symlink/type
flip) has ZERO reviewable content — `git diff --numstat` reports it as
"0<tab>0<tab><path>". Such a file must NOT count toward the core_files threshold
nor fire a fresh-eyes-code review: there is no code delta to review. The LOC
path already treated mode-only as 0; the fix makes the FILE-COUNT path
consistent so the gate measures CONTENT deltas, not mode flips.

Canonical residue class (g-115-1901): normalizing 434 Windows-authored *.sh
exec bits (100644->100755) is mode-only + zero-content; before the fix that
residue blew past core_files>=3 on the next deep close and dispatched a review
with nothing to look at — the "pre-existing-residue case" this test covers.

Cases:
  1. test_committed_mode_only_excluded            — committed scope: a commit of
     4 mode-only + 2 content core files → core_count == 2 (mode-only dropped).
  2. test_committed_all_mode_only_no_fire         — committed scope: a commit of
     4 mode-only-ONLY core files → core_count == 0, fired == false
     (the g-115-1901 exec-bit-commit scenario; would have fired 4>=3 pre-fix).
  3. test_worktree_mode_only_residue_excluded     — working-tree fallback: 5
     mode-only residue + 3 content files → core_count == 3, fires on the
     content only (the uncommitted-residue form). Linux-guarded (needs the
     filesystem exec bit; skipped where git does not detect mode changes).
  4. test_untracked_new_file_still_counts         — safety: an untracked NEW
     core file (new content, absent from numstat vs HEAD) is ALWAYS kept —
     the fix must never drop genuinely-new work.

Pattern mirrors test_post_state_update_gate_committed_files_only.py: subprocess
+ project-local tempdir + scripted git init, invoking the REAL gate by absolute
path with cwd=<temp repo>. World/meta redirected to empty temp dirs so the
attribution filter + cooldown peer-read are deterministic; the autouse fixture
neutralizes then restores zeta's fresh_eyes_last_fire WM slot.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE_SH = CORE_SCRIPTS / "post-state-update-gate.sh"
WM_READ_SH = CORE_SCRIPTS / "wm-read.sh"
WM_SET_SH = CORE_SCRIPTS / "wm-set.sh"

PROJECT_TMP = SCRIPT_DIR / "_tmp_mode_only_test"

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _to_bash_path(p) -> str:
    """Convert a Windows path to Git-Bash mount-prefix form (/c/...); no-op on POSIX."""
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=15
    )
    return out.stdout.strip()


def _init_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    subprocess.run(["git", "config", "core.filemode", "true"], cwd=repo, check=True)
    (repo / "core" / "scripts").mkdir(parents=True)
    (repo / "core" / "scripts" / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _run_gate(repo: Path, world: Path, meta: Path, commit_sha: str | None) -> dict:
    env = dict(os.environ)
    env["MIND_AGENT"] = "zeta"
    env["MIND_WORLD"] = _to_bash_path(world)
    env["MIND_META"] = _to_bash_path(meta)
    env.pop("COMMIT_SHA", None)
    if commit_sha is not None:
        env["COMMIT_SHA"] = commit_sha
    result = subprocess.run(
        [GIT_BASH, _to_bash_path(GATE_SH), "deep"],
        cwd=str(repo), env=env, capture_output=True, text=True, timeout=60,
    )
    parsed = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
    assert parsed is not None, (
        f"gate produced no JSON. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return parsed


@pytest.fixture(autouse=True)
def _preserve_zeta_wm():
    """Save, neutralize (null), then restore zeta's fresh_eyes_last_fire so the
    content-fires cases below are not suppressed by live cooldown state. Mirrors
    test_post_state_update_gate_committed_files_only.py::_preserve_zeta_wm."""
    saved = "null"
    try:
        r = subprocess.run(
            [GIT_BASH, _to_bash_path(WM_READ_SH), "fresh_eyes_last_fire", "--json"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "MIND_AGENT": "zeta"},
        )
        if r.returncode == 0 and r.stdout.strip():
            saved = r.stdout.strip()
    except Exception:
        pass
    try:
        subprocess.run(
            [GIT_BASH, _to_bash_path(WM_SET_SH), "fresh_eyes_last_fire"],
            input="null", capture_output=True, text=True, timeout=30,
            env={**os.environ, "MIND_AGENT": "zeta"},
        )
    except Exception:
        pass
    yield
    try:
        subprocess.run(
            [GIT_BASH, _to_bash_path(WM_SET_SH), "fresh_eyes_last_fire"],
            input=saved, capture_output=True, text=True, timeout=30,
            env={**os.environ, "MIND_AGENT": "zeta"},
        )
    except Exception:
        pass


def test_committed_mode_only_excluded():
    """Committed scope: a commit of 4 mode-only + 3 content core files counts
    only the 3 content files (mode-only dropped). 3 content >= the core_files
    threshold, so the gate fires and lists exactly the content files — proving
    both that mode-only is excluded AND that content deltas are still counted."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _init_repo(tmp)
        world = tmp / "world"; world.mkdir()
        meta = tmp / "meta"; meta.mkdir()

        mode_only = [f"core/scripts/mo-{i}.sh" for i in range(4)]
        content = [f"core/scripts/ct-{i}.sh" for i in range(3)]
        # Base commit: all exist with content, mode 644.
        for rel in mode_only + content:
            (repo / rel).write_text("#!/bin/sh\necho base\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
        # Goal commit: 4 mode-only (chmod +x staged) + 3 content edits.
        for rel in mode_only:
            subprocess.run(["git", "update-index", "--chmod=+x", rel], cwd=repo, check=True)
        for rel in content:
            (repo / rel).write_text("#!/bin/sh\necho base\necho edited\n")
        subprocess.run(["git", "add", "--", *content], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "goal"], cwd=repo, check=True)
        sha = _git(repo, "rev-parse", "HEAD")

        out = _run_gate(repo, world, meta, commit_sha=sha)
        assert out["core_count"] == 3, f"expected only 3 content files counted: {out}"
        assert out["fired"] is True, f"3 content files should fire: {out}"
        files = set(out.get("files", []))
        assert files == set(content), f"mode-only files leaked into the count: {files}"
        for mo in mode_only:
            assert mo not in files, f"mode-only file counted: {mo} in {files}"


def test_committed_all_mode_only_no_fire():
    """Committed scope: a commit of 4 mode-only-ONLY core files → core_count == 0,
    fired == false. Pre-fix this would have fired (4 >= 3 core_files). This is
    the g-115-1901 exec-bit-commit scenario."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _init_repo(tmp)
        world = tmp / "world"; world.mkdir()
        meta = tmp / "meta"; meta.mkdir()

        mode_only = [f"core/scripts/exec-{i}.sh" for i in range(4)]
        for rel in mode_only:
            (repo / rel).write_text("#!/bin/sh\necho x\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
        for rel in mode_only:
            subprocess.run(["git", "update-index", "--chmod=+x", rel], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "exec-bit normalize"], cwd=repo, check=True)
        sha = _git(repo, "rev-parse", "HEAD")

        out = _run_gate(repo, world, meta, commit_sha=sha)
        assert out["core_count"] == 0, f"mode-only-only commit must count 0: {out}"
        assert out["fired"] is False, f"mode-only-only commit must NOT fire: {out}"


def test_worktree_mode_only_residue_excluded():
    """Working-tree fallback: 5 mode-only exec-bit residue files + 3 content
    files. The residue is excluded; the gate fires on the 3 content files only.
    The uncommitted-residue form of g-115-1901. Skipped where git does not
    detect a filesystem mode change (e.g. Windows, core.filemode unsupported)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _init_repo(tmp)
        world = tmp / "world"; world.mkdir()
        meta = tmp / "meta"; meta.mkdir()

        residue = [f"core/scripts/res-{i}.sh" for i in range(5)]
        content = [f"core/scripts/edit-{i}.sh" for i in range(3)]
        for rel in residue + content:
            (repo / rel).write_text("#!/bin/sh\necho v0\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

        # Working-tree residue: filesystem exec-bit flip, no content change.
        for rel in residue:
            os.chmod(repo / rel, 0o755)
        # Probe: does git see the mode change here? If not (Windows), skip.
        seen = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=15,
        ).stdout
        if not any(r in seen for r in residue):
            pytest.skip("git does not detect filesystem mode changes on this platform")

        # 3 real content edits (uncommitted working-tree changes).
        for rel in content:
            (repo / rel).write_text("#!/bin/sh\necho v0\necho v1\n")

        out = _run_gate(repo, world, meta, commit_sha=None)
        assert out["core_count"] == 3, f"expected only 3 content files, residue excluded: {out}"
        files = set(out.get("files", []))
        assert files == set(content), f"residue leaked into count: {files}"
        assert out["fired"] is True, f"expected fire on the 3 content files: {out}"
        for r in residue:
            assert r not in files, f"mode-only residue counted: {r} in {files}"


def test_untracked_new_file_still_counts():
    """Safety: untracked NEW core files (new content, absent from numstat vs
    HEAD) are ALWAYS kept — the mode-only fix must never drop genuinely-new
    work. 3 new untracked scripts → core_count == 3, fires."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _init_repo(tmp)
        world = tmp / "world"; world.mkdir()
        meta = tmp / "meta"; meta.mkdir()

        new_files = [f"core/scripts/new-{i}.sh" for i in range(3)]
        for rel in new_files:
            (repo / rel).write_text("#!/bin/sh\necho brand new\n")
        # Left UNTRACKED (never git-added).

        out = _run_gate(repo, world, meta, commit_sha=None)
        assert out["core_count"] == 3, f"untracked new files must all count: {out}"
        files = set(out.get("files", []))
        assert files == set(new_files), f"an untracked new file was dropped: {files}"
        assert out["fired"] is True, f"expected fire on 3 new scripts: {out}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
