""": recurring-close.sh must not print its proceed imperative when a phase FAILED.

THE DEFECT. The terminal NEXT-ACTION block branched on exactly two things --
whether `agents/<agent>/session/deadman-disabled` exists, and whether OUTCOME is
deep or routine. MAX_RC appeared nowhere in it, so a close in which `verify`
failed printed a proceed instruction BYTE-IDENTICAL to a fully-successful close.
`exit $MAX_RC` was correct; nothing the reader of the imperative sees was.

Measured once (alpha, cc-04, 2026-08-06, goal g-115-817): verify died on a
10s changelog.lock timeout, the other three phases ran green, and the close
printed the normal proceed line. Ground truth after it: status=in-progress,
achievedCount unmoved, lastAchievedAt 15.5h stale. Because lastAchievedAt is the
field the selector scores recurring urgency from, the goal returned ranked
MAXIMALLY overdue and re-ran work that had already run.

WHAT IS PINNED HERE, and why each half matters:

  A. FAILURE PATH -- on MAX_RC != 0 the output announces the failure, names the
     failed phase, carries an exact retry command, and does NOT contain the
     clean-close proceed line. That last clause is the one that goes red if the
     gate is reverted; the others would survive a revert that merely appended a
     warning above the old proceed line, which is close to what the defect
     already was.

  B. CLEAN PATH -- on MAX_RC == 0 all four outcome x deadman combinations are
     BYTE-IDENTICAL to the pre-change strings. The proceed text is now computed
     once into _next_action and reused, so a refactor that drifts the wording is
     a behaviour change to the loop's terminal contract. These four literals are
     the guard against that.

  C. ARG CAPTURE -- the retry command is captured from the argv that actually
     failed, not reconstructed. The four phases take genuinely different flag
     sets, so a hand-written retry line is one refactor away from naming a shape
     the script never invokes (guard-920).

Both blocks are EXTRACTED FROM recurring-close.sh AND EXECUTED, never
reimplemented -- same pattern as test_recurring_close_canary_suppress.py. A test
that restated the block would pass against its own copy while production drifted.

Run: STORAGE_BACKEND=local python3 -m pytest core/scripts/tests/test_recurring_close_failure_imperative.py -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
RECURRING_CLOSE_SH = CORE_SCRIPTS / "recurring-close.sh"

sys.path.insert(0, str(CORE_SCRIPTS))
from _runtime_bash import BASH  # noqa: E402  (guard-580: never a bare "bash")

START = "# The proceed text is COMPUTED ONCE"
END = "A Bash echo or text summary as the terminal action kills the loop"


def _extract_terminal_block() -> str:
    """Pull the terminal NEXT-ACTION block out of recurring-close.sh verbatim."""
    src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
    i = src.find(START)
    if i < 0:
        raise RuntimeError(f"sentinel not found in recurring-close.sh: {START!r}")
    j = src.find(END, i)
    if j < 0:
        raise RuntimeError(f"end sentinel not found after {START!r}")
    return src[i:src.find("\n", j) + 1]


def _extract_run_phase() -> str:
    """Pull the run_phase function (with its MAX_RC/PHASE_RESULTS init) verbatim."""
    src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
    i = src.find("MAX_RC=0")
    if i < 0:
        raise RuntimeError("MAX_RC=0 init not found in recurring-close.sh")
    j = src.find("\n}\n", src.find("run_phase() {", i))
    if j < 0:
        raise RuntimeError("run_phase closing brace not found")
    return src[i:j + 3]


TERMINAL_BLOCK = _extract_terminal_block()
RUN_PHASE_BLOCK = _extract_run_phase()


def _run_terminal(max_rc, outcome, deadman_disabled,
                  failed_phases="", failed_retry_cmds="", phase_results=""):
    """Execute the EXTRACTED terminal block with controlled inputs."""
    with tempfile.TemporaryDirectory() as td:
        agent_dir = Path(td) / "agent"
        (agent_dir / "session").mkdir(parents=True)
        if deadman_disabled:
            (agent_dir / "session" / "deadman-disabled").write_text("")
        # Assign via bash ANSI-C quoting, which reproduces the caller's bytes
        # EXACTLY — including the presence or absence of a trailing newline.
        #
        # Two rejected forms, both of which silently changed the input shape
        # (guard-920) and cost a mutation-proof round each:
        #   - a python !r literal: repr renders a newline as the two characters
        #     \n, and bash single quotes do not interpret those, so the variable
        #     arrived with no real newline at all;
        #   - a quoted heredoc: `read -d ''` always picks up the heredoc's own
        #     terminating newline, so the no-trailing-newline case could not be
        #     constructed and the test pinning it passed against the reverted
        #     guard — vacuous, which reads as coverage and is worse than absent.
        def _assign(name, value):
            esc = (value.replace("\\", "\\\\").replace("'", r"\'")
                        .replace("\n", "\\n"))
            return f"{name}=$'{esc}'\n"

        script = (
            f'AGENT_DIR={agent_dir.as_posix()!r}\n'
            f'MAX_RC={max_rc}\nOUTCOME={outcome!r}\n'
            f'FAILED_PHASES={failed_phases!r}\n'
            f'PHASE_RESULTS={phase_results!r}\n'
            + _assign("FAILED_RETRY_CMDS", failed_retry_cmds)
            + TERMINAL_BLOCK
        )
        r = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout


# ── B. CLEAN PATH: the four literals, byte-identical ────────────────────────

CLEAN_DEADMAN_PAIR = (
    "emit the deadman pair as the loop re-entry (BOTH calls MANDATORY, in this order) "
    "— (1) ScheduleWakeup(prompt='<<autonomous-loop-dynamic>>', delaySeconds=600), "
    "the self-resurrection net, do NOT omit it; THEN (2) Skill(aspirations) with "
    "args='loop' (the LAST call). Skill ALONE keeps THIS iteration alive but leaves "
    "the NEXT unprotected against a silent text-death — arm the net EVERY iteration"
)

CLEAN_EXPECTED = {
    ("deep", True):
        "[recurring-close] OUTCOME=deep — NEXT ACTION REQUIRED: Call "
        "Skill(aspirations-spark) FIRST (Phase 6 fires on deep; NOT wrapped by "
        "recurring-close.sh), THEN Skill(aspirations) with args='loop'.",
    ("routine", True):
        "[recurring-close] OUTCOME=routine — NEXT ACTION REQUIRED: Call "
        "Skill(aspirations) with args='loop' as your VERY NEXT tool call.",
    ("deep", False):
        "[recurring-close] OUTCOME=deep (deadman-switch ON) — NEXT ACTION REQUIRED: "
        "Call Skill(aspirations-spark) FIRST (Phase 6 fires on deep; NOT wrapped by "
        "recurring-close.sh), THEN " + CLEAN_DEADMAN_PAIR + ".",
    ("routine", False):
        "[recurring-close] OUTCOME=routine (deadman-switch ON) — NEXT ACTION "
        "REQUIRED: " + CLEAN_DEADMAN_PAIR + ".",
}


@pytest.mark.parametrize("outcome,deadman_disabled", list(CLEAN_EXPECTED))
def test_clean_close_line_is_byte_identical(outcome, deadman_disabled):
    """MAX_RC==0 must emit exactly the pre-change string for every combination."""
    out = _run_terminal(0, outcome, deadman_disabled)
    assert CLEAN_EXPECTED[(outcome, deadman_disabled)] in out


def test_clean_close_says_nothing_about_failure():
    """A clean close must not mention a phase failure — no false alarm."""
    out = _run_terminal(0, "routine", False)
    assert "PHASE FAILURE" not in out
    assert "REPAIR FIRST" not in out


# ── A. FAILURE PATH ─────────────────────────────────────────────────────────

FAILED_ARGS = dict(
    failed_phases="verify ",
    phase_results="verify=fail(1) state-update=ok learning-gate=ok productivity=ok ",
    failed_retry_cmds=(
        "bash core/scripts/iteration-close.sh --phase verify --goal g-115-817 "
        "--source world --status completed --outcome deep\n"
    ),
)


def test_failure_announces_and_names_the_failed_phase():
    out = _run_terminal(1, "deep", False, **FAILED_ARGS)
    assert "PHASE FAILURE" in out
    assert "MAX_RC=1" in out
    assert "failed: verify" in out
    assert "verify=fail(1)" in out


def test_failure_carries_the_exact_retry_command():
    out = _run_terminal(1, "deep", False, **FAILED_ARGS)
    assert "iteration-close.sh --phase verify --goal g-115-817" in out


def test_retry_command_survives_a_missing_trailing_newline():
    """The retry line must print even when FAILED_RETRY_CMDS has no final \\n.

    A bare `while read` silently DROPS such a line, and the dropped line is the
    only actionable content in the repair imperative — an imperative that looks
    complete while omitting the one thing to do, which is this goal's own defect
    one level down. run_phase appends the newline today, so this pins the
    decoupling rather than the current caller.
    """
    out = _run_terminal(
        1, "deep", False,
        failed_phases="verify ",
        phase_results="verify=fail(1) ",
        failed_retry_cmds="bash core/scripts/iteration-close.sh --phase verify --goal g-9",
    )
    assert "--phase verify --goal g-9" in out


def test_failure_explains_the_stale_lastachievedat_consequence():
    """The imperative must say WHY, or a reader treats it as a cosmetic warning."""
    out = _run_terminal(1, "deep", False, **FAILED_ARGS)
    assert "lastAchievedAt" in out
    assert "MAXIMALLY overdue" in out


@pytest.mark.parametrize("outcome,deadman_disabled", list(CLEAN_EXPECTED))
def test_failure_never_emits_the_clean_proceed_line(outcome, deadman_disabled):
    """THE MUTATION TARGET. Revert the MAX_RC gate and this is what goes red.

    The other failure-path assertions would all survive a 'fix' that merely
    printed a warning ABOVE the unchanged proceed line — which is barely
    distinguishable from the original defect. This one does not: the clean-close
    line must be ABSENT entirely, so the proceed instruction can only reach the
    reader as step 3 of the repair sequence.
    """
    out = _run_terminal(1, outcome, deadman_disabled, **FAILED_ARGS)
    assert CLEAN_EXPECTED[(outcome, deadman_disabled)] not in out
    assert "NEXT ACTION REQUIRED — REPAIR FIRST" in out


def test_failure_still_tells_the_loop_how_to_continue():
    """Repair must not strand the loop: the re-entry is still stated, as step 3.

    A gate that printed only 'something failed' would trade a silent duplicate
    for a dead loop, which is worse (return-protocol.md).
    """
    out = _run_terminal(1, "routine", False, **FAILED_ARGS)
    assert "3. ONLY once the retry succeeds" in out
    assert "Skill(aspirations)" in out


# ── C. ARG CAPTURE ──────────────────────────────────────────────────────────

def test_retry_command_is_captured_from_the_argv_that_failed():
    """run_phase records the REAL argv, so the retry cannot drift from it."""
    with tempfile.TemporaryDirectory() as td:
        stub_dir = Path(td)
        stub = stub_dir / "iteration-close.sh"
        stub.write_text("#!/usr/bin/env bash\nexit 7\n")
        stub.chmod(0o755)
        script = (
            f'SCRIPT_DIR={stub_dir.as_posix()!r}\n'
            + RUN_PHASE_BLOCK
            + '\nrun_phase verify --phase verify --goal g-1 --source world '
              '--summary "two words" --status completed\n'
              'echo "RC=$MAX_RC"; echo "FAILED=[${FAILED_PHASES}]"; '
              'printf "CMD=%s" "$FAILED_RETRY_CMDS"\n'
        )
        r = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
        assert "RC=7" in r.stdout, r.stdout + r.stderr
        assert "FAILED=[verify ]" in r.stdout
        assert "--phase verify --goal g-1 --source world" in r.stdout
        # %q quoting keeps a multi-word --summary copy-pasteable
        assert "two\\ words" in r.stdout or "'two words'" in r.stdout


def test_successful_phase_records_no_retry_command():
    """Negative control: no failure -> no retry line, and MAX_RC stays 0.

    Without this, every assertion above is consistent with a run_phase that
    records a retry command unconditionally.
    """
    with tempfile.TemporaryDirectory() as td:
        stub_dir = Path(td)
        stub = stub_dir / "iteration-close.sh"
        stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        stub.chmod(0o755)
        script = (
            f'SCRIPT_DIR={stub_dir.as_posix()!r}\n'
            + RUN_PHASE_BLOCK
            + '\nrun_phase verify --phase verify --goal g-1\n'
              'echo "RC=$MAX_RC"; echo "FAILED=[${FAILED_PHASES}]"; '
              'echo "CMD=[${FAILED_RETRY_CMDS}]"; echo "RES=[${PHASE_RESULTS}]"\n'
        )
        r = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
        assert "RC=0" in r.stdout, r.stdout + r.stderr
        assert "FAILED=[]" in r.stdout
        assert "CMD=[]" in r.stdout
        assert "RES=[verify=ok ]" in r.stdout
