"""Cross-repo stranding half of the uncommitted-work gate (owner directive
2026-08-19, after g-115-6784 / g-115-6785: two goals in one six-hour window
closed 'completed' with product-repo commits stranded on unmerged branches).

BUILT ON THE FAILING SIDE, like test_uncommitted_work_delivery.py: before this
existed the gate scanned ONE repo (the framework repo), so every assertion
about a clean single repo would have passed against the defect. Each blocking
test here constructs the exact stranded state the two incidents occupied — a
delivery repo whose fresh commit is reachable from a side branch and absent
from origin's default — and asserts the close REFUSES.

The stale-origin case is the one that matters most and is easiest to get
wrong in the other direction: a PR merged remotely five minutes ago is not in
the local origin/<default> ref until a fetch, and blocking on that would
punish precisely the merge-then-close flow we want. The gate must fetch ONCE
on the failing path and then pass.
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
    _delivery_repo_roots,
    evaluate,
    get_stranded_repos,
)


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r.stdout.strip()


def _mk_origin_and_clone(tmp_path: Path, name: str) -> Path:
    origin = tmp_path / f"{name}-origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(origin)],
                   capture_output=True, check=True)
    clone = tmp_path / name
    seed = tmp_path / f"{name}-seed"
    subprocess.run(["git", "init", "-b", "master", str(seed)],
                   capture_output=True, check=True)
    _git(seed, "config", "user.email", "t@t"); _git(seed, "config", "user.name", "t")
    (seed / "app.py").write_text("v1\n")
    _git(seed, "add", "-A"); _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "master")
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                   capture_output=True, check=True)
    _git(clone, "config", "user.email", "t@t"); _git(clone, "config", "user.name", "t")
    return clone


def _world_with_manifest(tmp_path: Path, roots: list[str]) -> Path:
    world = tmp_path / "world"
    world.mkdir(exist_ok=True)
    lines = "\n".join(f'  - "{r}"' for r in roots)
    (world / "delivery-repos.yaml").write_text(
        f"# test manifest\nroots:\n{lines}\n")
    return world


@pytest.fixture
def framework_repo(tmp_path):
    """A trivially clean repo standing in for the framework repo, so the
    framework half of evaluate() stays silent and the cross-repo half is the
    only variable."""
    repo = tmp_path / "framework"
    subprocess.run(["git", "init", "-b", "master", str(repo)],
                   capture_output=True, check=True)
    _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-m", "seed")
    return repo


def test_fresh_commit_on_an_unmerged_branch_blocks_the_close(tmp_path, framework_repo):
    """THE INCIDENT SHAPE: committed, pushed to a side branch, PR never merged.
    Reachable from origin/side, absent from origin/master -> the close refuses.

    The commit message names the closing goal because that is the fleet's
    production commit shape (`fix(g-350-194): ...` — all 26 live stranded
    commits measured 2026-08-20 carry it), and since g-115-6851 attribution is
    what separates THIS goal's stranding (blocks, here) from a teammate's open
    PR (reports, next test). The fixture previously wrote an anonymous
    "stranded work" subject, which no production commit resembles."""
    clone = _mk_origin_and_clone(tmp_path, "product")
    _git(clone, "checkout", "-q", "-b", "feature")
    (clone / "app.py").write_text("v2 stranded\n")
    _git(clone, "add", "-A"); _git(clone, "commit", "-m", "fix(g-test-1): stranded work")
    _git(clone, "push", "-q", "-u", "origin", "feature")
    _git(clone, "checkout", "-q", "master")  # tree clean, nothing dirty

    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-1", override=None,
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is True
    assert res["would_block"] is True
    assert res["stranded_repos"][0]["stranded_commits"], res["stranded_repos"]


def test_another_goals_open_pr_does_not_block_this_close(tmp_path, framework_repo):
    """: the SAME stranding, attributed to a DIFFERENT goal, must
    report without vetoing.

    Measured 2026-08-20 (alpha worker, cc-08): the un-attributed predicate
    blocked 10 of 10 delivery repos on 26 commits, every one a teammate's open
    PR from 4-34h earlier. A gate that refuses every close fleet-wide has no
    discriminating power left (guard-2273) and trains an override reflex.

    This is the released direction and it is the half a revert would silently
    restore, so it is pinned here rather than left to the live tree."""
    clone = _mk_origin_and_clone(tmp_path, "product")
    _git(clone, "checkout", "-q", "-b", "feature")
    (clone / "app.py").write_text("v2 someone else's work\n")
    _git(clone, "add", "-A"); _git(clone, "commit", "-m", "fix(g-other-42): their work")
    _git(clone, "push", "-q", "-u", "origin", "feature")
    _git(clone, "checkout", "-q", "master")

    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-1b", override=None,
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is False, res["stranded_repos"]
    # Released, NOT invisible — a stranding that stops blocking silently is
    # indistinguishable from a deleted gate.
    assert res["stranded_repos"][0]["unattributed_unmerged"], res["stranded_repos"]

    # A PROSE MENTION of the closing goal is not authorship. Attribution reads
    # the conventional-commit SCOPE `(g-...)`, matching the sweep's needle; a
    # bare substring would re-block here on someone else's commit merely
    # referring to this goal.
    _git(clone, "checkout", "-q", "feature")
    (clone / "app.py").write_text("v3 still theirs\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "fix(g-other-42): their work, supersedes g-test-1b")
    _git(clone, "push", "-q", "origin", "feature")
    _git(clone, "checkout", "-q", "master")
    res2 = evaluate(goal_id="g-test-1b", override=None,
                    repo_path=framework_repo, world_dir=world)
    assert res2["stranded_would_block"] is False, res2["stranded_repos"]


def test_local_only_commit_blocks_regardless_of_attribution(tmp_path, framework_repo):
    """The attribution carve-out must NOT reach commits no remote ref contains.

    Only this box can deliver those, so they block whoever made them — the
    load-bearing half of g-115-6851's verification ("a fix that merely stops
    blocking is indistinguishable from deleting the gate"). Same commit,
    both sides of the push: blocks before, reports after."""
    clone = _mk_origin_and_clone(tmp_path, "product")
    (clone / "app.py").write_text("v2 local only\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "an anonymous subject naming no goal at all")

    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-1c", override=None,
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is True, res["stranded_repos"]

    # Push it: now a remote ref carries it, so it becomes another goal's
    # open-PR shape and releases.
    _git(clone, "push", "-q", "-u", "origin", "HEAD:refs/heads/side")
    res2 = evaluate(goal_id="g-test-1c", override=None,
                    repo_path=framework_repo, world_dir=world)
    assert res2["stranded_would_block"] is False, res2["stranded_repos"]


def test_unpushed_local_commit_blocks_the_close(tmp_path, framework_repo):
    """Committed and never pushed anywhere: the  shape, product edition."""
    clone = _mk_origin_and_clone(tmp_path, "product")
    (clone / "app.py").write_text("v2 local only\n")
    _git(clone, "add", "-A"); _git(clone, "commit", "-m", "never pushed")

    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-2", override=None,
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is True


def test_dirty_tracked_product_file_blocks_but_untracked_noise_does_not(tmp_path, framework_repo):
    """Product repos carry build junk; only TRACKED modifications block."""
    clone = _mk_origin_and_clone(tmp_path, "product")
    (clone / "build-junk.tmp").write_text("noise\n")          # untracked
    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-3a", override=None,
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is False, res["stranded_repos"]

    (clone / "app.py").write_text("uncommitted edit\n")        # tracked, dirty
    res = evaluate(goal_id="g-test-3b", override=None,
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is True


def test_merged_work_passes_even_when_the_local_default_ref_is_stale(tmp_path, framework_repo):
    """The false-block this design guards against: work merged to origin's
    default REMOTELY (as gh pr merge does), local origin/master not yet
    fetched. The gate must refresh once on the failing path and PASS."""
    clone = _mk_origin_and_clone(tmp_path, "product")
    _git(clone, "checkout", "-q", "-b", "feature")
    (clone / "app.py").write_text("v2 merged remotely\n")
    _git(clone, "add", "-A"); _git(clone, "commit", "-m", "work")
    _git(clone, "push", "-q", "-u", "origin", "feature")
    _git(clone, "checkout", "-q", "master")

    # Merge feature -> master REMOTELY via a second clone (stands in for the
    # GitHub merge button); THIS clone's origin/master ref stays stale.
    other = tmp_path / "merger"
    subprocess.run(["git", "clone", "-q", _git(clone, "remote", "get-url", "origin"),
                    str(other)], capture_output=True, check=True)
    _git(other, "config", "user.email", "t@t"); _git(other, "config", "user.name", "t")
    _git(other, "merge", "--no-edit", "-q", "origin/feature")
    _git(other, "push", "-q", "origin", "master")

    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-4", override=None,
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is False, (
        "a remotely-merged PR must not read as stranded after the "
        f"failing-path refresh: {res['stranded_repos']}")


def test_old_stranding_reports_but_does_not_veto(tmp_path, framework_repo):
    """A crusty months-old branch must not block every close forever — it is
    reported (visibility) without a veto (age bound)."""
    clone = _mk_origin_and_clone(tmp_path, "product")
    _git(clone, "checkout", "-q", "-b", "ancient")
    (clone / "app.py").write_text("old stranded\n")
    _git(clone, "add", "-A")
    env_date = "2020-01-01T00:00:00"
    subprocess.run(["git", "-C", str(clone), "-c", f"user.email=t@t",
                    "-c", "user.name=t", "commit", "-m", "ancient",
                    "--date", env_date],
                   capture_output=True, check=True,
                   env={**__import__("os").environ,
                        "GIT_COMMITTER_DATE": env_date})
    _git(clone, "checkout", "-q", "master")

    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-5", override=None,
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is False
    assert res["stranded_repos"] and \
        res["stranded_repos"][0]["stale_stranded_commits"]


def test_override_bypasses_and_no_manifest_disables(tmp_path, framework_repo):
    clone = _mk_origin_and_clone(tmp_path, "product")
    (clone / "app.py").write_text("dirty\n")

    # No manifest -> feature off, exactly as portable as before.
    world_empty = tmp_path / "world-empty"; world_empty.mkdir()
    res = evaluate(goal_id="g-test-6a", override=None,
                   repo_path=framework_repo, world_dir=world_empty)
    assert res["stranded_repos"] == [] and res["would_block"] is False

    # Manifest + override -> reported, audited, not blocking.
    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-6b", override="PR #999 open, close approved",
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is True   # the STATE is still stranded
    assert res["would_block"] is False           # the OVERRIDE carries the close
    ledger = (world / "uncommitted-work-overrides.jsonl").read_text()
    assert "g-test-6b" in ledger


def test_manifest_glob_expansion_skips_non_repos(tmp_path):
    (tmp_path / "not-a-repo").mkdir()
    repo = _mk_origin_and_clone(tmp_path, "real")
    world = _world_with_manifest(tmp_path, [str(tmp_path / "*")])
    roots = _delivery_repo_roots(world)
    assert repo in roots
    assert (tmp_path / "not-a-repo") not in roots


# ── guard-5538: the local-only test reads a LOCAL cache ──────────────────────
# Added by  after the gate false-blocked EVERY goal close fleet-wide
# for ~3 days (measured 2026-08-29, bravo/cc-05). Vinheim-Web-App was a
# single-branch clone holding 3 remote-tracking refs against 53 real branches
# on origin; a commit that had been on origin the whole time read as local-only.
# No fetch can repair that -- fetch obeys the same restricted refspec -- so the
# gate's existing fetch-retry was structurally powerless. Same mechanism
# guard-1250 measured one namespace over (refs/workers/* outside the default
# refspec); there it renders as a structural ZERO, here as a permanent veto.


def _narrow_refspec_to_default(clone: Path) -> None:
    """Make `clone` a single-branch clone: refs/remotes/* stops covering
    non-default branches, exactly as `git clone --single-branch` leaves it."""
    _git(clone, "config", "--unset-all", "remote.origin.fetch")
    _git(clone, "config", "--add", "remote.origin.fetch",
         "+refs/heads/master:refs/remotes/origin/master")


def test_single_branch_clone_does_not_false_block_delivered_work(tmp_path, framework_repo):
    """A commit that IS on origin must not block just because the local
    refs/remotes/* cache cannot see the branch carrying it."""
    clone = _mk_origin_and_clone(tmp_path, "product")
    _narrow_refspec_to_default(clone)

    (clone / "app.py").write_text("v2 delivered on a side branch\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "an anonymous subject naming no goal at all")
    _git(clone, "push", "-q", "origin", "HEAD:refs/heads/side")

    # PRECONDITION, asserted rather than assumed: the commit is genuinely on
    # origin AND genuinely invisible to the local cache. Without this the test
    # could pass for the wrong reason (e.g. push having created the ref anyway),
    # and would then certify nothing.
    sha = _git(clone, "rev-parse", "HEAD")
    assert sha in _git(clone, "ls-remote", "origin", "refs/heads/side"), \
        "setup failed: the commit is not actually on origin"
    _git(clone, "fetch", "-q", "origin")          # cannot help: same refspec
    assert _git(clone, "branch", "-r", "--contains", sha) == "", \
        "setup failed: the local cache CAN see it, so this is not the guard-5538 shape"

    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-5538", override=None,
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is False, res["stranded_repos"]
    assert res["stranded_repos"][0]["refspec_complete"] is False


def test_narrow_refspec_still_blocks_dirty_and_attributed_work(tmp_path, framework_repo):
    """THE MUTATION KILL (guard-3126). The fix drops ONE unsound inference; it
    must not become a way to close with real work outstanding. Both surviving
    blocking paths are asserted on the SAME narrow-refspec repo the test above
    proves is released."""
    clone = _mk_origin_and_clone(tmp_path, "product")
    _narrow_refspec_to_default(clone)

    # (a) dirty tracked file — never depended on remote refs, must still block.
    (clone / "app.py").write_text("uncommitted edit\n")
    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-5538b", override=None,
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is True, res["stranded_repos"]

    # (b) THIS goal's own commit, stranded off the default branch — the
    # /6785 shape the gate exists for. Attribution does not read
    # refs/remotes/*, so a narrow refspec must not release it.
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "fix(g-test-5538c): my own work, not merged")
    res2 = evaluate(goal_id="g-test-5538c", override=None,
                    repo_path=framework_repo, world_dir=world)
    assert res2["stranded_would_block"] is True, res2["stranded_repos"]


def test_complete_refspec_reports_the_flag_and_keeps_blocking(tmp_path, framework_repo):
    """The control for the two above: an ordinary clone is unaffected, and the
    flag says which half ran. A release that cannot be distinguished from a
    deleted gate is not a fix."""
    clone = _mk_origin_and_clone(tmp_path, "product")
    (clone / "app.py").write_text("v2 local only\n")
    _git(clone, "add", "-A"); _git(clone, "commit", "-m", "never pushed anywhere")

    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-5538d", override=None,
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is True, res["stranded_repos"]
    assert res["stranded_repos"][0]["refspec_complete"] is True


# ── : content-free history divergence ─────────────────────────────
# The lane reasons over the commit GRAPH and never compared TREES, so a
# divergence carrying no content read as undelivered work and blocked EVERY
# subsequent close on that box. Measured 2026-08-19 closing :
# one delivery repo reported 7 stranded commits, 4 of them merges whose
# tree was byte-identical to origin/main.
#
# WHY THE OBVIOUS FIXTURE IS VACUOUS, and why this one is not. An *empty*
# commit also has a tree identical to upstream, but for it
# merge-base(@{u}, HEAD) == @{u}, so a three-dot diff is already empty and the
# UNFIXED code returns the same answer — the case certifies nothing. The defect
# needs the merge-base to be STRICTLY OLDER than upstream, which is what makes
# the three-dot diff report files that are in fact already delivered. Both
# helpers below assert that precondition rather than assuming it, so the test
# cannot silently decay into the vacuous shape.


def _content_free_divergence(tmp_path: Path, name: str = "product") -> Path:
    """A clone whose local default has diverged from origin's WITHOUT content.

    Shape: local commits A2 (app.py -> v2); a second clone pushes B1 with the
    SAME bytes so origin moves ahead by another route; the first clone merges,
    resolving to a tree byte-identical to origin's. HEAD and origin/master are
    then different commits with the same tree.
    """
    clone = _mk_origin_and_clone(tmp_path, name)
    (clone / "app.py").write_text("v2\n")
    _git(clone, "add", "-A"); _git(clone, "commit", "-m", "A2 local only")

    other = tmp_path / f"{name}-other"
    subprocess.run(["git", "clone", "-q", _git(clone, "remote", "get-url", "origin"),
                    str(other)], capture_output=True, check=True)
    _git(other, "config", "user.email", "t@t"); _git(other, "config", "user.name", "t")
    (other / "app.py").write_text("v2\n")          # identical bytes, other route
    _git(other, "add", "-A"); _git(other, "commit", "-m", "B1 same content")
    _git(other, "push", "-q", "origin", "master")

    _git(clone, "fetch", "-q", "origin")
    _git(clone, "merge", "--no-edit", "-q", "origin/master")

    # PRECONDITIONS, asserted rather than assumed. Three separate ways this
    # fixture could stop modelling the defect while still passing.
    head_tree = _git(clone, "rev-parse", "HEAD^{tree}")
    up_tree = _git(clone, "rev-parse", "origin/master^{tree}")
    assert head_tree == up_tree, "setup failed: trees are not identical"
    assert _git(clone, "rev-parse", "HEAD") != _git(clone, "rev-parse", "origin/master"), \
        "setup failed: no divergence at all, so nothing would be reported"

    # THE ANTI-VACUITY CONDITION FOR *THIS* LANE, and it is not the same as the
    # framework lane's. That lane diffs three-dot `@{u}...HEAD`, so its vacuous
    # case is merge-base == upstream. Here the population comes from
    # `rev-list --branches --remotes=origin --not <default>`, and merging
    # upstream necessarily makes it an ancestor of HEAD — so merge-base ==
    # upstream ALWAYS holds after a merge and asserting otherwise would reject
    # every valid fixture. What makes THIS case non-vacuous is that the
    # divergent commits are reported at all AND are local-only, which is what
    # puts them in the blocking set (`fresh_local_only`) rather than in the
    # reported-only `unattributed_unmerged`.
    off_default = _git(clone, "rev-list", "--branches", "--remotes=origin",
                       "--not", "refs/remotes/origin/master").split()
    assert off_default, (
        "setup failed: nothing is off-default, so there is nothing to release "
        "and the test would pass against the unfixed code")
    contained = _git(clone, "branch", "-r", "--contains", off_default[0])
    assert contained.strip() == "", (
        "setup failed: the divergent commit is on a remote ref, so it would be "
        "reported as another goal's open PR and never blocked — the unfixed "
        f"code would pass too. contained-in: {contained!r}")
    return clone


def test_content_free_divergence_is_released_and_still_reported(tmp_path, framework_repo):
    """Outcomes 2 and 3: the identical-tree case does not block, and it is
    surfaced rather than silently dropped.

    Reported matters as much as released here: a stranding that stops blocking
    invisibly is indistinguishable from a gate someone deleted (guard-1760),
    and equal trees at the TIP do not prove the intervening commits are
    worthless — a revert pair has an identical tree too.
    """
    clone = _content_free_divergence(tmp_path)
    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-6872", override=None,
                   repo_path=framework_repo, world_dir=world)

    assert res["stranded_would_block"] is False, res["stranded_repos"]
    assert res["would_block"] is False, res

    # Released, NOT invisible — the repo must still reach the payload.
    assert res["stranded_repos"], "the repo vanished from the payload entirely"
    finding = res["stranded_repos"][0]
    assert finding["content_free_stranded_commits"], finding
    assert not finding["stranded_commits"], finding


def test_content_free_release_does_not_reach_real_work_on_a_side_branch(tmp_path, framework_repo):
    """THE MUTATION KILL (guard-3126). Equal trees prove only that HEAD's
    content is already delivered; the off-default population is drawn from ALL
    branches, so a HEAD-only proof must not release a side branch's real work.

    Same repo as the test above — the one proven released — plus one genuinely
    undelivered commit on a feature branch. It must still block.
    """
    clone = _content_free_divergence(tmp_path)
    _git(clone, "checkout", "-q", "-b", "feature")
    (clone / "real.py").write_text("genuinely undelivered\n")
    _git(clone, "add", "-A"); _git(clone, "commit", "-m", "fix(g-test-6872b): real work")
    _git(clone, "checkout", "-q", "master")

    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-6872b", override=None,
                   repo_path=framework_repo, world_dir=world)

    assert res["stranded_would_block"] is True, (
        "the tree-identity release swallowed real undelivered work on a side "
        f"branch: {res['stranded_repos']}")
    finding = res["stranded_repos"][0]
    # Both categories at once: the split is a partition, not a whole-repo verdict.
    assert finding["stranded_commits"], finding
    assert finding["content_free_stranded_commits"], finding


def test_dirty_tracked_still_blocks_under_an_identical_tree(tmp_path, framework_repo):
    """Second mutation kill. `dirty_tracked` is uncommitted work in the working
    tree; the tree-identity proof is about COMMITTED content at HEAD and says
    nothing about it. A dirty file must keep blocking even when HEAD's tree
    matches upstream exactly."""
    clone = _content_free_divergence(tmp_path)
    (clone / "app.py").write_text("uncommitted edit on top\n")

    world = _world_with_manifest(tmp_path, [str(clone)])
    res = evaluate(goal_id="g-test-6872c", override=None,
                   repo_path=framework_repo, world_dir=world)
    assert res["stranded_would_block"] is True, res["stranded_repos"]
    assert res["stranded_repos"][0]["dirty_tracked"], res["stranded_repos"]
