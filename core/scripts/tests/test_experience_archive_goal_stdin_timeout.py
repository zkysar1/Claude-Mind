"""test_experience_archive_goal_stdin_timeout.py -- regression for 2.

experience.py cmd_archive_goal read OPTIONAL stdin via the pattern:

    if not sys.stdin.isatty():
        raw = sys.stdin.read()

`isatty()` distinguishes a terminal from a non-terminal, but a NON-terminal
stdin is NOT guaranteed EOF-ready -- it can be an inherited pipe that never
closes. When experience-archive-goal.sh was invoked WITHOUT an `echo '...' |`
pipe or a `</dev/null` redirect, stdin was such a pipe, so the bare read()
blocked indefinitely: experience-archive-goal.sh hung >90s on the g-115-900
deep close and was killed (rb-1310). experience-add.sh never hangs only
because its callers ALWAYS pipe it (mandatory stdin).

Fix: `_read_optional_stdin()` reads in a daemon thread with a deadline
(EXPERIENCE_STDIN_TIMEOUT_S, default 10s). A non-EOF stdin degrades to "" after
the deadline instead of hanging; piped stdin (EOF arrives in << the deadline)
reads normally. Cross-platform by construction -- select()/signal.alarm do not
work on Windows pipes, but a thread does.

The helper is exercised through a CHILD process so the test fully controls the
child's stdin (an open subprocess.PIPE we never write/close == the exact hang
condition), rather than depending on whatever stdin the test harness inherits.

Cases:
  A  open non-EOF pipe (never written/closed) -> child EXITS within the
     deadline+margin and returns "" (pre-fix: hung until killed).
  B  piped JSON then EOF                       -> child returns the JSON
     (proves the canonical `echo '...' |` path still works).
  C  immediate EOF (empty input, closed)       -> child returns "" fast.

Filed by g-115-1232 (experience-archive-goal.sh >90s hang investigation, zeta).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent

# Child imports experience.py and echoes _read_optional_stdin() to stdout.
_CHILD_CODE = (
    "import sys; sys.path.insert(0, r'{core}'); "
    "import experience; "
    "sys.stdout.write(experience._read_optional_stdin())"
).format(core=str(CORE_SCRIPTS))

# Deadline the child enforces; HANG_GUARD is the parent's kill threshold.
# HANG_GUARD must sit well above the deadline (so a legitimately-slow but
# correct exit is not flagged) and well below the >90s pre-fix hang (so a
# regression is caught quickly).
CHILD_DEADLINE_S = "2"
HANG_GUARD_S = 20.0


def _spawn():
    env = dict(os.environ)
    env["EXPERIENCE_STDIN_TIMEOUT_S"] = CHILD_DEADLINE_S
    env.setdefault("MIND_AGENT", "zeta")
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_CODE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def main() -> int:
    failures = []

    # -- A: open non-EOF pipe -> must exit via the deadline, NOT hang ----------
    pa = _spawn()
    t0 = time.time()
    while pa.poll() is None and (time.time() - t0) < HANG_GUARD_S:
        time.sleep(0.1)
    elapsed_a = time.time() - t0
    if pa.poll() is None:
        pa.kill()
        try:
            pa.wait(timeout=5)
        except Exception:
            pass
        failures.append(
            f"A: child STILL RUNNING after {HANG_GUARD_S:.0f}s -- the deadline "
            f"never fired (this is the >90s-hang regression returning)"
        )
    else:
        out_a = pa.stdout.read()
        if out_a != "":
            failures.append(f"A: expected '' on non-EOF stdin, got {out_a!r}")
        # Generous ceiling: deadline is 2s; anything under 10s proves the
        # deadline (not some accidental fast-EOF) governed the exit.
        if elapsed_a > 10:
            failures.append(
                f"A: child exited but took {elapsed_a:.1f}s (>10s) -- deadline "
                f"not honored"
            )
    try:
        pa.stdin.close()
    except Exception:
        pass

    # -- B: piped JSON then EOF -> must read it --------------------------------
    pb = _spawn()
    try:
        out_b, _err_b = pb.communicate(input='{"k":"v"}', timeout=HANG_GUARD_S)
    except subprocess.TimeoutExpired:
        pb.kill()
        out_b = ""
        failures.append("B: child hung on piped input (communicate timed out)")
    if out_b.strip() != '{"k":"v"}':
        failures.append(f"B: expected piped JSON echoed, got {out_b!r}")

    # -- C: immediate EOF (empty input, closed) -> '' fast ---------------------
    pc = _spawn()
    try:
        out_c, _err_c = pc.communicate(input="", timeout=HANG_GUARD_S)
    except subprocess.TimeoutExpired:
        pc.kill()
        out_c = "x"
        failures.append("C: child hung on immediate-EOF stdin")
    if out_c != "":
        failures.append(f"C: expected '' on immediate EOF, got {out_c!r}")

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASS (3/3 cases; A exited in {elapsed_a:.1f}s via {CHILD_DEADLINE_S}s deadline)")
    return 0


def test_experience_archive_goal_stdin_timeout():
    """Pytest-collectable wrapper so this regression joins the
    `pytest core/scripts/tests` suite, not only the standalone run."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
