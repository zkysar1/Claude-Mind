"""Controls for core/scripts/branch-stall-probe.sh ().

The probe is REPORT-ONLY observability for two git states `git status` calls
healthy. Its whole value is that it fires when it should and stays quiet when it
should, so both directions are pinned here rather than recorded once in prose —
a prose control goes stale and nothing re-checks it.

Every fixture is a throwaway git repo in pytest's tmp_path: no Mind store is
read or written, so these tests are storage-backend agnostic.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (needs the path insert above)

REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE = REPO_ROOT / "core" / "scripts" / "branch-stall-probe.sh"


def _git(repo, *args, when=None):
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "probe-test", "GIT_AUTHOR_EMAIL": "probe@test",
        "GIT_COMMITTER_NAME": "probe-test", "GIT_COMMITTER_EMAIL": "probe@test",
    })
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    r = subprocess.run(["git", "-C", str(repo), *args], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"fixture git {args} failed: {r.stderr}"
    return r


def _run(repo, *extra):
    return subprocess.run([BASH, PROBE.as_posix(), "--repo", Path(repo).as_posix(), *extra],
                          capture_output=True, text=True)


def _commit(repo, name, when=None):
    (repo / name).write_text(name)
    _git(repo, "add", "-A", when=when)
    _git(repo, "commit", "-m", name, when=when)


@pytest.fixture
def synced(tmp_path):
    """A branch with an origin ref and nothing unpushed."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _commit(work, "a.txt")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return work


# ── POSITIVE CONTROL (a): detached HEAD ────────────────────────────────────
def test_detached_head_emits(synced):
    _commit(synced, "b.txt")
    _commit(synced, "c.txt")
    sha = _git(synced, "rev-parse", "HEAD").stdout.strip()
    # Detach FIRST: git refuses to force-update the branch that is checked
    # out, so moving main while on it silently no-ops and strands nothing.
    _git(synced, "checkout", "-q", "--detach", sha)
    _git(synced, "branch", "-f", "main", f"{sha}~2")   # strand b+c off every branch

    r = _run(synced)
    assert r.returncode == 0, "probe must never exit non-zero (report-only)"
    assert "DETACHED HEAD" in r.stdout, f"positive control did not fire: {r.stdout!r}"
    assert "2 commit(s) sit off every branch" in r.stdout

    j = json.loads(_run(synced, "--json").stdout)
    assert j["detached"] is True
    assert j["stranded_commits"] == 2
    assert j["branch_status"] == "not-applicable"


# ── POSITIVE CONTROL (b): stale unpushed commits under the depth alarm ─────
def test_stalled_branch_emits(synced):
    # ONE commit, 5h old: far under iteration-push's 25-commit stranded-depth
    # alarm, which is exactly the gap this condition exists to cover.
    _commit(synced, "old.txt", when="2005-04-07T22:13:13 +0000")

    r = _run(synced, "--max-age-min", "180")
    assert r.returncode == 0
    assert "BRANCH STALL" in r.stdout, f"positive control did not fire: {r.stdout!r}"

    j = json.loads(_run(synced, "--json", "--max-age-min", "180").stdout)
    assert j["branch_status"] == "stalled"
    assert j["ahead"] == 1
    assert j["oldest_unpushed_age_min"] > 180


# ── NEGATIVE CONTROLS ──────────────────────────────────────────────────────
def test_in_sync_branch_is_quiet(synced):
    r = _run(synced)
    assert r.returncode == 0
    assert r.stdout == "", f"must be quiet on the clean case, got {r.stdout!r}"


def test_fresh_unpushed_commits_are_quiet(synced):
    """Ahead of origin but recent -> NOT a stall. Pins the threshold branch, so
    the quiet above can never be quiet-because-it-never-fires."""
    _commit(synced, "fresh.txt")
    r = _run(synced, "--max-age-min", "180")
    assert r.stdout == "", f"recent unpushed work must not alarm, got {r.stdout!r}"
    j = json.loads(_run(synced, "--json", "--max-age-min", "180").stdout)
    assert j["ahead"] == 1 and j["branch_status"] == "clean"


def test_non_repo_is_quiet_and_zero(tmp_path):
    r = _run(tmp_path / "not-a-repo")
    assert r.returncode == 0
    assert r.stdout == ""


def test_missing_upstream_reports_unmeasurable_not_clean(tmp_path):
    """An unmeasurable lane must never read as an empty one (guard-4093)."""
    work = tmp_path / "solo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _commit(work, "a.txt")
    j = json.loads(_run(work, "--json").stdout)
    assert j["branch_status"] == "unmeasurable"


# ── CONTRACT: report-only ──────────────────────────────────────────────────
def test_probe_mutates_no_state():
    """The report-only contract, asserted against the source rather than trusted.

    Positive control on the parser: the script must be non-trivial, or an empty
    read would satisfy every 'not in' below (guard-2298)."""
    src = PROBE.read_text()
    assert len(src) > 2000, "positive control: probe source not actually read"
    for forbidden in ("session-signal-set", "defer_reason", "aspirations-update-goal",
                      "stop-requested", "wm-append", "wm-set"):
        assert forbidden not in src, f"report-only probe must not touch {forbidden}"
    assert "exit 1" not in src, "probe must never exit non-zero on a detection"
