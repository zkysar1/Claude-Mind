---
name: mutation-proof-regression-test
description: "Proves a regression test actually catches its bug by mutation-testing it: runs the exact test on real code (must PASS), applies a targeted sabotage that reverts the fix or breaks the guarded mechanism, re-runs it (must FAIL), then restores the file and re-runs it (must PASS). Fires whenever authoring or reviewing a regression test for a cross-component bug (off-by-one in shared indexing, key/name mismatch, schema or roster drift, producer/consumer boundary), whenever guard-1220's two-way proof is required, or whenever a new test 'passed on its first run against code not yet fixed' (the vacuous-test smell). MUST use the companion script core/scripts/mutation-proof-test.sh, never sabotage-and-restore a file by hand — a missed restore silently ships the sabotage. Use before trusting any regression guard the suite reports GREEN."
forged: true
forged_by: bravo
forged_date: "2026-07-19"
forged_from: gap-019
user-invocable: false
triggers:
  - "mutation-proof a regression test"
  - "prove the test catches the bug"
  - "two-way proof"
  - "is this regression test vacuous"
  - "guard-1220"
parameters:
  - name: target
    description: "File containing the fix/mechanism the test guards (the file to sabotage)"
    required: true
  - name: test-cmd
    description: "Exact command that runs the SPECIFIC test (not the whole suite — it runs 3x)"
    required: true
  - name: sabotage
    description: "The mutation: --sabotage-old/--sabotage-new (string revert) or --sabotage-sed"
    required: true
tools_used: [Bash]
companion_scripts: [core/scripts/mutation-proof-test.sh]
execution_history:
  total_invocations: 3
  outcome_tracking:
    successful: 3
    unsuccessful: 0
    success_rate: 1.0
  last_invocation: "2026-07-19T07:37:46"
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [reasoning-guardrails]
minimum_mode: assistant
revision_id: "skill-bootstrap-mutation-proof-regression-test-b1220f"
previous_revision_id: null
---

# mutation-proof-regression-test — prove a regression test is not vacuous

A regression test that PASSES against the buggy (unfixed) code catches nothing —
it is vacuous. A GREEN suite does not prove a guard enforces its invariant
(rb-4004: two independent bugs once canceled into a false pass). The only proof
is a **two-way mutation test**: make the code buggy again and confirm the test
goes RED, then restore and confirm it goes GREEN. `guard-1220` MANDATES this for
every cross-component regression test.

Doing it by hand is error-prone, and one failure mode is catastrophic: **a
missed restore silently ships the sabotage code**. This skill mandates the
companion script, whose restore is guaranteed by a shell trap and byte-verified
against a backup — so sabotage can never survive the run.

## When this fires

Invoke this skill when ALL or ANY of these hold:

- Authoring a regression test for a bug that spanned TWO components (a producer
  and a consumer, a builder and an adapter, a writer and a reader) — the
  `guard-1220` trigger.
- A new test **passed on its first run against code not yet fixed** — the
  clearest vacuous-test smell.
- Reviewing an existing regression guard the suite reports GREEN, before
  trusting it to protect against its bug class (off-by-one in shared indexing,
  key/name mismatch, schema or roster drift).

## Restricted operation — MUST use the companion script

MUST use `core/scripts/mutation-proof-test.sh`. NEVER back up, sabotage, run,
and restore a target file by hand — the script exists specifically because the
manual form drops the restore step and ships sabotage. The script owns the
mechanical safety (backup, guaranteed+verified restore); the LLM owns only the
semantic choice of WHAT to sabotage.

## Procedure

1. **Identify the target + the fix to revert.** The `--target` is the file
   holding the fix or the mechanism the test guards. Choose a sabotage that
   reverts exactly that fix / breaks exactly that mechanism — the narrowest
   mutation that should make the guarded bug reappear.

2. **Identify the exact test command.** `--test-cmd` must run the SPECIFIC test,
   not the whole suite (it runs three times). Examples of the *shape* (fill in
   your repo's real runner):
   - `--test-cmd './run-one-test.sh <TestClass>#<method>'`
   - `--test-cmd '<test-runner> <path>::<test_name>'`

3. **Run the two-way proof:**
   ```
   Bash: bash core/scripts/mutation-proof-test.sh \
       --target <file-with-the-fix> \
       --workdir <dir-to-run-the-test-from> \
       --test-cmd '<exact single-test command>' \
       --sabotage-old '<distinctive string that IS the fix>' \
       --sabotage-new '<the reverted/broken form>' \
       [--junit-xml '<result-xml-path-or-glob>']
   ```
   Prefer `--sabotage-old/--sabotage-new` (a literal revert of the fix). Use
   `--sabotage-sed '<sed-script>'` only when the mutation is not a clean string
   swap. Pass `--junit-xml` when the runner emits result XML — it asserts the
   test actually EXECUTED (tests>0), catching the guard-1220 case where a build
   reports success while the test was never run.

4. **Read the JSON verdict** (single line on stdout) and act on the exit code:

   | exit | verdict | meaning | action |
   |---|---|---|---|
   | 0 | PASS | GREEN on real code -> RED under sabotage -> GREEN after restore | the test is mutation-proof; trust it |
   | 1 | FAIL | vacuous (passed under sabotage), broken (RED on real code), or no-op mutation | fix the TEST (or the mutation), then re-run — do NOT trust the guard yet |
   | 2 | — | usage/operational error (missing target, no test-cmd) | fix the invocation |
   | 3 | RESTORE_FAILED | target does not match the backup after restore | CRITICAL — recover the file from the `.mutation-backup.<pid>` sibling manually before doing anything else |

5. **On PASS**, the regression test is proven. On FAIL, the `reason` field names
   which arm failed (vacuous / broken-baseline / no-op / test-not-executed /
   flaky-after-restore) — address it and re-run until PASS.

## Notes

- The script restores the target on EVERY exit path (normal, error, INT, TERM)
  via a trap, and verifies the restore byte-for-byte against the backup. Exit 3
  is the only path where the file is NOT guaranteed clean — it fires loudly and
  leaves the backup for manual recovery.
- `--skip-baseline` skips the first GREEN check when you have independently
  confirmed the test passes on real code; the RED-under-sabotage and
  GREEN-after-restore arms still run.
- This skill complements `.claude/rules/run-full-suite-after-deep-code.md` (run
  the full suite after deep code) — that rule proves the suite is GREEN; this
  skill proves a specific guard within it is not vacuously GREEN.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the `core/scripts/mutation-proof-test.sh` Bash invocation
(or a follow-up Bash echo handing control back to the caller after reading the
verdict). Never end with a text summary.
