"""test_proc_match_cross_platform.py — proc-match.sh contract ().

proc-match.sh replaced `pgrep -af "[r]un-full-suite"` in
.claude/rules/run-full-suite-after-deep-code.md, which ran on ONE of the fleet's
three platforms: Windows/MSYS has no pgrep at all, and BSD/macOS pgrep lacks the
`-a` flag. The Windows fallbacks are worse than absent — MSYS `ps -ef` prints no
arguments, so `ps | grep <script>` reports 0 while the run is live (measured:
0 found against 4 live processes), which is the fail-OPEN direction guard-3159
warns about.

These tests run the SCRIPT, on whatever platform is executing them, so a
platform whose branch is broken fails here rather than silently reporting an
empty process table.

POSITIVE CONTROL IS THE POINT (guard-3159: "prove the probe can succeed with a
positive control before believing any zero"). A test that only asserts the
negative case passes just as well against a probe that can never match anything
— which is the exact defect being fixed. So each run spawns a real, uniquely
marked child process and requires the probe to find it.

Case 4 is the one the bracket idiom structurally cannot satisfy: `[r]un-full-
suite` stops the matcher matching its OWN argv, but not an ENCLOSING wrapper's
(guard-1238, guard-2262), and that phantom aborts the launch the check was
written to protect.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

# conftest.py already puts core/scripts/ on sys.path for collected tests; this
# insert matches the sibling pattern so the file also imports when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _runtime_bash import BASH, bash_cmd  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "proc-match.sh"

# The Windows arm shells out to PowerShell and enumerates the whole process
# table (~9s measured on DESKTOP-O91DLK2). Keep the scan count per test low.
TIMEOUT = 120


def run_probe(pattern: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        bash_cmd(SCRIPT, *extra, pattern),
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        cwd=str(SCRIPT.parents[2]),
    )


# The child signals readiness by CREATING this file, then sleeps. Waiting on a
# file the child creates is a POSITIVE terminal token (guard-4396: never write a
# wait predicate as the ABSENCE of something — an empty or errored probe
# satisfies almost every absence predicate, so its failure becomes
# indistinguishable from the success being waited for). It is also portable:
# Windows, macOS and Linux all have a filesystem, whereas `ps -eo pid=,args=`
# does not exist usefully under MSYS.
_READY_ENV = "PROCMATCH_TEST_READY_FILE"

# The signal is carried in the ENVIRONMENT, not in argv, deliberately: argv is
# the thing under test, so the readiness mechanism must not perturb it. The
# token stays the LAST argv element exactly as before.
_CHILD_CODE = (
    "import os,pathlib,time;"
    "pathlib.Path(os.environ['" + _READY_ENV + "']).write_text('R');"
    "time.sleep(60)"
)

_READY_TIMEOUT = 30.0


@pytest.fixture
def marked_process(tmp_path):
    """A real child process carrying a unique token in its command line.

    The token is passed as an argv element after `-c`, so it lands in the
    process command line on Windows, macOS and Linux alike — which is exactly
    the thing a name-only probe cannot see.

    WAITS FOR THE CHILD TO BE REAL BEFORE YIELDING (g-115-8666). The original
    fixture yielded the instant Popen returned, and Popen returns after the
    FORK, before the EXEC — during which window the child's command line is
    still the PARENT's argv, so the token is not in the process table at all
    and any probe correctly finds nothing. Measured on cc-08 (idle, 20 cores):
    the token takes 2.3-4.1 ms to become visible to `ps` after Popen returns.
    That is a race in the HARNESS, not in the probe, and it fails in the
    fail-open direction the whole file exists to prevent — an rc=1 with empty
    stdout/stderr, which reads exactly like a broken probe.

    The wait does NOT weaken the positive control: it only removes the
    harness's own timing bug, so a probe that genuinely cannot see a live,
    fully-exec'd process still fails the assertion below. If the child never
    signals, that is itself a real defect and the fixture fails LOUDLY rather
    than yielding a process that was never running (guard-2700: a fixture must
    not make a failure mode unreachable).
    """
    token = "PROCMATCH_MARKER_" + uuid.uuid4().hex[:12]
    ready = tmp_path / "child-ready"
    env = dict(os.environ, **{_READY_ENV: str(ready)})
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_CODE, token],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    deadline = time.monotonic() + _READY_TIMEOUT
    while not ready.exists():
        if proc.poll() is not None:
            pytest.fail(
                f"marker child exited early (rc={proc.returncode}) without "
                f"signalling readiness — the spawn itself is broken, so no "
                f"conclusion about proc-match.sh can be drawn from this run."
            )
        if time.monotonic() > deadline:
            proc.kill()
            pytest.fail(
                f"marker child did not signal readiness within "
                f"{_READY_TIMEOUT}s. Not a proc-match.sh failure — the child "
                f"never started."
            )
        time.sleep(0.01)
    try:
        yield token, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _process_table_says(token: str) -> str:
    """Diagnostic ONLY, used in the failure message — never in an assertion.

    g-115-8666 was filed because this test failed with `stdout='' stderr=''`,
    which is compatible with three unrelated causes (probe self-exclusion, ps
    truncation, spawn race) and distinguishes none of them — so the goal had to
    enumerate all three as guesses. Reading the process table INDEPENDENTLY of
    proc-match.sh at the moment of failure separates "the probe cannot see a
    process that is plainly there" from "the process is not there at all".
    """
    try:
        snap = subprocess.run(
            ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=30
        ).stdout
    except Exception as exc:  # no ps (Windows/MSYS), or it failed — say so
        return f"independent ps read unavailable ({exc!r})"
    hits = [ln.strip() for ln in snap.splitlines() if token in ln]
    if hits:
        return (
            f"token IS in the process table ({len(hits)} line(s)): {hits[0][:200]!r} "
            f"-> the process exists and the PROBE failed to report it "
            f"(suspect self-exclusion or the grep/ps arm, NOT a spawn race)"
        )
    return (
        f"token is NOT in the process table at all ({len(snap.splitlines())} procs "
        f"scanned) -> the child was not visible, so this is a SPAWN/readiness "
        f"problem, not a proc-match.sh defect"
    )


def test_finds_a_live_process_by_command_line(marked_process):
    """POSITIVE CONTROL — without this, every other assertion here is vacuous."""
    token, proc = marked_process
    r = run_probe(token)
    assert r.returncode == 0, (
        f"probe failed to find a live process carrying {token!r} on "
        f"{sys.platform}. stdout={r.stdout!r} stderr={r.stderr!r}. "
        f"DIAGNOSIS: {_process_table_says(token)}"
    )
    assert token in r.stdout, f"match printed but token absent: {r.stdout!r}"
    assert str(proc.pid) in r.stdout, (
        f"expected pid {proc.pid} in output; got {r.stdout!r}"
    )


def test_absent_pattern_exits_1_with_no_output():
    """NEGATIVE CONTROL. The first draft of the Windows arm failed exactly here:
    PowerShell receives the pattern inside its own -Command text, so it matched
    ITSELF and returned rc=0 for an impossible pattern."""
    r = run_probe("zzz-no-such-process-" + uuid.uuid4().hex)
    assert r.returncode == 1, f"expected rc=1, got {r.returncode}: {r.stdout!r}"
    assert r.stdout.strip() == "", f"expected no matches, got {r.stdout!r}"


def test_excludes_itself():
    """Probing for the script's own name must not report the probe."""
    r = run_probe("proc-match")
    assert r.returncode == 1, (
        f"probe reported itself (rc={r.returncode}): {r.stdout!r}"
    )


def test_no_phantom_from_enclosing_wrapper_argv():
    """The case the bracket idiom cannot fix (guard-3159 / guard-1238).

    Invoke the probe from INSIDE a wrapper shell whose own argv contains the
    pattern. The bracket only protects the matcher's own argv, so the old recipe
    reported a phantom here and aborted the launch it was protecting. Uses an
    impossible pattern, so any hit is necessarily the wrapper itself.
    """
    absent = "zzz-no-such-process-" + uuid.uuid4().hex
    inner = f'"{BASH}" "{SCRIPT}" --count {absent}'
    r = subprocess.run(
        [BASH, "-c", inner],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        cwd=str(SCRIPT.parents[2]),
    )
    assert r.stdout.strip() == "0", (
        "phantom hit from the enclosing wrapper's argv — the exact false "
        f"POSITIVE this script exists to remove. got {r.stdout!r}"
    )


def test_usage_error_is_rc_2_not_a_silent_zero():
    """A plumbing error must be distinguishable from 'nothing matched'.
    Collapsing the two lets a broken invocation read as an all-clear."""
    r = subprocess.run(
        bash_cmd(SCRIPT),
        capture_output=True, text=True, timeout=TIMEOUT,
        cwd=str(SCRIPT.parents[2]),
    )
    assert r.returncode == 2, f"expected usage rc=2, got {r.returncode}"


def test_rule_no_longer_prescribes_pgrep():
    """The rule that motivated this script must not hand out the pgrep recipe
    as the thing to RUN. pgrep may still be discussed as the retired form."""
    rule = SCRIPT.parents[2] / ".claude" / "rules" / "run-full-suite-after-deep-code.md"
    text = rule.read_text(encoding="utf-8", errors="replace")
    assert "proc-match.sh" in text, "rule does not point at the cross-platform probe"
    for line in text.splitlines():
        s = line.strip()
        # Fenced command lines are what a reader copies; prose may cite pgrep.
        if s.startswith("pgrep ") or s.startswith("$ pgrep"):
            pytest.fail(f"rule still prescribes a bare pgrep command: {line!r}")
