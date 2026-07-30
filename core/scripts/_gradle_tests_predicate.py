"""Single source of truth: "would this gradle --tests pattern match nothing?"

Imported by:
  - gradle-tests-gate.py   (Layer A — enforce at PreToolUse[Bash])
  - gradle-tests-audit.py  (Layer C — observe in the committed corpus)

CRITICAL: do not duplicate these predicates inline anywhere else. If a third
caller needs the same check, import it from here. The two layers MUST agree on
what "bad" means or the detective layer's signal diverges from the gate's
enforcement.

THE MECHANISM (Gradle 8.x, TestSelectionMatcher$TestPattern):
`patternStartsWithUpperCase` picks the selector by looking at the FIRST
CHARACTER of the pattern only:

    first char uppercase -> SimpleClassNameSelector  (pattern = Class[.method])
    otherwise            -> FullQualifiedClassNameSelector

So for a package whose first segment is uppercase, the canonical
fully-qualified name is parsed as class=<first-segment>, method=<next-segment>
and matches nothing. Gradle reports no failure — it simply selects zero tests,
which reads like "the tests passed" or "test discovery is broken env-wide".

    MyPackage.MyTest    -> class "MyPackage", method "MyTest"  -> 0 tests
    MyTest.myMethod     -> class "MyTest",    method "myMethod" -> works
    MyTest              -> simple class name                    -> works
    *.MyTest            -> lowercase-ish first char ('*')       -> works
    com.foo.MyTest      -> lowercase first char -> FQN selector -> works

Lowercase-initial packages (the Java convention) are therefore UNAFFECTED, and
this predicate must never flag them.
"""

import re

# `--tests X` / `--tests=X` / `--tests 'X'` / `--tests "X"`.
# Group 1 is the optional quote; the backreference closes it (an empty group 1
# backreferences to empty, so the unquoted form matches too).
_TESTS_ARG = re.compile(r"--tests(?:\s+|=)(['\"]?)([^'\"\s]+)\1")

# A gradle invocation: the `gradlew` wrapper (./gradlew, gradlew.bat) or a bare
# `gradle` word. Anchored on word boundaries so `upgraded` does not match.
_GRADLE_INVOCATION = re.compile(r"\bgradlew?\b")

# Explicit escape hatch. Present anywhere in the command -> the gate approves.
OVERRIDE_TOKEN = "GRADLE_TESTS_GATE_OVERRIDE"


def _starts_upper(text) -> bool:
    """True when `text`'s first character is uppercase.

    Mirrors Gradle's `Character.isUpperCase(pattern.charAt(0))`, so it is
    Unicode-aware exactly as Gradle is. Empty / non-alphabetic first characters
    (notably the `*` wildcard) return False.
    """
    return bool(text[:1].isupper())


def is_package_qualified(pattern) -> bool:
    """True when `pattern` is a dotted gradle --tests pattern that Gradle will
    resolve to zero tests because its first character is uppercase.

    The discriminator is the FINAL dot-segment, not the presence of a dot:
    `MyTest.myMethod` is uppercase-initial and dotted yet correct, because the
    trailing segment is a method name. A trailing segment that is itself
    uppercase-initial means the pattern is package-qualified — Gradle reads the
    leading segment as the class and the trailing one as a method that does not
    exist.

    Non-string inputs return False (fail-open at the type boundary).
    """
    if not isinstance(pattern, str) or "." not in pattern:
        return False
    if not _starts_upper(pattern):
        return False
    return _starts_upper(pattern.rsplit(".", 1)[1])


def bad_test_patterns(command) -> list:
    """Return every package-qualified --tests pattern in a gradle command.

    Empty list when `command` is not a gradle invocation, carries no --tests
    argument, carries only well-formed patterns, or contains the override
    token. Order follows the command; duplicates are preserved so a caller can
    report a count.
    """
    if not isinstance(command, str):
        return []
    if OVERRIDE_TOKEN in command:
        return []
    if not _GRADLE_INVOCATION.search(command):
        return []
    return [
        pattern
        for _quote, pattern in _TESTS_ARG.findall(command)
        if is_package_qualified(pattern)
    ]


def suggest_forms(pattern) -> list:
    """The three working rewrites for a package-qualified pattern.

    Derived from the pattern itself so the advice is actionable rather than
    generic. The trailing segment is the class the caller actually meant.
    """
    simple = pattern.rsplit(".", 1)[1] if "." in pattern else pattern
    return [simple, "*.{}".format(simple), "{}.<methodName>".format(simple)]
