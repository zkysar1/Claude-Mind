"""test_iteration_commit_untracked_filter.py — regression test for 
+ 38fb983 M-status extension.

Verifies that iteration-commit.sh's cross-agent uncommitted-file filter
correctly drops files whose mtime predates the committer's claimed_at,
while preserving files modified after the claim (legitimate own work).

The filter covers BOTH untracked (??) and modified (' M' etc.) status codes
since the M-status extension landed for commit 38fb983 (3rd recurrence;
alpha's iteration-commit for g-115-671 swept bravo's 9 asp-304 M-status
files at core/scripts/). Diagnostic marker: "filtered (cross-agent-uncommitted)".

Cases covered (untracked, original g-248-87):
  1. Untracked file with mtime BEFORE claimed_at → filtered (skipped)
  2. Untracked file with mtime AFTER claimed_at → included (own work)
  3. --include-untracked override → all files included regardless of mtime
  4. No claimed_at available → filter inactive (fail-open)
  5. Own agent-dir path → never filtered (namespace handles other-agent dirs)

Cases covered (modified, 38fb983 extension):
  6. Modified tracked file with mtime BEFORE claimed_at → filtered
  7. Modified tracked file with mtime AFTER claimed_at → included
  8. Modified file under own agent dir → never filtered (matches case 5)

Each case runs iteration-commit.sh in --dry-run mode against a tempdir git
repo with a mocked AGENT_DIR/team-state.yaml structure. Parses script
stderr for the filter's diagnostic marker to verify decisions.

Pattern: subprocess + tempdir + scripted git init. Mirrors the integration-
test style used in test_defer_recheck_patterns.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
ITERATION_COMMIT_SH = CORE_SCRIPTS / "iteration-commit.sh"

# Use project-local tempdir; the OS tempdir under C:\Users\... is unreachable
# from this environment's bash (mountpoint mismatch). Project workspace is
# where every other bash invocation in this repo demonstrably works.
PROJECT_TMP = SCRIPT_DIR / "_tmp_iteration_commit_test"


def _to_bash_path(p) -> str:
    """Convert a Windows path to Git-Bash mount-prefix form (/c/...)."""
    s = str(p).replace("\\", "/")
    # Git-Bash on Windows mounts C: as /c/. Translate "C:/..." → "/c/...".
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


# Resolve bash via shared helper (, 2026-05-16). See
# core/scripts/tests/_bash_helpers.py for the canonical resolution
# priority (MIND_SHELL → Git-Bash candidates → shutil.which). GIT_BASH
# alias preserved because `_run_bash` builds Git-Bash mount-prefix paths
# explicitly (/c/...) and the variable name carries that intent.
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _run_bash(args, env=None, cwd=None):
    """Run bash command via Git-Bash (msys), not WSL bash."""
    cmd = [GIT_BASH] + [_to_bash_path(a) for a in args]
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd,
        env=full_env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _setup_repo(tmpdir: Path) -> Path:
    """Initialize a minimal git repo with alpha/zeta agent dirs."""
    repo = tmpdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    # Create alpha + zeta agent dirs with self.md sentinel ( namespace discovery).
    # Phase 2.5.D: agent dirs live under agents/ parent.
    (repo / "agents").mkdir()
    for a in ("alpha", "zeta"):
        d = repo / "agents" / a
        d.mkdir()
        (d / "self.md").write_text(f"# {a}\n")
    # Seed core/scripts/ with a placeholder so it's a TRACKED dir — otherwise
    # `git status --porcelain` collapses new files under untracked dirs to
    # the dir path, not the file path. Real repo has core/scripts/ tracked.
    core_scripts = repo / "core" / "scripts"
    core_scripts.mkdir(parents=True)
    (core_scripts / ".gitkeep").write_text("")
    # Seed initial commit so HEAD exists
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _mock_team_state_read(tmpdir: Path, claimed_at_iso: str | None) -> Path:
    """Create a shim team-state-read.sh that emits the desired claimed_at."""
    shim = tmpdir / "team-state-read.sh"
    if claimed_at_iso is None:
        body = '#!/usr/bin/env bash\necho "null"\nexit 0\n'
    else:
        body = f'#!/usr/bin/env bash\necho \'"{claimed_at_iso}"\'\nexit 0\n'
    shim.write_text(body)
    shim.chmod(0o755)
    return shim


def _run_iteration_commit(
    repo: Path,
    agent: str,
    extra_args: list[str] | None = None,
    shim_dir: Path | None = None,
):
    """Invoke iteration-commit.sh in dry-run with controlled environment."""
    env = {"MIND_AGENT": agent}
    # When shim_dir provided, prepend to PATH so our team-state-read.sh wins
    # over the real script's sibling lookup. BUT the script uses BASH_SOURCE
    # dirname to find team-state-read.sh, so we need to copy the actual script
    # into a temp dir and shim alongside it.
    args = [
        str(ITERATION_COMMIT_SH),
        "--goal-id", "g-test-01",
        "--title", "Apply: test filter",
        "--outcome", "deep",
        "--repo", str(repo),
        "--dry-run",
    ]
    if extra_args:
        args.extend(extra_args)
    return _run_bash(args, env=env)


def _shim_iteration_commit(tmpdir: Path, claimed_at_iso: str | None) -> Path:
    """Copy iteration-commit.sh into tmpdir alongside a mock team-state-read.sh."""
    shim_dir = tmpdir / "scripts"
    shim_dir.mkdir()
    # Copy iteration-commit.sh — preserve LF line endings (CRLF in shebang
    # makes bash look for a program named "bash\r" which fails opaque).
    target = shim_dir / "iteration-commit.sh"
    src_content = ITERATION_COMMIT_SH.read_bytes()
    target.write_bytes(src_content)
    target.chmod(0o755)
    # Create mock team-state-read.sh
    _mock_team_state_read(shim_dir, claimed_at_iso)
    return target


def test_untracked_file_predating_claimed_at_is_filtered():
    """File mtime BEFORE claimed_at → filtered out."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp, "2026-05-11T23:00:00")

        # Create untracked file with mtime 5 minutes BEFORE claimed_at
        # claimed_at = 2026-05-11T23:00:00 UTC → epoch via Python
        import datetime
        claimed_epoch = int(datetime.datetime.fromisoformat("2026-05-11T23:00:00").timestamp())
        file_mtime = claimed_epoch - 300  # 5 min before claim
        target_file = repo / "core" / "scripts" / "partner-wip.py"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("# partner WIP\n")
        os.utime(target_file, (file_mtime, file_mtime))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        # Filter must have caught it
        assert "filtered (cross-agent-uncommitted)" in (result.stderr + result.stdout), \
            f"Expected filter to catch the file. stderr={result.stderr!r} stdout={result.stdout!r}"
        assert "partner-wip.py" in (result.stderr + result.stdout), \
            f"Filtered file path missing in output. stderr={result.stderr!r}"


def test_untracked_file_postdating_claimed_at_is_included():
    """File mtime AFTER claimed_at → included (own work)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp, "2026-05-11T23:00:00")

        import datetime
        claimed_epoch = int(datetime.datetime.fromisoformat("2026-05-11T23:00:00").timestamp())
        file_mtime = claimed_epoch + 300  # 5 min AFTER claim

        target_file = repo / "core" / "scripts" / "own-work.py"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("# own work\n")
        os.utime(target_file, (file_mtime, file_mtime))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        # File should NOT be filtered
        combined = result.stderr + result.stdout
        assert "filtered (cross-agent-uncommitted)" not in combined or "own-work.py" not in combined, \
            f"Own-work file got filtered. combined={combined!r}"
        # Should appear in "files to stage"
        assert "own-work.py" in combined, \
            f"Own-work file missing from staging plan. combined={combined!r}"


def test_include_untracked_override():
    """--include-untracked → filter disabled, even for predating files."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp, "2026-05-11T23:00:00")

        import datetime
        claimed_epoch = int(datetime.datetime.fromisoformat("2026-05-11T23:00:00").timestamp())
        file_mtime = claimed_epoch - 300  # would normally be filtered

        target_file = repo / "core" / "scripts" / "override-test.py"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("# override test\n")
        os.utime(target_file, (file_mtime, file_mtime))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run",
             "--include-untracked"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        # No filter messages
        assert "filtered (cross-agent-uncommitted)" not in combined, \
            f"Filter fired despite --include-untracked. combined={combined!r}"
        # File should be staged
        assert "override-test.py" in combined, \
            f"Override file missing from staging. combined={combined!r}"


def test_own_agent_dir_path_not_filtered_even_if_predating():
    """File under committer's own agent dir → NOT filtered (legitimate cross-
    iteration own work; namespace filter handles OTHER agents)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp, "2026-05-11T23:00:00")

        import datetime
        claimed_epoch = int(datetime.datetime.fromisoformat("2026-05-11T23:00:00").timestamp())
        file_mtime = claimed_epoch - 300  # 5 min BEFORE claim — would filter at neutral path

        # File under committer's own agent dir (alpha/) — must NOT be filtered
        target_file = repo / "agents" / "alpha" / "experience" / "own-prior-work.md"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("# own prior iteration work\n")
        os.utime(target_file, (file_mtime, file_mtime))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        # MUST NOT be filtered by cross-agent-untracked (own agent path)
        assert "filtered (cross-agent-uncommitted)" not in combined, \
            f"Filter incorrectly fired on own-agent-dir path. combined={combined!r}"
        # Git collapses new untracked dirs — check for parent path in staging
        assert "alpha/experience" in combined, \
            f"Own agent-dir file missing from staging plan. combined={combined!r}"


def test_no_claimed_at_disables_filter():
    """When team-state has no claimed_at, filter is inactive (fail-open)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp, None)  # null claimed_at

        # Old file that WOULD be filtered if claimed_at were set
        file_mtime = int(time.time()) - 10000
        target_file = repo / "core" / "scripts" / "no-claim-test.py"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("# no claim\n")
        os.utime(target_file, (file_mtime, file_mtime))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        # Filter must NOT fire (no anchor)
        assert "filtered (cross-agent-uncommitted)" not in combined, \
            f"Filter fired with no claimed_at. combined={combined!r}"
        # File should be staged (fail-open)
        assert "no-claim-test.py" in combined, \
            f"File missing from staging. combined={combined!r}"


# ---------------------------------------------------------------------------
# M-status extension (38fb983 incident — 3rd recurrence)
# ---------------------------------------------------------------------------
# Same shape as untracked tests above, but for tracked files that get modified.
# The modify-then-stale-mtime path is how concurrent-session asp-304 work
# (bravo) ends up swept into another agent's iteration-commit (alpha ).

def _seed_tracked_file(repo: Path, rel_path: str, content: str) -> Path:
    """Create + commit a file so it's TRACKED. Subsequent modifications
    appear as ' M' in git status --porcelain."""
    full = repo / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    subprocess.run(["git", "add", str(full)], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", f"seed {rel_path}"], cwd=repo, check=True)
    return full


def test_modified_file_predating_claimed_at_is_filtered():
    """Tracked file modified BEFORE claimed_at → filtered (partner WIP)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp, "2026-05-13T12:00:00")

        # Seed a tracked file (will be committed by _seed_tracked_file)
        target = _seed_tracked_file(repo, "core/scripts/partner-mod.py", "# v1\n")

        # Now modify it with an mtime 5 min BEFORE claimed_at
        import datetime
        claimed_epoch = int(datetime.datetime.fromisoformat("2026-05-13T12:00:00").timestamp())
        mtime = claimed_epoch - 300  # 5 min before claim

        target.write_text("# v2 — partner modified before alpha claimed\n")
        os.utime(target, (mtime, mtime))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (cross-agent-uncommitted)" in combined, \
            f"M-status filter did NOT catch the file. combined={combined!r}"
        assert "partner-mod.py" in combined, \
            f"Filtered file path missing in output. combined={combined!r}"


def test_modified_file_postdating_claimed_at_is_included():
    """Tracked file modified AFTER claimed_at → included (legitimate own work)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp, "2026-05-13T12:00:00")

        target = _seed_tracked_file(repo, "core/scripts/own-mod.py", "# v1\n")

        import datetime
        claimed_epoch = int(datetime.datetime.fromisoformat("2026-05-13T12:00:00").timestamp())
        mtime = claimed_epoch + 300  # 5 min AFTER claim

        target.write_text("# v2 — alpha's own edit after claiming\n")
        os.utime(target, (mtime, mtime))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        # Inspect each filter line individually — file MUST NOT appear in any
        # "filtered (cross-agent-uncommitted): <path>" line. Pattern mirrors
        # how the untracked-postdating test would behave if rewritten today.
        filter_lines = [L for L in combined.splitlines()
                        if "filtered (cross-agent-uncommitted):" in L]
        assert not any("own-mod.py" in L for L in filter_lines), \
            f"Own-work M file got filtered. filter_lines={filter_lines!r}"
        # Must appear in staging plan
        assert "own-mod.py" in combined, \
            f"Own-work M file missing from staging plan. combined={combined!r}"


def test_modified_own_agent_dir_not_filtered_even_if_predating():
    """Tracked file under committer's own agent dir, modified BEFORE claim
    → NOT filtered (namespace exception, matches untracked own-dir case)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp, "2026-05-13T12:00:00")

        # File under alpha/ (committer's own dir)
        target = _seed_tracked_file(repo, "alpha/journal.jsonl", "# v1\n")

        import datetime
        claimed_epoch = int(datetime.datetime.fromisoformat("2026-05-13T12:00:00").timestamp())
        mtime = claimed_epoch - 300  # would filter if at neutral path

        target.write_text("# v2 — own dir prior-iteration work\n")
        os.utime(target, (mtime, mtime))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        # Must not be filtered (own agent dir)
        assert "filtered (cross-agent-uncommitted)" not in combined, \
            f"M-status filter incorrectly fired on own-agent-dir path. combined={combined!r}"
        assert "alpha/journal.jsonl" in combined, \
            f"Own agent-dir M file missing from staging plan. combined={combined!r}"


# ---------------------------------------------------------------------------
# gitignore exclusion (4 — iteration-commit git-add abort investigation)
# ---------------------------------------------------------------------------
# A gitignored file under agents/<agent>/temp/ must NEVER reach staged_files[],
# so the explicit `git add -A -- "${staged_files[@]}"` (iteration-commit.sh
# ~L1164) cannot abort on it. Historical failure (bravo 0): git add
# errored `paths are ignored by one of your .gitignore files ... use -f` (rc!=0),
# which is NOT an index.lock error, so it fell through to `exit 2` and aborted
# the whole deep-close commit — leaving the loop's changes uncommitted, silent
# until a manual commit. Root cause: staged_files is built from
# `git status --porcelain` (iteration-commit.sh L492), and an untracked-ignored
# path explicitly named in the add is the only shape git refuses (empirically:
# `git add -A -- <untracked-ignored-file>` rc!=0, while `git add -A -- <dir>` and
# a tracked-then-ignored path are both rc=0). Structurally resolved by a9c487af
# (5: gitignore ALL of temp/ + untrack the previously-tracked
# temp/drained/*.json), because `git status --porcelain` (no --ignored) does not
# surface untracked-ignored paths. This test LOCKS that guarantee: if the status
# source ever gains --ignored, the temp scratch file would appear in the staging
# plan and this test fails.

def test_gitignored_temp_file_excluded_from_staging_g115_1834():
    """A gitignored agents/<agent>/temp/ scratch file is excluded from the
    staging plan (never reaches staged_files[]), while a legitimate own-dir
    change is still staged. Regression guard for the git-add-abort class."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp, None)  # null claimed_at → filter fail-open

        # .gitignore that ignores all agent temp/ (mirrors real repo, 5).
        (repo / ".gitignore").write_text("agents/*/temp/\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "gitignore agent temp"], cwd=repo, check=True)

        # An UNTRACKED, gitignored scratch file under alpha's temp/ — the
        # historical abort trigger. Present in the working tree, must be invisible
        # to iteration-commit.
        scratch = repo / "agents" / "alpha" / "temp" / "scratch.log"
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text("ephemeral scratch\n")
        ci = subprocess.run(
            ["git", "check-ignore", "-q", "agents/alpha/temp/scratch.log"], cwd=repo
        )
        assert ci.returncode == 0, "test setup: temp scratch is not actually gitignored"

        # A LEGITIMATE change under alpha's own dir that MUST be staged.
        legit = repo / "agents" / "alpha" / "journal.jsonl"
        legit.write_text('{"entry":1}\n')

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        # The gitignored scratch file is excluded by `git status --porcelain`,
        # so it never appears in iteration-commit's view at all.
        assert "scratch.log" not in combined, \
            f"gitignored temp scratch reached iteration-commit (would abort git add). combined={combined!r}"
        # The legitimate own-dir change IS staged.
        assert "journal.jsonl" in combined, \
            f"legitimate own-dir change missing from staging plan. combined={combined!r}"


if __name__ == "__main__":
    test_untracked_file_predating_claimed_at_is_filtered()
    test_untracked_file_postdating_claimed_at_is_included()
    test_include_untracked_override()
    test_own_agent_dir_path_not_filtered_even_if_predating()
    test_no_claimed_at_disables_filter()
    test_modified_file_predating_claimed_at_is_filtered()
    test_modified_file_postdating_claimed_at_is_included()
    test_modified_own_agent_dir_not_filtered_even_if_predating()
    test_gitignored_temp_file_excluded_from_staging_g115_1834()
    print("All 9 tests passed.")
