"""test_cross_repo_commit.py —  regression test.

Verifies _cross_repo_commit.sh enumerates direct-child git repos under
AGENT_WRITE_PATH (resolved from <agent>/local-paths.conf) and invokes
iteration-commit.sh on each on deep outcomes. Routine outcomes short-
circuit. Missing AGENT_WRITE_PATH returns 0 silently.

Wiring lives in iteration-close.sh do_state_update right after the
existing PROJECT_ROOT iteration-commit.sh call (g-280-03). This test
exercises the helper directly via `source` + function call so the test
surface stays narrow — full do_state_update integration would couple
to ~12 other concerns (journal, team-state, WM, etc.).

Origin incident: g-115-744 recovery of f72fd70 (LOGIT zero-clamp fix)
that sat 3 days uncommitted because the 2026-05-14 g-250-85 close
hardcoded --repo "$PROJECT_ROOT" and skipped Ayoai-Environment-Server.
g-115-746 is the structural fix; this test pins it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
HELPER_SH = CORE_SCRIPTS / "_cross_repo_commit.sh"
ITERATION_COMMIT_SH = CORE_SCRIPTS / "iteration-commit.sh"

# Project-local tempdir (sibling test rationale: OS tempdir under
# C:\Users\... is unreachable from Git-Bash mount).
PROJECT_TMP = SCRIPT_DIR / "_tmp_cross_repo_commit_test"

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402


def _to_bash_path(p) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _run_bash(script: str, env=None, cwd=None, timeout=30):
    """Run a bash command. `script` is a shell snippet passed via -c."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [BASH, "-c", script],
        env=full_env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _init_git_repo(path: Path, with_dirty_file: bool = True):
    """Initialize a minimal git repo with an initial commit and optionally an
    uncommitted modified file."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    if with_dirty_file:
        # Seed-then-modify so the file shows as ' M' (modified tracked file)
        # rather than '??' (untracked) — iteration-commit.sh handles both
        # but ' M' avoids the untracked-filter machinery in test surface.
        (path / "feature.txt").write_text("seeded\n")
        subprocess.run(["git", "add", "feature.txt"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)
        (path / "feature.txt").write_text("modified for cross-repo test\n")


def _write_local_paths_conf(agent_dir: Path, agent_write_path: Path):
    conf = agent_dir / "local-paths.conf"
    conf.write_text(
        f"WORLD_PATH=/tmp/fake-world\n"
        f"META_PATH=/tmp/fake-meta\n"
        f"AGENT_WRITE_PATH={agent_write_path}\n",
        encoding="utf-8",
    )


def _setup_project_root(tmp: Path, agent: str = "alpha") -> Path:
    """Mock a project root with core/scripts/_cross_repo_commit.sh +
    core/scripts/iteration-commit.sh accessible via the resolved script_dir."""
    project = tmp / "project"
    (project / "core" / "scripts").mkdir(parents=True)
    # Copy the helper + iteration-commit.sh into the mock so the helper's
    # cd-based path resolution lands inside the test project, not the real one.
    (project / "core" / "scripts" / "_cross_repo_commit.sh").write_bytes(HELPER_SH.read_bytes())
    (project / "core" / "scripts" / "iteration-commit.sh").write_bytes(ITERATION_COMMIT_SH.read_bytes())
    # iteration-commit.sh sources _paths.sh — copy any siblings it needs.
    # For this narrow test we mock around iteration-commit.sh entirely; see
    # _replace_iteration_commit_with_shim below.
    (project / agent).mkdir()
    (project / agent / "self.md").write_text(f"# {agent}\n")
    return project


def _replace_iteration_commit_with_shim(project: Path, log_file: Path):
    """Replace the copied iteration-commit.sh with a recording shim. The shim
    appends one line per invocation to `log_file` capturing --repo / --goal-id /
    --title / --outcome, then exits 0. Decouples this test from the real
    iteration-commit.sh's dependencies (_paths.sh, team-state-read.sh, etc.).
    """
    shim = project / "core" / "scripts" / "iteration-commit.sh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "# Test shim recording invocations to LOG_FILE.\n"
        "log_file=\"$CROSS_REPO_TEST_LOG\"\n"
        "goal_id=\"\" title=\"\" outcome=\"\" repo=\"\"\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  case \"$1\" in\n"
        "    --goal-id) goal_id=\"$2\"; shift 2 ;;\n"
        "    --title) title=\"$2\"; shift 2 ;;\n"
        "    --outcome) outcome=\"$2\"; shift 2 ;;\n"
        "    --repo) repo=\"$2\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "printf '%s|%s|%s|%s\\n' \"$repo\" \"$goal_id\" \"$title\" \"$outcome\" >> \"$log_file\"\n"
        "echo \"shim-ok repo=$repo\"\n"
        "exit 0\n"
    )
    shim.chmod(0o755)


def _read_shim_log(log_file: Path) -> list[dict]:
    if not log_file.exists():
        return []
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    out = []
    for line in lines:
        parts = line.split("|")
        if len(parts) >= 4:
            out.append({
                "repo": parts[0],
                "goal_id": parts[1],
                "title": parts[2],
                "outcome": parts[3],
            })
    return out


def _source_and_call(project: Path, log_file: Path, *, agent: str, outcome: str,
                     goal_id: str = "g-test-746", title: str = "Apply: test cross-repo"):
    """Source the helper and invoke cross_repo_commit_product, returning the
    completed subprocess result."""
    helper = project / "core" / "scripts" / "_cross_repo_commit.sh"
    script = (
        f"source {_to_bash_path(helper)} && "
        f"cross_repo_commit_product "
        f"--goal-id {goal_id} "
        f"--title '{title}' "
        f"--outcome {outcome}"
    )
    return _run_bash(
        script,
        env={
            "MIND_AGENT": agent,
            "CROSS_REPO_TEST_LOG": str(log_file).replace("\\", "/"),
        },
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_deep_outcome_with_two_product_repos_invokes_iteration_commit_for_each():
    """AGENT_WRITE_PATH with two git repos under it → iteration-commit.sh
    fires once per repo, with the goal-id/title/outcome propagated.
    This is the canonical case g-115-746 was filed to fix."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        project = _setup_project_root(tmp)
        log_file = tmp / "shim.log"
        _replace_iteration_commit_with_shim(project, log_file)

        # Build the product workspace root with TWO git repos.
        product_root = tmp / "product-root"
        product_root.mkdir()
        repo_a = product_root / "Ayoai-Environment-Server"
        repo_b = product_root / "Ayoai-Roblox-Integration"
        _init_git_repo(repo_a, with_dirty_file=True)
        _init_git_repo(repo_b, with_dirty_file=True)
        # A non-git directory in the same root should be ignored.
        (product_root / "Ayoai-Docs").mkdir()
        (product_root / "Ayoai-Docs" / "README.md").write_text("not a git repo\n")

        _write_local_paths_conf(project / "alpha", product_root)

        result = _source_and_call(project, log_file, agent="alpha", outcome="deep")
        assert result.returncode == 0, f"helper non-zero exit: {result.stderr}"
        entries = _read_shim_log(log_file)
        repos_called = sorted(e["repo"] for e in entries)
        assert len(entries) == 2, (
            f"expected 2 iteration-commit invocations, got {len(entries)}; "
            f"stdout={result.stdout!r}; entries={entries!r}"
        )
        # Compare on basenames since the shim records the path as bash passed it.
        basenames = sorted(Path(r).name for r in repos_called)
        assert basenames == ["Ayoai-Environment-Server", "Ayoai-Roblox-Integration"], \
            f"basenames mismatch: {basenames}"
        # Goal metadata propagates correctly.
        for e in entries:
            assert e["goal_id"] == "g-test-746", f"goal_id mismatch: {e}"
            assert e["outcome"] == "deep", f"outcome mismatch: {e}"
        # stdout includes a [iteration-close] log line per repo.
        assert result.stdout.count("[iteration-close] cross-repo iteration-commit") == 2, \
            f"expected 2 log lines, stdout={result.stdout!r}"


def test_routine_outcome_short_circuits_no_iteration_commit_calls():
    """Routine outcome MUST short-circuit before reading local-paths.conf,
    even when AGENT_WRITE_PATH is configured and product repos exist.
    iteration-commit.sh would no-op on routine anyway; the short-circuit
    saves an unnecessary subprocess fork per iteration on the hot path."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        project = _setup_project_root(tmp)
        log_file = tmp / "shim.log"
        _replace_iteration_commit_with_shim(project, log_file)
        product_root = tmp / "product-root"
        product_root.mkdir()
        _init_git_repo(product_root / "Ayoai-Environment-Server", with_dirty_file=True)
        _write_local_paths_conf(project / "alpha", product_root)

        result = _source_and_call(project, log_file, agent="alpha", outcome="routine")
        assert result.returncode == 0, f"helper non-zero exit: {result.stderr}"
        entries = _read_shim_log(log_file)
        assert entries == [], (
            f"routine outcome should not invoke iteration-commit; got {entries}; "
            f"stdout={result.stdout!r}"
        )


def test_missing_agent_write_path_returns_silently():
    """local-paths.conf without an AGENT_WRITE_PATH line MUST return 0 with
    no output and no shim invocations. AGENT_WRITE_PATH is optional in the
    conf schema — agents without product workspace access stay quiet."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        project = _setup_project_root(tmp)
        log_file = tmp / "shim.log"
        _replace_iteration_commit_with_shim(project, log_file)
        # Write a local-paths.conf without AGENT_WRITE_PATH.
        (project / "alpha" / "local-paths.conf").write_text(
            "WORLD_PATH=/tmp/fake-world\nMETA_PATH=/tmp/fake-meta\n",
            encoding="utf-8",
        )

        result = _source_and_call(project, log_file, agent="alpha", outcome="deep")
        assert result.returncode == 0, f"helper non-zero exit: {result.stderr}"
        entries = _read_shim_log(log_file)
        assert entries == [], f"expected silent no-op; got {entries}"
        # No log line should be printed.
        assert "[iteration-close] cross-repo" not in result.stdout, \
            f"unexpected log on missing AGENT_WRITE_PATH: {result.stdout!r}"


def test_agent_write_path_directory_with_no_git_repos_emits_no_log():
    """AGENT_WRITE_PATH set but the directory contains only non-git
    subdirectories → no iteration-commit invocations, no log lines."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        project = _setup_project_root(tmp)
        log_file = tmp / "shim.log"
        _replace_iteration_commit_with_shim(project, log_file)
        product_root = tmp / "product-root"
        product_root.mkdir()
        (product_root / "Ayoai-Docs").mkdir()
        (product_root / "Ayoai-Docs" / "README.md").write_text("not a git repo\n")
        _write_local_paths_conf(project / "alpha", product_root)

        result = _source_and_call(project, log_file, agent="alpha", outcome="deep")
        assert result.returncode == 0, f"helper non-zero exit: {result.stderr}"
        entries = _read_shim_log(log_file)
        assert entries == [], f"expected no invocations; got {entries}"
        assert "[iteration-close] cross-repo" not in result.stdout, \
            f"unexpected log line: {result.stdout!r}"


def test_missing_ayoai_agent_env_returns_silently():
    """MIND_AGENT unset → silent return. Defensive against the
    autocompact-resume race where _paths.sh ran but MIND_AGENT wasn't
    re-injected. iteration-commit.sh would error on the same condition;
    skipping cross-repo entirely avoids cascading failures."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        project = _setup_project_root(tmp)
        log_file = tmp / "shim.log"
        _replace_iteration_commit_with_shim(project, log_file)

        # Call without setting MIND_AGENT — set to empty string.
        helper = project / "core" / "scripts" / "_cross_repo_commit.sh"
        script = (
            f"source {_to_bash_path(helper)} && "
            f"cross_repo_commit_product "
            f"--goal-id g-test --title 'Apply: x' --outcome deep"
        )
        full_env = dict(os.environ)
        full_env.pop("MIND_AGENT", None)
        full_env["CROSS_REPO_TEST_LOG"] = str(log_file).replace("\\", "/")
        result = subprocess.run(
            [BASH, "-c", script],
            env=full_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"helper non-zero exit: {result.stderr}"
        entries = _read_shim_log(log_file)
        assert entries == [], f"expected no invocations; got {entries}"


if __name__ == "__main__":
    # Manual run mode — execute each test, print pass/fail.
    tests = [
        test_deep_outcome_with_two_product_repos_invokes_iteration_commit_for_each,
        test_routine_outcome_short_circuits_no_iteration_commit_calls,
        test_missing_agent_write_path_returns_silently,
        test_agent_write_path_directory_with_no_git_repos_emits_no_log,
        test_missing_ayoai_agent_env_returns_silently,
    ]
    fail = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            fail += 1
        except Exception as e:
            print(f"ERR  {t.__name__}: {type(e).__name__}: {e}")
            fail += 1
    sys.exit(1 if fail else 0)
