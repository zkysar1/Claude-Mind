"""Pin : state-update / learning-gate must read the goal record forward.

THE DEFECT, measured twice in one day (echo, hostname cc-03, uname -r
6.8.0-136-generic, 2026-08-05) from two OPPOSITE causes:
  - g-315-532: verify NEVER RAN — the execution diary jumps from the claim
    straight to state-update.
  - g-115-4718, 56 minutes later: verify RAN and was REFUSED (the call omitted
    --status, so do_verify exits 2 at its entry check before doing anything).
Both times state-update, learning-gate and productivity-check all reported
SUCCESS over a goal record still at status=pending with a live claim, which
left it a live goal-selector candidate one step from re-executing work that had
already been committed.

The only pre-existing read of the record, `_probe_goal_status`, is called ONLY
inside `_print_recovery_instructions`, which by construction runs on rc!=0. In
both incidents every phase returned 0, so nothing read anything.

WHY THE PREDICATE IS `pending|in-progress` AND NOT "not terminal".
g-115-5001 proposed refusing when the live status "is not terminal". That is
the goal's REASONED half, not its measured half (guard-1719), and reading the
code falsifies it: do_verify legitimately accepts
`--status <completed|blocked|skipped>`, and `blocked` is NOT in
`_goal_census.TERMINAL_STATUSES` (completed + skipped/expired/decomposed/
superseded). A not-terminal predicate would therefore false-fire on EVERY
legitimate blocked close. `test_blocked_close_does_not_warn` is the pin for
that correction and is the load-bearing test in this file — it is the one that
would have caught the goal's own proposal.

WHY WARN AND NOT REFUSE: guard-2760 — adding a consumer of a failure signal
whose remedy is destructive (halting a close mid-sequence) requires evidence
that a reversible remedy is insufficient, and no loud warning had ever been
tried. A refusal also has a live false-positive path on own-cloud, where the
record is read through a cache and a verify that DID close the goal can still
read stale.

HOW THESE TESTS RUN THE REAL CODE. The precondition sits AFTER the phase entry
validation, so the cheap "validation-reject" invocation the sibling
test_iteration_close_recovery_probe.py uses cannot reach it, and invoking the
phase for real would run the entire ~1250-line state-update with live side
effects. So the behavioural tests EXTRACT the function's bytes from the real
script at test time (never a hardcoded copy — a copy would keep passing after
the real one drifted) and source it with `_probe_goal_status` stubbed. The
structural tests then pin the wiring the extraction cannot see.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _runtime_bash import BASH  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "iteration-close.sh"
FUNC = "_warn_if_goal_not_closed"


def _extract(func: str) -> str:
    """Return the real function's source text, from the real file, at test time.

    Anchors on a column-0 `}` as the terminator, which is this file's style for
    every top-level function. Raises rather than returning a partial body — a
    silently truncated extraction would make every behavioural test below pass
    vacuously, which is the failure mode these tests exist to prevent.
    """
    src = SCRIPT.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(src) if ln.startswith(f"{func}() {{")), None)
    assert start is not None, f"{func} not found in {SCRIPT.name}"
    end = next((i for i in range(start + 1, len(src)) if src[i] == "}"), None)
    assert end is not None, f"no column-0 close brace for {func}"
    return "\n".join(src[start:end + 1])


def _run_predicate(status: str, phase: str = "state-update"):
    """Source the real function with _probe_goal_status stubbed to `status`."""
    harness = f"""
set -uo pipefail
GOAL_ID="g-999-1"; GOAL_STATUS="completed"; SOURCE="world"; OUTCOME="deep"
_probe_goal_status() {{ printf '%s' "{status}"; }}
{_extract(FUNC)}
{FUNC} "{phase}"
echo "RC=$?"
"""
    return subprocess.run(
        [BASH, "-c", harness], capture_output=True, text=True, timeout=60
    )


WARN_MARKER = "FORWARD-PRECONDITION WARNING"


@pytest.mark.parametrize("status", ["pending", "in-progress"])
def test_open_statuses_warn(status):
    """The two statuses both incidents actually exhibited."""
    r = _run_predicate(status)
    assert WARN_MARKER in r.stderr, f"no warning for status={status}"
    assert f"status={status}" in r.stderr, "the warning must name the status it saw"
    assert "RC=0" in r.stdout, "the precondition must never change the phase's rc"


def test_blocked_close_does_not_warn():
    """THE LOAD-BEARING PIN — `blocked` is a legitimate verify close.

    do_verify accepts --status <completed|blocked|skipped>. `blocked` is NOT in
    _goal_census.TERMINAL_STATUSES, so the "refuse when not terminal" predicate
    g-115-5001 proposed would fire on every legitimate blocked close. If this
    test ever reddens, someone has re-widened the predicate back to the goal's
    original proposal and reintroduced that false positive.
    """
    r = _run_predicate("blocked")
    assert WARN_MARKER not in r.stderr, (
        "warned on a legitimate blocked close — the predicate has been widened "
        "back to 'not terminal'; see this module's docstring")


@pytest.mark.parametrize("status", ["completed", "skipped"])
def test_closed_statuses_do_not_warn(status):
    r = _run_predicate(status)
    assert WARN_MARKER not in r.stderr, f"false positive on a closed goal ({status})"


def test_unreadable_record_fails_open():
    """_probe_goal_status prints "" for an unset/unparseable id, an unreadable
    queue, and g-xw-* ids whose aspiration is not derivable. All must be silent:
    the recovery block already models this as "asserting neither direction"."""
    r = _run_predicate("")
    assert WARN_MARKER not in r.stderr, "asserted a direction on an unreadable record"
    assert "RC=0" in r.stdout


def test_predicate_is_not_the_terminal_set():
    """Guard the CORRECTION itself, at the source level.

    The behavioural tests above prove today's predicate behaves correctly. This
    one pins that nobody reintroduces the falsified formulation by reaching for
    the terminal-status vocabulary here — which would read as principled (it is
    the canonical set) while silently re-adding the blocked false positive.
    """
    body = _extract(FUNC)
    for token in ("TERMINAL_STATUSES", "ABANDONED_STATUSES"):
        assert token not in body, (
            f"{FUNC} references {token}; the terminal set INCLUDES neither "
            f"'blocked' nor a not-terminal test that is safe here — see the "
            f"module docstring")
    assert "pending" in body and "in-progress" in body


# ── structural pins: the wiring the extraction cannot see ──────────────────

def _code_lines():
    """Non-comment lines only — this file documents the call in comments right
    above it, so a raw substring count conflates prose with code (guard-1099)."""
    return [ln for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")]


def test_both_phases_call_it():
    """Both phases, not just the first:  showed learning-gate
    reporting success over an open record independently, and a closer may run
    learning-gate on its own."""
    calls = [ln for ln in _code_lines() if re.search(rf"^\s*{FUNC}\s+\"", ln)]
    assert len(calls) == 2, f"expected exactly 2 call sites, got {calls}"
    joined = "\n".join(calls)
    assert '"state-update"' in joined and '"learning-gate"' in joined


def test_calls_are_on_the_success_path_not_in_a_recovery_block():
    """The entire defect is that every pre-existing read sits behind rc!=0.

    A call placed inside _print_recovery_instructions would satisfy
    test_both_phases_call_it while restoring the exact blind spot, so pin that
    both calls land inside the phase functions and after
    _print_recovery_instructions has ended.
    """
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()

    def line_of(pat):
        return next(i for i, ln in enumerate(lines)
                    if re.search(pat, ln) and not ln.lstrip().startswith("#"))

    recovery = line_of(r"^_print_recovery_instructions\(\) \{")
    su = line_of(r"^do_state_update\(\) \{")
    lg = line_of(r"^do_learning_gate\(\) \{")
    calls = [i for i, ln in enumerate(lines)
             if re.search(rf"^\s*{FUNC}\s+\"", ln) and not ln.lstrip().startswith("#")]

    assert len(calls) == 2
    for c in calls:
        assert c > recovery, f"call at line {c+1} precedes the recovery block"
    assert su < calls[0] < lg, "first call must be inside do_state_update"
    assert calls[1] > lg, "second call must be inside do_learning_gate"


def test_precondition_runs_before_the_phase_body():
    """A read placed at the END of state-update would report the record open
    only after ~1250 lines of work had already run and written. Pin that each
    call sits near its phase's entry echo."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    for fn, phase in (("do_state_update", "state-update"),
                      ("do_learning_gate", "learning-gate")):
        start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{fn}() {{"))
        call = next(i for i in range(start, len(lines))
                    if re.search(rf"^\s*{FUNC}\s+\"{phase}\"", lines[i]))
        assert call - start < 30, (
            f"{fn}: precondition is {call - start} lines past the entry — it "
            f"must run before the phase body does its work")
