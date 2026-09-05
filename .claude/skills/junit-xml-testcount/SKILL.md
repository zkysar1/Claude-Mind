---
name: junit-xml-testcount
description: "Proves a targeted test run actually EXECUTED tests, by summing per-class counters out of JUnit result XML keyed on the FILENAME. Fires whenever a green build is about to be read as evidence — before claiming 'tests pass' from a targeted run, when a class reports 'no result file', when an executed count looks short against the source, or when a re-run after cleaning the results dir appears to have run nothing. Closes three silent traps that all point the WRONG way: the XML name attribute holds the @DisplayName so class-name matching returns nothing and reads as 'never ran'; @Nested containers write their own files so reading one file under-reports; and an UP-TO-DATE re-run writes no XML at all so absence reads as zero-executed. Mechanizes gradle-tests-pattern.md rule 3 — a run reporting zero tests executed is a FAILED measurement, not a pass."
forged_by: alpha
forged_date: "2026-09-03"
forged_from: gap-050
user-invocable: false
minimum_mode: assistant
tools_used: [Bash]
triggers:
  - "did that test run actually execute anything"
  - "prove the tests actually ran"
  - "zero tests executed"
  - "vacuous green build"
  - "read the executed count from the result xml"
  - "per-class test count from junit xml"
  - "the class reports no result file"
  - "gradle-tests-pattern rule 3"
companion_scripts:
  - core/scripts/junit-xml-testcount.py
conventions: [infrastructure]
---

# /junit-xml-testcount — Prove a Test Run Executed

`BUILD SUCCESSFUL` says the build succeeded. It does not say a single test ran.
This skill turns "tests pass" from a claim into a measurement.

## When to fire

Any moment a green build is about to become evidence — a closure narrative, a
verification step, a "no regression" conclusion. Also fire on the three shapes
below, each of which is a trap rather than a finding.

## The one command

```bash
py -3 core/scripts/junit-xml-testcount.py --results-dir <dir> \
    [--class <ClassName> ...] [--source-dir <test-source-root>] \
    [--newer-than <path edited under test>] [--json]
```

Exit `0` every requested class is proven executed · `1` at least one is not ·
`2` usage/environment error.

Resolve `<dir>` from the repo you are actually in — for a Gradle project it is
typically `<module>/build/test-results/test`. **Do not copy an absolute path out
of any document into this command**: a path recorded on one box is in that box's
filesystem shape and is wrong everywhere else (guard-2612). Resolve the repo
under this box's `AGENT_WRITE_PATH`.

## The three traps, and why each is silent

| Verdict | What happened | What it does NOT mean |
|---|---|---|
| `NO_RESULT_FILE` | No file matched the class | **Not** zero tests. A re-run whose inputs are unchanged is UP-TO-DATE and writes no XML, so absence follows a cleaned results dir. Force execution (`--rerun` / `--rerun-tasks`) and measure again. |
| `ZERO_EXECUTED` | Files exist, counters sum to 0 | A selector matched nothing. Rule 3: this is a FAILED measurement, not a pass. |
| `UNDER_DECLARED` | Executed < declared in source | Usually a nested container whose own file is missing from the sum. |
| `STALE_RESULTS` | Oldest result predates the edits | The counts describe a previous build. |
| `EXECUTED` | Counts are trustworthy | Only that they RAN — failures/errors are reported beside them, read those too. |

**Why the tool keys on the FILENAME.** The `name` attribute on `<testsuite>`
carries the `@DisplayName`, not the class name. Matching on it returns nothing
for any class that declares one — which reads exactly like "the class never
ran". Measured twice on real suites: once a class matched nothing and had run
2/2 green; once a class reported 3 against 5 declared because its `@Nested`
containers wrote their own files (rb-5425).

**The console log is NOT an independent second signal.** `testLogging` prints
the display name too, so grepping the console by class name fails for the same
root cause. Two zeros from two surfaces with one root cause is one signal, and
the second one raises confidence while adding no information (guard-1550). If
you want a console-side check, grep the `@DisplayName` text, not the class name.

## The positive control

`--source-dir` counts `@Test` / `@ParameterizedTest` / `@RepeatedTest` /
`@TestFactory` / `@TestTemplate` declarations in the class's source and compares
them against what executed. When the source cannot be located the control is
reported **unavailable** — never silently treated as satisfied. `@Nested` is
deliberately not counted: it declares a container, not a case.

## Reading the output honestly

- A `PASS` is about EXECUTION, not correctness. Read `failures`/`errors` too.
- `unparseable` files are listed rather than skipped: a swallowed parse error is
  indistinguishable from a zero.
- Without `--source-dir` there is no declared-count control, so a silently
  short sum can still pass. Supply it when the count matters.

## Provenance — registration is PENDING, and deliberately so

Forged from `gap-050` by the alpha **worker Body** on cc-10, 2026-09-03.

This file is intentionally **not yet tagged `forged: true`** and is **not yet in
`world/forged-skills.yaml`**. Those two must land TOGETHER, in one reducer
iteration (guard-5291): the registry syncs fleet-wide instantly through the
own-cloud backend, while this file travels only by git. Writing the registry
entry first orphans it on every other box and refuses every promotion hop until
the two agree — measured 2026-08-27, a registered-but-unpushed skill blocked a
release. A worker cannot close that window because it pushes to
`refs/workers/<agent>/<sid>`, never to main, so `/forge-skill` step 4's
"the iteration close-commit sweeps it to origin" does not hold for this Body.

**To complete the forge (reducer):** merge the worker ref carrying this file,
then in the SAME iteration add `forged: true` to the front matter above and the
matching `world/forged-skills.yaml` entry, then set `gap-050` to a terminal
status. `bash core/scripts/audit-forged-skill-tagging.sh` must read PASS before
and after — it is bidirectional, so tag-without-registry is as red as
registry-without-tag.

## Return Protocol

See `.claude/rules/return-protocol.md`. This skill is a sub-skill: end with a
Bash tool call handing control back to the orchestrator, never a text summary.
