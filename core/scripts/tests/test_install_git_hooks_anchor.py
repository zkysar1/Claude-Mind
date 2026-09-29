"""install-git-hooks.sh configures the repo it LIVES in, never the caller's cwd.

The installer runs at every session start (sessionstart-orchestrator.sh Step
0.5), and a SessionStart hook runs in the session's CURRENT directory. Measured
2026-09-28 (alpha, DESKTOP-O91DLK2): the session cwd was inside a scratch clone
nested under the project tree, so `git rev-parse --show-toplevel` answered with
the CLONE and the main repo's installer set the clone's core.hooksPath. That
switched on the clone's post-commit hook; the next commit there touched daemon
code, and the hook launched `mind-api-start.sh --restart` rooted in the clone —
a stray daemon that ran its own sync sweep over the real world store for ~8h.

Anchoring on the script's own location fixes the wrong-repo half, and opens the
guard-5267 trap: git searches ANCESTORS, so a copy that is not itself a repo top
level would configure whatever repo encloses it. The second test pins that.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _bash_helpers import BASH  # noqa: E402

INSTALLER = Path(__file__).resolve().parents[1] / "install-git-hooks.sh"

# A parent git hook (a suite run from pre-commit, say) exports GIT_DIR and
# friends, which would point every git call below at the wrong repository.
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], env=ENV,
                          capture_output=True, text=True)


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    assert git(path, "init", "-q").returncode == 0
    return path


def plant_installer(root):
    scripts = root / "core" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(INSTALLER, scripts / "install-git-hooks.sh")
    (root / "core" / "githooks").mkdir()
    return scripts / "install-git-hooks.sh"


def hooks_path(repo):
    return git(repo, "config", "--local", "--get", "core.hooksPath").stdout.strip()


def run_installer(script, cwd):
    return subprocess.run([BASH, str(script)], cwd=str(cwd), env=ENV,
                          capture_output=True, text=True, timeout=60)


def test_configures_its_own_repo_not_the_nested_repo_it_runs_from(tmp_path):
    main = make_repo(tmp_path / "main")
    script = plant_installer(main)
    clone = make_repo(main / "scratch" / "clone")

    proc = run_installer(script, cwd=clone)

    assert proc.returncode == 0, proc.stderr
    assert hooks_path(clone) == "", "the caller's repo must be left alone"
    assert hooks_path(main) == "core/githooks"


def test_a_copy_that_is_not_a_repo_top_level_configures_nothing(tmp_path):
    main = make_repo(tmp_path / "main")
    stray = main / "scratch" / "copy"  # a clone whose .git is gone
    script = plant_installer(stray)

    proc = run_installer(script, cwd=stray)

    assert proc.returncode == 0, proc.stderr
    assert hooks_path(main) == "", "must not climb to the enclosing repo"
    assert "not a git top level" in proc.stderr
