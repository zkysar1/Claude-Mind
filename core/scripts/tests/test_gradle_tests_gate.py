"""Tests for the gradle --tests uppercase-package gate (g-115-3341).

guard-1451: structural/source-text assertions are NEVER sufficient for a wired
gate. Every case below is BEHAVIORAL — it spawns the real gate as a subprocess,
feeds it a real PreToolUse payload on stdin, and asserts on the actual
deny/approve decision. The predicate unit tests at the bottom are a supplement,
not the evidence.

Hook contract under test (hook_helpers): approve = exit 0 + EMPTY stdout;
deny = exit 0 + JSON on stdout carrying permissionDecision "deny".
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
GATE = PROJECT_ROOT / "core" / "scripts" / "gradle-tests-gate.py"
AUDIT = PROJECT_ROOT / "core" / "scripts" / "gradle-tests-audit.py"

sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
from _gradle_tests_predicate import (  # noqa: E402
    bad_test_patterns,
    is_package_qualified,
    suggest_forms,
)


def run_gate(payload):
    """Invoke the gate exactly as Claude Code does. Returns (rc, stdout)."""
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout


def bash_payload(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def assert_denied(command):
    rc, out = run_gate(bash_payload(command))
    assert rc == 0, "hook contract violated: must always exit 0, got {}".format(rc)
    assert out.strip(), "expected a deny payload for {!r}, got empty stdout".format(
        command
    )
    parsed = json.loads(out)
    decision = parsed["hookSpecificOutput"]["permissionDecision"]
    assert decision == "deny", "expected deny for {!r}, got {}".format(command, decision)
    return parsed["hookSpecificOutput"]["permissionDecisionReason"]


def assert_approved(command):
    rc, out = run_gate(bash_payload(command))
    assert rc == 0, "hook contract violated: must always exit 0, got {}".format(rc)
    assert out.strip() == "", (
        "expected approval (empty stdout) for {!r}, got: {}".format(command, out[:400])
    )


# --- The goal's own verification criterion -----------------------------------
# "the gate refuses the failing form and passes all three working forms"


def test_refuses_the_failing_form():
    """The canonical footgun: package-qualified, uppercase-initial package."""
    assert_denied("./gradlew test --tests 'MyPackage.MyTest'")


@pytest.mark.parametrize(
    "pattern",
    [
        "MyTest",             # bare simple class name
        "*.MyTest",           # wildcard-qualified
        "MyTest.myMethod",    # class + method NAME
    ],
)
def test_passes_all_three_working_forms(pattern):
    assert_approved("./gradlew test --tests '{}'".format(pattern))


# --- No-false-positive cases (the expensive failure for a Bash-path gate) ----


def test_lowercase_package_is_never_flagged():
    """Conventional Java packages resolve via FullQualifiedClassNameSelector
    and work fine. Flagging them would break every normal repo."""
    assert_approved("./gradlew test --tests 'com.foo.bar.MyTest'")


def test_non_gradle_command_with_tests_flag_is_ignored():
    """`--tests` is not exclusive to gradle; only gate gradle invocations."""
    assert_approved("some-other-runner --tests 'MyPackage.MyTest'")


def test_plain_command_approved():
    assert_approved("ls -la")


def test_non_bash_tool_is_ignored():
    rc, out = run_gate(
        {"tool_name": "Write", "tool_input": {"command": "./gradlew --tests 'A.B'"}}
    )
    assert rc == 0 and out.strip() == ""


# --- Fail-open contract ------------------------------------------------------


def test_malformed_stdin_fails_open():
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_missing_tool_input_fails_open():
    rc, out = run_gate({"tool_name": "Bash"})
    assert rc == 0 and out.strip() == ""


def test_override_token_bypasses():
    assert_approved(
        "GRADLE_TESTS_GATE_OVERRIDE=1 ./gradlew test --tests 'MyPackage.MyTest'"
    )


# --- Shape coverage ----------------------------------------------------------


def test_equals_form_is_caught():
    assert_denied("./gradlew test --tests=MyPackage.MyTest")


def test_double_quoted_form_is_caught():
    assert_denied('./gradlew test --tests "MyPackage.MyTest"')


def test_unquoted_form_is_caught():
    assert_denied("./gradlew test --tests MyPackage.MyTest")


def test_wildcard_that_leaves_first_char_uppercase_still_fails():
    """Truncating the package does not help — the FIRST CHARACTER decides."""
    assert_denied("./gradlew test --tests 'MyPack*.MyTest'")


def test_deep_package_is_caught():
    assert_denied("./gradlew test --tests 'MyPackage.sub.MyTest'")


def test_deny_message_names_the_three_working_forms():
    reason = assert_denied("./gradlew test --tests 'MyPackage.MyTest'")
    assert "--tests 'MyTest'" in reason, "must offer the bare simple name"
    assert "--tests '*.MyTest'" in reason, "must offer the wildcard form"
    assert "MyTest.<methodName>" in reason, "must offer the class+method form"
    assert "zero" in reason.lower() or "0 tests" in reason.lower()


def test_multiple_bad_patterns_all_reported():
    reason = assert_denied(
        "./gradlew test --tests 'MyPackage.MyTest' --tests 'OtherPkg.OtherTest'"
    )
    assert "MyPackage.MyTest" in reason
    assert "OtherPkg.OtherTest" in reason


def test_mixed_good_and_bad_denies():
    """One bad pattern poisons the run even alongside a valid one."""
    assert_denied("./gradlew test --tests 'MyTest' --tests 'MyPackage.MyTest'")


# --- Layer C detective -------------------------------------------------------


def test_audit_runs_clean_on_the_live_corpus():
    """The detective must not flag its own documentation (guard-319 / rb-349).

    This is the regression guard for the false-positive class: the rule file,
    the gate, and this test all quote the bad form deliberately.
    """
    proc = subprocess.run(
        [sys.executable, str(AUDIT), "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(PROJECT_ROOT),
    )
    assert proc.returncode == 0, proc.stderr[:500]
    result = json.loads(proc.stdout)
    assert result["count"] == 0, "audit flagged: {}".format(result["hits"][:5])


def test_audit_detects_a_planted_hit(tmp_path):
    """Positive control — proves the clean result above is a real measurement
    and not a scanner that matches nothing."""
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "planted.sh").write_text("./gradlew test --tests 'MyPackage.MyTest'\n")

    proc = subprocess.run(
        [sys.executable, str(AUDIT), "--json", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = json.loads(proc.stdout)
    assert result["count"] == 1, result
    assert result["hits"][0]["pattern"] == "MyPackage.MyTest"

    proc = subprocess.run(
        [sys.executable, str(AUDIT), "--exit-on-hits", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1, "--exit-on-hits must exit 1 when hits exist"


def test_audit_skips_commented_and_backticked_mentions(tmp_path):
    """guard-319's two required layers, proven independently."""
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "commented.sh").write_text(
        "# ./gradlew test --tests 'MyPackage.MyTest'   <- counter-example\n"
    )
    rules = tmp_path / ".claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "doc.md").write_text(
        "Never write `./gradlew test --tests 'MyPackage.MyTest'` in a build.\n"
    )

    proc = subprocess.run(
        [sys.executable, str(AUDIT), "--json", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = json.loads(proc.stdout)
    assert result["count"] == 0, "prose filter failed: {}".format(result["hits"])


# --- Predicate units (supplement to the behavioral cases above) --------------


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("MyPackage.MyTest", True),
        ("MyPackage.sub.MyTest", True),
        ("MyPack*.MyTest", True),
        ("MyTest", False),
        ("*.MyTest", False),
        ("*yPackage.MyTest", False),
        ("MyTest.myMethod", False),
        ("com.foo.MyTest", False),
        ("", False),
        ("MyTest.", False),
        (None, False),
        (123, False),
    ],
)
def test_is_package_qualified(pattern, expected):
    assert is_package_qualified(pattern) is expected


def test_bad_test_patterns_requires_gradle():
    assert bad_test_patterns("runner --tests 'A.B'") == []
    assert bad_test_patterns("./gradlew --tests 'A.B'") == ["A.B"]
    assert bad_test_patterns("gradle --tests 'A.B'") == ["A.B"]


def test_bad_test_patterns_non_string_fails_open():
    assert bad_test_patterns(None) == []
    assert bad_test_patterns(42) == []


def test_suggest_forms():
    assert suggest_forms("MyPackage.MyTest") == [
        "MyTest",
        "*.MyTest",
        "MyTest.<methodName>",
    ]
