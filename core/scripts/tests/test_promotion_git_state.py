"""Regression tests for core/scripts/promotion-git-state.py.

The load-bearing one is `test_dirty_set_preserves_porcelain_columns` and its
end-to-end twin. The defect it pins was found by a POSITIVE CONTROL, not by
reading the code: a generic `.strip()` in the subprocess helper ate the leading
space of `git status --porcelain`'s two-character status field, so ` M a.txt`
parsed to `hared`-style off-by-one paths. The dirty COUNT stayed correct — two
lines in, two paths out — so every surface a reader would check looked healthy
while the dirty-vs-incoming set intersection came back EMPTY on a genuine
collision, turning the "is this fast-forward safe?" gate into a rubber stamp.

These build REAL git repos rather than mocking `git`, because the defect lives
in the byte-level shape of git's own output; a mock would have reproduced my
misunderstanding instead of git's behaviour (guard-920 — replicate the
production shape, not the contract-ideal one).
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "promotion-git-state.py"


def _load():
    spec = importlib.util.spec_from_file_location("_pgs_under_test", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pgs = _load()


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def pair(tmp_path):
    """An upstream repo and a clone that is 2 commits behind it."""
    up = tmp_path / "up"
    up.mkdir()
    _git(up, "init", "-q")
    _git(up, "config", "user.email", "t@t")
    _git(up, "config", "user.name", "t")
    (up / "shared.txt").write_text("a\n")
    (up / "other.txt").write_text("b\n")
    _git(up, "add", "-A")
    _git(up, "commit", "-qm", "init")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(up), str(clone)], check=True, capture_output=True
    )
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")

    (up / "shared.txt").write_text("a2\n")
    _git(up, "commit", "-qam", "c2")
    (up / "other.txt").write_text("b2\n")
    _git(up, "commit", "-qam", "c3")

    branch = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return up, clone, f"origin/{branch}"


def test_dirty_set_preserves_porcelain_columns(pair):
    """A modified TRACKED file yields ` M path`; the leading space must survive.

    Without preserve_columns this returns {"hared.txt"} — a set of the right
    SIZE holding a path that exists nowhere, which is why a count-based check
    cannot catch it.
    """
    _, clone, _ = pair
    (clone / "shared.txt").write_text("dirty\n")
    dirty, err = pgs._dirty_set(str(clone))
    assert err is None
    assert dirty == {"shared.txt"}, f"porcelain column shift: {dirty!r}"


def test_dirty_set_untracked(pair):
    _, clone, _ = pair
    (clone / "brand-new.txt").write_text("x\n")
    dirty, err = pgs._dirty_set(str(clone))
    assert err is None
    assert dirty == {"brand-new.txt"}


def test_run_preserve_columns_is_opt_in():
    """The default still strips; only the opt-in keeps column 0."""
    rc, out, _ = pgs._run(["printf", "  lead\\n"], preserve_columns=False)
    assert rc == 0 and out == "lead"
    rc, out, _ = pgs._run(["printf", "  lead\\n"], preserve_columns=True)
    assert rc == 0 and out == "  lead"


def _freshness(clone, upstream, apply_ff=False):
    # Bind to locals first: `upstream = upstream` inside a class body reads the
    # CLASS namespace, not the enclosing function's, and raises NameError.
    _t, _u, _a = str(clone), upstream, apply_ff

    class A:
        target = _t
        upstream = _u
        apply = _a

    return pgs.cmd_freshness(A())


def test_freshness_unsafe_when_dirty_intersects_incoming(pair):
    """THE regression. A dirty file the ff would overwrite must refuse."""
    _, clone, up = pair
    (clone / "shared.txt").write_text("dirty\n")
    r, rc = _freshness(clone, up)
    assert r["verdict"] == "UNSAFE", r
    assert r["intersection"] == ["shared.txt"], r
    assert rc == 2


def test_freshness_safe_when_dirty_is_disjoint(pair):
    """A dirty file the ff does NOT touch must not block the hop.

    This is the half that makes the gate worth having: fleet agents dirty
    shared ledgers continuously, so a blanket dirty-tree refusal would stop
    every promotion.
    """
    _, clone, up = pair
    (clone / "unrelated.txt").write_text("x\n")
    r, rc = _freshness(clone, up)
    assert r["verdict"] == "SAFE", r
    assert r["intersection"] == []
    assert rc == 0


def test_freshness_untracked_collision_is_unsafe(pair):
    """An untracked file the ff would create is a clobber too."""
    _, clone, up = pair
    (clone / "other.txt").write_text("local\n")
    _git(clone, "rm", "-q", "--cached", "other.txt")
    r, rc = _freshness(clone, up)
    assert r["verdict"] == "UNSAFE", r
    assert "other.txt" in r["intersection"]
    assert rc == 2


def test_freshness_applies_ff_and_keeps_disjoint_dirt(pair):
    _, clone, up = pair
    (clone / "unrelated.txt").write_text("x\n")
    r, rc = _freshness(clone, up, apply_ff=True)
    assert r["verdict"] == "SAFE" and r["applied_ff"] is True and rc == 0
    behind = subprocess.run(
        ["git", "-C", str(clone), "rev-list", "--count", f"HEAD..{up}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert behind == "0"
    assert (clone / "unrelated.txt").exists(), "ff must not eat disjoint dirt"


def test_freshness_fresh_when_already_up_to_date(pair):
    _, clone, up = pair
    _freshness(clone, up, apply_ff=True)
    r, rc = _freshness(clone, up)
    assert r["verdict"] == "FRESH" and rc == 0


def test_freshness_diverged_refuses_rather_than_merging(pair):
    """Ahead AND behind cannot fast-forward — that is a reconcile-UP question."""
    _, clone, up = pair
    (clone / "local-only.txt").write_text("x\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "local")
    r, rc = _freshness(clone, up)
    assert r["verdict"] == "DIVERGED" and rc == 2


def test_freshness_non_repo_is_unreadable_not_clean(tmp_path):
    """A bad --target must refuse, never produce a clean verdict (guard-1587)."""
    r, rc = _freshness(tmp_path / "nope", "origin/main")
    assert r["verdict"] == "UNREADABLE" and rc == 3


def test_postflight_reports_llm_obligations_separately(pair):
    """Category (b) items must be DETECTED and never silently resolved."""
    _, clone, up = pair
    (clone / "shared.txt").write_text("stash-me\n")
    _git(clone, "stash", "push", "-m", "pre-promotion")

    class A:
        target = str(clone)
        branch = ""
        pr = ""
        tag = ""
        upstream = up
        plant_clone = ""
        also_confirm = []
        apply = False

    r, rc = pgs.cmd_postflight(A())
    kinds = {o["kind"] for o in r["llm_obligations"]}
    assert "stash" in kinds, r["llm_obligations"]
    assert rc == 2
    # The stash must still be there — detection is not resolution.
    out = subprocess.run(
        ["git", "-C", str(clone), "stash", "list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "pre-promotion" in out


def test_postflight_refuses_branch_delete_without_merged_verdict(pair):
    """No --pr means no MERGED verdict, so deletion must REFUSE — ancestry is
    never the gate (a squash merge makes the tip a permanent non-ancestor)."""
    _, clone, up = pair
    _git(clone, "branch", "promote/v9.9.9")

    class A:
        target = str(clone)
        branch = "promote/v9.9.9"
        pr = ""
        tag = ""
        upstream = up
        plant_clone = ""
        also_confirm = []
        apply = True

    r, _ = pgs.cmd_postflight(A())
    assert "REFUSED" in r["mechanical"]["branch_cleanup"]["action"]
    rc = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--verify", "refs/heads/promote/v9.9.9"],
        capture_output=True,
    ).returncode
    assert rc == 0, "branch must survive a refused delete"
