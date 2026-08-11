#!/usr/bin/env python3
"""Tests for corpus-freshness-precheck.py (gap-056, ).

The integration tests build REAL git repos in tmp rather than mocking `_git`.
The whole tool is a statement about git behaviour -- which ref a grep reads,
whether a fetch touches the worktree -- so a mocked `_git` would pin the mock's
behaviour and not git's, which is precisely the class of test that passes while
the thing it covers is broken.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name, modname):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cfp = _load("corpus-freshness-precheck.py", "_cfp")
prf = _load("product-repo-freshness.py", "_prf_t")


def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, "git %s failed: %s" % (args, p.stderr)
    return p.stdout.strip()


@pytest.fixture
def repo_pair(tmp_path):
    """(clone, origin) where clone tracks origin/master and is level with it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--quiet", "--initial-branch=master")
    git(origin, "config", "user.email", "t@t.t")
    git(origin, "config", "user.name", "t")
    (origin / "src.py").write_text("def alpha():\n    return 1\n")
    git(origin, "add", "src.py")
    git(origin, "commit", "--quiet", "-m", "initial")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)],
                   capture_output=True, text=True, timeout=60, check=True)
    git(clone, "config", "user.email", "t@t.t")
    git(clone, "config", "user.name", "t")
    return clone, origin


def advance_origin(origin, filename, content, msg):
    (origin / filename).write_text(content)
    git(origin, "add", filename)
    git(origin, "commit", "--quiet", "-m", msg)


# ---------------------------------------------------------------- unit: verdicts

def test_verdict_cannot_check_outranks_everything():
    """An unreadable repo must never contribute a reassuring answer."""
    assert cfp.verdict_for({"verdict": "in-sync", "grep_error": "boom"}) == "CANNOT-CHECK"
    assert cfp.verdict_for({"verdict": "unknown"}) == "CANNOT-CHECK"


def test_no_upstream_is_cannot_check_not_clean():
    """Local-only/detached is legitimately not stale, but nothing proves it
    current either -- folding it into CLEAN would manufacture confidence."""
    assert cfp.verdict_for({"verdict": "no-upstream", "matches": []}) == "CANNOT-CHECK"


def test_stale_outranks_review():
    """Matches read off a stale ref are not evidence either way."""
    rec = {"verdict": "behind", "behind": 3, "matches": ["a.py:1:x"]}
    assert cfp.verdict_for(rec) == "STALE"


def test_matches_make_review_never_refuted():
    """Constraint 1: a match set is for READING, so it can never self-adjudicate
    into a refutation. CLEAN additionally requires fetch_verified — zero matches
    on an unverified ref is CANNOT-CHECK, not CLEAN."""
    assert cfp.verdict_for({"verdict": "in-sync", "matches": ["a.py:1:x"],
                            "fetch_verified": True}) == "REVIEW"
    assert cfp.verdict_for({"verdict": "in-sync", "matches": [],
                            "fetch_verified": True}) == "CLEAN"


# ---------------------------------------------------------------- unit: exit codes

def test_empty_records_is_cannot_check_not_success():
    """The vacuity guard: nothing examined must never exit 0."""
    assert cfp.overall([])[0] == 2


def test_overall_precedence():
    clean = {"verdict": "in-sync", "matches": [], "fetch_verified": True}
    stale = {"verdict": "behind", "behind": 1, "matches": [], "fetch_verified": True}
    review = {"verdict": "in-sync", "matches": ["a:1:x"], "fetch_verified": True}
    cannot = {"verdict": "unknown"}
    assert cfp.overall([clean])[0] == 0
    assert cfp.overall([clean, review])[0] == 1
    assert cfp.overall([clean, stale])[0] == 1
    assert cfp.overall([clean, stale, cannot])[0] == 2


# ---------------------------------------------------------------- integration

def test_clean_repo_zero_matches_is_safe_to_assert(repo_pair):
    """SAFE TO ASSERT requires a VERIFIED-current ref, so this deliberately does
    NOT pass --no-fetch (the repo_pair origin is local, so the fetch succeeds).
    This test asserted rc==0 WITH --no-fetch in the first version — it was
    encoding the bug rather than catching it."""
    clone, _ = repo_pair
    rc = cfp.main(["--pattern", "no_such_symbol_anywhere", "--repo", str(clone)])
    assert rc == 0


def test_match_present_blocks_the_assertion(repo_pair):
    clone, _ = repo_pair
    rc = cfp.main(["--pattern", "def alpha", "--repo", str(clone), "--no-fetch"])
    assert rc == 1


def test_stale_corpus_blocks_and_names_the_touching_commit(repo_pair, capsys):
    """The encounter-2 shape: behind, and one missing commit CREATED the surface.

    A bare behind-count would not say the staleness bears on THIS claim; the
    pathspec-filtered log is what turns 'behind' into 'behind in a way that
    invalidates you'.
    """
    clone, origin = repo_pair
    advance_origin(origin, "unrelated.md", "docs\n", "docs only")
    advance_origin(origin, "target.py", "def the_surface():\n    pass\n",
                   "create the surface under evaluation")
    git(clone, "fetch", "--quiet")

    rec = cfp.check_repo(prf, clone, "the_surface", ["target.py"],
                         do_fetch=False, max_matches=40)
    assert cfp.verdict_for(rec) == "STALE"
    assert rec["behind"] == 2
    touching = rec["missing_commits_touching_surface"]
    assert touching is not None and len(touching) == 1, touching
    assert "create the surface" in touching[0]


def test_grep_reads_the_fresh_ref_not_the_working_tree(repo_pair):
    """The core correctness claim: the symbol exists ONLY on the remote, and the
    working tree has never seen it. A worktree grep returns 0 here -- which is
    exactly the false negative this tool exists to prevent."""
    clone, origin = repo_pair
    advance_origin(origin, "new.py", "def only_on_remote():\n    pass\n", "add")
    git(clone, "fetch", "--quiet")

    assert not (clone / "new.py").exists()          # worktree genuinely lacks it
    rec = cfp.check_repo(prf, clone, "only_on_remote", [],
                         do_fetch=False, max_matches=40)
    assert rec["matches"], "fresh-ref grep must find what the worktree lacks"
    assert any("only_on_remote" in m for m in rec["matches"])


def test_matches_are_reported_with_file_and_line_not_a_count(repo_pair):
    """Constraint 1, structurally: every reported match must be adjudicable."""
    clone, _ = repo_pair
    rec = cfp.check_repo(prf, clone, "alpha", [], do_fetch=False, max_matches=40)
    assert rec["matches"]
    for m in rec["matches"]:
        # git grep -n on a ref yields "<ref>:<path>:<line>:<text>"
        assert m.count(":") >= 3, m
        assert "alpha" in m


def test_never_mutates_the_shared_checkout(repo_pair):
    """Constraint 2: a partner's in-flight edits must survive the probe."""
    clone, origin = repo_pair
    dirty = clone / "partner_wip.txt"
    dirty.write_text("in-flight partner work\n")
    advance_origin(origin, "new.py", "x = 1\n", "advance")
    git(clone, "fetch", "--quiet")

    head_before = git(clone, "rev-parse", "HEAD")
    status_before = git(clone, "status", "--porcelain")

    cfp.check_repo(prf, clone, "x = 1", [], do_fetch=False, max_matches=40)

    assert git(clone, "rev-parse", "HEAD") == head_before
    assert git(clone, "status", "--porcelain") == status_before
    assert dirty.read_text() == "in-flight partner work\n"


def test_no_fetch_can_never_be_clean(repo_pair, capsys):
    """The fresh-eyes finding, pinned. The FIRST version of this script printed
    "0 matches on a VERIFIED-CURRENT ref", "VERDICT: SAFE TO ASSERT" and exit 0
    for a symbol that existed on the remote — the exact false negative the tool
    exists to refuse, reproduced inside the tool. Zero matches on a ref nobody
    verified is current is the DANGEROUS shape, not a clean one."""
    clone, origin = repo_pair
    advance_origin(origin, "new.py", "def only_on_remote():\n    pass\n", "adds the surface")
    # Deliberately do NOT fetch: the clone's remote-tracking ref is stale, so a
    # grep of it cannot see the symbol that provably exists upstream.
    rc = cfp.main(["--pattern", "only_on_remote", "--repo", str(clone), "--no-fetch"])
    out = capsys.readouterr().out
    assert rc == 2, "an unverified ref must never yield SAFE TO ASSERT"
    assert "CANNOT CHECK" in out
    assert "SAFE TO ASSERT" not in out
    # ground truth: the symbol really is on the remote
    assert git(origin, "grep", "-l", "only_on_remote", "master", "--")


def test_failed_fetch_can_never_be_clean(tmp_path):
    """Second instance of the same defect, and the one a --no-fetch-only fix
    would have missed: freshness() reports a fetch failure in `detail` and then
    CONTINUES to the count (correctly — a stale count beats no count), so a repo
    whose fetch failed also greps an unverified ref."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--quiet", "--initial-branch=master")
    git(origin, "config", "user.email", "t@t.t")
    git(origin, "config", "user.name", "t")
    (origin / "a.py").write_text("x = 1\n")
    git(origin, "add", "a.py")
    git(origin, "commit", "--quiet", "-m", "init")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)],
                   capture_output=True, timeout=60, check=True)
    # Break the remote so the fetch cannot succeed.
    git(clone, "remote", "set-url", "origin", str(tmp_path / "does-not-exist"))

    rec = cfp.check_repo(prf, clone, "no_such_symbol", [],
                         do_fetch=True, max_matches=40)
    assert rec["fetch_verified"] is False
    assert "fetch FAILED" in (rec.get("detail") or "")
    assert cfp.verdict_for(rec) == "CANNOT-CHECK"


def test_successful_fetch_clears_the_stale_no_fetch_note(repo_pair):
    """freshness() stamps its own no-fetch note whenever told not to fetch, and
    check_repo now fetches one level up — so that note must be cleared, or every
    verified run would carry prose claiming it was never fetched."""
    clone, _ = repo_pair
    rec = cfp.check_repo(prf, clone, "alpha", [], do_fetch=True, max_matches=40)
    assert rec["fetch_verified"] is True
    assert not str(rec.get("detail") or "").startswith("no-fetch:")


def test_matches_still_review_even_on_an_unverified_ref(repo_pair):
    """A match on a stale ref is still a real match, so REVIEW stays the more
    informative verdict than CANNOT-CHECK. Both block the assertion."""
    clone, _ = repo_pair
    rec = cfp.check_repo(prf, clone, "alpha", [], do_fetch=False, max_matches=40)
    assert rec["fetch_verified"] is False
    assert rec["matches"]
    assert cfp.verdict_for(rec) == "REVIEW"


def test_head_date_is_reported(repo_pair):
    """gap-056 asks for HEAD date alongside behind: behind=0 on a months-old
    HEAD means the REMOTE is idle, a different situation from being current."""
    clone, _ = repo_pair
    rec = cfp.check_repo(prf, clone, "zzz", [], do_fetch=False, max_matches=40)
    assert rec["head_date"] and rec["head_date"].startswith("20")


def test_json_output_carries_verdict_and_exit_code(repo_pair, capsys):
    clone, _ = repo_pair
    rc = cfp.main(["--pattern", "def alpha", "--repo", str(clone),
                   "--no-fetch", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == rc == 1
    assert "DO NOT ASSERT" in payload["verdict"]
    assert payload["records"][0]["matches"]


def test_non_repo_path_yields_cannot_check_not_clean(tmp_path, capsys):
    """A typo'd path must not read as 'checked, nothing found'."""
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    rc = cfp.main(["--pattern", "anything", "--repo", str(plain), "--no-fetch"])
    assert rc == 2
    assert "CANNOT CHECK" in capsys.readouterr().err


def test_sibling_load_failure_is_cannot_check(monkeypatch, capsys):
    """The reuse dependency failing must withhold confidence, not degrade to a
    hand-rolled fallback -- a second enumeration copy is the drift being avoided."""
    monkeypatch.setattr(cfp, "_load_sibling", lambda: None)
    rc = cfp.main(["--pattern", "x", "--repo", "/tmp"])
    assert rc == 2
    assert "CANNOT CHECK" in capsys.readouterr().err


def test_max_matches_truncation_is_flagged(repo_pair):
    """Truncation must be visible: a silently-cut match list is a count in
    disguise, and the caller would read fewer matches than exist."""
    clone, origin = repo_pair
    body = "\n".join("hit_%d = 1" % i for i in range(30))
    advance_origin(origin, "many.py", body + "\n", "many hits")
    git(clone, "fetch", "--quiet")
    rec = cfp.check_repo(prf, clone, "hit_", [], do_fetch=False, max_matches=5)
    assert len(rec["matches"]) == 5
    assert rec["matches_truncated"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
