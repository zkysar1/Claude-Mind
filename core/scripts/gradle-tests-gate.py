#!/usr/bin/env python3
"""PreToolUse[Bash] hook -- refuse gradle --tests patterns that silently match
zero tests because the package's first segment is uppercase.

Canonical failure: `./gradlew test --tests 'MyPackage.MyTest'` runs green with
0 tests selected. Gradle emits no error -- it simply selects nothing -- so the
caller reads the result as "the change is fine" or "test discovery is broken
env-wide" and moves on. The class was rediscovered SEVEN times across this
fleet, twice with a wrong mechanism recorded, before the real cause was
isolated: Gradle's `TestSelectionMatcher$TestPattern.patternStartsWithUpperCase`
selects the matcher from the pattern's FIRST CHARACTER alone.

Correct patterns (all three verified working):
  - bare simple class name:  --tests 'MyTest'
  - wildcard-qualified:      --tests '*.MyTest'
  - class + method NAME:     --tests 'MyTest.myMethodName'

The bad-vs-good predicate lives in `_gradle_tests_predicate.py` -- single
source of truth shared with the Layer C audit script. Do NOT inline the check
here.

Fail-open contract (CRITICAL -- do not change without revisiting the trade):
this gate exists to catch a known, repeatedly-rediscovered mistake, not to be a
critical-path dependency. Any parse/IO/logic error -> approve. A broken gate is
recoverable (the audit script catches what we missed); a fail-closed gate would
block legitimate Bash calls and stall autonomous loops. The explicit escape
hatch is the GRADLE_TESTS_GATE_OVERRIDE token anywhere in the command.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    stdin_json_or_approve,
)
from _gradle_tests_predicate import (  # noqa: E402
    OVERRIDE_TOKEN,
    bad_test_patterns,
    suggest_forms,
)


def build_reason(patterns) -> str:
    """Compose the deny message, naming the offending pattern(s) and the three
    working rewrites derived from each."""
    head = (
        "gradle --tests pattern rejected: {} matches ZERO tests and Gradle "
        "reports no error.\n\n"
        "MECHANISM: Gradle's TestSelectionMatcher$TestPattern picks the "
        "selector from the pattern's FIRST CHARACTER only. An uppercase first "
        "character selects SimpleClassNameSelector, so a package-qualified "
        "name parses as class=<first segment>, method=<next segment> and "
        "resolves to nothing. The run then goes green having executed no "
        "tests, which reads exactly like a pass.\n"
    ).format(", ".join("'{}'".format(p) for p in patterns))

    body = []
    for pattern in patterns:
        simple, wildcard, method = suggest_forms(pattern)
        body.append(
            "\nFor '{}' use any of:\n"
            "  --tests '{}'              (bare simple class name)\n"
            "  --tests '{}'            (wildcard-qualified)\n"
            "  --tests '{}'  (class + method NAME, never the display text)".format(
                pattern, simple, wildcard, method
            )
        )

    tail = (
        "\n\nNote: lowercase-initial packages (the Java convention) are "
        "unaffected and are never flagged -- this fires only on "
        "uppercase-initial package segments.\n"
        "If this pattern is genuinely intended, put {} anywhere in the "
        "command to bypass.\n"
        "See .claude/rules/gradle-tests-pattern.md.".format(OVERRIDE_TOKEN)
    )
    return head + "".join(body) + tail


def main():
    payload = stdin_json_or_approve()
    if not isinstance(payload, dict):
        approve_no_mutation()

    if payload.get("tool_name") != "Bash":
        approve_no_mutation()

    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None

    patterns = bad_test_patterns(command)
    if patterns:
        emit_deny(build_reason(patterns))

    approve_no_mutation()


if __name__ == "__main__":
    # except Exception lets SystemExit (raised by approve/emit_deny via
    # sys.exit) propagate cleanly. The catch is only for unexpected bugs in
    # main() - in which case we still fail-open per the docstring contract.
    try:
        main()
    except Exception:
        sys.exit(0)
