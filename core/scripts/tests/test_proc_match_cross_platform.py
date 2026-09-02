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


@pytest.fixture
def marked_process():
    """A real child process carrying a unique token in its command line.

    The token is passed as an argv element after `-c`, so it lands in the
    process command line on Windows, macOS and Linux alike — which is exactly
    the thing a name-only probe cannot see.
    """
    token = "PROCMATCH_MARKER_" + uuid.uuid4().hex[:12]
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", token],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield token, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_finds_a_live_process_by_command_line(marked_process):
    """POSITIVE CONTROL — without this, every other assertion here is vacuous."""
    token, proc = marked_process
    r = run_probe(token)
    assert r.returncode == 0, (
        f"probe failed to find a live process carrying {token!r} on "
        f"{sys.platform}. stdout={r.stdout!r} stderr={r.stderr!r}"
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
