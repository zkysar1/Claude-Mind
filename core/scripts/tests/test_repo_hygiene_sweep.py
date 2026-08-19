"""Pins for repo-hygiene-sweep.py (, Phase A report-only).

Every behavioural test here corresponds to a defect the FIRST smoke run of the
script actually produced, not to a contract-ideal reading of the spec. Two of
them were caught only because the smoke run was checked against the filing
goal's own named example rather than against a count:

  * `origin/recover/orphan-chain-20260809` was classified a DELETION CANDIDATE.
    It is the exact branch the goal calls out as untouchable (load-bearing for
    open g-115-5637), and it survived the goal-status join because the join ran
    in the wrong direction -- goal-id-in-branch-name, when the branch name
    embeds no goal id at all and the link lives in the GOAL's text.

  * A branch literally named `origin` was a candidate. git abbreviates
    `refs/remotes/origin/HEAD` to the short name `origin`, which matches nothing
    in PROTECTED_BRANCHES.

The structural test at the bottom pins the Phase-A doctrine itself: this script
must contain no mutation path at all. That is deliberately a source-level
assertion rather than a behavioural one -- a behavioural test can only prove the
paths it exercises did not mutate, while the doctrine is that no such path
EXISTS to be reached.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# core/scripts/tests/<this> -> core/scripts. Derived from THIS file rather
# than by counting .parent hops up to a PROJECT_ROOT, which is the 
# bug class CLAUDE.md keeps an audit grep for.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPTS_DIR / "repo-hygiene-sweep.py"


def _load():
    spec = importlib.util.spec_from_file_location("repo_hygiene_sweep", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rhs = _load()
GIT = shutil.which("git") or "git"


# ------------------------------------------------- direction (b): the join ---

def test_branch_named_in_goal_text_catches_a_branch_with_no_goal_id():
    """The canonical incident. Verbatim from the filing goal's own example."""
    goal_text = ("live example: recover/orphan-chain-20260809 looks stale but is "
                 "load-bearing for open g-115-5637")
    assert rhs.branch_goal_refs("recover/orphan-chain-20260809", {"g-115-5637"}) == [], (
        "precondition: the branch name embeds NO goal id, so direction (a) "
        "cannot see it -- that is why direction (b) has to exist")
    assert rhs.branch_named_in_goal_text("recover/orphan-chain-20260809", goal_text)


def test_the_join_strips_the_origin_prefix_before_matching():
    """A goal writes the branch name, never the remote-qualified form.

    Matching only `origin/<name>` finds nothing and silently degrades direction
    (b) back to direction (a) -- which is the defect, wearing a passing test.
    """
    goal_text = "blocked on recover/orphan-chain-20260809 landing"
    assert rhs.branch_named_in_goal_text("origin/recover/orphan-chain-20260809",
                                         goal_text) == "recover/orphan-chain-20260809"


def test_a_short_or_generic_branch_name_does_not_match_prose():
    """Over-keeping is the safe direction, but not past the point of usefulness.

    `dev` appearing in 14MB of goal prose must not keep every branch forever.
    """
    goal_text = "the dev box is behind; see main for the fix"
    assert rhs.branch_named_in_goal_text("dev", goal_text) is None
    assert rhs.branch_named_in_goal_text("main", goal_text) is None


def test_the_join_returns_none_against_an_empty_index():
    assert rhs.branch_named_in_goal_text("recover/orphan-chain-20260809", "") is None


# ------------------------------------------------- direction (a): goal ids ---

def test_direction_a_matches_only_non_terminal_goals():
    """A goal id in the name is not enough -- that goal must still be open."""
    assert rhs.branch_goal_refs("feat/g-115-5637-thing", {"g-115-5637"}) == ["g-115-5637"]
    assert rhs.branch_goal_refs("feat/g-115-5637-thing", set()) == []
    assert rhs.branch_goal_refs("feat/g-115-5637-thing", {"g-115-9999"}) == []


def test_an_unknown_status_reads_as_non_terminal():
    """Fail-safe: a status this sweep has never heard of must NOT be terminal.

    NON_TERMINAL is a membership set rather than the complement of
    {completed, skipped} precisely so a new status defaults to untouchable.
    """
    assert "pending" in rhs.NON_TERMINAL
    assert "completed" not in rhs.NON_TERMINAL
    assert "some-future-status" not in rhs.NON_TERMINAL, (
        "a status not in the set is treated as TERMINAL by the loader's "
        "membership test, so adding a real new non-terminal status means "
        "adding it here -- this pin exists to make that requirement visible")


# ------------------------------------------------- the branch lane on git ----

@pytest.fixture()
def tiny_repo(tmp_path):
    """A real git repo with a real origin, so the lane runs against real refs."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run([GIT, "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    subprocess.run([GIT, "init", "-b", "main", str(work)],
                   capture_output=True, check=True)
    for k, v in (("user.email", "t@t.t"), ("user.name", "t")):
        subprocess.run([GIT, "-C", str(work), "config", k, v],
                       capture_output=True, check=True)
    (work / "f.txt").write_text("1\n", encoding="utf-8")
    subprocess.run([GIT, "-C", str(work), "add", "-A"], capture_output=True, check=True)
    subprocess.run([GIT, "-C", str(work), "commit", "-m", "init"],
                   capture_output=True, check=True)
    subprocess.run([GIT, "-C", str(work), "remote", "add", "origin", str(origin)],
                   capture_output=True, check=True)
    subprocess.run([GIT, "-C", str(work), "push", "-u", "origin", "main"],
                   capture_output=True, check=True)
    subprocess.run([GIT, "-C", str(work), "remote", "set-head", "origin", "main"],
                   capture_output=True, check=True)
    return work


def test_origin_head_is_never_a_candidate(tiny_repo):
    """git abbreviates refs/remotes/origin/HEAD to the short name `origin`.

    That short name matches nothing in PROTECTED_BRANCHES, so the first build
    emitted a deletion candidate for a branch that does not exist. The skip test
    reads the FULL refname for this reason.
    """
    res = rhs.lane_branches(tiny_repo, set(), "", False, {})
    names = [c["branch"] for c in res["merged_candidates"]] + \
            [c["branch"] for c in res["unmerged_report"]]
    assert "origin" not in names, res
    assert not any(n.endswith("/HEAD") for n in names), res


def test_a_merged_branch_is_a_candidate_and_carries_its_tip_sha(tiny_repo):
    """The recovery handle is the point -- a candidate without a SHA is unsafe."""
    subprocess.run([GIT, "-C", str(tiny_repo), "branch", "spent"],
                   capture_output=True, check=True)
    res = rhs.lane_branches(tiny_repo, set(), "", False, {})
    hit = [c for c in res["merged_candidates"] if c["branch"] == "spent"]
    assert hit, res
    assert len(hit[0]["sha"]) == 40
    assert hit[0]["classifier"] == "ancestry"


def test_the_goal_join_removes_a_branch_from_the_candidate_list(tiny_repo):
    """Same repo, same branch, only the goal index differs. That is the control."""
    subprocess.run([GIT, "-C", str(tiny_repo), "branch",
                    "recover/orphan-chain-20260809"], capture_output=True, check=True)

    without = rhs.lane_branches(tiny_repo, set(), "", False, {})
    assert any(c["branch"] == "recover/orphan-chain-20260809"
               for c in without["merged_candidates"]), (
        "positive control: with NO goal index this branch IS a candidate -- if "
        "it is not, this test proves nothing about the join")

    with_join = rhs.lane_branches(
        tiny_repo, {"g-115-5637"},
        "load-bearing for open g-115-5637: recover/orphan-chain-20260809",
        True, {})
    assert not any(c["branch"] == "recover/orphan-chain-20260809"
                   for c in with_join["merged_candidates"])
    kept = [k for k in with_join["kept"]
            if k["branch"] == "recover/orphan-chain-20260809"]
    assert kept and kept[0]["join"] == "name-in-goal-text", with_join["kept"]


def test_an_unavailable_goal_index_keeps_goal_named_branches(tiny_repo):
    """Fail-safe: index down must not mean 'nothing is owned'."""
    subprocess.run([GIT, "-C", str(tiny_repo), "branch", "feat/g-115-5637-x"],
                   capture_output=True, check=True)
    res = rhs.lane_branches(tiny_repo, set(), "", False, {})
    assert not any(c["branch"] == "feat/g-115-5637-x"
                   for c in res["merged_candidates"])
    assert any(k["branch"] == "feat/g-115-5637-x" for k in res["kept"])


def test_user_namespace_and_backup_refs_are_never_candidates(tiny_repo):
    for name in ("zakcc/deterministic-driver", "backup/snapshot-20260806"):
        subprocess.run([GIT, "-C", str(tiny_repo), "branch", name],
                       capture_output=True, check=True)
    res = rhs.lane_branches(tiny_repo, set(), "", False, {})
    cands = [c["branch"] for c in res["merged_candidates"]]
    assert "zakcc/deterministic-driver" not in cands, res
    assert "backup/snapshot-20260806" not in cands, res


def test_gh_pr_state_merged_classifies_a_squash_merged_branch(tiny_repo):
    """Ancestry-UNMERGED + PR MERGED must still classify as merged.

    This is the measured squash-merge case (staging promote/v2.3.1 et al). The
    branch below is genuinely NOT an ancestor of main, so ancestry alone says
    unmerged -- exactly the shape that hid the residue.
    """
    subprocess.run([GIT, "-C", str(tiny_repo), "checkout", "-q", "-b", "squashed"],
                   capture_output=True, check=True)
    (tiny_repo / "g.txt").write_text("2\n", encoding="utf-8")
    subprocess.run([GIT, "-C", str(tiny_repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run([GIT, "-C", str(tiny_repo), "commit", "-m", "diverge"],
                   capture_output=True, check=True)
    subprocess.run([GIT, "-C", str(tiny_repo), "checkout", "-q", "main"],
                   capture_output=True, check=True)

    plain = rhs.lane_branches(tiny_repo, set(), "", False, {})
    assert any(c["branch"] == "squashed" for c in plain["unmerged_report"]), (
        "positive control: without PR state this branch reads UNMERGED")

    heads = {"squashed": {"pr": 42, "merge_commit": "deadbeef", "merged_at": "x"}}
    withpr = rhs.lane_branches(tiny_repo, set(), "", False, heads)
    hit = [c for c in withpr["merged_candidates"] if c["branch"] == "squashed"]
    assert hit, withpr
    assert hit[0]["classifier"] == "gh-pr-state"
    assert hit[0]["pr"] == 42


def test_freshness_reports_tree_identity_not_only_counts(tiny_repo):
    """guard-1996: the counts lie on a protected-branch repo; the tree does not."""
    out = rhs.lane_freshness(tiny_repo)
    assert out["tree_identical"] is True, out
    assert out["behind"] == 0 and out["ahead"] == 0, out
    assert out["on_default"] is True and out["tree_compare_meaningful"] is True, out


def test_freshness_names_the_head_branch_and_voids_the_tree_test_off_default(tiny_repo):
    """A checkout parked on a feature branch is not a diverged checkout.

    Measured on the first full run: 20 of 59 repos reported ahead>0 with
    differing trees and were read as "genuinely diverged". AcceptTosLambda's
    `ahead=1 behind=2` was HEAD sitting on fix/g-335-1026-zip-cache-exclusions,
    one commit ahead of origin/master -- correct arithmetic, wrong question.
    Without `head_branch` the pair is uninterpretable and reads as divergence.
    """
    on_default = rhs.lane_freshness(tiny_repo)
    assert on_default["head_branch"] == on_default["base"], on_default

    subprocess.run([GIT, "-C", str(tiny_repo), "checkout", "-q", "-b", "feat/x"],
                   capture_output=True, check=True)
    (tiny_repo / "h.txt").write_text("3\n", encoding="utf-8")
    subprocess.run([GIT, "-C", str(tiny_repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run([GIT, "-C", str(tiny_repo), "commit", "-m", "wip"],
                   capture_output=True, check=True)

    off = rhs.lane_freshness(tiny_repo)
    assert off["head_branch"] == "feat/x", off
    assert off["on_default"] is False, off
    assert off["ahead"] == 1, off
    assert off["tree_identical"] is False, off
    assert off["tree_compare_meaningful"] is False, (
        "the tree test must be VOIDED off the default branch -- reporting it as "
        "meaningful is what manufactured a 20-repo divergence finding")


# ----------------------------------------------- the Phase-A doctrine pin ----

# Each entry is the CODE shape of a mutation, not the English name of one. The
# first build of this pin used the bare token "--apply" and FAILED against the
# module's own docstring, which says "there is deliberately NO `--apply` flag" --
# a test that forbids a word cannot coexist with the documentation explaining
# why the word is forbidden. So these match argv-list fragments and argparse
# registrations, which prose does not contain.
MUTATING = (
    '"worktree", "remove"', '"branch", "-D"', '"branch", "-d"',
    '"push", "--delete"', '"stash", "drop"', '"stash", "clear"',
    '"reset", "--hard"', 'add_argument("--apply"',
)


def test_the_module_contains_no_mutation_path_at_all():
    """Phase A is report-only, and that is a property of the SOURCE.

    A behavioural test can only show the paths it exercised did not mutate. The
    doctrine is stronger: no such path exists to be reached, so there is nothing
    for a future `--apply` to accidentally enable. `worktree prune --dry-run` is
    the one prune verb present and it is explicitly the dry form.
    """
    src = MODULE_PATH.read_text(encoding="utf-8")
    for verb in MUTATING:
        assert verb not in src, (
            "repo-hygiene-sweep.py is Phase-A report-only and must contain no "
            "mutation path; found %r" % verb)
    assert "worktree\", \"prune\", \"--dry-run\"" in src or \
           '"prune", "--dry-run"' in src, (
        "the worktree lane must use the DRY form -- a bare prune here would "
        "delete worktree admin state before daemon-orphan-sweep.sh has run")


def test_fetch_uses_prune_because_a_plain_fetch_leaves_ghosts():
    """rb-7719: plain fetch leaves deleted-branch tracking refs behind."""
    src = MODULE_PATH.read_text(encoding="utf-8")
    assert '"fetch", "--prune"' in src, (
        "a plain fetch re-discovers already-deleted branches as work")


def test_cli_refuses_an_apply_flag():
    """Behavioural, not a --help grep.

    Grepping --help for the string fails against the docstring that EXPLAINS the
    absence (RawDescriptionHelpFormatter prints it verbatim). Asserting argparse
    REJECTS the flag is both immune to that and a stronger claim: it proves the
    flag is unroutable, not merely undocumented.
    """
    rc = subprocess.run([sys.executable, str(MODULE_PATH), "--apply"],
                        capture_output=True, text=True, timeout=60)
    assert rc.returncode != 0, (
        "--apply must be REFUSED by argparse, not silently accepted")
    assert "unrecognized arguments" in (rc.stderr or ""), rc.stderr
