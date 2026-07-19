"""Tests for promotion-content-diff.sh — exhaustive deterministic framework diff.

Builds two tiny temp git repos with controlled file states (identical,
source-only, target-only, target-ahead, source-ahead) and validates the
script's --json output classifies each correctly and returns the expected
exit code.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT = SCRIPT_DIR.parent / "promotion-content-diff.sh"

# Use the canonical bash resolution helper (prefers Git\bin\bash.exe login
# launcher on Windows — the raw usr\bin\bash.exe fails to resolve /c/ paths).
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402


def _posix(p: Path) -> str:
    """Convert a Path to a POSIX-style string suitable for bash on Windows."""
    s = str(p)
    # Convert Windows drive letter paths: C:\foo -> /c/foo
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s.replace("\\", "/")


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    """Run a git command in the given repo."""
    merged_env = {**os.environ}
    if env:
        merged_env.update(env)
    r = subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=merged_env,
        timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}:\n{r.stderr}")
    return r.stdout.strip()


def _init_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    # Initial commit so git log works
    (path / ".gitkeep").write_text("")
    _git(path, "add", ".gitkeep")
    _git(path, "commit", "-m", "init")


def _write_and_commit(
    repo: Path,
    rel_path: str,
    content: str,
    msg: str,
    commit_date: str | None = None,
) -> None:
    """Write a file and commit it, optionally with a fixed date."""
    full = repo / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    _git(repo, "add", rel_path)
    env: dict[str, str] = {}
    if commit_date:
        env["GIT_COMMITTER_DATE"] = commit_date
        env["GIT_AUTHOR_DATE"] = commit_date
    _git(repo, "commit", "-m", msg, env=env)


def _run_diff(source: Path, target: Path, extra_args: list[str] | None = None) -> tuple[int, dict]:
    """Run promotion-content-diff.sh --json and return (exit_code, parsed_json)."""
    cmd = [BASH, _posix(SCRIPT), "--source", _posix(source), "--target", _posix(target), "--json"]
    if extra_args:
        cmd.extend(extra_args)
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # JSON goes to stdout, human report to stderr when --json
    stdout = r.stdout.strip()
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        pytest.fail(
            f"Failed to parse JSON from stdout.\n"
            f"Exit code: {r.returncode}\n"
            f"stdout: {stdout}\n"
            f"stderr: {r.stderr}"
        )
    return r.returncode, data


@pytest.fixture
def repos(tmp_path: Path):
    """Create source and target repos with controlled framework files."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    _init_repo(source)
    _init_repo(target)

    # ── 1. IDENTICAL file ──
    # Same content, committed at the same time in both
    _write_and_commit(
        source, "core/scripts/identical.py",
        "# identical content\nprint('hello')\n",
        "add identical file",
        commit_date="2025-01-01T12:00:00+00:00",
    )
    _write_and_commit(
        target, "core/scripts/identical.py",
        "# identical content\nprint('hello')\n",
        "add identical file",
        commit_date="2025-01-01T12:00:00+00:00",
    )

    # ── 2. SOURCE-ONLY file ──
    # Exists only in source
    _write_and_commit(
        source, "core/scripts/source-only.py",
        "# only in source\n",
        "add source-only file",
        commit_date="2025-06-01T10:00:00+00:00",
    )

    # ── 3. TARGET-ONLY file ──
    # Exists only in target
    _write_and_commit(
        target, "core/scripts/target-only.py",
        "# only in target\n",
        "add target-only file",
        commit_date="2025-06-01T10:00:00+00:00",
    )

    # ── 4. TARGET-AHEAD file ──
    # Both have it, but target committed later
    _write_and_commit(
        source, "core/config/target-ahead.yaml",
        "version: 1\n",
        "add target-ahead file (old)",
        commit_date="2025-01-15T08:00:00+00:00",
    )
    _write_and_commit(
        target, "core/config/target-ahead.yaml",
        "version: 2\n# target evolved this\n",
        "add target-ahead file (new)",
        commit_date="2025-07-01T14:00:00+00:00",
    )

    # ── 5. SOURCE-AHEAD file ──
    # Both have it, but source committed later
    _write_and_commit(
        target, "core/scripts/source-ahead.sh",
        "#!/bin/bash\necho old\n",
        "add source-ahead file (old)",
        commit_date="2025-02-01T06:00:00+00:00",
    )
    _write_and_commit(
        source, "core/scripts/source-ahead.sh",
        "#!/bin/bash\necho new_version\n",
        "add source-ahead file (new)",
        commit_date="2025-08-01T18:00:00+00:00",
    )

    # ── 6. Excluded file (__pycache__) — should NOT appear ──
    _write_and_commit(
        source, "core/scripts/__pycache__/mod.cpython-312.pyc",
        "bytecode",
        "add pycache",
    )
    _write_and_commit(
        target, "core/scripts/__pycache__/mod.cpython-312.pyc",
        "bytecode different",
        "add pycache",
    )

    return source, target


class TestBasicClassification:
    """Test that files are classified into the correct buckets."""

    def test_identical_file(self, repos):
        source, target = repos
        rc, data = _run_diff(source, target)
        assert "core/scripts/identical.py" not in data["target_ahead"]
        assert "core/scripts/identical.py" not in data["source_ahead"]
        assert "core/scripts/identical.py" not in data["target_only"]
        assert "core/scripts/identical.py" not in data["source_only"]
        assert "core/scripts/identical.py" not in data["ambiguous"]
        assert data["counts"]["identical"] >= 1

    def test_source_only(self, repos):
        source, target = repos
        _, data = _run_diff(source, target)
        assert "core/scripts/source-only.py" in data["source_only"]

    def test_target_only(self, repos):
        source, target = repos
        _, data = _run_diff(source, target)
        assert "core/scripts/target-only.py" in data["target_only"]

    def test_target_ahead(self, repos):
        source, target = repos
        _, data = _run_diff(source, target)
        assert "core/config/target-ahead.yaml" in data["target_ahead"]

    def test_source_ahead(self, repos):
        source, target = repos
        _, data = _run_diff(source, target)
        assert "core/scripts/source-ahead.sh" in data["source_ahead"]

    def test_excluded_pycache_absent(self, repos):
        source, target = repos
        _, data = _run_diff(source, target)
        all_listed = (
            data["target_ahead"]
            + data["source_ahead"]
            + data["target_only"]
            + data["source_only"]
            + data["ambiguous"]
        )
        pycache_files = [f for f in all_listed if "__pycache__" in f or ".pyc" in f]
        assert pycache_files == [], f"pycache files should be excluded: {pycache_files}"


class TestExitCodes:
    """Test exit code behavior."""

    def test_exits_nonzero_on_target_ahead_core(self, repos):
        """Target-ahead core file (core/config/target-ahead.yaml) should cause exit 2."""
        source, target = repos
        rc, data = _run_diff(source, target)
        assert rc == 2, f"Expected exit 2 for target-ahead core file, got {rc}"

    def test_exits_nonzero_on_target_only_core(self, repos):
        """Target-only core file should cause exit 2."""
        source, target = repos
        rc, data = _run_diff(source, target)
        assert rc == 2

    def test_exits_zero_when_clean(self, tmp_path):
        """Two identical repos should produce exit 0."""
        source = tmp_path / "clean_source"
        target = tmp_path / "clean_target"
        _init_repo(source)
        _init_repo(target)
        # Same file in both
        _write_and_commit(source, "core/scripts/a.py", "same\n", "add a")
        _write_and_commit(target, "core/scripts/a.py", "same\n", "add a")
        rc, data = _run_diff(source, target)
        assert rc == 0, f"Expected exit 0 for clean repos, got {rc}. data={data}"

    def test_strict_exits_nonzero_on_source_ahead(self, tmp_path):
        """--strict should exit 2 even for source-ahead (any diff blocks)."""
        source = tmp_path / "strict_src"
        target = tmp_path / "strict_tgt"
        _init_repo(source)
        _init_repo(target)
        # source-ahead only (no target-ahead)
        _write_and_commit(
            target, "core/scripts/x.py", "old\n", "old",
            commit_date="2025-01-01T00:00:00+00:00",
        )
        _write_and_commit(
            source, "core/scripts/x.py", "new\n", "new",
            commit_date="2025-12-01T00:00:00+00:00",
        )
        # Without --strict: should be clean (only source-ahead, no target-ahead)
        rc, data = _run_diff(source, target)
        assert rc == 0, f"Without --strict, source-ahead only should be clean. data={data}"
        # With --strict: should block
        rc, data = _run_diff(source, target, extra_args=["--strict"])
        assert rc == 2, f"With --strict, source-ahead should cause exit 2. data={data}"


class TestCounts:
    """Test that counts are consistent."""

    def test_total_equals_sum(self, repos):
        source, target = repos
        _, data = _run_diff(source, target)
        c = data["counts"]
        listed_sum = (
            len(data["target_ahead"])
            + len(data["source_ahead"])
            + len(data["target_only"])
            + len(data["source_only"])
            + len(data["ambiguous"])
            + c["identical"]
        )
        assert listed_sum == c["total"], (
            f"Sum of buckets ({listed_sum}) != total ({c['total']})"
        )


class TestJsonSchema:
    """Test that the JSON output has the expected shape."""

    def test_required_keys(self, repos):
        source, target = repos
        _, data = _run_diff(source, target)
        required = {
            "source_root", "target_root", "source_head", "target_head",
            "counts", "target_ahead", "source_ahead", "target_only",
            "source_only", "ambiguous", "exit_code",
        }
        assert required.issubset(set(data.keys())), (
            f"Missing keys: {required - set(data.keys())}"
        )

    def test_counts_keys(self, repos):
        source, target = repos
        _, data = _run_diff(source, target)
        count_keys = {"identical", "source_only", "target_only", "source_ahead", "target_ahead", "ambiguous", "total"}
        assert count_keys.issubset(set(data["counts"].keys())), (
            f"Missing count keys: {count_keys - set(data['counts'].keys())}"
        )


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_repos(self, tmp_path):
        """Two repos with no framework files should produce exit 0."""
        source = tmp_path / "empty_src"
        target = tmp_path / "empty_tgt"
        _init_repo(source)
        _init_repo(target)
        rc, data = _run_diff(source, target)
        assert rc == 0
        assert data["counts"]["total"] == 0

    def test_same_dir_rejected(self, tmp_path):
        """--source == --target should produce exit 1."""
        repo = tmp_path / "single"
        _init_repo(repo)
        cmd = [BASH, _posix(SCRIPT), "--source", _posix(repo), "--target", _posix(repo), "--json"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        assert r.returncode == 1

    def test_missing_args(self):
        """Missing required args should produce exit 1."""
        r = subprocess.run(
            [BASH, _posix(SCRIPT)],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 1


if __name__ == "__main__":
    import pytest
    # Script-mode entry (run-invisible-suites.sh executes `python3 <file>`).
    # Without this block the file defined its test classes and exited 0 having
    # run NOTHING — a vacuous pass in every invisible-suite sweep (2).
    # SystemExit propagates pytest's exit code (mirrors
    # test_recurring_close_outcome_origin).
    raise SystemExit(pytest.main([__file__, "-v"]))
