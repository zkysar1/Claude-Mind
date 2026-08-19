"""Delivery half of the uncommitted-work gate ().

WHY THESE ARE BUILT ON THE FAILING SIDE
    The pre-fix gate had no upstream comparison at all, so any assertion about
    a CLEAN repo passing would have passed against the defect — vacuous by
    construction. Every assertion below turns on a repo whose tree is clean and
    whose HEAD is ahead of upstream: the exact state the old gate called fine
    and the originating incident (g-306-261, a fix stranded ~15h after a
    non-fast-forward push rejection) actually occupied.

WHY THE ROLE SPLIT IS TESTED IN BOTH DIRECTIONS
    "Reducer is blocked" alone is satisfiable by a gate that blocks everyone,
    which would refuse every legitimate worker close in the fleet — a worker
    commits locally and does not push BY CONTRACT (g-306-233). The
    worker-permitted case is therefore not a nicety; it is the half that proves
    the check discriminates on role rather than on the presence of unpushed
    commits.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "gates"))

from gates.uncommitted_work import (  # noqa: E402
    evaluate,
    get_undelivered_framework_files,
)


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r.stdout.strip()


@pytest.fixture
def repo_ahead(tmp_path):
    """A clone whose tree is CLEAN and whose HEAD is 1 framework commit ahead.

    Built with a real origin rather than a mocked one: `@{u}` resolution is the
    thing under test, and a fake would let the code pass while the real ref
    spelling was wrong.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)

    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")

    (work / "core" / "scripts").mkdir(parents=True)
    (work / "core" / "scripts" / "base.py").write_text("x = 1\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    _git(work, "push", "-q", "origin", "HEAD")
    _git(work, "branch", "--set-upstream-to", "origin/" + _git(work, "rev-parse", "--abbrev-ref", "HEAD"))

    # The stranded commit: framework code, committed, never pushed.
    (work / "core" / "scripts" / "shipped.py").write_text("y = 2\n", encoding="utf-8")
    (work / "notes.txt").write_text("not framework code\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "the fix nobody received")
    assert _git(work, "status", "--porcelain") == "", "tree must be CLEAN"
    return work


def _ev(repo, role, override=None):
    return evaluate(goal_id="g-test-1", override=override, repo_path=repo,
                    world_dir=None, agent_name="tester", body_role=role)


def test_undelivered_framework_file_is_detected_on_a_clean_tree(repo_ahead):
    """THE PRIMARY TARGET. Clean tree + unpushed framework commit."""
    files = get_undelivered_framework_files(repo_ahead)
    assert files == ["core/scripts/shipped.py"]


def test_non_framework_paths_are_not_reported(repo_ahead):
    """notes.txt rode in the same unpushed commit and must not appear —
    otherwise every agent-state churn commit would block a reducer close."""
    assert "notes.txt" not in get_undelivered_framework_files(repo_ahead)


def test_reducer_close_is_blocked_by_undelivered_work(repo_ahead):
    p = _ev(repo_ahead, "reducer")
    assert p["would_block"] is True
    assert p["delivery_would_block"] is True
    assert p["undelivered_framework_files"] == ["core/scripts/shipped.py"]
    assert p["dirty_framework_files"] == [], (
        "the tree IS clean — the commit half must not claim credit for this block")


def test_worker_close_is_NOT_blocked_by_undelivered_work(repo_ahead):
    """The contract half: a worker does not push, so it must still close."""
    p = _ev(repo_ahead, "worker")
    assert p["would_block"] is False
    assert p["delivery_would_block"] is False
    assert p["undelivered_framework_files"] == ["core/scripts/shipped.py"], (
        "still REPORTED — the reducer consuming the carrier ref needs to know")


def test_unknown_role_reports_but_does_not_block(repo_ahead):
    """BODY_ROLE may be absent on the daemon path. Blocking on unknown would
    make the verdict depend on which call path the caller took."""
    p = _ev(repo_ahead, None)
    assert p["would_block"] is False
    assert p["body_role"] is None
    assert p["undelivered_framework_files"] == ["core/scripts/shipped.py"]


def test_override_bypasses_a_delivery_block(repo_ahead):
    p = _ev(repo_ahead, "reducer", override="deliberate: partner holds the push")
    assert p["would_block"] is False
    assert p["override_applied"] == "deliberate: partner holds the push"


def test_fully_pushed_reducer_close_is_clean(repo_ahead):
    """Negative control. Without it, a gate that blocks reducers
    unconditionally would pass every other test in this file."""
    _git(repo_ahead, "push", "-q", "origin", "HEAD")
    p = _ev(repo_ahead, "reducer")
    assert p["undelivered_framework_files"] == []
    assert p["would_block"] is False


def test_no_upstream_fails_open(tmp_path):
    """A branch with no upstream is not evidence of undelivered work."""
    solo = tmp_path / "solo"
    solo.mkdir()
    subprocess.run(["git", "init", "-q", str(solo)], check=True)
    _git(solo, "config", "user.email", "t@example.com")
    _git(solo, "config", "user.name", "t")
    (solo / "core").mkdir()
    (solo / "core" / "x.py").write_text("z = 3\n", encoding="utf-8")
    _git(solo, "add", "-A")
    _git(solo, "commit", "-qm", "solo")
    assert get_undelivered_framework_files(solo) == []
    assert _ev(solo, "reducer")["would_block"] is False


def test_upstream_ahead_does_not_attribute_partner_files_to_this_box(repo_ahead, tmp_path):
    """Three-dot vs two-dot. A live fleet pushes while you work, which is
    exactly the state a rejected push leaves behind; two-dot would report the
    PARTNER's incoming framework files as though this box owed them."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q",
                    _git(repo_ahead, "remote", "get-url", "origin"), str(other)],
                   check=True)
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "o")
    (other / "core" / "scripts" / "partner.py").write_text("p = 9\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "partner work")
    _git(other, "push", "-q", "origin", "HEAD")

    _git(repo_ahead, "fetch", "-q", "origin")
    files = get_undelivered_framework_files(repo_ahead)
    assert "core/scripts/partner.py" not in files, (
        "partner's pushed file is not this box's undelivered work")
    assert files == ["core/scripts/shipped.py"]
