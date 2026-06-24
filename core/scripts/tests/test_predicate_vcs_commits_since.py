"""test_predicate_vcs_commits_since.py — unit tests for the vcs_commits_since
structured-precondition predicate (g-115-1383).

The predicate passes when a target git repo has >= min_count commits committed
strictly after a cutoff timestamp. The cutoff comes from `after_ref`
(git:/iso:/file:) OR `since_goal_last_achieved` (a goal_id whose lastAchievedAt
is the cutoff — the event-gate form used to fire recurring review goals only on
new commits).

These are pure in-process tests (no daemon): they git-init a temp repo with
commits at controlled committer dates and assert the count logic, plus the
strict-no-grace boundary (the streak-contraction-artifact fix) and fail-closed
paths.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import predicate  # noqa: E402


# --------------------------------------------------------------------------
# Temp-repo helpers
# --------------------------------------------------------------------------

def _git(repo: Path, *args: str, date_iso: str | None = None) -> str:
    env = os.environ.copy()
    if date_iso is not None:
        env["GIT_AUTHOR_DATE"] = date_iso
        env["GIT_COMMITTER_DATE"] = date_iso
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=env, timeout=20,
    )
    assert out.returncode == 0, f"git {args} failed: {out.stderr}"
    return out.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    return path


def _commit(repo: Path, rel: str, content: str, date_iso: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", f"commit {rel} @ {date_iso}", date_iso=date_iso)


def _eval(p: dict):
    return predicate.evaluate(p)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_commits_after_iso_cutoff_pass(tmp_path):
    repo = _init_repo(tmp_path / "r")
    _commit(repo, "code/a.py", "x=1", "2020-01-01T12:00:00")
    _commit(repo, "code/b.py", "y=2", "2026-06-01T12:00:00")
    r = _eval({"type": "vcs_commits_since", "id": "pc",
               "repo": str(repo), "after_ref": "iso:2023-01-01T00:00:00"})
    assert r.passed is True
    assert r.observed_value["commits_since"] == 1  # only the 2026 commit


def test_no_commits_after_future_cutoff_fail(tmp_path):
    repo = _init_repo(tmp_path / "r")
    _commit(repo, "code/a.py", "x=1", "2020-01-01T12:00:00")
    _commit(repo, "code/b.py", "y=2", "2026-06-01T12:00:00")
    r = _eval({"type": "vcs_commits_since", "id": "pc",
               "repo": str(repo), "after_ref": "iso:2027-01-01T00:00:00"})
    assert r.passed is False
    assert r.observed_value["commits_since"] == 0


def test_min_count_threshold(tmp_path):
    repo = _init_repo(tmp_path / "r")
    _commit(repo, "a.py", "1", "2026-06-01T12:00:00")
    _commit(repo, "b.py", "2", "2026-06-02T12:00:00")
    base = {"type": "vcs_commits_since", "id": "pc",
            "repo": str(repo), "after_ref": "iso:2026-01-01T00:00:00"}
    assert _eval({**base, "min_count": 2}).passed is True
    assert _eval({**base, "min_count": 3}).passed is False


def test_paths_pathspec_excludes_state_churn(tmp_path):
    repo = _init_repo(tmp_path / "r")
    # One code commit, one agent-state commit, both after cutoff.
    _commit(repo, "core/scripts/x.py", "code", "2026-06-01T12:00:00")
    _commit(repo, "agents/alpha/journal.jsonl", "state", "2026-06-02T12:00:00")
    cutoff = "iso:2026-01-01T00:00:00"
    # Scoped to code paths → only the code commit counts.
    r_code = _eval({"type": "vcs_commits_since", "id": "pc", "repo": str(repo),
                    "after_ref": cutoff, "paths": ["core/scripts"]})
    assert r_code.passed is True
    assert r_code.observed_value["commits_since"] == 1
    # Scoped to agents/ → only the state commit counts (proves filter works).
    r_state = _eval({"type": "vcs_commits_since", "id": "pc", "repo": str(repo),
                     "after_ref": cutoff, "paths": ["agents"]})
    assert r_state.observed_value["commits_since"] == 1


def test_strict_no_grace_excludes_commit_at_cutoff(tmp_path):
    """The event-gate fix: a commit at EXACTLY the cutoff (the triggering
    commit, after lastAchievedAt advances) must NOT re-count. Strict > , no
    clock-skew grace. This is the streak-contraction-artifact fix."""
    repo = _init_repo(tmp_path / "r")
    _commit(repo, "a.py", "1", "2026-06-01T12:00:00")
    # Read back the exact committer-ISO git recorded, use it AS the cutoff.
    exact = _git(repo, "log", "-1", "--format=%cI")
    r = _eval({"type": "vcs_commits_since", "id": "pc",
               "repo": str(repo), "after_ref": f"iso:{exact}"})
    assert r.passed is False  # commit_date == cutoff → strict > excludes it
    assert r.observed_value["commits_since"] == 0


def test_non_git_repo_fail_closed(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    r = _eval({"type": "vcs_commits_since", "id": "pc",
               "repo": str(plain), "after_ref": "iso:2020-01-01T00:00:00"})
    assert r.passed is False
    assert "rc=" in r.reason


def test_missing_cutoff_source_fail(tmp_path):
    repo = _init_repo(tmp_path / "r")
    r = _eval({"type": "vcs_commits_since", "id": "pc", "repo": str(repo)})
    assert r.passed is False
    assert "since_goal_last_achieved or after_ref" in r.reason


def test_since_goal_last_achieved_uses_goal_timestamp(tmp_path, monkeypatch):
    """Event-gate form: cutoff resolved from a goal's lastAchievedAt. Commit
    after lastAchievedAt → pass; bump lastAchievedAt past the commit → fail."""
    repo = _init_repo(tmp_path / "r")
    _commit(repo, "a.py", "1", "2026-06-01T12:00:00")

    # Goal last ran BEFORE the commit → there is a new commit → pass.
    monkeypatch.setattr(predicate, "_lookup_goal_record",
                        lambda gid: {"lastAchievedAt": "2026-05-01T00:00:00"})
    r1 = _eval({"type": "vcs_commits_since", "id": "pc", "repo": str(repo),
                "since_goal_last_achieved": "g-test-12"})
    assert r1.passed is True

    # Goal last ran AFTER the commit → no new commit → fail (the post-run state).
    monkeypatch.setattr(predicate, "_lookup_goal_record",
                        lambda gid: {"lastAchievedAt": "2026-07-01T00:00:00"})
    r2 = _eval({"type": "vcs_commits_since", "id": "pc", "repo": str(repo),
                "since_goal_last_achieved": "g-test-12"})
    assert r2.passed is False


def test_since_goal_not_found_fail(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "r")
    monkeypatch.setattr(predicate, "_lookup_goal_record", lambda gid: None)
    r = _eval({"type": "vcs_commits_since", "id": "pc", "repo": str(repo),
               "since_goal_last_achieved": "g-missing"})
    assert r.passed is False
    assert "not found" in r.reason


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
