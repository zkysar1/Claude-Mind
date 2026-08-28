""" — the plan verdict auto-excuses flags when dest is provably frozen.

A "prod-ahead" flag means dest carries lines the transformed seed lacks. That
single observation has several causes and only one of them should stop a
promotion. When dest HEAD *is* the last promote-PR merge and the tree is clean,
there have been zero commits since the plant, so no dest-only line CAN be
locally authored — every flag is seed-forward-motion. Measured at v2.8.10:
18/18 flags at the staging hop and 88/108 at the prod hop were exactly this.

TWO DIRECTIONS, AND THE SECOND SET IS THE LOAD-BEARING ONE. Excusing is
suppression, and a suppression gate that over-fires silently converts a safety
refusal into a rubber stamp — the failure mode is a promotion that overwrites
real downstream work. So the frozen case is pinned by ONE test and the
must-NOT-excuse cases by FOUR, including the guard-487 fail-closed pin: when the
gate's own input cannot be read, it must not suppress.

Fixture shape and the engine invocation deliberately mirror
test_seed_plan_verdict_exit_code.py (guard-920: replicate the production arg
shape). Exit vocabulary SSOT is the `plan` dispatch comment in _seed_engine.py:
0 = SAFE, 20 = REVIEW REQUIRED, 21 = DO NOT PROMOTE.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

ENGINE_PATH = CORE_SCRIPTS / "_seed_engine.py"

MANIFEST_YAML = """\
include:
  - path: core/base.py
    type: file
transformations: []
"""

SRC_BASE = "BASE = 'x'\n"
# Dest carries a line the seed lacks -> prod-ahead -> flagged.
DEST_BASE_PROD_AHEAD = "BASE = 'x'\nDOWNSTREAM_ONLY = 'tuned here'\n"

PLANT_SUBJECT = "Merge pull request #42 from ayoai/promote/v2.8.10"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def _fixture(tmp_path: Path, *, git_dest: bool, head_subject: str | None,
             dirty: bool):
    src = tmp_path / "src"
    (src / "core").mkdir(parents=True)
    (src / "core" / "base.py").write_text(SRC_BASE, encoding="utf-8")

    dest = tmp_path / "dest"
    (dest / "core").mkdir(parents=True)
    (dest / "core" / "base.py").write_text(DEST_BASE_PROD_AHEAD, encoding="utf-8")

    if git_dest:
        _git(dest, "init", "-q")
        _git(dest, "config", "user.email", "t@t.t")
        _git(dest, "config", "user.name", "t")
        _git(dest, "add", "-A")
        _git(dest, "commit", "-q", "-m", head_subject or "chore: something")
        if dirty:
            (dest / "core" / "base.py").write_text(
                DEST_BASE_PROD_AHEAD + "LOCAL_EDIT = 1\n", encoding="utf-8")

    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(MANIFEST_YAML, encoding="utf-8")
    return src, dest, manifest


def _run_plan(src: Path, dest: Path, manifest: Path):
    return subprocess.run(
        [sys.executable, ENGINE_PATH.as_posix(), "plan",
         "--manifest", manifest.as_posix(),
         "--source", src.as_posix(),
         "--dest", dest.as_posix()],
        capture_output=True, text=True,
    )


@pytest.fixture(autouse=True)
def _require_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except Exception:                                    # pragma: no cover
        pytest.skip("git unavailable")


# ── the excusal fires ────────────────────────────────────────────────────────

def test_frozen_dest_excuses_the_flag_and_clears_the_refusal(tmp_path):
    """THE discriminating test — the only one here that fails against the
    pre-g-115-4389 engine, which refused unconditionally on any prod-ahead."""
    src, dest, manifest = _fixture(
        tmp_path, git_dest=True, head_subject=PLANT_SUBJECT, dirty=False)
    r = _run_plan(src, dest, manifest)
    assert "AUTO-EXCUSED" in r.stdout, (
        "frozen dest did not auto-excuse; the exit assertion below would be "
        "vacuous. stdout:\n" + r.stdout[:3000])
    assert "SEED-MOTION" in r.stdout, "excused flags must be re-labelled, not dropped"
    assert "core/base.py" in r.stdout, (
        "an excused file must still be NAMED — a detector that goes quiet is "
        "indistinguishable from one that was fixed (guard-2499)")
    assert r.returncode == 0, (
        "every flag was excused, so the verdict must not be DO NOT PROMOTE. "
        "got rc=%d\n%s" % (r.returncode, r.stdout[:3000]))


# ── the excusal must NOT fire (four ways) ────────────────────────────────────

def test_dirty_tree_does_not_excuse(tmp_path):
    """A clean tree is half the proof. An uncommitted local edit is exactly the
    prod authorship the flag exists to catch."""
    src, dest, manifest = _fixture(
        tmp_path, git_dest=True, head_subject=PLANT_SUBJECT, dirty=True)
    r = _run_plan(src, dest, manifest)
    assert "AUTO-EXCUSED" not in r.stdout, (
        "a dirty dest was excused — this is the direction that overwrites real "
        "downstream work. stdout:\n" + r.stdout[:3000])
    assert r.returncode == 21


def test_head_not_a_plant_does_not_excuse(tmp_path):
    """Clean tree, but the last commit is local authorship, not a plant."""
    src, dest, manifest = _fixture(
        tmp_path, git_dest=True,
        head_subject="fix: omni tuned the retry loop", dirty=False)
    r = _run_plan(src, dest, manifest)
    assert "AUTO-EXCUSED" not in r.stdout, (
        "a locally-authored HEAD was excused. stdout:\n" + r.stdout[:3000])
    assert r.returncode == 21


def test_non_git_dest_fails_closed(tmp_path):
    """guard-487: a suppression gate whose input cannot be read MUST NOT
    suppress. No git metadata means the proof is unavailable, not satisfied."""
    src, dest, manifest = _fixture(
        tmp_path, git_dest=False, head_subject=None, dirty=False)
    r = _run_plan(src, dest, manifest)
    assert "AUTO-EXCUSED" not in r.stdout, (
        "a dest with no git history was excused — the gate failed OPEN, which "
        "is the guard-487 defect. stdout:\n" + r.stdout[:3000])
    assert "auto-excusal unavailable" in r.stdout, (
        "the fail-closed path must SAY it could not classify, or the operator "
        "reads a full flag block as if the check had cleared it")
    assert r.returncode == 21


def test_empty_git_repo_fails_closed(tmp_path):
    """A repo with no commits: `git log -1` exits non-zero. Distinct code path
    from the non-git case above (git present, command fails)."""
    src, dest, manifest = _fixture(
        tmp_path, git_dest=False, head_subject=None, dirty=False)
    _git(dest, "init", "-q")
    r = _run_plan(src, dest, manifest)
    assert "AUTO-EXCUSED" not in r.stdout, (
        "an empty repo was excused. stdout:\n" + r.stdout[:3000])
    assert r.returncode == 21


def test_plan_stays_read_only_through_the_excusal(tmp_path):
    """The excusal added two git reads. It must not have added a write."""
    src, dest, manifest = _fixture(
        tmp_path, git_dest=True, head_subject=PLANT_SUBJECT, dirty=False)
    before = (dest / "core" / "base.py").read_text(encoding="utf-8")
    _run_plan(src, dest, manifest)
    assert (dest / "core" / "base.py").read_text(encoding="utf-8") == before
    assert not list(dest.glob(".seed-backup-*"))
