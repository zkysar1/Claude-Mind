"""test_loop_state_save_stdin_timeout.py -- regression for .

loop-state-save.py read OPTIONAL stdin through a bare guard-664 violation, in
BOTH cmd_init and cmd_update:

    elif not sys.stdin.isatty():
        raw = sys.stdin.read().strip()

`isatty()` distinguishes a terminal from a non-terminal but does NOT guarantee
EOF: a non-tty stdin can be an inherited pipe that never closes, so the read
blocks forever. Measured 2026-07-28 (bravo): a bare `loop-state-save.sh init`
sat 11 minutes at 0% CPU, wchan `unix_stream_data_wait`, and wrote no
checkpoint. Re-measured 2026-08-26 (alpha, cc-07) under a never-EOF FIFO:
rc=124 at the timeout, i.e. it never returned on its own.

What made this worse than a generic instance: the WARN emitted on every
skipped Phase 2.95 actively prescribed that exact hanging command, so the
remediation advice was itself the trap.

Fix (guard-664 daemon-thread+join pattern, mirrored from
experience.py::_read_optional_stdin, presence-tick.py and
iteration-close-reminder.py): read stdin in a daemon thread with a
join(timeout) deadline so a non-EOF stdin degrades to "" instead of hanging.
select()/signal.alarm do NOT work on Windows pipes; a daemon thread does --
when main() returns the interpreter exits and the blocked reader dies with it.

The behavioral cases drive the REAL script as a child so the test fully
controls the child's stdin (an open subprocess.PIPE we never write/close IS
the never-EOF condition), rather than depending on the harness's stdin -- which
is not stable: on cc-07 the same Bash tool supplied /dev/null in one
invocation and socket:[...] in the next, so a harness-dependent test would
pass or fail by luck.

Cases (NONE writes a checkpoint -- every case is refused before the write):
  A  init,   open non-EOF pipe        -> exits within the deadline, rc=2
  B  init,   piped '{}' then EOF      -> exits FAST, rc=1 (parsed, then refused
                                          on missing required keys -- proves the
                                          bounded reader still delivers piped
                                          input, without writing)
  C  init,   immediate EOF (empty)    -> exits FAST, rc=2
  D  update, open non-EOF pipe        -> exits within the deadline, rc=0
                                          (cmd_update's documented fail-open on
                                          empty input -- a DIFFERENT rc from A,
                                          so this pins that the two call sites
                                          keep their distinct semantics)
Plus STRUCTURAL guards pinning the fix against a revert.

Filed by g-115-3661.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
LOOP_STATE_SAVE_PY = CORE_SCRIPTS / "loop-state-save.py"

# Deadline the child enforces (via env); HANG_GUARD is the parent's kill
# threshold. HANG_GUARD sits well above the deadline (a legitimately-slow but
# correct exit is not flagged) and far below the 11-minute pre-fix hang (a
# regression is caught in seconds).
CHILD_DEADLINE_S = "2"
HANG_GUARD_S = 20.0
FAST_S = 10.0


def _spawn(subcommand):
    env = dict(os.environ)
    env["LOOP_STATE_SAVE_STDIN_TIMEOUT_S"] = CHILD_DEADLINE_S
    return subprocess.Popen(
        [sys.executable, str(LOOP_STATE_SAVE_PY), subcommand],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _run_open_pipe(subcommand, label, expected_rc, failures):
    """Spawn with an stdin PIPE we never write to and never close."""
    p = _spawn(subcommand)
    t0 = time.time()
    while p.poll() is None and (time.time() - t0) < HANG_GUARD_S:
        time.sleep(0.1)
    elapsed = time.time() - t0
    if p.poll() is None:
        p.kill()
        try:
            p.wait(timeout=5)
        except Exception:
            pass
        failures.append(
            f"{label}: child STILL RUNNING after {HANG_GUARD_S:.0f}s -- the "
            f"deadline never fired (the 11-minute guard-664 hang returning)"
        )
        return None
    if elapsed > FAST_S:
        failures.append(
            f"{label}: child exited but took {elapsed:.1f}s (>{FAST_S:.0f}s) -- "
            f"deadline not honored"
        )
    if p.returncode != expected_rc:
        failures.append(
            f"{label}: expected rc={expected_rc} on degrade-to-empty, got {p.returncode}"
        )
    try:
        p.stdin.close()
    except Exception:
        pass
    return elapsed


def _run_piped(subcommand, payload, label, expected_rc, failures):
    p = _spawn(subcommand)
    try:
        p.communicate(input=payload, timeout=HANG_GUARD_S)
    except subprocess.TimeoutExpired:
        p.kill()
        failures.append(f"{label}: child hung on piped input (communicate timed out)")
        return
    if p.returncode != expected_rc:
        failures.append(f"{label}: expected rc={expected_rc}, got {p.returncode}")


def main() -> int:
    failures = []

    elapsed_a = _run_open_pipe("init", "A(init/non-EOF)", 2, failures)
    _run_piped("init", "{}", "B(init/piped-then-EOF)", 1, failures)
    _run_piped("init", "", "C(init/immediate-EOF)", 2, failures)
    _run_open_pipe("update", "D(update/non-EOF)", 0, failures)

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    shown = f"{elapsed_a:.1f}s" if elapsed_a is not None else "n/a"
    print(f"PASS (4/4 cases; A exited in {shown} via the {CHILD_DEADLINE_S}s deadline)")
    return 0


def test_loop_state_save_stdin_timeout():
    """Behavioral: neither subcommand may hang on a non-EOF stdin."""
    assert main() == 0


def test_loop_state_save_stdin_reads_are_bounded():
    """Structural: both call sites must route through the bounded reader, and
    neither may revert to the bare `raw = sys.stdin.read()` that hung 11
    minutes (g-115-3661 / guard-664)."""
    src = LOOP_STATE_SAVE_PY.read_text(encoding="utf-8")
    assert "def _read_optional_stdin" in src, (
        "loop-state-save.py lost the bounded stdin reader (guard-664 hang regression)"
    )
    assert src.count("_read_optional_stdin().strip()") == 2, (
        "expected BOTH cmd_init and cmd_update to read through the bounded "
        f"reader; found {src.count('_read_optional_stdin().strip()')} call site(s)"
    )
    assert "raw = sys.stdin.read()" not in src, (
        "loop-state-save.py reverted to a bare `raw = sys.stdin.read()` -- the "
        "unbounded read re-introduces the 11-minute hang (g-115-3661)"
    )


def test_missing_checkpoint_warn_prescribes_the_non_blocking_form():
    """The WARN emitted on a skipped Phase 2.95 must name the --json form.

    This is the half that made the original defect self-inflicting: the WARN
    told the reader to run the very command that hung. It must prescribe the
    argument form, and must not claim the bare form 'exits 1' (measured: 2)."""
    # Assert on the RENDERED message, not the raw source. The WARN is written
    # as adjacent string literals, so any substring spanning a line break is
    # absent from the source text while being present in the emitted string --
    # a brittleness this test hit on its first run. ast.parse folds implicit
    # concatenation into one Constant, so this survives any re-wrapping.
    import ast
    tree = ast.parse(LOOP_STATE_SAVE_PY.read_text(encoding="utf-8"))
    literals = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    warn = next(
        (t for t in literals if "MISSING iteration-checkpoint" in t),
        None,
    )
    assert warn is not None, "the missing-checkpoint WARN literal is gone"
    assert "loop-state-save.sh init --json" in warn, (
        "the missing-checkpoint WARN no longer prescribes the non-blocking "
        "--json form -- it must not send a reader back to the command that "
        "hung (g-115-3661 item d)"
    )
    assert "exits 2" in warn and "exits 1" not in warn, (
        "the WARN reverted to claiming a bare `init` 'exits 1'; measured rc is 2"
    )


if __name__ == "__main__":
    sys.exit(main())
