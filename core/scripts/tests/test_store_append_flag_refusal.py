"""Store-append wrappers must REFUSE an unrecognised flag, not silently discard it.

g-115-3975 / g-115-5524. The six store-append wrappers below parse args with a
`case` block and then slurp their record via `BODY="$(cat)"`. Before this guard
the catch-all was `*) shift;;`, so a flag-shaped call dropped every flag and fell
into `$(cat)` with nothing piped. What happened next depended on the box:

  * stdin = never-EOF socket (cc-03)  -> the process HUNG FOREVER (7d and 4d old
    processes were found alive).
  * stdin = /dev/null (cc-02)         -> immediate EOF, and the caller got
    {"error": "invalid_body"} — a message naming neither the flag nor argv.

Both are wrong, and the second is what makes it hard to spot: it looks like a
daemon/payload fault. See guard-3393 for the two-doors model (this file covers
the FLAG door only; the idle-stdin door is g-115-4765's bounded-read guard).

DISCRIMINATION (self.md corollary 3, rb-5828/guard-1943): a test that merely
asserts the current behaviour cannot tell a working guard from a vacuous one.
`test_reverting_to_shift_reddens_this_test` re-introduces the exact defect into
a temp copy and asserts the refusal STOPS firing — so this file is proven to
redden rather than assumed to.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]

# Resolve bash via the shared helper (). A bare "bash" argv[0] resolves
# to System32 WSL on win32 and can hang forever (guard-580) — which would be a
# particularly poor failure mode for a test suite about processes that hang.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

WRAPPERS = [
    "experience-add.sh",
    "guardrails-add.sh",
    "journal-add.sh",
    "pattern-signatures-add.sh",
    "reasoning-bank-add.sh",
    "spark-questions-add.sh",
]

# The literal the pre-guard catch-all used. Kept as a constant so the
# discrimination test below re-introduces the *exact* historical defect.
SILENT_DISCARD = "        *) shift;;"


def _run(args, cwd=None, stdin=subprocess.DEVNULL, timeout=30):
    """Invoke a wrapper. stdin defaults to DEVNULL so a REGRESSION (the guard
    removed) can never wedge the test runner — it degrades to a fast empty-body
    error instead of hanging the suite."""
    return subprocess.run(
        # .as_posix(), never str(Path): bash silently strips the backslashes of a
        # str(WindowsPath) (guard-581).
        [BASH, Path(args[0]).as_posix()] + [str(a) for a in args[1:]],
        cwd=str(cwd or SCRIPTS.parents[1]),
        stdin=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"STORAGE_BACKEND": "local", "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"},
    )


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_unknown_flag_is_refused(wrapper):
    """rc != 0, and the message names BOTH the offending flag and the contract."""
    r = _run([SCRIPTS / wrapper, "--rule", "x", "--category", "framework"])
    assert r.returncode != 0, f"{wrapper}: unknown flag was accepted (rc=0)"
    err = r.stderr
    assert "--rule" in err, f"{wrapper}: stderr does not name the offending flag: {err!r}"
    assert "stdin" in err.lower(), f"{wrapper}: stderr does not name the stdin contract: {err!r}"


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_refusal_is_fast(wrapper):
    """The refusal must happen in the arg loop, BEFORE `$(cat)` is reached.

    This is the property that actually prevents the multi-day hang: a guard that
    fired after the slurp would be useless on the box where stdin never EOFs.
    A 15s ceiling separates 'returned from argv parsing' from 'blocked on IO'.
    """
    r = _run([SCRIPTS / wrapper, "--rule", "x"], timeout=15)
    assert r.returncode != 0


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_help_does_not_hang(wrapper):
    """guard-3145: a stdin-body reader HANGS on --help unless it has a branch."""
    r = _run([SCRIPTS / wrapper, "--help"], timeout=15)
    assert r.returncode == 0, f"{wrapper}: --help exited {r.returncode}"
    assert "stdin" in (r.stdout + r.stderr).lower()


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_guard_precedes_the_stdin_slurp(wrapper):
    """Static: the refusal branch must appear BEFORE the `$(cat)` line."""
    txt = (SCRIPTS / wrapper).read_text(encoding="utf-8")
    reject = txt.find("is not a CLI flag for this script")
    # Anchor to the STATEMENT at column 0, not any `$(cat)` mention. The guard's
    # own explanatory comment quotes `BODY="$(cat)"`, so a bare find() matches the
    # comment and reports the guard as coming AFTER the slurp — a false failure
    # this test produced on all six wrappers when first written.
    m = re.search(r'^BODY="\$\(cat\)"', txt, re.M)
    slurp = m.start() if m else -1
    assert reject != -1, f"{wrapper}: refusal branch missing"
    assert slurp != -1, f"{wrapper}: expected a top-level BODY=\"$(cat)\" slurp"
    assert reject < slurp, f"{wrapper}: refusal appears AFTER the stdin slurp — it cannot prevent the hang"


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_schema_path_unchanged(wrapper):
    """--schema must still reach its own handler, not the new catch-all."""
    r = _run([SCRIPTS / wrapper, "--schema"], timeout=20)
    assert "is not a CLI flag" not in r.stderr, (
        f"{wrapper}: --schema was swallowed by the unknown-flag branch"
    )


def test_reverting_to_shift_reddens_this_test(tmp_path):
    """DISCRIMINATION PROOF — re-introduce the defect, assert the guard stops firing.

    Without this, every assertion above could be passing for a reason unrelated to
    the guard (a usage error, a missing dependency, any non-zero exit). Here the
    ONLY thing that changes is the catch-all branch.
    """
    src = SCRIPTS / "guardrails-add.sh"
    txt = src.read_text(encoding="utf-8")

    # Excise the guard: drop --help + the refusal branch, restore `*) shift;;`.
    reverted = re.sub(
        r"        --help\|-h\)\n(?:.*\n)*?            exit 0;;\n        \*\)\n(?:.*\n)*?            exit 2;;",
        SILENT_DISCARD,
        txt,
    )
    assert reverted != txt, "failed to excise the guard — the regex no longer matches the source"
    assert SILENT_DISCARD in reverted
    assert "is not a CLI flag for this script" not in reverted

    victim = tmp_path / "guardrails-add.sh"
    victim.write_text(reverted, encoding="utf-8")
    shutil.copystat(src, victim)

    # DEVNULL stdin: on the reverted copy this is what stops the historical hang
    # from wedging the suite. It yields a fast empty-body error instead.
    r = _run([victim, "--rule", "x", "--category", "framework"], timeout=20)
    assert "is not a CLI flag" not in r.stderr, (
        "the reverted copy STILL refused the flag — this test is not discriminating, "
        "so the green above proves nothing about the guard"
    )


def test_a_flagless_call_still_reaches_the_store_validator():
    """THE POSITIVE CONTROL (guard-1665), added alongside a duplicate fix.

    Every other case above asserts a REFUSAL. Without this one, a wrapper that
    refused EVERYTHING -- including a correct flagless call -- would pass this
    entire suite, and the guard would read as working while having broken the
    only path that matters. The mutation test above proves the guard is
    discriminating; this proves it is not over-broad. They are different claims.

    Keeps the module's DEVNULL default rather than piping a record: an empty
    body is rejected by the store as invalid_body, which is all this needs (the
    call reached the body path instead of the argv guard) and it can never
    append to a live governed store. PIPE with no input would hand the child a
    pipe that never delivers EOF -- reintroducing, inside the test, the very
    hang this file exists to pin.
    """
    r = _run([SCRIPTS / "guardrails-add.sh"], timeout=30)
    assert "is not a CLI flag" not in r.stderr, (
        "a call with NO flags must never hit the unknown-flag refusal; the guard "
        f"is over-broad. stderr={r.stderr[:300]!r}")
    assert r.returncode != 2, (
        f"rc=2 is the refusal code and must not fire on a flagless call; "
        f"got rc={r.returncode} stderr={r.stderr[:200]!r}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
