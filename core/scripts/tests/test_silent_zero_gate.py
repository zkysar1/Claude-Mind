"""Tests for the silent-zero pipeline gate ().

guard-1451: structural/source-text assertions are NEVER sufficient for a wired
gate. Every case below is BEHAVIORAL -- it spawns the real gate as a subprocess,
feeds it a real PreToolUse payload on stdin, and asserts on the actual
deny/approve decision. The predicate unit tests at the bottom are a supplement,
not the evidence.

guard-1943: a green suite certifies the FUNCTION, never the WIRING. The last
test asserts the gate is actually registered on the Bash matcher in
.claude/settings.json -- without it this whole file could pass while the hook
never runs, which is the exact failure shape the gate exists to catch.

Hook contract under test (hook_helpers): approve = exit 0 + EMPTY stdout;
deny = exit 0 + JSON on stdout carrying permissionDecision "deny".
"""

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
GATE = PROJECT_ROOT / "core" / "scripts" / "silent-zero-gate.py"
SETTINGS = PROJECT_ROOT / ".claude" / "settings.json"

sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
from _silent_zero_predicate import (  # noqa: E402
    OVERRIDE_TOKEN,
    coerced_fallbacks,
    has_scoring_consumer,
    invokes_framework_wrapper,
    reads_exit_status,
    silent_zero_violations,
)

# The founding incident's command shape, verbatim in structure ().
FOUNDING = (
    "bash core/scripts/aspirations-query.sh --text msg-20260728-202428-bravo-4822 "
    "2>/dev/null | py -3 -c \"import sys,json; "
    "d=json.loads(sys.stdin.read() or '[]'); print(len(d))\""
)


def run_gate(command, tool_name="Bash"):
    """Invoke the gate exactly as Claude Code does. Returns (rc, stdout)."""
    payload = {"tool_name": tool_name, "tool_input": {"command": command}}
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout


def assert_denied(command):
    rc, out = run_gate(command)
    assert rc == 0, f"gate must always exit 0; got {rc}"
    assert out.strip(), f"expected a deny payload, got empty stdout for: {command}"
    payload = json.loads(out)
    hso = payload["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny", hso
    return hso["permissionDecisionReason"]


def assert_approved(command, tool_name="Bash"):
    rc, out = run_gate(command, tool_name=tool_name)
    assert rc == 0, f"gate must always exit 0; got {rc}"
    assert out.strip() == "", f"expected approve (empty stdout), got: {out[:400]}"


# ---------------------------------------------------------------- DENY cases

def test_founding_incident_shape_is_denied():
    """The exact shape that produced a false filed goal must not get through."""
    reason = assert_denied(FOUNDING)
    assert "silent-zero" in reason
    assert "exit status" in reason


def test_denies_without_stderr_silencing():
    """`or '[]'` erases the failure whether or not stderr was discarded, so the
    predicate deliberately does NOT require 2>/dev/null. Pinned because
    requiring it was the tighter of the two measured candidates (78 vs 98) and
    a future reader may assume silencing is part of the shape."""
    assert_denied(
        "bash core/scripts/guardrails-read.sh --active | py -3 -c "
        "\"import sys,json; g=json.loads(sys.stdin.read() or '[]'); print(len(g))\""
    )


def test_denies_brace_coercion_and_domain_wrapper():
    """`or '{}'` and an external $WORLD_DIR domain script are both in scope."""
    assert_denied(
        'bash "$WORLD_DIR/scripts/aws-exec.sh" describe 2>/dev/null | python3 -c '
        "\"import sys,json; d=json.loads(sys.stdin.read() or '{}'); print(len(d))\""
    )


def test_deny_message_names_the_rewrite_and_the_override():
    reason = assert_denied(FOUNDING)
    assert "PIPESTATUS" in reason, "must name a concrete working rewrite"
    assert OVERRIDE_TOKEN in reason, "must name its own escape hatch"
    assert "stderr" in reason, "must pre-empt the wrong fix (move the message)"


# ------------------------------------------------------------- APPROVE cases

def test_pipestatus_guard_is_approved():
    """PIPESTATUS is the CORRECT bash idiom for a piped producer's status.
    Omitting it from the rc predicate misclassified thousands of deliberately
    guarded calls during the g-318-80 survey -- pinned so it cannot regress."""
    assert_approved(
        "bash core/scripts/aspirations-query.sh --goal-status pending | py -3 -c "
        "\"import sys,json; d=json.loads(sys.stdin.read() or '[]'); print(len(d))\"; "
        'echo "producer_rc=${PIPESTATUS[0]}"'
    )


def test_head_tail_windowing_is_approved():
    """`| head -20` windows output for DISPLAY and derives no quantity.
    Counting it as a scoring consumer inflated the measured population ~2.5x
    with non-instances. This is that false-positive class, pinned."""
    assert_approved("bash core/scripts/world-cat.sh program.md 2>&1 | head -20")
    assert_approved("bash core/scripts/journal-read.sh --meta 2>&1 | tail -5")


def test_scoring_without_coercion_is_approved():
    """A scoring pipeline with NO empty-coercion fallback is out of scope.

    This is the noise-budget decision made load-bearing: the broad
    'silenced scoring call' predicate matched 12.71% of all Bash calls and
    would be trained past within a day, versus 0.10% for the coercion idiom.
    Widening the gate to this case must be a deliberate act that breaks this
    test, not a quiet edit to the regex."""
    assert_approved(
        "bash core/scripts/aspirations-query.sh --goal-status pending 2>/dev/null "
        '| grep -c goal_id'
    )


def test_explicit_rc_capture_is_approved():
    assert_approved(
        "out=$(bash core/scripts/guardrails-read.sh --active); rc=$?; "
        "printf '%s' \"$out\" | py -3 -c "
        '"import sys,json; print(len(json.loads(sys.stdin.read() or \'[]\')))"'
    )


def test_override_token_is_approved():
    assert_approved(FOUNDING + f"  # {OVERRIDE_TOKEN}: wrapper prints nothing on success")


def test_non_framework_command_is_approved():
    """No framework wrapper -> not this class, whatever the pipeline looks like."""
    assert_approved(
        "cat somefile.json | py -3 -c "
        "\"import sys,json; d=json.loads(sys.stdin.read() or '[]'); print(len(d))\""
    )


def test_non_bash_tool_is_approved():
    assert_approved(FOUNDING, tool_name="Edit")


def test_malformed_payload_fails_open():
    """Fail-open contract: any parse error approves rather than blocking."""
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_missing_command_key_fails_open():
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {}}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ------------------------------------------------------- predicate unit tests

def test_predicate_components():
    assert invokes_framework_wrapper("bash core/scripts/x.sh")
    assert not invokes_framework_wrapper("bash ./other/x.sh")
    assert has_scoring_consumer("x | grep -c foo")
    assert has_scoring_consumer("x | py -3 -c \"json.loads(s)\"")
    assert not has_scoring_consumer("x | py -3 -c \"print(1)\""), (
        "a python one-liner that parses nothing scores nothing"
    )
    assert not has_scoring_consumer("x | head -3")
    assert reads_exit_status("a; echo ${PIPESTATUS[0]}")
    assert reads_exit_status("set -o pipefail; a | b")
    assert not reads_exit_status("bash x.sh | grep -c y")
    assert coerced_fallbacks("json.loads(sys.stdin.read() or '[]')")
    assert coerced_fallbacks('json.loads(sys.stdin.read() or "{}")')
    assert not coerced_fallbacks("json.loads(sys.stdin.read())")


def test_predicate_type_boundary_fails_open():
    for bad in (None, 123, [], {}):
        assert silent_zero_violations(bad) == []


# ------------------------------------------------------------- WIRING (guard-1943)

def test_gate_is_wired_on_the_bash_matcher():
    """A gate that is not registered never runs, and every other test here
    would still pass. Assert the registration itself."""
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    commands = []
    for entry in settings.get("hooks", {}).get("PreToolUse", []):
        if "Bash" not in str(entry.get("matcher", "")):
            continue
        for hook in entry.get("hooks", []):
            commands.append(hook.get("command", ""))
    assert any("silent-zero-gate.sh" in c for c in commands), (
        "silent-zero-gate.sh is NOT registered on the PreToolUse[Bash] matcher; "
        f"registered commands: {commands}"
    )
