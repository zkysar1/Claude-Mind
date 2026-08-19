"""tree-update.sh must close BOTH wedge doors, and say something useful on --help.

g-115-3839. This wrapper slurped its JSON payload with a bare `STDIN_DATA="$(cat)"`
behind a bare `[ ! -t 0 ]`, and its catch-all was `*) shift;;`. guard-3393 models
the two failure doors that combination opens, and they are STRUCTURALLY
INDEPENDENT — neither guard substitutes for the other:

  (a) THE FLAG DOOR   -- the caller passes the child JSON positionally instead of
      on stdin. `*) shift;;` discarded it silently, execution fell into the stdin
      read with nothing piped, and the outcome was decided entirely by an
      inherited descriptor: /dev/null stdin gave a fast, misleading
      {"error": "missing_param"}; an open-but-idle stdin HUNG until the harness
      timeout and landed nothing.
  (b) THE IDLE-STDIN DOOR -- argv is irrelevant. `[ ! -t 0 ]` proves stdin is not
      a terminal; it does NOT promise EOF. Measured here before the fix:
      rc=124 under a 5s timeout. After: rc=1 in 2558ms.

The silence is what made this expensive: the phase looks busy, burns the full
timeout, lands nothing, and an agent that does not re-read the tree afterward
records the close as encoded.

Sibling coverage, deliberately not duplicated: test_store_append_flag_refusal.py
covers door (a) for the six store-append wrappers (tree-update.sh is not one --
it is an op dispatcher, not a store writer). g-115-4765 ports door (b) to the 11
store-writers; g-115-3284 does board-post.sh; g-115-5866 covers the one remaining
unguarded site this goal's census found, recovery-gate.sh.

DISCRIMINATION (guard-1943, rb-5828): asserting current behaviour cannot separate
a working guard from a vacuous test. The two `*_reverting_*` tests re-introduce
each historical defect into a temp copy and assert the protection STOPS -- so
this file is proven to redden rather than assumed to.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
TREE_UPDATE = SCRIPTS / "tree-update.sh"

# Resolve bash via the shared helper (). A bare "bash" argv[0] resolves
# to System32 WSL on win32 and can hang forever (guard-580) -- a poor failure
# mode for a suite about processes that hang.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

# The exact historical defects, kept as constants so the discrimination tests
# re-introduce what actually shipped rather than an approximation of it.
SILENT_DISCARD = "        *) shift;;"
BARE_SLURP = '    STDIN_DATA="$(cat)"'

_ENV = {"STORAGE_BACKEND": "local", "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"}


def _run(args, stdin=subprocess.DEVNULL, timeout=30):
    """Invoke the wrapper. stdin defaults to DEVNULL so a REGRESSION can never
    wedge the runner -- it degrades to a fast error instead of hanging."""
    return subprocess.run(
        # .as_posix(), never str(Path): bash silently strips the backslashes of a
        # str(WindowsPath) (guard-581).
        [BASH, Path(args[0]).as_posix()] + [str(a) for a in args[1:]],
        cwd=str(SCRIPTS.parents[1]),
        stdin=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_ENV,
    )


def _run_with_idle_stdin(script, args, timeout, capture=True):
    """Run with stdin held OPEN by a descriptor nobody ever writes to.

    This is the door-(b) condition and it cannot be simulated with DEVNULL (which
    EOFs immediately) or with a closed pipe. The parent keeps the write end open
    for the whole call, so a bare `cat` in the child can never terminate.

    capture=False DISCARDS stdout/stderr, and a caller that only asserts
    TimeoutExpired MUST use it or the timeout is decorative on Windows.
    MEASURED (g-115-6226, DESKTOP-O91DLK2, py 3.12.10 / pytest 9.0.2), production
    shape `subprocess.run(argv, stdin=<idle pipe>, timeout=4)`:

        both streams DEVNULL ....... RAISED 4.02s
        stdout DEVNULL, stderr PIPE  BLOCKED past 20s
        stdout PIPE, stderr DEVNULL  BLOCKED past 20s
        both streams PIPE .......... BLOCKED past 20s
        python child, both PIPE .... RAISED 4.27s

    So EITHER captured pipe alone is sufficient to wedge it, and a python child
    bounds correctly in every shape — this is Git-Bash specific, not a bad
    timeout value. The kill itself is fine (the DEVNULL row proves the child
    dies on schedule); what blocks is subprocess.run's post-kill communicate()
    reap, which takes NO timeout, because a surviving Git-Bash descendant still
    holds the inherited pipe write handle so the reader threads never see EOF.
    DISCARD is the sanctioned handling when output is not surfaced (rb-516,
    guard-436). The wide fix for every other bash-child timeout in the corpus is
    tracked separately; this parameter only unblocks the callers that need it.
    """
    r_fd, w_fd = os.pipe()
    sink = ({"capture_output": True} if capture
            else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL})
    try:
        return subprocess.run(
            [BASH, Path(script).as_posix()] + [str(a) for a in args],
            cwd=str(SCRIPTS.parents[1]),
            stdin=r_fd,
            text=True,
            timeout=timeout,
            env=_ENV,
            **sink,
        )
    finally:
        os.close(r_fd)
        os.close(w_fd)


# --- door (a): the flag door ------------------------------------------------

def test_positional_json_is_refused():
    """rc != 0, and stderr names BOTH the offending argument and the contract."""
    payload = '{"key":"zz"}'
    r = _run([TREE_UPDATE, "--add-child", "some-parent", payload])
    assert r.returncode != 0, "positional JSON was accepted (rc=0)"
    if sys.platform == "win32":
        # The arg-echo assertion tests ARGV TRANSIT, not the guard, and transit
        # is broken here for this literal — so on this platform it is
        # unfalsifiable rather than failing. MEASURED (,
        # DESKTOP-O91DLK2) over the same Python -> Git-Bash path this test uses,
        # with controls: 'PLAINWORD' arrives intact (len 9), '{key:zz}' arrives
        # brace-stripped as 'key:zz' (len 6), and '{"key":"zz"}' arrives as the
        # EMPTY STRING (len 0). Neither MSYS_NO_PATHCONV=1 nor
        # MSYS2_ARG_CONV_EXCL=* changes any of it. The script therefore never
        # receives the payload and correctly echoes what it DID receive (''), so
        # the guard is working. Substituting the strongest assertion that is
        # still decidable here rather than skipping the test: the guard must
        # still fire, and the stdin contract below is still asserted on every
        # platform. Do NOT "fix" this by weakening the else-branch.
        assert "not a recognized argument" in r.stderr, (
            f"the guard did not fire at all: {r.stderr!r}"
        )
    else:
        assert payload in r.stderr, f"stderr does not name the offending arg: {r.stderr!r}"
    assert "stdin" in r.stderr.lower(), f"stderr does not name the stdin contract: {r.stderr!r}"


def test_refusal_is_fast():
    """The refusal must fire in the arg loop, BEFORE the stdin read is reached.

    This is the property that prevents the hang: a guard firing after the read
    would be useless on exactly the descriptor state that wedges.
    """
    r = _run([TREE_UPDATE, "--add-child", "p", '{"key":"zz"}'], timeout=15)
    assert r.returncode != 0


def test_argv_reject_precedes_the_stdin_read():
    """Static ordering check, anchored to STATEMENTS not prose.

    The guards' own comments quote both `*) shift;;` and `$(cat)`, so a bare
    find() matches the explanation instead of the code -- the false-failure the
    sibling test hit on all six of its wrappers.
    """
    txt = TREE_UPDATE.read_text(encoding="utf-8")
    reject = txt.find("is not a recognized argument for this script")
    m = re.search(r"^\s+IFS= read -r -t 2 _first_chunk", txt, re.M)
    assert reject != -1, "argv reject branch missing"
    assert m is not None, "bounded stdin read missing"
    assert reject < m.start(), "argv reject appears AFTER the stdin read — it cannot prevent the hang"


# --- door (b): the idle-stdin door ------------------------------------------

def test_idle_stdin_does_not_hang():
    """The whole point: an open-but-idle descriptor must degrade, not wedge."""
    r = _run_with_idle_stdin(TREE_UPDATE, ["--add-child", "nonexistent-parent-probe"], timeout=25)
    assert "backgrounded-task guard" in r.stderr, (
        f"the bounded-read degradation note is absent — stderr={r.stderr!r}"
    )


def test_piped_payload_is_still_read():
    """REGRESSION: the guard must not break the normal caller.

    Asserted via the ABSENCE of the degradation note rather than via the daemon's
    answer, so the test says nothing about daemon availability.
    """
    r = subprocess.run(
        [BASH, TREE_UPDATE.as_posix(), "--add-child", "nonexistent-parent-probe"],
        cwd=str(SCRIPTS.parents[1]),
        input='{"key":"zz-probe-key","summary":"probe"}\n',
        capture_output=True,
        text=True,
        timeout=30,
        env=_ENV,
    )
    assert "stdin open but idle" not in r.stderr, (
        f"a normally-piped payload tripped the idle guard — stderr={r.stderr!r}"
    )


# --- usage --------------------------------------------------------------------

def test_help_exits_zero_and_names_the_ops():
    """--help used to print 'Use --help for options.' and exit 1 — it answered
    itself, named nothing, and rc=1 made it look like a failure rather than a
    dead end. guard-3145 is why this is tested by RUNNING it: a stdin-body reader
    can hang on --help unless it has an explicit branch."""
    r = _run([TREE_UPDATE, "--help"], timeout=15)
    assert r.returncode == 0, f"--help exited {r.returncode}"
    out = r.stdout + r.stderr
    assert "--add-child" in out, "--help does not name --add-child"
    assert "Use --help for options" not in out, "--help still refers the reader back to itself"


def test_bare_invocation_prints_usage_and_exits_nonzero():
    """Bare invocation stays machine-detectable (rc != 0) AND becomes readable.

    Note the rc half was ALREADY correct before this goal: the addendum that
    prompted the change reported rc=0 and proposed making it non-zero, but
    measurement showed rc=1 all along (guard-1150 — a trailing pipe replaces the
    rc, which is the likely source of the rc=0 reading). Only the TEXT was
    broken. This test pins both halves so neither regresses.
    """
    r = _run([TREE_UPDATE], timeout=15)
    assert r.returncode != 0, "bare invocation must stay machine-detectable"
    out = r.stdout + r.stderr
    assert "--add-child" in out, "bare invocation does not name the available ops"


def test_known_ops_are_not_swallowed_by_the_catch_all():
    """The reject must not eat legitimate flags — it fires only on unknown argv."""
    r = _run([TREE_UPDATE, "--set"], timeout=15)
    assert "is not a recognized argument" not in r.stderr, (
        "--set was swallowed by the unknown-argument branch"
    )


# --- discrimination proofs ----------------------------------------------------

def test_reverting_the_argv_guard_reddens(tmp_path):
    """Re-introduce `*) shift;;`; the refusal must STOP firing."""
    txt = TREE_UPDATE.read_text(encoding="utf-8")
    reverted = re.sub(
        r"        --help\|-h\)\n(?:.*\n)*?            exit 0;;\n        \*\)\n(?:.*\n)*?            exit 2;;",
        SILENT_DISCARD,
        txt,
    )
    assert reverted != txt, "failed to excise the argv guard — the regex no longer matches the source"
    assert "is not a recognized argument for this script" not in reverted

    victim = tmp_path / "tree-update.sh"
    victim.write_text(reverted, encoding="utf-8")
    shutil.copystat(TREE_UPDATE, victim)

    r = _run([victim, "--add-child", "p", '{"key":"zz"}'], timeout=20)
    assert "is not a recognized argument" not in r.stderr, (
        "the reverted copy STILL refused the positional — this test is not "
        "discriminating, so the green above proves nothing about the guard"
    )


def test_reverting_the_bounded_read_reddens(tmp_path):
    """Re-introduce the bare slurp; an idle stdin must go back to hanging.

    The historical defect is the ONLY thing changed. If the guarded script and
    this one behave the same under an idle descriptor, every door-(b) assertion
    above is vacuous.
    """
    txt = TREE_UPDATE.read_text(encoding="utf-8")
    reverted = re.sub(
        r"    # BOUNDED READ — guard-3393 door \(b\).*?\n    # correct answer\.\n",
        BARE_SLURP + "\n",
        txt,
        flags=re.S,
    )
    assert reverted != txt, "failed to excise the bounded read — the regex no longer matches the source"
    assert "backgrounded-task guard" not in reverted

    victim = tmp_path / "tree-update.sh"
    victim.write_text(reverted, encoding="utf-8")
    shutil.copystat(TREE_UPDATE, victim)

    # capture=False is load-bearing, not tidiness: this call asserts ONLY that
    # the timeout fires, and capturing either stream makes it never fire on
    # Windows (the 600s faulthandler abort that made every full-suite run report
    # INVALID). Nothing here reads stdout/stderr, so discarding them is lossless.
    with pytest.raises(subprocess.TimeoutExpired):
        _run_with_idle_stdin(
            victim, ["--add-child", "nonexistent-parent-probe"], timeout=8, capture=False
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
