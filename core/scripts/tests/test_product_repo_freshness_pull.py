"""Tests for product-repo-freshness.py --pull ().

Every OTHER mode in this script reports. This one MUTATES working trees, and
that difference sets what these tests have to assert. `sig-227`
(accepted-argument-with-no-downstream-effect) names the failure a
rc==0/parses-clean test suite cannot see: a flag that is accepted, prints a
plausible banner, and moves nothing. So every test here asserts the HEAD sha
before and after — the effect, not the report.

The skip ladder gets the heavier coverage, deliberately. Advancing a tree that
should have been left alone is the irreversible direction (it can consume a
partner's in-flight work or a live goal's feature branch), while failing to
advance one merely leaves the status quo this goal already measured. Three of
the skips encode contracts documented elsewhere in the module that a naive
reading INVERTS, and each has a test pinning the inversion:

  * `_dirty_paths` returns None for a FAILED PROBE, [] for a clean tree.
    Reading None as clean would pull over an unknown tree (g-115-5013).
  * `_default_branch` returns "" for UNKNOWN, which its own docstring says
    must not be read as "not the default branch".
  * off-default is a SKIP, because fast-forwarding a feature branch leaves the
    read hazard fully intact while consuming the signal that it exists.

Mutation-checked in the manner this file's siblings require: each test was run
against a deliberately broken implementation and observed to FAIL before being
kept (notably, the off-default and dirty tests were run against a build whose
ladder ran in the wrong order, and both went red).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:                                    # SSOT first, chain only as fallback
    from _paths import PROJECT_ROOT
except Exception:
    # parents[3], NOT parents[2] — tests=0, scripts=1, core=2, root=3.
    PROJECT_ROOT = Path(__file__).resolve().parents[3]

SCRIPT = PROJECT_ROOT / "core" / "scripts" / "product-repo-freshness.py"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=30)


def _commit_content(repo, name, text, msg):
    """A commit that CHANGES the tree — --allow-empty builds topology only."""
    (Path(repo) / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", msg)


def _head(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _make_pair(root, name):
    """An origin plus a clone of it, both on `main`, clone in sync."""
    bare = root / (name + ".git")
    work = root / name
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=30)
    subprocess.run(["git", "clone", "-q", str(bare), str(work)],
                   capture_output=True, check=True, timeout=30)
    _commit_content(work, "base.txt", "base\n", "base")
    _git(work, "branch", "-q", "-M", "main")
    _git(work, "push", "-q", "-u", "origin", "main")
    return work, bare


def _advance_origin(root, bare, n=1, tag="up"):
    """Push n commits to `bare` from a throwaway clone, leaving `work` behind."""
    side = root / ("side-" + bare.name)
    if not side.exists():
        subprocess.run(["git", "clone", "-q", str(bare), str(side)],
                       capture_output=True, check=True, timeout=30)
    _git(side, "checkout", "-q", "main")
    _git(side, "pull", "-q", "--ff-only")
    for i in range(n):
        _commit_content(side, "%s-%d.txt" % (tag, i), "x\n", "%s %d" % (tag, i))
    _git(side, "push", "-q", "origin", "main")


def run_pull(*repos, extra=()):
    """Invoke the real script the way a caller does; return parsed JSON."""
    argv = [sys.executable, str(SCRIPT), "--pull", "--no-fetch", "--json"]
    for r in repos:
        argv += ["--repo", str(r)]
    argv += list(extra)
    env = dict(os.environ, STORAGE_BACKEND="local")   # guard-955
    p = subprocess.run(argv, capture_output=True, text=True, timeout=180,
                       cwd=str(PROJECT_ROOT), env=env)
    assert p.returncode == 0, "pull must always exit 0 (advisory): %s" % p.stderr
    return json.loads(p.stdout[p.stdout.find("{"):])


def rec_for(payload, name):
    return next(r for r in payload["records"] if r["name"] == name)


# --------------------------------------------------------------------------
# The effect. Without this one, every other test could pass against a no-op.
# --------------------------------------------------------------------------

def test_behind_repo_is_actually_fast_forwarded(tmp_path):
    """The whole point: a behind tree MOVES.

    Asserts the HEAD sha changed and lands exactly on the tracking ref — not
    merely that the banner said 'pulled'.
    """
    work, bare = _make_pair(tmp_path, "work")
    _advance_origin(tmp_path, bare, n=3)
    _git(work, "fetch", "-q", "origin")

    before = _head(work)
    assert _git(work, "rev-list", "--count", "HEAD..@{u}").stdout.strip() == "3"

    r = rec_for(run_pull(work), "work")
    after = _head(work)

    assert r["action"] == "pulled", r
    assert r["behind"] == 3
    assert after != before, "HEAD did not move — the flag had no effect"
    assert after == _git(work, "rev-parse", "@{u}").stdout.strip()
    assert _git(work, "rev-list", "--count", "HEAD..@{u}").stdout.strip() == "0"
    # The advance must not leave the tree dirty.
    assert _git(work, "status", "--porcelain").stdout.strip() == ""


def test_in_sync_repo_reports_current_and_does_not_move(tmp_path):
    work, _bare = _make_pair(tmp_path, "work")
    before = _head(work)
    r = rec_for(run_pull(work), "work")
    assert r["action"] == "current"
    assert r["behind"] == 0
    assert _head(work) == before


# --------------------------------------------------------------------------
# The skip ladder — the irreversible direction, so the heavier coverage.
# --------------------------------------------------------------------------

def test_off_default_branch_is_skipped_not_fast_forwarded(tmp_path):
    """A feature branch behind ITS upstream is still not advanced.

    This is the case a 'just pull everything' implementation gets wrong while
    looking correct: the fast-forward succeeds, the banner says pulled, and
    the working tree STILL does not contain origin/main's content — so the
    grep that motivated this goal still returns the inverted answer, and the
    off-default signal that would have warned about it has been consumed.
    """
    work, bare = _make_pair(tmp_path, "work")
    _git(work, "checkout", "-q", "-b", "feature")
    _git(work, "push", "-q", "-u", "origin", "feature")
    _advance_origin(tmp_path, bare, n=2, tag="mainonly")
    # Give `feature` a genuine upstream gap too, so a naive implementation
    # would have something to fast-forward and this test can catch it.
    side = tmp_path / "side-feature"
    subprocess.run(["git", "clone", "-q", str(bare), str(side)],
                   capture_output=True, check=True, timeout=30)
    _git(side, "checkout", "-q", "feature")
    _commit_content(side, "feat.txt", "f\n", "feature upstream")
    _git(side, "push", "-q", "origin", "feature")
    _git(work, "fetch", "-q", "origin")

    before = _head(work)
    assert _git(work, "rev-list", "--count", "HEAD..@{u}").stdout.strip() == "1"

    r = rec_for(run_pull(work), "work")

    assert r["action"] == "skipped-off-default", r
    assert r["branch"] == "feature"
    assert r["default_branch"] == "main"
    assert _head(work) == before, "an off-default tree must never be advanced"
    assert "false" in r["detail"].lower()


def test_dirty_tree_is_never_pulled_over(tmp_path):
    """Uncommitted edits may be a same-box partner's in-flight work."""
    work, bare = _make_pair(tmp_path, "work")
    _advance_origin(tmp_path, bare, n=2)
    _git(work, "fetch", "-q", "origin")
    (work / "base.txt").write_text("locally edited\n", encoding="utf-8")

    before = _head(work)
    r = rec_for(run_pull(work), "work")

    assert r["action"] == "skipped-dirty", r
    assert _head(work) == before
    assert (work / "base.txt").read_text(encoding="utf-8") == "locally edited\n"


def test_ahead_repo_is_not_fast_forwarded(tmp_path):
    """Local commits origin lacks are never risked, even when also behind."""
    work, bare = _make_pair(tmp_path, "work")
    _advance_origin(tmp_path, bare, n=1)
    _commit_content(work, "mine.txt", "mine\n", "local work")
    _git(work, "fetch", "-q", "origin")

    before = _head(work)
    r = rec_for(run_pull(work), "work")

    assert r["action"] == "skipped-ahead", r
    assert r["ahead"] == 1
    assert _head(work) == before


def test_repo_without_upstream_is_skipped(tmp_path):
    work = tmp_path / "solo"
    subprocess.run(["git", "init", "-q", str(work)], check=True, timeout=30)
    _commit_content(work, "a.txt", "a\n", "base")
    _git(work, "branch", "-q", "-M", "main")

    before = _head(work)
    r = rec_for(run_pull(work), "solo")
    assert r["action"] == "skipped-no-upstream", r
    assert _head(work) == before


# --------------------------------------------------------------------------
# Throttle semantics — the half that is easy to get backwards.
# --------------------------------------------------------------------------

def test_no_fetch_still_advances_from_already_fetched_refs(tmp_path):
    """--no-fetch suppresses the NETWORK call, not the local advance.

    This is the design's load-bearing distinction. `--sweep` already refreshes
    FETCH_HEAD without moving any working tree, so a throttle keyed on
    FETCH_HEAD that ALSO gated the advance would be silenced by the very sweep
    that just proved the trees are behind. Here the refs are fetched by hand
    and the advance must still happen with the network call suppressed.
    """
    work, bare = _make_pair(tmp_path, "work")
    _advance_origin(tmp_path, bare, n=2)
    _git(work, "fetch", "-q", "origin")     # refs fresh, tree still behind

    before = _head(work)
    r = rec_for(run_pull(work), "work")     # run_pull passes --no-fetch

    assert r["action"] == "pulled"
    assert r["fetched"] is False, "no network fetch should have occurred"
    assert _head(work) != before


def test_throttle_zero_forces_a_fetch(tmp_path):
    """--pull-interval-min 0 means always fetch; the repo advances without a
    hand-run fetch first."""
    work, bare = _make_pair(tmp_path, "work")
    _advance_origin(tmp_path, bare, n=1)

    before = _head(work)
    argv = [sys.executable, str(SCRIPT), "--pull", "--json",
            "--repo", str(work), "--pull-interval-min", "0"]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=180,
                       cwd=str(PROJECT_ROOT),
                       env=dict(os.environ, STORAGE_BACKEND="local"))
    assert p.returncode == 0, p.stderr
    r = rec_for(json.loads(p.stdout[p.stdout.find("{"):]), "work")

    assert r["fetched"] is True
    assert r["action"] == "pulled"
    assert _head(work) != before


def test_pull_ignores_goal_id_selection(tmp_path):
    """--goal-id must not narrow --pull.

    The incident class is a read of a repo NO goal named, so honouring the
    goal filter here would reintroduce exactly the blind spot. Mirrors the
    same guarantee --sweep documents.
    """
    work, bare = _make_pair(tmp_path, "work")
    _advance_origin(tmp_path, bare, n=1)
    _git(work, "fetch", "-q", "origin")

    r = rec_for(run_pull(work, extra=("--goal-id", "g-000-00")), "work")
    assert r["action"] == "pulled", (
        "a goal id that names no repo must not suppress the pull")


def test_always_exits_zero_and_reports_every_repo(tmp_path):
    """Advisory posture: a mixed estate never yields a non-zero exit."""
    good, bare = _make_pair(tmp_path, "good")
    _advance_origin(tmp_path, bare, n=1)
    _git(good, "fetch", "-q", "origin")
    solo = tmp_path / "solo"
    subprocess.run(["git", "init", "-q", str(solo)], check=True, timeout=30)
    _commit_content(solo, "a.txt", "a\n", "base")

    payload = run_pull(good, solo)
    assert payload["mode"] == "pull"
    assert payload["scanned"] == 2
    assert {r["name"] for r in payload["records"]} == {"good", "solo"}
    assert rec_for(payload, "good")["action"] == "pulled"
    assert rec_for(payload, "solo")["action"] == "skipped-no-upstream"


def test_off_default_distinguishes_dead_residue_from_live_work(tmp_path):
    """The off-default flag must say WHICH of the two it is.

    Measured on cc-08 2026-08-20: 13 repos off-default, 3 of them fully merged
    (safe to return) and 10 holding unmerged work. Reported undifferentiated,
    all 13 read as the same ambient warning and a reader learns to skip the
    section — which costs the 3 that are one command from being fixed and, on
    the goal's own named case (Ayoai-Operator, 13 behind main, missing the fix
    commit), the whole point of the flag.
    """
    # (a) MERGED: the branch adds nothing origin/main lacks.
    merged, bare_m = _make_pair(tmp_path, "merged")
    _git(merged, "checkout", "-q", "-b", "stale-topic")
    _git(merged, "push", "-q", "-u", "origin", "stale-topic")
    _advance_origin(tmp_path, bare_m, n=4, tag="mainmoved")
    _git(merged, "fetch", "-q", "origin")

    r = rec_for(run_pull(merged), "merged")
    assert r["action"] == "skipped-off-default"
    assert r["branch_merged_into_default"] is True, r
    assert r["behind_default"] == 4
    assert "safe to return" in r["detail"].lower()

    # (b) UNMERGED: the branch carries a commit origin/main does not have.
    live, bare_l = _make_pair(tmp_path, "live")
    _git(live, "checkout", "-q", "-b", "wip")
    # PUSH THE BRANCH FIRST. Every off-default repo measured on cc-08 has an
    # upstream (they were pushed when the goal that made them ran), and the
    # skip ladder tests no-upstream BEFORE off-default — so a local-only
    # fixture branch reports `skipped-no-upstream` and never exercises the
    # classification under test. The fixture has to model the real shape.
    _git(live, "push", "-q", "-u", "origin", "wip")
    _commit_content(live, "wip.txt", "wip\n", "unmerged work")
    _advance_origin(tmp_path, bare_l, n=1, tag="other")
    _git(live, "fetch", "-q", "origin")

    r2 = rec_for(run_pull(live), "live")
    assert r2["action"] == "skipped-off-default"
    assert r2["branch_merged_into_default"] is False, r2
    assert "leave it alone" in r2["detail"].lower()

    # Neither was moved.
    assert _head(merged) == _git(merged, "rev-parse", "HEAD").stdout.strip()


def test_render_names_the_off_default_hazard(tmp_path):
    """The human-readable banner must surface off-default repos explicitly.

    JSON is what tooling reads; the banner is what an agent reads mid-loop,
    and the off-default hazard is useless if it only exists in a field nobody
    prints.
    """
    work, _bare = _make_pair(tmp_path, "work")
    _git(work, "checkout", "-q", "-b", "feature")
    _git(work, "push", "-q", "-u", "origin", "feature")

    argv = [sys.executable, str(SCRIPT), "--pull", "--no-fetch",
            "--repo", str(work)]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=180,
                       cwd=str(PROJECT_ROOT),
                       env=dict(os.environ, STORAGE_BACKEND="local"))
    assert p.returncode == 0, p.stderr
    assert "OFF-DEFAULT" in p.stdout
    assert "feature" in p.stdout
