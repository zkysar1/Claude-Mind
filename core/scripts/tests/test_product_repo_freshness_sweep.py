"""Tests for product-repo-freshness.py --sweep ().

The sweep answers a DIFFERENT question from the freshness() path it lives
beside, and `test_sweep_sees_what_freshness_misses` is the test that pins WHY
both exist: on one fixture, freshness() reports `in-sync` while three unpushed
commits sit on other local branches. That fixture is the a55add9 incident in
miniature (a completed fix committed to a local branch and never pushed, found
five days later), so if a future refactor collapses the two readings into one,
that test is the one that should fail.

Every test here was mutation-checked: each was run against a deliberately
wrong implementation and observed to FAIL before being kept. That matters more
than usual for this file, because most assertions are about a report being
EMPTY or a severity being LOW — the shape that passes against broken code for
free (a test that cannot fail is the vacuous-zero defect with the sign
flipped, rb-245 / guard-2421).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:                                    # SSOT first, chain only as fallback
    from _paths import PROJECT_ROOT
except Exception:
    # parents[3], NOT parents[2]: this file sits at core/scripts/tests/, so
    # tests=0, scripts=1, core=2, PROJECT_ROOT=3. The one-level-short chain was
    # written here first and produced `<root>/core/core/scripts/...`, which
    # spawns rc=2 "No such file" — every test red for a reason having nothing
    # to do with the code under test. That is the  re-derivation
    # class CLAUDE.md keeps an audit grep for, and the script under test warns
    # about it in PROJECT_ROOT_FOR_WRAPPERS() because it already bit this goal
    # once in its own test file. Hence the import above: derive from the SSOT,
    # and let the chain be the thing that never runs.
    PROJECT_ROOT = Path(__file__).resolve().parents[3]

SCRIPT = PROJECT_ROOT / "core" / "scripts" / "product-repo-freshness.py"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=30)


def _commit(repo, msg):
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", msg)


def _commit_content(repo, name, text, msg):
    """A commit that actually CHANGES the tree.

    `_commit` above uses --allow-empty, which is a fine shortcut for building
    TOPOLOGY but produces zero CONTENT divergence — and content divergence is
    precisely what the tree-identity filter measures (guard-1996 / g-115-6355).
    An empty commit is therefore, correctly, no longer reported as unpushed
    work: it is indistinguishable from the squash-merge topology the sweep now
    excludes. So any fixture whose intent is "real unpushed WORK" must write a
    file — the incident this sweep exists for was a completed FIX, and a fix
    has content. `test_genuinely_absent_work_survives_the_cherry_filter` was
    already written this way, for the same reason, one filter earlier.
    """
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", msg)


def _make_repo(root, name, with_remote=True):
    """A checkout on `main` whose base commit is pushed (when it has a remote)."""
    work = root / name
    if with_remote:
        bare = root / (name + ".git")
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=30)
        subprocess.run(["git", "clone", "-q", str(bare), str(work)],
                       capture_output=True, check=True, timeout=30)
    else:
        subprocess.run(["git", "init", "-q", str(work)], check=True, timeout=30)
    _commit(work, "base")
    _git(work, "branch", "-q", "-M", "main")
    if with_remote:
        _git(work, "push", "-q", "-u", "origin", "main")
    return work


def run_sweep(*repos, extra=()):
    """Invoke the real script the way a caller does, and return parsed JSON."""
    argv = [sys.executable, str(SCRIPT), "--sweep", "--no-fetch", "--json"]
    for r in repos:
        argv += ["--repo", str(r)]
    argv += list(extra)
    env = dict(os.environ, STORAGE_BACKEND="local")   # guard-955
    p = subprocess.run(argv, capture_output=True, text=True, timeout=180,
                       cwd=str(PROJECT_ROOT), env=env)
    assert p.returncode == 0, "sweep must always exit 0 (advisory): %s" % p.stderr
    return json.loads(p.stdout[p.stdout.find("{"):])


def rec_for(payload, name):
    return next(r for r in payload["records"] if r["name"] == name)


# --------------------------------------------------------------------------
# The reason the sweep exists at all
# --------------------------------------------------------------------------

def test_sweep_sees_what_freshness_misses(tmp_path):
    """freshness() says in-sync; the sweep finds 3 unpushed commits.

    Not a contrived divergence — this is the incident shape. The checked-out
    branch genuinely IS in sync with its upstream, so freshness() is not
    wrong; it is answering "is this tree stale before I edit it". The unpushed
    work is on branches it never looks at.
    """
    work = _make_repo(tmp_path, "work")
    _git(work, "checkout", "-q", "-b", "feature")
    _commit_content(work, "one.txt", "one\n", "unpushed one")
    _commit_content(work, "two.txt", "two\n", "unpushed two")
    _git(work, "checkout", "-q", "-b", "other")
    _commit_content(work, "three.txt", "three\n", "unpushed three")
    _git(work, "checkout", "-q", "main")

    # freshness(): the current branch is in sync, so it reports nothing wrong.
    p = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(work), "--no-fetch", "--json"],
        capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT),
        env=dict(os.environ, STORAGE_BACKEND="local"))
    fresh = json.loads(p.stdout[p.stdout.find("{"):])["records"][0]
    assert fresh["verdict"] == "in-sync"
    assert fresh["ahead"] == 0

    # sweep: the same repo, the same moment, three unpushed commits.
    r = rec_for(run_sweep(work), "work")
    assert r["unpushed_total"] == 3
    assert {u["branch"] for u in r["unpushed"]} == {"feature", "other"}


def test_unpushed_total_is_distinct_not_a_per_branch_sum(tmp_path):
    """Branches share ancestry, so summing per-branch counts double-counts.

    `other` is cut from `feature`, so it CONTAINS feature's two commits. The
    per-branch counts (2 and 3) are each correct; adding them yields 5 for a
    repo holding 3 unpushed commits. This test exists because the first
    implementation did exactly that and the inflated total looked entirely
    plausible — nothing about a wrong-but-believable count announces itself.
    """
    work = _make_repo(tmp_path, "shared")
    _git(work, "checkout", "-q", "-b", "feature")
    _commit_content(work, "u1.txt", "u1\n", "u1")
    _commit_content(work, "u2.txt", "u2\n", "u2")
    _git(work, "checkout", "-q", "-b", "other")
    _commit_content(work, "u3.txt", "u3\n", "u3")
    _git(work, "checkout", "-q", "main")

    r = rec_for(run_sweep(work), "shared")
    by_branch = {u["branch"]: u["count"] for u in r["unpushed"]}
    assert by_branch == {"feature": 2, "other": 3}, "per-branch counts stay as-is"
    assert sum(by_branch.values()) == 5, "the naive sum is 5 — this is the trap"
    assert r["unpushed_total"] == 3, "the reported total must be the distinct union"


# --------------------------------------------------------------------------
# Severity
# --------------------------------------------------------------------------

def test_unpushed_on_default_branch_is_high(tmp_path):
    """The a55add9 class: committed to local main, never pushed."""
    work = _make_repo(tmp_path, "app")
    _commit_content(work, "fix.txt", "the fix\n", "fix that never left this box")
    r = rec_for(run_sweep(work), "app")
    assert r["severity"] == "high"
    # Assert the fields that carry meaning, not the whole dict: an exact-dict
    # comparison here broke the moment `patches_absent` was added, reporting a
    # red for a record that was entirely correct.
    assert len(r["unpushed"]) == 1
    u = r["unpushed"][0]
    assert (u["branch"], u["count"], u["on_default"]) == ("main", 1, True)


def test_unpushed_on_feature_branch_is_medium_not_high(tmp_path):
    """Severity must DISCRIMINATE — if everything is HIGH, nothing is."""
    work = _make_repo(tmp_path, "app")
    _git(work, "checkout", "-q", "-b", "wip")
    _commit_content(work, "wip.txt", "work in progress\n", "work in progress")
    _git(work, "checkout", "-q", "main")
    r = rec_for(run_sweep(work), "app")
    assert r["severity"] == "medium"
    assert r["unpushed"][0]["on_default"] is False


def test_squash_merged_branch_is_not_reported_as_unpushed(tmp_path):
    """A squash-merged branch keeps local commits that no remote will ever hold.

    THE MEASURED CASE, not a hypothetical. On this sweep's first real run
    (cc-08, 56 repos, 2026-08-06) four branches were flagged and two were
    exactly this: work already merged upstream via a squash, whose local
    commits can never match a remote sha. Reporting them is a permanent false
    positive — the branch will be flagged on every run forever, and it made up
    HALF of day-one output.

    `git cherry` is what separates them: it compares patch equivalence, so the
    squashed work reads as already-upstream while genuinely-absent work does
    not.
    """
    work = _make_repo(tmp_path, "squashed")

    # Feature branch with real content (patch-id equivalence needs a real diff).
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "feature.txt").write_text("the feature\n", encoding="utf-8")
    _git(work, "add", "feature.txt")
    _git(work, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "feat: the work")

    # Upstream takes the SAME content as a different commit (what a squash-merge
    # does), and main is pushed. The local feature branch is now stale-but-merged.
    _git(work, "checkout", "-q", "main")
    (work / "feature.txt").write_text("the feature\n", encoding="utf-8")
    _git(work, "add", "feature.txt")
    _git(work, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "squashed: the work (#42)")
    _git(work, "push", "-q", "origin", "main")

    r = rec_for(run_sweep(work), "squashed")
    assert r["merged_equivalent"] == ["feature"], \
        "the squash-merged branch must be recognised as already upstream"
    assert r["unpushed"] == [], "and must NOT be reported as unpushed work"
    assert r["severity"] == "clean"


def test_genuinely_absent_work_survives_the_cherry_filter(tmp_path):
    """The other half of the discrimination — the filter must not eat real work.

    Paired deliberately with the squash test above: a filter that suppressed
    everything would make that test pass while destroying the sweep. This is
    the positive control for the filter itself.
    """
    work = _make_repo(tmp_path, "genuine")
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "only-here.txt").write_text("never pushed anywhere\n", encoding="utf-8")
    _git(work, "add", "only-here.txt")
    _git(work, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "feat: work that exists only on this box")
    _git(work, "checkout", "-q", "main")

    r = rec_for(run_sweep(work), "genuine")
    assert r["merged_equivalent"] == []
    assert len(r["unpushed"]) == 1
    assert r["unpushed"][0]["patches_absent"] == 1
    assert r["severity"] == "medium"


def test_squash_merge_topology_with_identical_tree_is_not_flagged(tmp_path):
    """`git cherry` cannot see a MULTI-commit squash — the  defect.

    The cherry filter one test up catches a squash only when the branch was a
    SINGLE commit, because then the squashed patch-id still matches. Squash N
    commits and the combined patch matches NONE of the N originals, so every
    one of them reports `+` and the branch sails through untouched.

    THE MEASURED CASE, not a hypothetical. A live product repo reported HIGH at
    `ahead 9, behind 0` while `HEAD^{tree}` and `origin/main^{tree}` were the
    same hash and `git diff --name-only origin/main HEAD` listed zero files.
    That false HIGH went on to generate a downstream product goal whose
    acceptance criteria would have opened an EMPTY-diff PR against a protected
    production repo — so this false positive manufactures work, it does not
    merely add a line to a report.
    """
    bare = tmp_path / "topological.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=30)
    work = tmp_path / "topological"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)],
                   capture_output=True, check=True, timeout=30)
    _commit(work, "base")
    _git(work, "branch", "-q", "-M", "main")
    _git(work, "push", "-q", "-u", "origin", "main")

    # TWO real commits on local main — what a locally-merged feature leaves
    # behind. Each is its own patch, which is exactly what defeats cherry.
    _commit_content(work, "a.txt", "A\n", "feat: a")
    _commit_content(work, "b.txt", "B\n", "feat: b")

    # Upstream takes the SAME content as ONE commit, pushed from a peer
    # checkout. That is what a squash-merged PR does to the shared branch.
    peer = tmp_path / "peer"
    subprocess.run(["git", "clone", "-q", str(bare), str(peer)],
                   capture_output=True, check=True, timeout=30)
    # Bind explicitly to origin/main. `git init --bare` honours
    # init.defaultBranch, which need not be `main`, so the bare repo's HEAD can
    # name a branch nothing ever created and the clone lands on an UNBORN
    # branch — after which `push origin main` fails and `_git` swallows the rc.
    # That is how this fixture first ran: silent no-op push, origin/main left at
    # the empty-tree base, and the trees differed for a reason having nothing to
    # do with the code under test (guard-1091).
    _git(peer, "checkout", "-q", "-B", "main", "origin/main")
    (peer / "a.txt").write_text("A\n", encoding="utf-8")
    (peer / "b.txt").write_text("B\n", encoding="utf-8")
    _git(peer, "add", "a.txt", "b.txt")
    _git(peer, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "squashed: a+b (#42)")
    push = _git(peer, "push", "-q", "origin", "main")
    assert push.returncode == 0, "fixture drift: peer push failed: %s" % push.stderr
    _git(work, "fetch", "-q", "origin")

    # FIXTURE PRECONDITIONS. Without these the assertions below can pass for
    # the wrong reason — a fixture that stopped reproducing the measured state
    # would report a green that means nothing (rb-245 / guard-2421).
    assert _git(work, "rev-parse", "main^{tree}").stdout.strip() == \
           _git(work, "rev-parse", "origin/main^{tree}").stdout.strip(), \
        "fixture drift: the trees must be IDENTICAL or this is not the measured case"
    cherry = _git(work, "cherry", "origin/main", "main").stdout
    assert sum(1 for ln in cherry.splitlines() if ln.startswith("+")) == 2, \
        "fixture drift: `git cherry` must STILL report both commits absent — if " \
        "it does not, this no longer exercises the gap the tree check fills"

    r = rec_for(run_sweep(work), "topological")
    assert r["unpushed"] == [], \
        "zero content divergence — a push here would carry an empty diff"
    assert r["severity"] == "clean"
    assert [t["branch"] for t in r["tree_identical_branches"]] == ["main"]
    assert r["tree_identical_branches"][0]["upstream"] == "origin/main"


def test_genuine_default_branch_divergence_survives_the_tree_filter(tmp_path):
    """The positive control for the tree filter, on the lane it was added to.

    Paired deliberately with the topology test above: a filter that dropped
    everything would make that test pass while silencing the sweep's entire
    reason to exist. Discrimination is the claim here, not exclusion.
    """
    work = _make_repo(tmp_path, "genuine-main")
    _commit_content(work, "shipped.txt", "real work\n", "fix nobody pushed")

    r = rec_for(run_sweep(work), "genuine-main")
    assert r["tree_identical_branches"] == [], "real content must not be excluded"
    assert r["severity"] == "high"
    assert len(r["unpushed"]) == 1
    u = r["unpushed"][0]
    assert (u["branch"], u["on_default"]) == ("main", True)
    assert u["tree_identical"] is False, \
        "the compare must have RUN and answered 'differs' — None would mean unmeasured"


def test_unmeasurable_tree_compare_does_not_promote_a_branch_to_clean(tmp_path):
    """CANNOT CHECK, per branch: `None` is neither `False` nor `True`.

    A remote is configured but nothing was ever pushed, so `origin/main` does
    not exist and there is no ref to compare a tree against. The compare is
    UNMEASURED — and an unmeasured check must leave the branch reported,
    exactly as a failed `git status` leaves the repo non-clean. The opposite
    default is the vacuity this script guards at every other level, and it
    would be the worst version of it here: the promotion would be to silence.
    """
    bare = tmp_path / "never-pushed.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=30)
    work = tmp_path / "never-pushed"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)],
                   capture_output=True, check=True, timeout=30)
    _commit_content(work, "only-here.txt", "never left this box\n", "work")
    _git(work, "branch", "-q", "-M", "main")

    assert _git(work, "rev-parse", "--verify", "--quiet",
                "origin/main").returncode != 0, \
        "fixture drift: origin/main must NOT exist, or the compare is measurable"

    r = rec_for(run_sweep(work), "never-pushed")
    assert r["tree_identical_branches"] == []
    assert len(r["unpushed"]) == 1
    assert r["unpushed"][0]["tree_identical"] is None, \
        "an unmeasurable compare must record None, never a measured False"
    assert r["severity"] != "clean"


def test_clean_repo_is_clean(tmp_path):
    work = _make_repo(tmp_path, "tidy")
    r = rec_for(run_sweep(work), "tidy")
    assert r["severity"] == "clean"
    assert r["unpushed"] == [] and r["unpushed_total"] == 0
    assert r["no_remote"] is False


# --------------------------------------------------------------------------
# The no-remote guard (measured: --not --remotes excludes nothing without one)
# --------------------------------------------------------------------------

def test_remoteless_repo_does_not_report_its_whole_history(tmp_path):
    """Without the guard this reports EVERY commit as unpushed.

    `git rev-list --count <branch> --not --remotes` subtracts the set of
    remote-tracking refs. With no remotes that set is empty, so the count is
    the full history — measured, not theorised. The guard converts it into
    the finding it actually is ("nothing here is pushed anywhere") with no
    commit count attached.
    """
    work = _make_repo(tmp_path, "local-only", with_remote=False)
    _commit(work, "second")
    _commit(work, "third")
    r = rec_for(run_sweep(work), "local-only")
    assert r["no_remote"] is True
    assert r["unpushed"] == [], "a remoteless repo must not report a commit count"
    assert r["unpushed_total"] == 0
    assert r["severity"] == "medium"


# --------------------------------------------------------------------------
# Dirty-tree age
# --------------------------------------------------------------------------

def test_freshly_dirty_tree_is_not_reported(tmp_path):
    """A tree being dirty is not the finding — being dirty and ABANDONED is."""
    work = _make_repo(tmp_path, "busy")
    (work / "f.txt").write_text("edited just now", encoding="utf-8")
    r = rec_for(run_sweep(work), "busy")
    assert r["dirty_files"] == 1, "the file must actually be seen as dirty"
    assert r["dirty_age_h"] is not None and r["dirty_age_h"] < 1
    assert r["severity"] == "clean"


def test_stale_dirty_tree_is_low(tmp_path):
    work = _make_repo(tmp_path, "abandoned")
    f = work / "f.txt"
    f.write_text("edited days ago", encoding="utf-8")
    old = time.time() - 72 * 3600
    os.utime(f, (old, old))
    r = rec_for(run_sweep(work), "abandoned")
    assert r["dirty_files"] == 1
    assert r["dirty_age_h"] > 24
    assert r["severity"] == "low"


def test_dirty_path_with_spaces_is_counted(tmp_path):
    """The -z parse exists so quoted filenames are not silently dropped.

    Without -z, git quotes a path containing a space and the naive split
    mis-parses it — under-reporting exactly the hand-edited files most likely
    to be sitting in an abandoned tree.
    """
    work = _make_repo(tmp_path, "spacey")
    (work / "a file with spaces.txt").write_text("x", encoding="utf-8")
    r = rec_for(run_sweep(work), "spacey")
    assert r["dirty_files"] == 1


# --------------------------------------------------------------------------
# The report itself
# --------------------------------------------------------------------------

def test_clean_banner_states_what_it_scanned(tmp_path):
    """A clean sweep must not be silent.

    Silence on a clean run is indistinguishable from a sweep that crashed,
    mis-parsed its enumeration, or never ran — the vacuous-zero class this
    script already guards twice elsewhere. The count is what makes the zero
    mean something.
    """
    work = _make_repo(tmp_path, "tidy")
    p = subprocess.run(
        [sys.executable, str(SCRIPT), "--sweep", "--no-fetch", "--repo", str(work)],
        capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT),
        env=dict(os.environ, STORAGE_BACKEND="local"))
    assert p.returncode == 0
    assert "CLEAN" in p.stdout
    assert "1 repo(s) scanned" in p.stdout


def test_no_fetch_run_discloses_that_counts_may_be_stale(tmp_path):
    """--no-fetch can report a partner's already-pushed commit as unpushed."""
    work = _make_repo(tmp_path, "tidy")
    p = subprocess.run(
        [sys.executable, str(SCRIPT), "--sweep", "--no-fetch", "--repo", str(work)],
        capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT),
        env=dict(os.environ, STORAGE_BACKEND="local"))
    assert "--no-fetch" in p.stdout and "last known remote refs" in p.stdout


def test_sweep_ignores_goal_id_selection(tmp_path):
    """The incident class is unpushed work in a repo NO goal named.

    If --sweep respected the goal-text filter it would inherit the very blind
    spot it exists to remove.
    """
    work = _make_repo(tmp_path, "unnamed-by-any-goal")
    _commit_content(work, "orphan.txt", "orphan work\n", "orphan")
    payload = run_sweep(work, extra=["--goal-id", "g-999-99"])
    assert payload["scanned"] == 1
    assert rec_for(payload, "unnamed-by-any-goal")["severity"] == "high"
