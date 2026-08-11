"""Suite for core/scripts/embedded-python-audit.py ().

This file is BOTH the positive control and the wiring. The audit is a corpus
guard, so "it runs" has to mean something a suite can prove: these tests fail if
the audit stops discriminating, and `test_live_corpus_baseline_is_clean` fails if
anyone lands an uncompilable embedded block.

The load-bearing test is `test_even_apostrophe_caught_where_bash_n_passes`. That
is the case `bash -n` cannot see (guard-504) and therefore the only one that
justifies the audit existing at all -- it asserts bash -n exits 0 on the same
input the audit rejects. If that test ever passes vacuously, the audit is
redundant with `bash -n` and should be deleted rather than kept.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# This file sits at core/scripts/tests/, one level deeper than the audit itself,
# so the audit's own three-parent chain to PROJECT_ROOT is one short from here.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parents[2]
AUDIT = REPO / "core" / "scripts" / "embedded-python-audit.py"

# Resolve bash via the shared helper, never a bare "bash" argv[0]: on win32 a
# bare argv[0] resolves through System32 and reaches the WSL launcher, which can
# hang forever (guard-580). Paths cross as POSIX strings because bash silently
# strips the backslashes of a str(WindowsPath) (guard-581).
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402


def run_audit(root):
    """Run the audit scoped to one root. Returns (rc, stdout)."""
    p = subprocess.run(
        [sys.executable, AUDIT.as_posix(), "--root", Path(root).as_posix(), "--json"],
        capture_output=True, text=True,
    )
    return p.returncode, p.stdout


def bash_n(path):
    """Return bash -n's exit code for a script, or None if bash is absent."""
    try:
        p = subprocess.run([BASH, "-n", Path(path).as_posix()],
                           capture_output=True, text=True)
    except FileNotFoundError:
        return None
    return p.returncode


def write(root, name, text):
    f = root / name
    f.write_text(text, encoding="utf-8")
    return f


def test_clean_block_passes(tmp_path):
    write(tmp_path, "clean.sh", """#!/usr/bin/env bash
out="$(py -3 -c '
import json
print(json.dumps({"ok": True}))
')"
""")
    rc, _ = run_audit(tmp_path)
    assert rc == 0


def test_odd_apostrophe_caught(tmp_path):
    """One apostrophe truncates the source; the remnant must not compile."""
    write(tmp_path, "odd.sh", """#!/usr/bin/env bash
out="$(py -3 -c '
import json
# the sweep's output is truncated right here
print(json.dumps({"ok": True}))
')"
""")
    rc, out = run_audit(tmp_path)
    assert rc == 1, out
    assert "odd.sh" in out


def test_even_apostrophe_caught_where_bash_n_passes(tmp_path):
    """The case that justifies this audit: bash -n is GREEN, the python is wrong.

    An even number of apostrophes closes and REOPENS the bash string, so bash
    sees a syntactically valid script while the python that reaches the
    interpreter has been silently cut short.
    """
    f = write(tmp_path, "even.sh", """#!/usr/bin/env bash
out="$(py -3 -c '
import json
# do not trust the sweep's output, and don't skip this
print(json.dumps({"ok": True}))
')"
""")
    rc_bash = bash_n(f)
    if rc_bash is not None:
        assert rc_bash == 0, (
            "premise broken: bash -n rejected this input, so it no longer "
            "demonstrates the blind spot the audit exists to cover"
        )
    rc, out = run_audit(tmp_path)
    assert rc == 1, out
    assert "even.sh" in out


def test_comment_opener_not_treated_as_block(tmp_path):
    """Prose ABOUT an embedded block must not be extracted as one.

    This corpus documents its own hazards in comments, so a comment containing
    the opener pattern plus an unbalanced quote is common. Treating it as code
    produced 2 of the 6 findings in the original baseline, both false.
    """
    write(tmp_path, "commented.sh", """#!/usr/bin/env bash
# the writer is located by regex-matching its `py -3 -c '` prefix -- this
# comment is prose, not code, and it isn't python at all
echo ok
""")
    rc, out = run_audit(tmp_path)
    assert rc == 0, out


def test_quarantine_marker_in_multiline_comment_block(tmp_path):
    """The opt-out must be found anywhere in the comment block above the opener.

    A quarantine worth having carries a rationale spanning several lines, which
    puts the marker on the FIRST of them. Checking only the immediately-preceding
    line made the opt-out silently inert.
    """
    write(tmp_path, "quar.sh", """#!/usr/bin/env bash
# embedded-python-audit: skip -- deliberate fixture, see tracking ref
# second line of rationale
# third line of rationale
cat > broken.py <<'PYEOF'
def load_all(:
    pass
PYEOF
""")
    rc, out = run_audit(tmp_path)
    assert rc == 0, out
    assert '"quarantined": 1' in out


def test_all_blocks_in_multiblock_file_are_checked(tmp_path):
    """Enumeration is the capability a first-match extractor cannot provide.

    55% of this corpus sits in multi-block files, so a scanner that stops at the
    first opener would report clean on a file whose SECOND block is broken.
    """
    write(tmp_path, "multi.sh", """#!/usr/bin/env bash
a="$(py -3 -c '
print("first block is fine")
')"
b="$(py -3 -c '
def broken(:
    pass
')"
""")
    rc, out = run_audit(tmp_path)
    assert rc == 1, out
    assert '"total_blocks": 2' in out, out


def test_backslash_continued_heredoc_opener_body_offset(tmp_path):
    r"""A `\`-continued opener carries its continuation BEFORE the heredoc body.

    bash begins a here-document at the first UNESCAPED newline, so the `|| \`
    fallback line belongs to the COMMAND, not to the python. Starting the body
    one physical line after the opener swallows that indented fallback as
    python line 1 and reports a phantom "unexpected indent" — a false FAIL on
    a block bash executes correctly. Measured 2026-08-10 (cc-05): this shape is
    1 of 133 python-tagged heredoc openers across both scanned roots, and it
    was the sole finding holding test_live_corpus_baseline_is_clean RED.
    """
    f = write(tmp_path, "continued.sh", r"""#!/usr/bin/env bash
emit() {
    python3 - "$V" <<'PY' 2>/dev/null || \
      printf 'FALLBACK\n'
import json,sys
print(json.dumps({"v": sys.argv[1]}))
PY
}
""")
    assert bash_n(f) in (0, None), "fixture must itself be valid bash"
    rc, out = run_audit(tmp_path)
    assert rc == 0, out


def test_backslash_continued_heredoc_still_catches_real_defect(tmp_path):
    r"""The offset fix must not turn the continued-opener shape into a blind spot.

    Same `\`-continued opener, genuinely uncompilable body. The block must
    still FAIL, and at the block-relative line of the REAL defect — the line
    number is what proves the body offset is correct rather than merely
    permissive (a scanner that skipped the block entirely would also pass the
    positive test above). guard-1655: the control must reproduce the defect,
    not merely disable the fix.
    """
    write(tmp_path, "continued-bad.sh", r"""#!/usr/bin/env bash
emit() {
    python3 - "$V" <<'PY' 2>/dev/null || \
      printf 'FALLBACK\n'
import json,sys
def broken(
print("never")
PY
}
""")
    rc, out = run_audit(tmp_path)
    assert rc == 1, out
    assert "block-relative line 2" in out, out


def test_live_corpus_baseline_is_clean():
    """The standing guard: the real tree must have no uncompilable blocks."""
    p = subprocess.run([sys.executable, AUDIT.as_posix()], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_live_corpus_discovery_is_not_vacuous():
    """A deriving scanner that matches NOTHING makes every other test trivially true.

    Carried forward from test_bash_embedded_python_blocks.py, which this suite
    supersedes. It catches a REGEX collapse -- an opener derivation that stops
    matching -- and nothing else.

    IT IS NOT THIS SUITE'S ANTI-VACUITY GUARD, and describing it as one is the
    error guard-1793 names: an aggregate chosen as the vacuity proof is often
    INVARIANT under the exact defect it is trusted to catch, because it
    summarises a different axis. This floor summarises BLOCK COUNT; the defect
    that actually occurred collapsed ROOT COVERAGE. Measured 2026-08-10 (zeta,
    hostname cc-02, uname -r 6.8.0-136-generic): a bare invocation scanned 137
    blocks over 1 of 2 roots -- 50.4% of the corpus unseen -- and 137 > 100, so
    this assertion passed green straight through it.

    Do NOT "fix" that by raising the floor to sit above the single-root count:
    the number is box-dependent and tuning a threshold to reach a known value is
    guard-2950. The root axis is asserted structurally, and separately, by
    test_resolved_scope_does_not_depend_on_ambient_world_path below.
    """
    p = subprocess.run([sys.executable, AUDIT.as_posix(), "--json"],
                       capture_output=True, text=True)
    data = json.loads(p.stdout)
    assert data["total_blocks"] > 100, (
        "discovery collapsed to %d blocks -- the opener derivation is probably "
        "broken, which would make every other assertion here vacuous"
        % data["total_blocks"]
    )


def _audit_json(extra_env=None, drop_env=()):
    """Run the audit over the LIVE corpus with a controlled environment."""
    env = dict(os.environ)
    for k in drop_env:
        env.pop(k, None)
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, AUDIT.as_posix(), "--json"],
                       capture_output=True, text=True, env=env)
    assert p.returncode in (0, 1), p.stdout + p.stderr
    return json.loads(p.stdout)


def _world_scripts_from_authority():
    """Independent oracle: the world/scripts path the SSOT names, or None.

    This is NOT a self-supplied expectation (guard-1220 / illusion #2). The
    code under test is the AUDIT; `_paths.py` is the authority the audit is
    required to agree with, and every other framework consumer resolves through
    it. Deriving the expected root from the audit's own resolver is what would
    make the assertion circular -- this deliberately does not.
    """
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location(
            "_t_paths", str(REPO / "core" / "scripts" / "_paths.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        wd = getattr(mod, "WORLD_DIR", None)
        return Path(wd) / "scripts" if wd else None
    except Exception:
        return None


def test_resolved_scope_does_not_depend_on_ambient_world_path():
    r"""The audit's SCOPE must be a property of the repo, not of the launcher.

    `WORLD_PATH` is exported by `_paths.sh`. While the audit read only that env
    var, its coverage depended on HOW IT WAS STARTED: the wrapper sources the
    resolver and got 2 roots; a bare `py -3 embedded-python-audit.py` -- which
    is exactly how `test_live_corpus_baseline_is_clean` above invokes it -- got
    1 root and still printed "clean" at rc=0. Measured 2026-08-10 (zeta,
    hostname cc-02, uname -r 6.8.0-136-generic): 137 blocks / 1 root vs 276
    blocks / 2 roots, so 139 of 276 blocks (50.4%) were unscanned by the
    standing guard while it reported a clean corpus.

    That is illusion #7 in the `test-coverage-illusions` node -- green
    conditional on the runner: an assertion depending on ambient state that no
    fixture establishes. The remedy is the one that entry prescribes, applied to
    the scanner rather than to the test: establish BOTH arms and require them to
    agree, so nothing is left to the machine.

    THE ORACLE IS THE AUTHORITY, NOT THE OTHER ARM -- and the first version of
    this test got that wrong in an instructive way. It compared a WORLD_PATH-set
    run against a WORLD_PATH-dropped run and required them to be equal. That
    reads as a clean differential and is VACUOUS here: pytest itself runs without
    `WORLD_PATH` exported, so the "with" arm never had it and both arms were the
    same object. Reverting the production fix left it GREEN. That is illusion #17
    (two subjects aliased by ambient state) reproduced inside the fix for
    illusion #7 -- the arms must be ESTABLISHED, never assumed. Caught only
    because the mutation proof below was actually run.

    So the assertion is: whatever `_paths.py` says the world root is, a bare
    invocation with NO ambient env must have scanned it. Both branches assert --
    a skip on a world-less clone would be illusion #13 (green because the
    population is empty).
    """
    bare = _audit_json(drop_env=("WORLD_PATH", "MIND_AGENT"))
    world_scripts = _world_scripts_from_authority()

    if world_scripts is not None and world_scripts.exists():
        assert bare["world_root_state"] == "scanned", (
            "the authority names %s and it exists, but a bare invocation "
            "reported world_root_state=%r -- scope has gone ambient again, and "
            "this is how the standing guards invoke the audit"
            % (world_scripts, bare["world_root_state"])
        )
        assert str(world_scripts) in bare["roots_scanned"], (
            "authority names %s; bare run scanned %r"
            % (world_scripts, bare["roots_scanned"])
        )
        # The env override must still AGREE with the authority rather than
        # widening or narrowing it. This arm is established explicitly -- that
        # is the whole lesson above.
        with_env = _audit_json(extra_env={"WORLD_PATH": str(world_scripts.parent)})
        assert with_env["roots_scanned"] == bare["roots_scanned"], (
            "WORLD_PATH override disagrees with the authority: %r vs %r"
            % (with_env["roots_scanned"], bare["roots_scanned"])
        )
    else:
        # No world configured on this box. The tool must SAY so rather than
        # printing an unqualified "clean" over a silently narrowed corpus
        # (guard-3097), and must never refuse on it (clause 1).
        assert bare["world_root_state"] in ("unresolved", "configured-absent"), (
            bare["world_root_state"]
        )
        assert len(bare["roots_scanned"]) == 1, bare["roots_scanned"]


def test_explicit_root_replaces_defaults_and_never_appends_the_world_root(tmp_path):
    """Negative control for the coverage assertion above (guard-1836).

    A coverage assertion has zero discriminating power against OVER-matching: a
    resolver that appended the world root unconditionally would satisfy the
    differential test perfectly, while silently un-hermeticizing every fixture
    case in this file -- their counts would include the live corpus and no
    assertion here could hold. This names what must NOT be scanned.

    Mutation proof in both directions (guard-1836): removing the world root from
    the default set reddens the differential test above; widening `iter_roots`
    to always append it leaves that test green and reddens THIS one.
    """
    write(tmp_path, "solo.sh", """#!/usr/bin/env bash
run() {
    python3 - <<'PY'
import json
print(json.dumps({"ok": True}))
PY
}
""")
    rc, out = run_audit(tmp_path)
    assert rc == 0, out
    data = json.loads(out)
    # Normalize BOTH sides through as_posix(). The audit emits `str(Path(...))`,
    # which on win32 is backslash-separated while the `--root` argument this test
    # passes is forward-slashed -- so a raw comparison fails on Windows for a
    # reason that has nothing to do with what this control tests, and the two
    # assertions below (the actual over-match controls) never run. Measured:
    # str(PureWindowsPath("C:/a")) == "C:\\a" != "C:/a" (guard-581 family).
    assert [Path(r).as_posix() for r in data["roots_scanned"]] == [
        Path(tmp_path).as_posix()], data["roots_scanned"]
    assert data["world_root_state"] == "explicit-root", data["world_root_state"]
    # One block, from the fixture alone -- the live corpus is three orders of
    # magnitude larger, so any leak shows up immediately here.
    assert data["total_blocks"] == 1, data["total_blocks"]
