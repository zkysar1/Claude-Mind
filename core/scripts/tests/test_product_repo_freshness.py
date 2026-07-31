"""test_product_repo_freshness.py — the moment-of-action product-repo freshness advisory.

g-115-4041. `guard-1939` and `world/conventions/pre-execution.md` Step 2 both
already required a pull before touching a shared checkout, and neither fired on
the g-335-572 near-miss. The measured reason was not discipline: Step 2
enumerated its shared checkouts as two hardcoded absolute Windows roots, so on a
Linux box the MANDATORY step iterated an EMPTY SET and reported success while
the correct roots sat unread in `AGENT_WRITE_PATH`.

Every assertion below is anchored on that failure shape rather than on the happy
path, because the happy path was never what broke. The mutants used to prove
these tests are derived from the emitter's BRANCHES, not chosen to hit a count
(rb-5996): a mutation run is evidence about the mutant set you wrote, and its
reassurance scales with that set's coverage of the branches, not with its size.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
# .parent.parent, NOT .parent: core/scripts -> core -> PROJECT_ROOT. The first
# draft was one level short and resolved to `<root>/core`, which made
# test_the_mind_repo_itself_is_never_enumerated_as_a_product_repo compare
# against a path that is never enumerated -- so it passed under the very mutant
# it was written to kill. Caught only by re-running that mutant (guard-385);
# this is the `.parent`-re-derivation class CLAUDE.md keeps a dedicated audit
# grep for ().
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
SCRIPT = CORE_SCRIPTS / "product-repo-freshness.py"

if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))   # for _bash_helpers


def _load():
    spec = importlib.util.spec_from_file_location(
        "product_repo_freshness", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PRF = _load()


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


def _commit(repo: Path, msg: str) -> None:
    p = repo / "f.txt"
    p.write_text(p.read_text() + msg + "\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", msg], check=True)


# ------------------------------------------------- enumeration (the root cause)

def test_enumeration_is_routed_through_the_agent_write_path_ssot(tmp_path, monkeypatch):
    """The defect was a hand-maintained root list. Prove the roots come from
    AGENT_WRITE_PATH — including the multi-root ';' form — and not from a
    literal baked into this script."""
    a, b = tmp_path / "RepoA", tmp_path / "RepoB"
    _init_repo(a)
    _init_repo(b)
    monkeypatch.setattr(PRF, "enumerate_repos", PRF.enumerate_repos)
    monkeypatch.setenv("AGENT_WRITE_PATH", "%s;%s" % (a, b))
    # Force the env fallback path by making the conf lookup unavailable.
    monkeypatch.setitem(sys.modules, "_path_roots", None)
    found = {p.name for p in PRF.enumerate_repos()}
    assert found == {"RepoA", "RepoB"}, (
        "both ';'-separated AGENT_WRITE_PATH roots must be enumerated; got %r" % (found,))


def test_the_mind_repo_itself_is_never_enumerated_as_a_product_repo():
    """Constrains the `label == "AGENT_WRITE_PATH"` filter through the REAL
    `compute_allowed_roots` path, which the test above cannot reach (it
    monkeypatches `_path_roots` away to exercise the env fallback).

    PROJECT_ROOT is itself a git repo and is the FIRST root
    compute_allowed_roots returns, so dropping the label filter silently adds
    the Mind repo to the product-repo list -- and pre-execution.md Step 2 is
    explicit that the Mind repo is on a separate rail (iteration-commit /
    iteration-push at loop close), never a per-goal pull. A per-goal
    `git pull --ff-only` against the live multi-agent tree is exactly the
    write this framework coordinates carefully elsewhere.

    Added because the mutation run scored 9/9 while THIS branch was only
    incidentally covered: the label-filter mutant died to the unrelated
    empty-enumeration test. A kill by a test that was measuring something else
    is not evidence the branch is constrained (rb-5996).
    """
    enumerated = {str(p) for p in PRF.enumerate_repos()}
    assert str(PROJECT_ROOT) not in enumerated, (
        "the Mind repo (PROJECT_ROOT) must never appear in the product-repo "
        "enumeration -- it is on the loop-close rail, not the per-goal pull "
        "rail. Its presence means the AGENT_WRITE_PATH label filter was lost.")
    if enumerated:  # only meaningful where a conf actually names write roots
        assert all(not str(PROJECT_ROOT / "core") == e for e in enumerated)


def test_a_root_that_is_a_parent_of_repos_enumerates_its_children(tmp_path, monkeypatch):
    """Live confs carry BOTH shapes — a root that is itself a checkout, and a
    root holding many. Assuming one shape silently drops the other."""
    parent = tmp_path / "GitHub"
    _init_repo(parent / "One")
    _init_repo(parent / "Two")
    (parent / "NotARepo").mkdir()
    monkeypatch.setenv("AGENT_WRITE_PATH", str(parent))
    monkeypatch.setitem(sys.modules, "_path_roots", None)
    found = {p.name for p in PRF.enumerate_repos()}
    assert found == {"One", "Two"}, (
        "a parent root must yield its child repos and NOT the non-repo dir; got %r" % (found,))


def test_a_root_that_is_itself_a_repo_enumerates_itself(tmp_path, monkeypatch):
    solo = tmp_path / "Solo"
    _init_repo(solo)
    monkeypatch.setenv("AGENT_WRITE_PATH", str(solo))
    monkeypatch.setitem(sys.modules, "_path_roots", None)
    assert [p.name for p in PRF.enumerate_repos()] == ["Solo"]


def test_a_nonexistent_root_is_skipped_without_raising(tmp_path, monkeypatch):
    """The Windows literals were nonexistent on Linux. That must not crash --
    but per the test below it must also not pass silently."""
    real = tmp_path / "Real"
    _init_repo(real)
    monkeypatch.setenv("AGENT_WRITE_PATH", "%s;%s" % (tmp_path / "C:/nope", real))
    monkeypatch.setitem(sys.modules, "_path_roots", None)
    assert [p.name for p in PRF.enumerate_repos()] == ["Real"]


# ------------------------------------------- the vacuous all-clear (the defect)

def test_empty_enumeration_says_CANNOT_CHECK_instead_of_staying_silent(tmp_path):
    """THE test. Measured during this goal: with MIND_AGENT unset the
    production-shape call printed NOTHING and exited 0 -- byte-identical to
    'checked, all repos in sync'. A check whose silence can mean 'could not
    check' manufactures the confidence it should be withholding."""
    env = {"PATH": __import__("os").environ.get("PATH", ""),
           "AGENT_WRITE_PATH": str(tmp_path / "definitely-absent")}
    p = subprocess.run([sys.executable, str(SCRIPT), "--goal-id", "g-1-1"],
                       capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT))
    combined = p.stdout + p.stderr
    assert "CANNOT CHECK" in combined, (
        "an empty enumeration must announce itself; silence here is the exact "
        "vacuous all-clear this script exists to prevent. got stdout=%r stderr=%r"
        % (p.stdout, p.stderr))
    assert "NOT an all-clear" in combined, (
        "the message must say what the zero does NOT mean, not merely report a "
        "count -- a bare '0 repos' reads as good news. got %r" % (combined,))
    assert p.returncode == 0, "advisory must never block, even when it cannot check"


def test_clean_selection_prints_nothing_so_silence_keeps_one_meaning(tmp_path, monkeypatch):
    """The complement of the test above: when repos ARE examined and are in
    sync, output must be empty -- otherwise the advisory speaks on every clean
    path, gets tuned out, and stops being an advisory."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    clone = tmp_path / "Widget"
    _clone(origin, clone)
    rec = PRF.freshness(clone, do_fetch=False)
    assert rec["verdict"] == "in-sync"
    assert PRF.render([rec], 1) == "", (
        "an in-sync repo must render to the empty string; got %r" % (PRF.render([rec], 1),))


# --------------------------------------------------------- freshness verdicts

def test_behind_clone_is_reported_behind_with_the_commit_count(tmp_path):
    origin = tmp_path / "origin"
    _init_repo(origin)
    clone = tmp_path / "Widget"
    _clone(origin, clone)
    _commit(origin, "c2")
    _commit(origin, "c3")
    rec = PRF.freshness(clone, do_fetch=True)
    assert rec["verdict"] == "behind", rec
    assert rec["behind"] == 2, (
        "must report HOW FAR behind -- the g-335-572 near-miss was one commit, "
        "so a boolean stale/fresh answer would not have distinguished it: %r" % (rec,))
    assert rec["ahead"] == 0, rec


def test_ahead_clone_is_reported_ahead_not_lumped_into_stale(tmp_path):
    """behind and ahead demand OPPOSITE responses (pull vs push). Collapsing
    them into one 'out of sync' verdict would prescribe the wrong action."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    clone = tmp_path / "Widget"
    _clone(origin, clone)
    _commit(clone, "local")
    rec = PRF.freshness(clone, do_fetch=True)
    assert rec["verdict"] == "ahead", rec
    assert (rec["ahead"], rec["behind"]) == (1, 0), rec


def test_diverged_clone_is_reported_diverged_and_never_advises_reset(tmp_path):
    """A diverged tree may hold a same-box partner's in-flight work.
    pre-execution.md Step 2 forbids stash/reset there, so the banner must not
    suggest one."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    clone = tmp_path / "Widget"
    _clone(origin, clone)
    _commit(origin, "theirs")
    _commit(clone, "mine")
    rec = PRF.freshness(clone, do_fetch=True)
    assert rec["verdict"] == "diverged", rec
    assert rec["behind"] == 1 and rec["ahead"] == 1, rec
    banner = PRF.render([rec], 1)
    assert "do NOT stash or reset" in banner, banner
    for forbidden in ("git reset --hard", "git stash pop", "git checkout --"):
        assert forbidden not in banner, (
            "banner must not prescribe a destructive reconcile: %r" % (banner,))


def test_repo_without_upstream_reports_no_upstream_not_in_sync(tmp_path):
    """A local-only branch has no remote to be fresh against. Calling that
    'in-sync' would silently promote 'unknown' to 'good'."""
    solo = tmp_path / "Solo"
    _init_repo(solo)
    rec = PRF.freshness(solo, do_fetch=False)
    assert rec["verdict"] == "no-upstream", rec
    assert rec["verdict"] != "in-sync"
    assert PRF.render([rec], 1) != "", (
        "no-upstream must still surface -- it is a non-answer, and the whole "
        "point of this script is that non-answers must not read as all-clear")


def test_fetch_failure_still_counts_but_labels_the_numbers_as_stale(tmp_path):
    """Offline is the common case on a box with no network. Reporting the
    last-known counts beats reporting nothing -- but the caller must be told
    the numbers predate this moment, or a stale zero reads as fresh."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    clone = tmp_path / "Widget"
    _clone(origin, clone)
    _commit(origin, "c2")
    subprocess.run(["git", "-C", str(clone), "fetch", "-q"], check=True)
    subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin",
                    str(tmp_path / "vanished")], check=True)
    rec = PRF.freshness(clone, do_fetch=True)
    assert rec["behind"] == 1, (
        "the already-fetched remote ref must still be counted after a failed "
        "fetch: %r" % (rec,))
    assert "fetch failed" in rec["detail"], rec
    assert "not from now" in rec["detail"], (
        "the detail must say the counts are not current, not merely that fetch "
        "failed -- the caller needs to know what the number means: %r" % (rec,))


# --------------------------------------------------------------- selection

def test_selection_is_empty_when_the_goal_names_no_repo(tmp_path):
    """Cost control: 58 repos are enumerated live here, and fetching all of
    them per goal is a tax large enough that the step would get skipped."""
    assert PRF.select_repos([Path("/x/Widget")], "unrelated goal text") == []


def test_selection_matches_the_repo_basename_case_insensitively(tmp_path):
    repos = [Path("/x/Ayoai-Operator"), Path("/x/Other")]
    got = PRF.select_repos(repos, "Fix the ayoai-operator timeout budget")
    assert [p.name for p in got] == ["Ayoai-Operator"], got


def test_unreadable_goal_text_selects_nothing_rather_than_everything(tmp_path):
    """Fail-open must mean 'check nothing extra', never 'fetch all 58'."""
    assert PRF.select_repos([Path("/x/Widget")], "") == []


# ------------------------------------------------------------ never blocks

def test_every_invocation_shape_exits_zero(tmp_path):
    """Advisory posture. A non-zero exit here would abort Phase 3.9."""
    import os
    env = dict(os.environ)
    env["AGENT_WRITE_PATH"] = str(tmp_path)
    for argv in (["--list"], ["--goal-id", "g-1-1"], ["--repo", "/nonexistent"],
                 ["--list", "--json"], ["--goal-id", "g-1-1", "--no-fetch"]):
        p = subprocess.run([sys.executable, str(SCRIPT), *argv],
                           capture_output=True, text=True, env=env,
                           cwd=str(PROJECT_ROOT))
        assert p.returncode == 0, (argv, p.returncode, p.stderr)


def test_convention_step_2_no_longer_hardcodes_absolute_roots():
    """The convention half of the fix. Anchored on the CODE line (the enumerate
    command), not on prose that merely mentions the roots -- an unanchored grep
    matches the paragraph explaining the rule (guard-1099)."""
    # Resolve via _paths.WORLD_DIR, NOT a hand-rolled read of local-paths.conf.
    # The first draft of this test parsed the conf directly and skipped on every
    # box: `.mind-data/` outranks the conf in the resolution chain, so the conf's
    # WORLD_PATH is not where the world actually is. A silent skip here would
    # have proved nothing about the convention half of the fix -- the same
    # re-implemented-resolution mistake this whole goal is about.
    from _paths import WORLD_DIR
    world = Path(WORLD_DIR)
    if not (world / "conventions" / "pre-execution.md").is_file():
        import pytest
        pytest.skip("world conventions not reachable on this box")
    text = (world / "conventions" / "pre-execution.md").read_text(encoding="utf-8")
    step2 = text.split("## Step 2:", 1)[1].split("\n## ", 1)[0]
    code = [ln for ln in step2.splitlines() if ln.strip().startswith("Bash:")]
    assert any("product-repo-freshness.py --list" in ln for ln in code), (
        "Step 2 must ENUMERATE its shared checkouts via the SSOT; got %r" % (code,))
    assert any("--goal-id" in ln for ln in code), (
        "Step 2 must run the freshness advisory for the goal's own repos")


def test_phase_3_9_can_actually_reach_the_convention_that_carries_the_check():
    """The INTEGRATION PATH pin (sq-019), and the one link the tests above miss.

    Every other test here checks an endpoint: the script behaves, and the
    convention text carries the right instruction. Neither touches the link
    BETWEEN them -- `load-conventions.sh pre-execution`, which is how Phase 3.9
    reaches Step 2 at all (execute-protocol-digest.md:26-28). If that loader
    stops returning pre-execution.md, Step 2 is never read, the freshness call
    is never made, and BOTH endpoint tests stay green while the fix is dark.

    That is precisely the vacuity class this goal exists to fix, one level up:
    a step that is never reached is indistinguishable from a step that ran
    clean. So it gets a pin rather than an assumption.
    """
    import os
    import subprocess as sp
    from _bash_helpers import BASH  # never a bare "bash" argv[0] ()
    p = sp.run([BASH, "core/scripts/load-conventions.sh", "pre-execution"],
               capture_output=True, text=True, cwd=str(PROJECT_ROOT),
               env=dict(os.environ), timeout=60)
    out = (p.stdout or "").strip()
    if not out and "not reachable" in (p.stderr or ""):
        import pytest
        pytest.skip("world conventions not reachable on this box")
    assert "pre-execution.md" in out, (
        "load-conventions.sh must resolve 'pre-execution' to the convention file "
        "Phase 3.9 reads; without it Step 2 is unreachable and the freshness "
        "check silently never fires. stdout=%r stderr=%r" % (p.stdout, p.stderr))
    assert Path(out.splitlines()[0].strip()).is_file(), (
        "the loader returned a path that is not a readable file: %r" % (out,))


# ---------------------------------------- the SECOND vacuity (fresh-eyes finding)

def test_a_failed_goal_lookup_says_CANNOT_CHECK_not_silence(monkeypatch, capsys):
    """The layer BELOW the empty-enumeration guard, and the one that shipped.

    Enumeration succeeding keeps the first CANNOT CHECK quiet, so a goal lookup
    that FAILS yields an empty selection that renders as silence -- again
    byte-identical to 'all repos in sync'. Measured on the fleet's Windows box:
    _run_wrapper passed a .sh as argv[0] with no interpreter, which Windows
    cannot spawn; the bare except swallowed it and main() exited 0 printing
    nothing. Two guards are needed because either alone leaves a path where
    'could not check' looks exactly like 'checked and clean'.
    """
    monkeypatch.setattr(PRF, "_run_wrapper", lambda argv: (127, "", "OSError"))
    monkeypatch.setattr(PRF, "enumerate_repos", lambda: [Path("/x/Widget")])
    rc = PRF.main(["--goal-id", "g-1-1", "--no-fetch"])
    err = capsys.readouterr().err
    assert rc == 0, "advisory must never block"
    assert "CANNOT CHECK" in err, (
        "a failed goal lookup must announce itself; silence here is the exact "
        "vacuous all-clear one layer below the enumeration guard. stderr=%r" % (err,))
    assert "NOT an all-clear" in err, err


def test_a_goal_that_simply_names_no_repo_stays_silent(monkeypatch, capsys):
    """The complement, and the reason lookup_ok is not just 'selection empty'.

    A goal read SUCCESSFULLY that happens to name no repo is a real answer, not
    a failure -- it must stay quiet, or the advisory speaks on the common path
    and gets tuned out. Collapsing these two cases is what made the bug silent.
    """
    monkeypatch.setattr(PRF, "goal_text", lambda gid, src: ("unrelated text", True))
    monkeypatch.setattr(PRF, "enumerate_repos", lambda: [Path("/x/Widget")])
    rc = PRF.main(["--goal-id", "g-1-1", "--no-fetch"])
    cap = capsys.readouterr()
    assert rc == 0
    assert cap.out == "" and cap.err == "", (
        "a successful lookup naming no repo must be SILENT; got out=%r err=%r"
        % (cap.out, cap.err))


def test_goal_text_reports_lookup_failure_distinctly_from_goal_not_found():
    """lookup_ok must key on whether a READ succeeded, not on whether the goal
    was found -- a successfully-read aspiration that lacks the id is a real
    answer, and reporting it as a failure would make the advisory cry wolf."""
    import types
    saved = PRF._run_wrapper
    try:
        PRF._run_wrapper = lambda argv: (127, "", "spawn failed")
        assert PRF.goal_text("g-1-1", "world") == ("", False), "spawn failure -> lookup_ok False"
        PRF._run_wrapper = lambda argv: (0, '{"goals": [{"id": "g-9-9"}]}', "")
        assert PRF.goal_text("g-1-1", "world") == ("", True), (
            "a successful read that does not contain the goal is a real answer, "
            "not a lookup failure")
    finally:
        PRF._run_wrapper = saved


def test_run_wrapper_does_not_exec_a_shell_script_without_an_interpreter():
    """Anchored on the CODE, because the bug is invisible on Linux (the exec bit
    and shebang make the bad form work) and only fails on the box nobody tests
    on -- the guard-920 'platform is part of the production shape' trap."""
    import ast
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_wrapper")
    # Strip the docstring via AST, NOT a line filter. The first draft filtered
    # lines containing triple quotes, which removes only the DELIMITERS -- the
    # docstring PROSE survived, and it says "_runtime_bash.BASH", so the
    # assertion passed against the very mutant written to kill it. That is
    # guard-1099 verbatim: an unanchored source grep matching the text that
    # DESCRIBES the rule instead of the code that implements it.
    body_nodes = [n for n in fn.body
                  if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                          and isinstance(n.value.value, str))]
    joined = "\n".join(ast.unparse(n) for n in body_nodes)
    assert "_runtime_bash" in joined or "BASH" in joined, (
        "_run_wrapper must route .sh through the sanctioned bash resolver "
        "(_runtime_bash.BASH); executing a .sh as argv[0] works only where the "
        "exec bit and shebang are honored, which Windows does not do. Note "
        "guard-580's gate greps for [\"bash\", ...] literals and does NOT catch "
        "the no-interpreter form. body=%r" % (joined[:400],))
