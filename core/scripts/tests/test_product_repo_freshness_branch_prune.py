"""Tests for prune_merged_local_branches (local gone-branch cleanup).

Every other classifier in product-repo-freshness.py REPORTS; a wrong verdict
there mislabels a line. This one DELETES, so the same wrong verdict destroys
work — and `_patches_absent_upstream`'s docstring records a 50% false-positive
rate on its own first live run. The coverage is therefore weighted toward the
KEEP side: a branch wrongly kept is clutter, a branch wrongly deleted is gone.

The load-bearing case is `gone upstream + content NOT upstream`. Closing a PR
with --delete-branch removes the remote ref WITHOUT merging, so `gone` alone
cannot authorize deletion; 19 such branches existed on cc-08 2026-09-20. If
only one test in this file survives, it should be that one.

Squash-merge gets its own test because it breaks the sha-reachability arm by
construction (g-115-6355): the branch is NOT an ancestor of the default branch
even though its content is fully present, so the tree arm is the only thing
that classifies it — exactly the case that made 152 of 176 branches on cc-08
read as unlanded under a naive check.

Mutation-checked: each test was run against a build with the containment check
removed (verifying the KEEP tests go red) and against one with the `gone`
check removed (verifying the never-pushed test goes red).
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util

try:
    from _paths import PROJECT_ROOT
except Exception:
    PROJECT_ROOT = Path(__file__).resolve().parents[3]

_spec = importlib.util.spec_from_file_location(
    "prf", PROJECT_ROOT / "core" / "scripts" / "product-repo-freshness.py")
prf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prf)


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=True, timeout=30)


def _commit(repo, name, text, msg):
    (Path(repo) / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", msg)


def _estate(tmp_path):
    """An origin + a clone, both on `main`, sharing one commit."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _commit(origin, "a.txt", "one\n", "init")
    _git(origin, "config", "receive.denyCurrentBranch", "ignore")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                   check=True, timeout=60)
    return origin, clone


def _make_gone_branch(clone, name, files, merge_into_main=False):
    """Create `name`, push it, optionally land its content, then delete remote."""
    _git(clone, "checkout", "-q", "-b", name)
    for fn, txt in files:
        _commit(clone, fn, txt, "work on %s" % name)
    _git(clone, "push", "-q", "-u", "origin", name)
    if merge_into_main:
        _git(clone, "checkout", "-q", "main")
        _git(clone, "merge", "-q", "--no-edit", name)
        _git(clone, "push", "-q", "origin", "main")
    _git(clone, "checkout", "-q", "main")
    _git(clone, "push", "-q", "origin", "--delete", name)
    _git(clone, "fetch", "-q", "--prune", "origin")


def test_gone_upstream_with_contained_content_is_prunable(tmp_path):
    _o, clone = _estate(tmp_path)
    _make_gone_branch(clone, "feat/done", [("b.txt", "two\n")],
                      merge_into_main=True)
    res = prf.prune_merged_local_branches(clone, "main")
    assert [n for n, _ in res["prunable"]] == ["feat/done"], res


def test_gone_upstream_but_unmerged_content_is_KEPT(tmp_path):
    """The load-bearing case: closing a PR with --delete-branch looks like this."""
    _o, clone = _estate(tmp_path)
    _make_gone_branch(clone, "feat/rejected", [("c.txt", "unique\n")],
                      merge_into_main=False)
    res = prf.prune_merged_local_branches(clone, "main")
    assert res["prunable"] == [], res
    assert any(n == "feat/rejected" and "NOT upstream" in w
               for n, w in res["kept"]), res


def test_gone_upstream_unmerged_is_not_deleted_even_when_applying(tmp_path):
    """apply=True must not widen WHICH branches qualify, only act on them."""
    _o, clone = _estate(tmp_path)
    _make_gone_branch(clone, "feat/rejected", [("c.txt", "unique\n")],
                      merge_into_main=False)
    res = prf.prune_merged_local_branches(clone, "main", apply=True)
    assert res["deleted"] == [], res
    out = _git(clone, "branch", "--list", "feat/rejected").stdout
    assert "feat/rejected" in out, "branch with unmerged work was deleted"


def test_squash_merged_branch_is_prunable_via_the_tree_arm(tmp_path):
    """Squash breaks sha reachability; only the tree comparison sees it."""
    _o, clone = _estate(tmp_path)
    _git(clone, "checkout", "-q", "-b", "feat/squash")
    _commit(clone, "s.txt", "sq\n", "part 1")
    _commit(clone, "s2.txt", "sq2\n", "part 2")
    _git(clone, "push", "-q", "-u", "origin", "feat/squash")
    _git(clone, "checkout", "-q", "main")
    _git(clone, "merge", "-q", "--squash", "feat/squash")
    _git(clone, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "squashed")
    _git(clone, "push", "-q", "origin", "main")
    _git(clone, "push", "-q", "origin", "--delete", "feat/squash")
    _git(clone, "fetch", "-q", "--prune", "origin")
    anc = _git(clone, "merge-base", "--is-ancestor", "feat/squash",
               "origin/main").returncode
    assert anc != 0, "precondition: squash must break sha reachability"
    res = prf.prune_merged_local_branches(clone, "main")
    assert [n for n, _ in res["prunable"]] == ["feat/squash"], res


def test_never_pushed_branch_is_untouched(tmp_path):
    """No upstream at all is not a GONE upstream — local WIP must survive."""
    _o, clone = _estate(tmp_path)
    _git(clone, "checkout", "-q", "-b", "wip/local")
    _commit(clone, "w.txt", "wip\n", "wip")
    _git(clone, "checkout", "-q", "main")
    res = prf.prune_merged_local_branches(clone, "main", apply=True)
    assert res["prunable"] == [] and res["deleted"] == [], res
    assert "wip/local" in _git(clone, "branch", "--list", "wip/local").stdout


def test_branch_whose_upstream_still_exists_is_untouched(tmp_path):
    _o, clone = _estate(tmp_path)
    _git(clone, "checkout", "-q", "-b", "feat/live")
    _commit(clone, "l.txt", "live\n", "live")
    _git(clone, "push", "-q", "-u", "origin", "feat/live")
    _git(clone, "checkout", "-q", "main")
    res = prf.prune_merged_local_branches(clone, "main", apply=True)
    assert res["deleted"] == [], res
    assert "feat/live" in _git(clone, "branch", "--list", "feat/live").stdout


def test_checked_out_branch_is_kept_not_advertised(tmp_path):
    _o, clone = _estate(tmp_path)
    _make_gone_branch(clone, "feat/done", [("b.txt", "two\n")],
                      merge_into_main=True)
    _git(clone, "checkout", "-q", "feat/done")
    res = prf.prune_merged_local_branches(clone, "main", apply=True)
    assert res["deleted"] == [], res
    assert any(n == "feat/done" and w == "checked out" for n, w in res["kept"])


def test_apply_actually_deletes_the_qualifying_branch(tmp_path):
    """sig-227: assert the EFFECT, not the report."""
    _o, clone = _estate(tmp_path)
    _make_gone_branch(clone, "feat/done", [("b.txt", "two\n")],
                      merge_into_main=True)
    assert "feat/done" in _git(clone, "branch", "--list", "feat/done").stdout
    res = prf.prune_merged_local_branches(clone, "main", apply=True)
    assert res["deleted"] == ["feat/done"], res
    assert _git(clone, "branch", "--list", "feat/done").stdout.strip() == ""


def test_unknown_default_branch_prunes_nothing(tmp_path):
    """`_default_branch` returns '' for UNKNOWN — it must not mean 'no guard'."""
    _o, clone = _estate(tmp_path)
    _make_gone_branch(clone, "feat/done", [("b.txt", "two\n")],
                      merge_into_main=True)
    res = prf.prune_merged_local_branches(clone, "", apply=True)
    assert res["deleted"] == [] and res["prunable"] == [], res
    assert res["error"], "an unmeasurable run must say so, not read as clean"


def test_default_branch_itself_is_never_considered(tmp_path):
    _o, clone = _estate(tmp_path)
    res = prf.prune_merged_local_branches(clone, "main", apply=True)
    assert res["deleted"] == [], res
    assert "main" in _git(clone, "branch", "--list", "main").stdout
