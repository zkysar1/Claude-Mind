"""test_product_repo_freshness_off_default.py — the parked-checkout false negative.

g-115-6371. The Phase-A repo-hygiene sweep measured 22 of 59 product checkouts
with HEAD on a goal-named feature branch rather than the default branch, and the
shape had ALREADY produced a wrong finding: a goal read a parked tree, found
nothing, and reported "this code does not exist" about a function that was
present on origin/main the whole time.

The defect was never that the branch went unrecorded. `freshness()` has captured
`rec["branch"]` since it was written, and `render()` prints it on every noisy
line. The defect is that a parked checkout is not NOISY: every count in
`freshness()` is computed against the CURRENT branch's upstream, so a parked
tree pulls that branch, reports `in-sync`, lands in CLEAN_VERDICTS, and
`render()` returns "". The advisory was silent on precisely the shape that
manufactures a false negative — correct arithmetic to a question nobody asked.

So the assertions below are anchored on SILENCE-WHEN-CLEAN and on the verdict
that gates a read, not on whether a branch name appears somewhere in the output.
A test that only asserted "the branch is named" would have passed against the
unfixed code (measured — it does).

Every pin here was proven RED by mutating the emitter's own branches back to
their pre-fix form; the mutant set is derived from those branches rather than
chosen to hit a count (rb-5996).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
SCRIPT = CORE_SCRIPTS / "product-repo-freshness.py"
CFP_SCRIPT = CORE_SCRIPTS / "corpus-freshness-precheck.py"

if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PRF = _load("product_repo_freshness", SCRIPT)
CFP = _load("corpus_freshness_precheck", CFP_SCRIPT)


# ---------------------------------------------------------------- git fixtures

def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "f.txt").write_text("1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "c1"], check=True)


def _clone(origin: Path, dest: Path) -> None:
    subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.name", "t"], check=True)


def _park(repo: Path, branch: str) -> None:
    """Put the checkout on a feature branch WITH its own pushed upstream.

    Pushing is what makes this fixture the real incident rather than a
    convenient one: the parked branch is fully in sync with its own upstream,
    so every count in freshness() is legitimately zero and the verdict is
    honestly `in-sync`. A parked branch with NO upstream would short-circuit to
    `no-upstream` and would be reported for an unrelated reason, which would
    let these tests pass without the fix ever running.
    """
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", branch], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "-u", "origin", branch], check=True)


def _parked_clone(tmp_path, branch="g-000-01-feature"):
    origin = tmp_path / "origin"
    _init_repo(origin)
    clone = tmp_path / "Widget"
    _clone(origin, clone)
    _park(clone, branch)
    return clone


# --------------------------------------------------------- freshness() records

def test_a_fresh_clone_on_the_default_branch_is_not_off_default(tmp_path):
    """The negative case, and the reason the existing silence pin survives this
    change: a clone sits on the default branch, so off_default is False and
    nothing new is emitted."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    clone = tmp_path / "Widget"
    _clone(origin, clone)
    rec = PRF.freshness(clone, do_fetch=False)
    assert rec["off_default"] is False, (
        "a checkout on its default branch must report off_default False, not a "
        "truthy or None value; got %r" % (rec["off_default"],))
    assert rec["default_branch"] == "main"
    assert rec["verdict"] == "in-sync"


def test_a_parked_checkout_is_flagged_off_default_while_its_verdict_stays_in_sync(tmp_path):
    """The whole defect in one assertion pair. The verdict is HONESTLY in-sync —
    the parked branch really is level with its own upstream — and that is
    exactly why the verdict cannot be the thing that carries this signal."""
    clone = _parked_clone(tmp_path)
    rec = PRF.freshness(clone, do_fetch=False)
    assert rec["verdict"] == "in-sync", (
        "fixture must reproduce the dangerous shape: a parked branch that is "
        "genuinely level with its own upstream. got verdict=%r detail=%r"
        % (rec["verdict"], rec["detail"]))
    assert rec["off_default"] is True
    assert rec["branch"] == "g-000-01-feature"
    assert rec["default_branch"] == "main", (
        "the default branch must be resolved independently of HEAD -- reading it "
        "off the current branch would make off_default structurally always False")


def test_an_undiscoverable_default_branch_yields_None_and_never_escalates(tmp_path):
    """Fail-safe direction. _default_branch returns "" for 'cannot tell', and its
    docstring forbids reading that as 'not the default branch'. A None here is
    what keeps the advisory quiet on repos it cannot judge; flipping this to a
    truthy default would make every unresolvable repo shout."""
    repo = tmp_path / "Solo"
    _init_repo(repo)
    # Rename away from both fallback candidates and give it no origin at all, so
    # symbolic-ref fails AND neither main nor master exists.
    subprocess.run(["git", "-C", str(repo), "branch", "-m", "trunk"], check=True)
    rec = PRF.freshness(repo, do_fetch=False)
    assert rec["default_branch"] == ""
    assert rec["off_default"] is None, (
        "an unknown default must be None (cannot tell), never True (would shout "
        "on every unresolvable repo) and never False (would silently vouch for "
        "a tree nobody checked); got %r" % (rec["off_default"],))


# ------------------------------------------------------------------- render()

def test_render_speaks_for_a_parked_repo_whose_verdict_is_clean(tmp_path):
    """THE regression pin. Pre-fix this returned "" — the advisory was silent on
    the one shape that produces a false 'this code does not exist'."""
    clone = _parked_clone(tmp_path)
    rec = PRF.freshness(clone, do_fetch=False)
    out = PRF.render([rec], 1)
    assert out != "", (
        "a parked-but-in-sync checkout must NOT render to the empty string -- "
        "silence here is the false-negative the goal was filed about")
    assert "OFF-DEFAULT" in out
    assert "g-000-01-feature" in out and "main" in out, (
        "the line must name BOTH the branch it is on and the branch it is not, "
        "or the reader cannot tell which ref to re-check; got %r" % (out,))


def test_render_does_not_contradict_itself_with_an_in_sync_line(tmp_path):
    """The noisy set now admits repos whose verdict is clean. A bare `else` in
    the verdict chain printed 'IN-SYNC <repo> — no detail' directly under the
    OFF-DEFAULT warning, which reads as a rebuttal of it."""
    clone = _parked_clone(tmp_path)
    out = PRF.render([PRF.freshness(clone, do_fetch=False)], 1)
    assert "IN-SYNC" not in out.upper().replace("OFF-DEFAULT", ""), (
        "an off-default repo must not also be rendered as IN-SYNC; got %r" % (out,))


def test_behind_and_off_default_are_reported_as_two_distinct_facts(tmp_path):
    """Verification outcome 2: 'behind on the default branch' and 'on a different
    branch entirely' have different remedies -- a pull fixes one and cannot fix
    the other -- so one line must not stand in for the other."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    clone = tmp_path / "Widget"
    _clone(origin, clone)
    _park(clone, "g-000-02-feature")
    # Advance the parked branch's own upstream so the checkout is BOTH behind
    # its tracked ref AND off the default branch.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", "g-000-02-feature",
                    str(origin), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "t"], check=True)
    p = other / "f.txt"
    p.write_text(p.read_text() + "x\n")
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-qm", "c2"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q"], check=True)

    rec = PRF.freshness(clone, do_fetch=True)
    assert rec["verdict"] == "behind", "fixture must be behind; got %r" % (rec["verdict"],)
    assert rec["off_default"] is True
    out = PRF.render([rec], 1)
    assert "OFF-DEFAULT" in out and "BEHIND" in out, (
        "both facts must appear; collapsing them loses the one the reader "
        "cannot recover from the commit counts. got %r" % (out,))


def test_a_clean_default_branch_repo_still_renders_nothing(tmp_path):
    """The advisory's founding property, re-pinned here because this change is
    the first to widen the noisy set: speaking on the clean path is how an
    advisory gets tuned out."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    clone = tmp_path / "Widget"
    _clone(origin, clone)
    assert PRF.render([PRF.freshness(clone, do_fetch=False)], 1) == ""


# -------------------------------------------------- corpus-freshness-precheck

def test_zero_matches_on_a_parked_checkout_is_not_clean():
    """The gating half. A zero read off origin/<feature-branch> says nothing
    about the default branch, so it must not reach 'SAFE TO ASSERT'."""
    rec = {"verdict": "in-sync", "matches": [], "fetch_verified": True,
           "off_default": True, "branch": "g-000-01-feature", "default_branch": "main"}
    assert CFP.verdict_for(rec) == "CANNOT-CHECK"


def test_matches_on_a_parked_checkout_stay_REVIEW_not_CANNOT_CHECK():
    """Ordering pin. The off-default check sits AFTER the matches check on
    purpose: a match proves the symbol exists as of that ref, which is more
    information than CANNOT-CHECK and blocks the assertion just as firmly.
    Moving the check earlier would trade a useful answer for a vaguer one and
    buy no safety."""
    rec = {"verdict": "in-sync", "matches": ["a.py:1:x"], "fetch_verified": True,
           "off_default": True, "branch": "g-000-01-feature", "default_branch": "main"}
    assert CFP.verdict_for(rec) == "REVIEW"


def test_off_default_None_or_False_leaves_the_verdict_untouched():
    """The unknown case must not be escalated, and neither must the ordinary
    one. `is True` rather than a truthiness test is what makes None inert."""
    base = {"verdict": "in-sync", "matches": [], "fetch_verified": True}
    assert CFP.verdict_for(dict(base, off_default=False)) == "CLEAN"
    assert CFP.verdict_for(dict(base, off_default=None)) == "CLEAN"
    assert CFP.verdict_for(dict(base)) == "CLEAN", (
        "a record with no off_default key at all -- every caller predating this "
        "field -- must be unaffected")


def test_precheck_render_says_which_ref_was_actually_grepped():
    """CANNOT-CHECK without the reason sends the reader to debug the tool. The
    line must name the parked branch, the default branch, and the ref the grep
    actually read."""
    rec = {"name": "Widget", "verdict": "in-sync", "matches": [],
           "fetch_verified": True, "off_default": True,
           "branch": "g-000-01-feature", "default_branch": "main",
           "grep_ref": "origin/g-000-01-feature", "head_date": "2026-08-16"}
    out = CFP.render([rec], "someSymbol")
    assert "OFF-DEFAULT" in out
    assert "origin/g-000-01-feature" in out, (
        "must name the ref the grep READ, not merely the branch -- the ref is "
        "what makes the zero unpersuasive; got %r" % (out,))
    assert "main" in out
