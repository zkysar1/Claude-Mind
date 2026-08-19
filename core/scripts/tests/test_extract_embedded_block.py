"""Dogfood suite for core/scripts/extract-embedded-block.py (gap-042 forge).

WHY THIS FILE EXISTS. The script is a VERIFIER — it emits a verdict other code
trusts — so forge-skill Step 3.6 requires proof it DISCRIMINATES before the
skill is registered. A verifier returning the same verdict on a PASS fixture and
a FAIL fixture is vacuous (guard-1220, rb-4133), and the whole point of this
primitive is that three of its four verdicts are the ones a naive PASS-prefix
test gets wrong.

WHAT THE FIXTURE SEAM EXCLUDES (guard-1462 — a fixture's injection point is a
silent scope declaration, so name it). Fixtures here supply a synthetic HOST
FILE and exercise capture + execution + classification. Structurally
unfalsifiable by any fixture in this file:

  * `read_host` git plumbing for --from staged / --from <ref>. `git show` is
    stubbed nowhere; the worktree path is what these fixtures drive. Covered
    instead by a LIVE run in test_live_worktree_and_git_ref_agree.
  * The real /verify-learning corpus grammar. A synthetic host file proves the
    rule as WRITTEN, not that it matches the file the rule was derived from.
    Covered instead by test_live_corpus_capture_is_indent_based, which asserts
    against the actual SKILL.md.

Both excluded layers therefore get a live assertion rather than a fixture, which
is the g-250-269 lesson: a green fixture suite said nothing about a defect that
lived upstream of the injection point.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "core" / "scripts" / "extract-embedded-block.py"
LIVE_CORPUS = REPO / ".claude" / "skills" / "verify-learning" / "SKILL.md"

# A synthetic host file in the `check` grammar. Every structural line sits at
# indent 3; every continuation line is column-0 anchored. Both facts are the
# contract the capture rule keys on.
HOST = "\n".join([
    "   # a comment at structural indent",
    '   Bash (emits-pass): echo "PASS: all good"',
    "   Check: this prose line must NOT be swallowed into the body above",
    "   -> expect PASS",
    "",
    '   Bash (emits-fail): echo "FAIL: something broke"',
    "   Check: trailing prose after the failing check",
    "",
    '   Bash (emits-ratchet-stable): echo "[orphan-ratchet] STABLE: 0 orphans"',
    "",
    '   Bash (emits-ratchet-regressed): echo "[orphan-ratchet] REGRESSED: 4 orphans"',
    "",
    "   Bash (crashes): bash -c 'exit 7'",
    "",
    "   Bash (silent-rc0): true",
    "",
    '   Bash (multi-line): py -3 -c "',
    "import sys",
    "print('PASS: multi-line body executed')",
    '"',
    "   Check: prose after the multi-line check",
    "",
])


def run(args, host_path=None, cwd=None):
    argv = [sys.executable, str(SCRIPT), "--json"]
    if host_path:
        argv += ["--file", str(host_path)]
    argv += args
    p = subprocess.run(argv, capture_output=True, text=True, cwd=str(cwd or REPO))
    payload = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {}
    return p.returncode, payload, p.stderr


def host_file(tmp_path):
    f = tmp_path / "host-skill.md"
    f.write_text(HOST, encoding="utf-8")
    return f


# ---------------------------------------------------------------- capture ----

def test_one_line_check_does_not_swallow_trailing_prose(tmp_path):
    """The over-capture footgun, on the token gap-042 does NOT name.

    gap-042's stated terminator set is (Bash \\(|#|->). `   Check:` matches
    none of them, and 25 of 318 live checks are followed by one -- so the
    stated rule swallows prose into 7.9% of bodies. The indent rule does not.
    """
    _, payload, _ = run(["--name", "emits-pass"], host_file(tmp_path))
    assert payload["continuation_lines"] == 0
    assert "Check:" not in payload["body"]
    assert "-> expect" not in payload["body"]
    assert payload["body"].strip() == 'echo "PASS: all good"'


def test_multi_line_check_captures_column_zero_continuations(tmp_path):
    """The under-capture footgun: continuation lines sit at column 0."""
    _, payload, _ = run(["--name", "multi-line"], host_file(tmp_path))
    assert payload["continuation_lines"] == 3
    assert "import sys" in payload["body"]
    assert payload["body"].count('"') == 2, "quotes must balance or the body cannot run"
    assert "Check:" not in payload["body"]


def test_list_enumerates_every_check(tmp_path):
    _, payload, _ = run(["--list"], host_file(tmp_path))
    names = [c["name"] for c in payload["checks"]]
    assert names == ["emits-pass", "emits-fail", "emits-ratchet-stable",
                     "emits-ratchet-regressed", "crashes", "silent-rc0", "multi-line"]


# ------------------------------------------------------------- verdicts ------
# Each case below must produce a DISTINCT verdict from its neighbours. That is
# the discrimination proof: a classifier that collapsed any two of these would
# be the exact defect this primitive exists to prevent.

def test_pass_fixture(tmp_path):
    rc, payload, _ = run(["--name", "emits-pass", "--run"], host_file(tmp_path))
    assert payload["verdict"] == "PASS"
    assert rc == 0


def test_fail_fixture(tmp_path):
    rc, payload, _ = run(["--name", "emits-fail", "--run"], host_file(tmp_path))
    assert payload["verdict"] == "FAIL"
    assert rc == 1


def test_ratchet_stable_is_pass_not_fail(tmp_path):
    """gap-042 footgun 3: ratchet vocabulary is a legitimate PASS."""
    rc, payload, _ = run(["--name", "emits-ratchet-stable", "--run"], host_file(tmp_path))
    assert payload["verdict"] == "PASS", "STABLE must not be scored as a failure"
    assert rc == 0


def test_ratchet_regressed_is_fail(tmp_path):
    rc, payload, _ = run(["--name", "emits-ratchet-regressed", "--run"], host_file(tmp_path))
    assert payload["verdict"] == "FAIL"
    assert rc == 1


def test_crash_is_error_not_fail(tmp_path):
    """: 5 of 6 apparent reds were harness artifacts, not assertions.

    Collapsing ERROR into FAIL is what produced that false report, so these two
    verdicts must stay distinguishable.
    """
    rc, payload, _ = run(["--name", "crashes", "--run"], host_file(tmp_path))
    assert payload["verdict"] == "ERROR"
    assert rc == 2
    assert "harness artifact" in payload["reason"]


def test_silent_rc0_is_indeterminate_not_pass(tmp_path):
    """rb-5871 / guard-1977: a check that DECLINES to run reports success by
    default, so green is its only observable state. Refuse to render that PASS.
    """
    rc, payload, _ = run(["--name", "silent-rc0", "--run"], host_file(tmp_path))
    assert payload["verdict"] == "INDETERMINATE"
    assert rc == 3
    assert payload["verdict"] != "PASS"


def test_multi_line_body_actually_executes(tmp_path):
    rc, payload, _ = run(["--name", "multi-line", "--run"], host_file(tmp_path))
    assert payload["verdict"] == "PASS"
    assert "multi-line body executed" in payload["stdout"]


def test_all_seven_fixtures_span_four_distinct_verdicts(tmp_path):
    """Discrimination proof.

    Deliberately NOT the suite's only guard (guard-1793): an aggregate
    summarises one axis and reads green through a defect on another. Every
    verdict above is pinned individually; this asserts the classifier's RANGE,
    which no single per-fixture test can.
    """
    h = host_file(tmp_path)
    verdicts = {n: run(["--name", n, "--run"], h)[1]["verdict"] for n in
                ["emits-pass", "emits-fail", "emits-ratchet-stable",
                 "emits-ratchet-regressed", "crashes", "silent-rc0", "multi-line"]}
    assert set(verdicts.values()) == {"PASS", "FAIL", "ERROR", "INDETERMINATE"}


# ------------------------------------------------------- shell grammar -------

SH_HOST = "\n".join([
    "#!/usr/bin/env bash",
    "RESULT=$(echo \"$IN\" | python3 -c '",
    "import json, sys",
    "d = json.load(sys.stdin)",
    'print("PASS: saw %d envs" % len(d))',
    "'",
    ")",
])

SH_HOST_APOSTROPHE = SH_HOST.replace(
    'print("PASS: saw %d envs" % len(d))',
    'print("PASS: it\'s fine")',
)


def test_shell_grammar_extracts_between_markers(tmp_path):
    f = tmp_path / "probe.sh"
    f.write_text(SH_HOST, encoding="utf-8")
    _, payload, _ = run(["--grammar", "shell", "--open-marker", "python3 -c '"], f)
    assert "import json, sys" in payload["body"]
    assert "RESULT=$(" not in payload["body"], "must not capture the opening line"
    assert ")" != payload["body"].strip().splitlines()[-1], "must stop at the close line"


def test_shell_grammar_runs_with_stdin(tmp_path):
    f = tmp_path / "probe.sh"
    f.write_text(SH_HOST, encoding="utf-8")
    rc, payload, _ = run(["--grammar", "shell", "--open-marker", "python3 -c '",
                          "--run", "--stdin-json", json.dumps([1, 2, 3])], f)
    assert payload["verdict"] == "PASS"
    assert "saw 3 envs" in payload["stdout"]
    assert rc == 0


def test_apostrophe_assertion_discriminates(tmp_path):
    """The assertion must be RED on a body with an apostrophe and GREEN without.

    Both directions are required: an assertion that only ever passes is
    indistinguishable from one that cannot fire.
    """
    clean = tmp_path / "clean.sh"
    clean.write_text(SH_HOST, encoding="utf-8")
    rc_clean, pay_clean, _ = run(
        ["--grammar", "shell", "--open-marker", "python3 -c '",
         "--assert-no-apostrophe"], clean)
    assert rc_clean == 0
    assert pay_clean.get("apostrophe_offenders") == []

    dirty = tmp_path / "dirty.sh"
    dirty.write_text(SH_HOST_APOSTROPHE, encoding="utf-8")
    rc_dirty, pay_dirty, _ = run(
        ["--grammar", "shell", "--open-marker", "python3 -c '",
         "--assert-no-apostrophe"], dirty)
    assert rc_dirty == 1
    assert pay_dirty["verdict"] == "FAIL"
    assert pay_dirty["apostrophe_offenders"], "offending line numbers must be reported"


# ------------------------------------------------------------ live layer -----
# These two cover what the fixture seam above structurally cannot.

def test_live_corpus_capture_is_indent_based():
    """Assert against the REAL corpus, not a synthetic restatement of the rule.

    Pins the measured 2026-07-30 finding: `   Check:` prose follows one-line
    checks, so a token-based terminator over-captures where the indent rule
    does not.
    """
    if not LIVE_CORPUS.exists():
        import pytest
        pytest.skip("verify-learning SKILL.md not present on this box")
    _, payload, _ = run(["--name", "no-stray-roots"], LIVE_CORPUS)
    assert payload["continuation_lines"] == 0
    assert "Check:" not in payload["body"]
    assert "PASS:" in payload["body"] and "FAIL:" in payload["body"]

    _, multi, _ = run(["--name", "progression-field-tuple-sync"], LIVE_CORPUS)
    assert multi["continuation_lines"] > 10, "multi-line body must not truncate"
    assert multi["body"].count('"') % 2 == 0, "unbalanced quotes = under-capture"


def test_quote_state_scanner_beats_naive_parity():
    """Pins the two shapes a `count('"') % 2` test gets wrong.

    Substituting the counter for the scanner was measured to make 5 corpus
    checks over-capture, so both directions are pinned here rather than left
    to the corpus test to catch indirectly.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("eeb", str(SCRIPT))
    eeb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eeb)

    # ODD double-quote count, but every quote is inside single quotes -> closed.
    fooled_high = """grep -qF '_commit_sha="$(printf' x.sh && echo "PASS: ok\""""
    assert fooled_high.count('"') % 2 == 1, "fixture must fool the naive counter"
    assert eeb.ends_inside_quote(fooled_high) is False

    # Genuinely open double quote -> the multi-line continuation case.
    assert eeb.ends_inside_quote('py -3 -c "') is True
    # Escaped quote inside a double-quoted string must not close it.
    assert eeb.ends_inside_quote('echo "a \\" b') is True
    assert eeb.ends_inside_quote('echo "a \\" b"') is False


def test_live_corpus_extracts_executable_bodies():
    """Ratchet on the real corpus: captured bodies must parse as shell.

    A capture that produces unparseable text is indistinguishable from a
    failing check, which is the confusion this primitive removes. The residual
    is bounded AND characterised: every non-parsing body must have zero
    continuation lines, i.e. the extractor took the source line verbatim and
    the defect is in the corpus, not in the capture. That distinction is the
    assertion -- a raw pass-rate alone would let a capture regression hide
    inside the allowance.
    """
    import importlib.util
    import pytest
    if not LIVE_CORPUS.exists():
        pytest.skip("verify-learning SKILL.md not present on this box")
    spec = importlib.util.spec_from_file_location("eeb", str(SCRIPT))
    eeb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eeb)
    sys.path.insert(0, str(REPO / "core" / "scripts"))
    from _runtime_bash import BASH  # bare "bash" argv[0] is refused (guard-580)

    # The corpus moved to a registry on 2026-08-18 (). This test's
    # stated intent is "assert against the REAL corpus", so it follows the
    # corpus rather than the 175-line skill file that no longer hosts checks.
    # Reading LIVE_CORPUS directly here bypassed read_host, so the production
    # redirect could not cover it.
    import _verify_corpus
    text = _verify_corpus.corpus_text()
    names = [c["name"] for c in eeb.list_checks(text)]
    assert len(names) > 250, "corpus shrank unexpectedly — re-baseline before trusting this"

    unparseable = []
    for n in names:
        code, _, cont, _ = eeb.extract_check(text, n)
        p = subprocess.run([BASH, "-n", "-c", code], capture_output=True, text=True)
        if p.returncode != 0:
            unparseable.append((n, cont))

    # Measured 2026-07-30 (cc-02, Linux 6.8.0-136-generic): 6 of 318.
    assert len(unparseable) <= 8, (
        "capture regression: %d unparseable bodies (baseline 6 of 318): %s"
        % (len(unparseable), [n for n, _ in unparseable]))
    multi = [n for n, cont in unparseable if cont > 0]
    assert not multi, (
        "these failed AND spanned continuation lines, so the capture is at "
        "fault rather than the corpus: %s" % multi)


def test_live_worktree_and_git_ref_agree(tmp_path):
    """Covers read_host's git plumbing, which no synthetic-host fixture reaches.

    A committed, unmodified file must extract identically from the worktree and
    from HEAD. If they diverge, the git path is broken -- and the authoring-time
    consumer depends on that path being the only difference.

    Uses a THROWAWAY git repo rather than the live corpus. It used to compare
    worktree-vs-HEAD on verify-learning/SKILL.md, and g-115-6689 made that
    subject uniquely unfit: the worktree read of THAT path is redirected to the
    check registry while a git-ref read still returns the file, so the two
    disagree by design and the check name is not in HEAD's copy at all. A temp
    repo restores what the test was actually for — real `git show` plumbing,
    against bytes that are identical on both sides — and removes the coupling
    to a file whose two read paths are deliberately different.
    """
    import pytest
    repo = tmp_path / "repo"
    repo.mkdir()
    host = repo / "host-skill.md"
    host.write_text(HOST, encoding="utf-8")
    env_git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    for cmd in (["init", "-q"], ["add", "host-skill.md"],
                ["commit", "-q", "-m", "fixture"]):
        r = subprocess.run(env_git + cmd, cwd=str(repo),
                           capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"git unavailable for the plumbing fixture: {r.stderr.strip()[:120]}")

    _, wt, _ = run(["--name", "multi-line", "--file", "host-skill.md",
                    "--from", "worktree"], cwd=repo)
    _, head, _ = run(["--name", "multi-line", "--file", "host-skill.md",
                      "--from", "HEAD"], cwd=repo)
    assert wt["body"] == head["body"], "git-ref read diverged from the worktree read"
    assert head["source"] == "HEAD"
    assert head["continuation_lines"] == 3, "the git path must capture continuations too"


# ------------------------------------------------- extraction-failure rc ----

def test_extraction_failures_exit_error_not_fail(tmp_path):
    """Every extraction failure is ERROR (2), never FAIL (1).

    Found by Phase 5 verify of the forging goal, in this tool, against its own
    documented contract. All eight failure paths used `raise SystemExit("msg")`,
    and Python maps a STRING argument to exit code 1 -- which is this tool's
    FAIL code. So a check name that does not resolve reported the same verdict
    as an assertion that fired: precisely the ERROR/FAIL merge the tool exists
    to prevent (g-115-3280), shipped inside the tool built to prevent it.

    The paths are indistinguishable from the caller's side, which is what makes
    the collapse dangerous rather than merely untidy -- a typo'd check name
    reads as a red.
    """
    host = host_file(tmp_path)
    cases = [
        (["--name", "no-such-check-zzz"], host, "name does not resolve"),
        (["--grammar", "shell", "--open-marker", "x"], None, "missing --file"),
        (["--name", "emits-pass", "--from", "no-such-ref-zzz"], host, "bad git ref"),
        (["--grammar", "shell", "--open-marker", "ABSENT-MARKER"], host, "marker absent"),
        # The sibling the SystemExit sweep could not find, because this path
        # never used SystemExit at all -- a bare open() let OSError escape as a
        # raw traceback and exit 1. Found by the fresh-eyes pass, not the fix.
        (["--name", "emits-pass", "--file", "no/such/host.md"], None, "host file unreadable"),
    ]
    for args, hp, label in cases:
        rc, _, _ = run(args, hp)
        assert rc == 2, "%s must exit ERROR(2), got %d" % (label, rc)


def test_error_exit_does_not_cannibalise_fail_or_pass(tmp_path):
    """Two-way proof: routing failures to 2 must not collapse 1 and 0 into it.

    Without this, `die()` returning 2 unconditionally would satisfy the test
    above while destroying the distinction it is meant to protect.
    """
    host = host_file(tmp_path)
    assert run(["--name", "emits-pass", "--run"], host)[0] == 0
    assert run(["--name", "emits-fail", "--run"], host)[0] == 1
    assert run(["--name", "crashes", "--run"], host)[0] == 2
    assert run(["--name", "silent-rc0", "--run"], host)[0] == 3
