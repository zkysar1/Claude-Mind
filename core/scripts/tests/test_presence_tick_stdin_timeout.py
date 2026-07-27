"""test_presence_tick_stdin_timeout.py -- regression for .

presence-tick.py is a LIVE PostToolUse hook (settings.json matcher='*', fires
on every tool call). It read its event payload via an UNBOUNDED
`json.load(sys.stdin)`. When Claude Code handed it an inherited stdin pipe
that never reached EOF (parent session died / payload write interrupted), the
read blocked forever and the py.exe child orphaned: the stale-jobs scanner
observed pid-18456 at 129h and pid-23168 at 120h
(core/logs/stale-scanner-report.jsonl, 2026-06-20).

Fix (guard-664 / rb-1568 daemon-thread+join pattern, mirrored from
experience.py::_read_optional_stdin and iteration-close-reminder.py): read
stdin in a daemon thread with a join(timeout) deadline; on timeout return ""
and exit instead of blocking. select()/signal.alarm do NOT work on Windows
pipes; a daemon thread does -- when main() returns the interpreter exits and
the still-blocked daemon reader is killed with it.

The behavioral cases drive the REAL script as a child so the test fully
controls the child's stdin (an open subprocess.PIPE we never write/close ==
the exact orphan condition), rather than depending on the harness's stdin.

Cases:
  A  open non-EOF pipe (never written/closed) -> child EXITS within the
     deadline+margin (pre-fix: hung 120-129h until killed).
  B  piped JSON then EOF                       -> child exits 0 fast
     (happy path: a valid payload still parses through the bounded reader).
  C  immediate EOF (empty input, closed)       -> child exits 0 fast.
Plus a STRUCTURAL guard: the source uses the bounded reader and contains no
bare `json.load(sys.stdin)` (pins against a future revert).

Filed by g-115-1578 (chronic hook-python orphan investigation, zeta).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PRESENCE_TICK_PY = CORE_SCRIPTS / "presence-tick.py"

# Deadline the child enforces (via env); HANG_GUARD is the parent's kill
# threshold. HANG_GUARD sits well above the deadline (a legitimately-slow but
# correct exit is not flagged) and well below the 120-129h pre-fix orphan (a
# regression is caught in seconds).
CHILD_DEADLINE_S = "2"
HANG_GUARD_S = 20.0


def _spawn():
    env = dict(os.environ)
    env["PRESENCE_TICK_STDIN_TIMEOUT_S"] = CHILD_DEADLINE_S
    # Drop MIND_AGENT so a valid-but-unbound payload returns early without
    # writing a presence record (hermetic; no side-effect on world/presence/).
    env.pop("MIND_AGENT", None)
    return subprocess.Popen(
        [sys.executable, str(PRESENCE_TICK_PY)],
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
            f"never fired (the 120-129h orphan regression returning)"
        )
    elif elapsed_a > 10:
        failures.append(
            f"A: child exited but took {elapsed_a:.1f}s (>10s) -- deadline not honored"
        )
    try:
        pa.stdin.close()
    except Exception:
        pass

    # -- B: piped JSON then EOF -> parses + exits 0 fast (happy path) ----------
    pb = _spawn()
    try:
        pb.communicate(input='{"tool_name":""}', timeout=HANG_GUARD_S)
    except subprocess.TimeoutExpired:
        pb.kill()
        failures.append("B: child hung on piped valid JSON (communicate timed out)")
    else:
        if pb.returncode != 0:
            failures.append(f"B: expected exit 0 on valid JSON, got {pb.returncode}")

    # -- C: immediate EOF (empty input, closed) -> exits 0 fast ----------------
    pc = _spawn()
    try:
        pc.communicate(input="", timeout=HANG_GUARD_S)
    except subprocess.TimeoutExpired:
        pc.kill()
        failures.append("C: child hung on immediate-EOF stdin")
    else:
        if pc.returncode != 0:
            failures.append(f"C: expected exit 0 on immediate EOF, got {pc.returncode}")

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASS (3/3 cases; A exited in {elapsed_a:.1f}s via {CHILD_DEADLINE_S}s deadline)")
    return 0


def test_presence_tick_stdin_timeout():
    """Behavioral: the live PostToolUse hook must not hang on a non-EOF pipe."""
    assert main() == 0


def test_presence_tick_stdin_read_is_bounded():
    """Structural guard: presence-tick.py must use the bounded reader and must
    NOT revert to a bare `json.load(sys.stdin)` (the unbounded read that
    orphaned the hook 120-129h, g-115-1578 / rb-1568)."""
    src = PRESENCE_TICK_PY.read_text(encoding="utf-8")
    assert "_read_stdin_with_timeout" in src, (
        "presence-tick.py lost the bounded stdin reader (guard-664 hang regression)"
    )
    assert "json.load(sys.stdin)" not in src, (
        "presence-tick.py reverted to bare json.load(sys.stdin) -- unbounded read "
        "re-introduces the 120-129h orphan (g-115-1578 / rb-1568)"
    )


if __name__ == "__main__":
    sys.exit(main())
