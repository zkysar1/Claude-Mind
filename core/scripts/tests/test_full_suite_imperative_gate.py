#!/usr/bin/env python3
"""Pins for the PreToolUse[Bash] full-suite imperative advisory ().

Two things are pinned, and the split matters:

  * the PREDICATE, imported from the gate rather than re-typed (rb-8183). A
    copied predicate agrees on the day it is written and stops agreeing the
    first time either side is edited, without anything going red.
  * the ADVISORY CONTRACT -- `permissionDecision: "allow"` on all four measured
    delivery fields, and exit 0 on every path. This gate is the safety half of
    path-scoping a 37 KB rule; if it ever starts denying, it stalls every test
    invocation in the fleet, and if it stops delivering, the scoping becomes a
    silent regression.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from _full_suite_imperative import (  # noqa: E402
    OVERRIDE_TOKEN,
    build_message,
    matched_families,
)

GATE = SCRIPT_DIR / "full-suite-imperative-gate.py"


# --------------------------------------------------------------------------
# Predicate: fires at command position
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "python3 -m pytest core/scripts/tests -q",
        "python -m pytest mind_api/tests",
        "py -3 -m pytest core/tests/gates",
        "STORAGE_BACKEND=local python3 -m pytest core/scripts/tests",
        "pytest core/scripts/tests",
        "bash core/scripts/run-full-suite.sh --chunks 16",
        "core/scripts/run-full-suite.sh",
        "py -3 core/scripts/run-full-suite.py --triage",
        "cd /tmp; STORAGE_BACKEND=local pytest -q",
        "echo start && python3 -m pytest core/scripts/tests",
    ],
)
def test_framework_family_fires(command):
    assert "framework" in matched_families(command), command


@pytest.mark.parametrize(
    "command",
    [
        "./gradlew test --no-daemon",
        "gradlew test",
        "cd /opt/repo && ./gradlew build",
        "/opt/repo/gradlew test",
    ],
)
def test_gradle_family_fires(command):
    assert "gradle" in matched_families(command), command


# --------------------------------------------------------------------------
# Predicate: does NOT fire on a mere mention
#
# This is the half the naive `\bpytest\b` form gets wrong, and the reason the
# gate resolves statement structure at all. Every case below is an ordinary
# command an agent runs while WORKING ON the test suite -- exactly the
# population that would otherwise generate the most false advisories.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "grep -rn pytest core/scripts/tests | head",
        "ls core/scripts/tests | grep pytest",
        "cat pytest.ini",
        "echo 'run pytest later'",
        "python3 -c \"print('pytest')\"",
        "wc -c .claude/rules/run-full-suite-after-deep-code.md",
        "git log --oneline --grep gradlew",
        "sed -n '1,20p' core/scripts/run-full-suite.py",
        "git add core/scripts/run-full-suite.sh",
        # `bash`/`sh` ARE stripped as wrappers so `bash run-full-suite.sh`
        # fires, but a `-c` payload is a quoted string this gate deliberately
        # does not unwrap: the head becomes `-c`, which matches nothing.
        # Fail-quiet, documented, and pinned so the omission stays deliberate.
        'bash -c "grep pytest notes.md"',
        "bash core/scripts/domain-leak-check.sh",
    ],
)
def test_mention_does_not_fire(command):
    assert matched_families(command) == [], command


@pytest.mark.parametrize(
    "command",
    [
        # The form this repo actually writes everywhere. Omitting `bash` from
        # the wrapper set made the gate miss its single most common trigger
        # while every hand-written probe still passed -- green in test, inert
        # in production (guard-920). Pinned so it cannot regress silently.
        "bash core/scripts/run-full-suite.sh",
        "bash core/scripts/run-full-suite.sh --chunks 16 --confirm-solo",
        "sh core/scripts/run-full-suite.sh",
        "py -3 core/scripts/run-full-suite.py --triage",
    ],
)
def test_repo_native_invocation_forms_fire(command):
    # Every form in the list above IS the runner, so each must be recognised as
    # the EXPENSIVE form too. Asserting the pair (rather than just membership)
    # keeps this pinning both halves: drop the full_suite detection and this
    # fails, rather than silently degrading to the old advisory-only behaviour.
    assert matched_families(command) == ["framework", "full_suite"], command


def test_both_families_in_one_command():
    fams = matched_families("./gradlew test && python3 -m pytest core/scripts/tests")
    assert fams == ["framework", "gradle"]


def test_families_are_deduplicated_and_ordered():
    fams = matched_families("pytest a; pytest b; ./gradlew t; ./gradlew u")
    assert fams == ["framework", "gradle"]


def test_empty_and_garbage_do_not_fire():
    for command in ("", "   ", ";;;", "&&"):
        assert matched_families(command) == []


# --------------------------------------------------------------------------
# Message content: the five heads must survive an edit of the rule
# --------------------------------------------------------------------------

def test_framework_message_carries_the_five_heads():
    msg = build_message(["framework"])
    # Each assertion names a distinct failure mode the rule exists to prevent.
    assert "VERDICT" in msg
    assert "GENUINE" in msg and "FALSE" in msg          # a GENUINE verdict can lie
    assert "RETRY PROTOCOL" in msg                       # the ladder is not a setting
    assert "NEVER PIPE" in msg                           # exit code + verdict loss
    assert "mind_api/tests" in msg                       # CLEAN does not cover it
    assert "STORAGE_BACKEND=local" in msg                # guard-955


def test_gradle_message_carries_the_zero_test_head():
    msg = build_message(["gradle"])
    assert "ZERO tests" in msg
    assert "FULL suite" in msg


def test_message_for_both_families_contains_both():
    msg = build_message(["framework", "gradle"])
    assert "ZERO tests" in msg and "NEVER PIPE" in msg


# --------------------------------------------------------------------------
# Hook contract: advisory, never a deny; exit 0 on every path
# --------------------------------------------------------------------------

def _run(payload):
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc


def test_advisory_shape_on_a_matching_command():
    proc = _run(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "python3 -m pytest core/scripts/tests"},
        }
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    # NEVER a deny. This gate has no opinion on whether the suite should run.
    assert hso["permissionDecision"] == "allow"
    # All four measured delivery fields (). Dropping any one of these
    # was measured NOT to reach the model, so this is a delivery pin, not style.
    assert hso["permissionDecisionReason"]
    assert hso["additionalContext"]
    assert out["systemMessage"]
    assert "VERDICT" in out["systemMessage"]


def test_stderr_stays_one_line():
    """The structured channel carries the 25-line imperative; stderr carries a
    pointer. This gate fires on ordinary daily commands, and a full banner in
    the terminal each time trains a human to stop reading hook output."""
    proc = _run(
        {"tool_name": "Bash", "tool_input": {"command": "pytest core/scripts/tests"}}
    )
    assert len([l for l in proc.stderr.splitlines() if l.strip()]) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Read", "tool_input": {"command": "pytest x"}},
        {"tool_name": "Bash", "tool_input": {"command": "ls -la"}},
        {"tool_name": "Bash", "tool_input": {}},
        {"tool_name": "Bash"},
        {},
    ],
)
def test_non_matching_inputs_approve_silently(payload):
    proc = _run(payload)
    assert proc.returncode == 0
    assert "permissionDecisionReason" not in proc.stdout


def test_malformed_stdin_fails_open():
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0


def test_override_token_suppresses():
    proc = _run(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "python3 -m pytest core/scripts/tests  # " + OVERRIDE_TOKEN
            },
        }
    )
    assert proc.returncode == 0
    assert "VERDICT" not in proc.stdout


# --------------------------------------------------------------------------
# Wiring: the hook must actually be registered, or all of the above is inert
#
# guard-1943 / the pre-edit-context-gate precedent: pinning a gate's behaviour
# says NOTHING about whether anything calls it. Both of those shipped correct
# and inert, with green suites throughout.
# --------------------------------------------------------------------------

def test_hook_is_wired_into_settings():
    settings = json.loads((SCRIPT_DIR.parent.parent / ".claude" / "settings.json").read_text())
    commands = [
        h.get("command", "")
        for entry in settings.get("hooks", {}).get("PreToolUse", [])
        if entry.get("matcher") == "Bash"
        for h in entry.get("hooks", [])
    ]
    assert any("full-suite-imperative-gate.sh" in c for c in commands), (
        "gate is not registered as a PreToolUse[Bash] hook — it will never fire"
    )


def test_wrapper_and_body_both_exist():
    assert GATE.is_file()
    assert (SCRIPT_DIR / "full-suite-imperative-gate.sh").is_file()


# --------------------------------------------------------------------------
# The EXPENSIVE-form consultation trigger (Layer 3, 2026-09-04)
#
# The advisory fires on every pytest invocation; the consult must fire ONLY on
# the runner. The negative controls below are the load-bearing half -- without
# them, a regression that emitted "full_suite" unconditionally would satisfy
# every positive assertion in this file.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "bash core/scripts/run-full-suite.sh",
        "bash core/scripts/run-full-suite.sh --chunks 24",
        "sh core/scripts/run-full-suite.sh",
        "py -3 core/scripts/run-full-suite.py --triage",
        "python3 core/scripts/run-full-suite.py",
        "STORAGE_BACKEND=local bash core/scripts/run-full-suite.sh",
    ],
)
def test_runner_forms_are_flagged_expensive(command):
    assert "full_suite" in matched_families(command), command


@pytest.mark.parametrize(
    "command",
    [
        "pytest core/scripts/tests/test_one.py",
        "pytest",
        "python3 -m pytest core/scripts/tests",
        "py -3 -m pytest core/scripts/tests/test_one.py -q",
        "./gradlew test --no-daemon",
    ],
)
def test_cheap_and_unrelated_forms_are_not_flagged_expensive(command):
    """A targeted pytest is seconds, not hours. Asking for two retrieval
    queries before each one is how a reader learns to skip the banner."""
    assert "full_suite" not in matched_families(command), command


def test_consult_text_only_accompanies_the_expensive_form():
    expensive = build_message(matched_families("bash core/scripts/run-full-suite.sh"))
    cheap = build_message(matched_families("pytest core/scripts/tests/test_one.py"))
    assert "CONSULT THE LIVE STORE BEFORE YOU LAUNCH" in expensive
    assert "CONSULT THE LIVE STORE BEFORE YOU LAUNCH" not in cheap
    # The cheap form must still get the ordinary imperative -- this change adds
    # a section, it does not gate the existing advisory behind the runner.
    assert "VERDICT:" in cheap


def test_consult_carries_two_runnable_queries_both_with_include_framework():
    """The text asserts --include-framework is REQUIRED, so both emitted
    commands must actually carry it (guard-355 / guard-5994: a check form that
    was never run is a prediction). Two queries, not one: a subject-only query
    systematically misses guardrails indexed on the mechanism."""
    msg = build_message(["full_suite"])
    queries = [
        ln for ln in msg.splitlines()
        if "retrieve.sh" in ln and "--category" in ln
    ]
    assert len(queries) == 2, queries
    for q in queries:
        assert "--include-framework" in q, q


def test_consult_names_the_measured_incident_and_its_guardrail():
    """The cost figure is what makes this readable rather than nagging. If a
    later edit trims it to a bare instruction, this fails."""
    msg = build_message(["full_suite"])
    assert "guard-5866" in msg
    assert "111" in msg


def test_consult_follows_the_imperative_it_qualifies():
    msg = build_message(matched_families("bash core/scripts/run-full-suite.sh"))
    assert msg.index("VERDICT:") < msg.index("CONSULT THE LIVE STORE")


def test_full_suite_alone_does_not_emit_the_gradle_section():
    msg = build_message(["full_suite"])
    assert "gradlew" not in msg
