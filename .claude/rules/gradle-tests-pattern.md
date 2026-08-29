---
description: "A gradle --tests pattern with an uppercase first char AND uppercase last dot-segment selects zero tests silently; use the gate's shapes."
paths:
  - "**/*.gradle"
  - "**/*.gradle.kts"
  - "**/build.gradle*"
  - "**/settings.gradle*"
  - "**/gradlew*"
---

<!--
  THIS IS THE g-115-6469 `paths:` EXPERIMENT — the first path-scoped rule in
  this repo. If you are reading this because the rule surprised you, see
  core/config/conventions/rules-loading.md for the design, the instrument and
  how to read out the result.

  Why THIS rule was chosen as the probe: its enforcement does not depend on the
  rule text at all. gradle-tests-gate.sh is a PreToolUse[Bash] hook wired in
  .claude/settings.json, and gradle-tests-audit.py is the detective. So if
  `paths:` turns out to suppress this file more aggressively than intended, the
  gate still refuses a bad pattern and nothing regresses — the worst case is a
  lost explanation, not a lost defence. No other rule in the set has that
  property as cleanly.
-->

# Gradle `--tests` Pattern Correctness

## Principle

A `gradle --tests` pattern whose first character is uppercase AND whose final
dot-segment is also uppercase selects **zero tests**, and Gradle reports no
error. The build goes green having executed nothing.

This is the worst possible failure shape: the signal you get back from a
zero-test run is indistinguishable from the signal you get back from a passing
run. Every consumer downstream — the agent narrating "tests pass", a
verification step, a closure claim — is reading a result that was never
produced.

## The mechanism

Gradle's `TestSelectionMatcher$TestPattern.patternStartsWithUpperCase` chooses
the selector by inspecting the pattern's **first character only**:

| First character | Selector chosen | Pattern is read as |
|---|---|---|
| uppercase | `SimpleClassNameSelector` | `Class` or `Class.method` |
| anything else | `FullQualifiedClassNameSelector` | `pkg.sub.Class` |

So for a package whose first segment is uppercase, the canonical
fully-qualified name is parsed as **class = first segment, method = next
segment** — a method that does not exist — and matches nothing.

```
--tests 'MyPackage.MyTest'   -> class "MyPackage", method "MyTest"   -> 0 tests
--tests 'MyTest'             -> simple class name                    -> works
--tests '*.MyTest'           -> first char '*' -> FQN selector       -> works
--tests 'MyTest.myMethod'    -> class "MyTest", method "myMethod"    -> works
--tests 'com.foo.MyTest'     -> first char lowercase -> FQN selector -> works
```

**Lowercase-initial packages — the Java naming convention — are unaffected.**
This footgun exists only where a package's first segment is capitalized. If
your packages are conventional, you will never meet it.

## Rules

1. **Never package-qualify a `--tests` pattern when the package's first
   segment is uppercase.** Use the bare simple class name, the
   wildcard-qualified form `*.ClassName`, or `ClassName.methodName`.
2. **`ClassName.methodName` takes the METHOD NAME**, never a human-readable
   display name. The method name is what appears in the source.
3. **A `--tests` run that reports zero tests executed is a FAILED
   measurement, not a pass.** Read the test count before drawing any
   conclusion from a targeted run. "BUILD SUCCESSFUL" with 0 tests selected
   tells you nothing about the code.
4. **Do not conclude "test discovery is broken in this environment" from a
   pattern that matches nothing.** That inference has been drawn — and
   recorded as a mechanism — more than once. Check the pattern's first
   character before blaming the toolchain.

## Wildcard subtlety

The wildcard must displace the *first character* to change the selector.
Truncating the package leaves the first character uppercase and still fails:

```
'*.MyTest'          -> works  (first char '*')
'*yPackage.MyTest'  -> works  (first char '*')
'MyPack*.MyTest'    -> FAILS  (first char 'M' — still SimpleClassNameSelector)
```

## Enforcement

| Layer | Mechanism | What it catches |
|---|---|---|
| **A** — gate | `core/scripts/gradle-tests-gate.{py,sh}` (PreToolUse[Bash]) refuses a gradle command carrying a package-qualified `--tests` pattern, naming the three working rewrites derived from the pattern itself | Prevents the zero-test run from ever executing. Fail-open on any error; explicit escape hatch is the `GRADLE_TESTS_GATE_OVERRIDE` token anywhere in the command. |
| **B** — rule (this file) | Behavioral guidance read on demand | Documents the mechanism and the working forms for human and LLM authors. |
| **C** — detective | `core/scripts/gradle-tests-audit.py` scans the framework corpus (`core/scripts`, `core/config`, `.claude/skills`, `.claude/rules`) for the same pattern. Predicate is shared with the gate via `core/scripts/_gradle_tests_predicate.py` (single source of truth). | Catches text authored before the gate shipped, content written through Write/Edit rather than Bash, and any command that ran while the hook was fail-open. Pair with `--exit-on-hits` to fail a recurring goal or pre-commit check. |

## Known behavior: writing *about* the pattern also trips the gate

The gate matches the Bash command's TEXT. A command that merely contains a
package-qualified pattern in a string — generating a fixture, echoing
documentation, composing a test payload — is refused even though no gradle run
would occur. This is the deliberate trade: the predicate cannot distinguish
"about to run this" from "writing this down", and a false negative costs a
silent zero-test run while a false positive costs one token.

Use `GRADLE_TESTS_GATE_OVERRIDE` in the command, or assemble the pattern from
shell/Python fragments at runtime so the literal never appears in the command
text.

## Anti-patterns

- Reading "BUILD SUCCESSFUL" from a `--tests` run without checking the
  executed-test count
- Recording a *mechanism* for the failure ("`--tests` operates on method
  matchers", "discovery is broken env-wide") without isolating which character
  of the pattern changes the behavior
- Truncating a package with a wildcard while leaving the first character
  uppercase
- Reaching for `GRADLE_TESTS_GATE_OVERRIDE` to silence the gate instead of
  rewriting the pattern — the three working forms cover every real case

## Cross-references

- `core/scripts/_gradle_tests_predicate.py` — shared predicate, single source
  of truth for both enforcement layers
- `core/scripts/gradle-tests-gate.py` — Layer A enforcement
- `core/scripts/gradle-tests-audit.py` — Layer C detective
- `.claude/rules/schedule-wakeup-correctness.md` — the gate + shared-predicate
  + rule + detective shape this file's enforcement mirrors
- `.claude/rules/verify-before-assuming.md` — a zero-test run is a
  single silent signal; "tests pass" from it is an unverified positive claim
- `.claude/rules/run-full-suite-after-deep-code.md` — why a targeted run is
  never sufficient evidence on its own

## Origin

The class was rediscovered **seven times** across this fleet before the cause
was isolated, twice with an incorrect mechanism recorded and propagated. The
repeated rediscovery is the reason enforcement moved to the Bash chokepoint:
the knowledge existed each time and did not reach the moment of use. The
domain-specific instance (which repositories and package names exhibit the
uppercase-initial shape) is recorded in the corresponding domain guardrail —
this rule carries only the mechanism, which is a property of Gradle.
