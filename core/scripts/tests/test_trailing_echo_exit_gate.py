"""Layer-B gate for the trailing-echo exit mask (, guard-1150).

The gate is ADVISORY: it warns on stderr and always approves. Two properties
carry the whole design and are asserted throughout:

  1. It fires ONLY for `run_in_background: true`. That scope restriction is what
     makes it high-precision -- for a backgrounded command the harness surfaces
     the exit status as the task-completion notification, so a masked status is
     ALWAYS a real loss of signal. Foreground commands show their own output.
  2. It NEVER DENIES. The advisory rides the structured channel with
     `permissionDecision: "allow"` -- the command still runs. Plain stderr was
     built first and MEASURED not to reach the model (on exit 0 stderr goes to
     the user's terminal, not to Claude), so every test asserts the decision is
     literally "allow" and never "deny": a warning that blocks would be a
     different feature than the one authorized.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
GATE = CORE_SCRIPTS / "trailing-echo-exit-gate.py"


def _load():
    sys.path.insert(0, str(CORE_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_te_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_g = _load()


def run_gate(command, background=True, tool="Bash"):
    """Invoke the gate exactly as the hook does: JSON on stdin."""
    payload = {"tool_name": tool,
               "tool_input": {"command": command, "run_in_background": background}}
    r = subprocess.run([sys.executable, str(GATE)],
                       input=json.dumps(payload),
                       capture_output=True, text=True, timeout=60)
    # Invariants that hold for EVERY input, warn or not.
    assert r.returncode == 0, "advisory hook must always exit 0 (guard-613)"
    if r.stdout.strip():
        decision = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"]
        assert decision == "allow", (
            "gate emitted permissionDecision=%r. This is an ADVISORY: the "
            "authorized behaviour is warn-and-proceed. Denying would block "
            "work nobody agreed to block." % decision)
    return r


def warned(r):
    return "trailing-echo-exit-gate" in r.stderr


# --------------------------------------------------------------------------
# The masking cases -- these are the bug.
# --------------------------------------------------------------------------

def test_semicolon_echo_warns():
    """The canonical form: `cmd; echo "EXIT=$?"` reports echo's 0."""
    assert warned(run_gate('make build > log 2>&1; echo "EXIT=$?"'))


def test_or_separator_warns():
    """`||` runs the echo precisely WHEN the command failed, so 0 wins."""
    assert warned(run_gate("run-suite.sh > log 2>&1 || echo FAILED"))


def test_newline_separator_warns():
    """A newline is as much a separator as `;`."""
    assert warned(run_gate('pytest -q > log 2>&1\necho "done"'))


def test_printf_also_warns():
    """printf is the same class of ~always-0 reporting statement."""
    assert warned(run_gate('gradle test > log 2>&1; printf "EXIT=%%s\\n" "$?"'))


def test_observed_incident_shape():
    """The 2026-07-27 shape that produced three false 'exit 0' notifications."""
    assert warned(run_gate('git push origin main > /tmp/p.log 2>&1; echo "push exit=$?"'))


# --------------------------------------------------------------------------
# The non-masking cases -- firing on these would be a false positive.
# --------------------------------------------------------------------------

def test_correct_idiom_is_silent():
    """The idiom the advisory itself recommends must not trip the gate."""
    assert not warned(run_gate(
        'make build > log 2>&1; rc=$?; echo "EXIT=$rc"; exit $rc'))


def test_and_separator_is_silent():
    """`&&` short-circuits on failure, so the nonzero status survives."""
    assert not warned(run_gate("make build > log 2>&1 && echo OK"))


def test_foreground_is_silent():
    """Scope restriction: only backgrounded commands mask a notification."""
    assert not warned(run_gate('make build > log 2>&1; echo "EXIT=$?"',
                               background=False))


def test_lone_echo_is_silent():
    """Nothing precedes the echo, so no status is being masked."""
    assert not warned(run_gate('echo "hello world"'))


def test_non_bash_tool_is_silent():
    assert not warned(run_gate('cmd; echo x', tool="Write"))


def test_heredoc_fails_open():
    """A heredoc body can contain anything; the scan cannot be trusted."""
    assert not warned(run_gate("py -3 - <<'PY'\nprint(1)\nPY"))


def test_unbalanced_quotes_fail_open():
    assert not warned(run_gate('make build > log; echo "unterminated'))


def test_override_token_suppresses():
    assert not warned(run_gate(
        'make build > log 2>&1; echo "EXIT=$?"  # TRAILING_ECHO_GATE_OVERRIDE'))


# --------------------------------------------------------------------------
# Predicate unit tests -- quoting is where a hand-rolled scanner breaks.
# --------------------------------------------------------------------------

def test_separator_inside_quotes_is_literal():
    """`;` inside quotes must not split -- else the last statement is wrong."""
    assert _g.analyze('echo "a;b"; make build > log') is None


def test_echo_containing_quoted_semicolon_still_warns():
    """The quote handling must not lose a genuine trailing echo either."""
    assert _g.analyze('make build > log 2>&1; echo "a;b done"') is not None


def test_split_top_level_reports_separators():
    pairs = _g.split_top_level("a; b && c || d")
    assert pairs is not None
    assert [s for s, _ in pairs] == ["", ";", "&&", "||"]


def test_split_top_level_none_on_unbalanced():
    assert _g.split_top_level('echo "oops') is None


def test_analyze_returns_head_stmt_and_separator():
    got = _g.analyze('make build > log 2>&1; echo "EXIT=$?"')
    assert got is not None
    head, stmt, sep = got
    assert head == "echo"
    assert "EXIT=$?" in stmt
    assert sep == ";"


def test_advisory_names_the_correct_idiom():
    """The banner must carry the fix, not just the complaint."""
    err = run_gate('make build > log 2>&1; echo "EXIT=$?"').stderr
    assert "rc=$?" in err and "exit $rc" in err
    assert "PIPESTATUS" in err, "pipes are the sibling failure; name the idiom"
    assert "guard-1150" in err


def test_warning_rides_the_structured_channel():
    """THE delivery property.

    Plain stderr was the first implementation and was measured NOT to reach the
    model: the hook-fire sentinel confirmed the hook ran on a live backgrounded
    command while no banner arrived. On exit 0 Claude Code routes hook stderr
    to the user's terminal, not to Claude. So the reason must travel on the
    structured channel or the gate is decorative.
    """
    r = run_gate('make build > log 2>&1; echo "EXIT=$?"')
    assert r.stdout.strip(), (
        "no structured output -- the advisory would never reach the model, "
        "which is the exact silent failure this gate exists to prevent")
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert "guard-1150" in out["permissionDecisionReason"]
    assert "exit $rc" in out["permissionDecisionReason"]


def test_payload_carries_every_delivery_channel():
    """Pin the exact shape MEASURED to reach the model (2026-07-28).

    Five live probes with the hook-fire sentinel confirming the hook ran every
    time: `allow` + `permissionDecisionReason` alone did NOT deliver; the run
    carrying additionalContext AND systemMessage DID. Whether both are strictly
    required could not be settled in-session (hook context appears deduped per
    session, so a later negative is consistent with "wrong field" AND with
    "already delivered"), so the working shape ships intact.

    This test exists because narrowing these fields is a one-line edit that
    would look like harmless cleanup and would silently make the gate
    decorative -- the precise failure the gate was built to end.
    """
    r = run_gate('make build > log 2>&1; echo "EXIT=$?"')
    payload = json.loads(r.stdout)
    hso = payload["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    for field, container in (("permissionDecisionReason", hso),
                             ("additionalContext", hso),
                             ("systemMessage", payload)):
        assert container.get(field), (
            "%s missing -- do not narrow the delivery channels without a "
            "FRESH-session probe proving the narrower shape delivers "
            "(g-115-3598)" % field)
    assert r.stderr, "stderr copy is what a human at the terminal sees"


def test_silent_cases_emit_no_structured_output():
    """A non-warning call must stay a true no-op: empty stdout, exit 0."""
    r = run_gate('make build > log 2>&1; rc=$?; echo "EXIT=$rc"; exit $rc')
    assert r.stdout == ""


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
