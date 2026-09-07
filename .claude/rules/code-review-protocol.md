---
description: "Treat review findings as hypotheses: probe each with evidence and run the subject+mechanism retrieve.sh consults before applying a fix."
---

# Code Review Protocol

## Principle

When asked to review code — own session work, prior session work, or
arbitrary code — follow a structured protocol that forces hypotheses to
meet evidence before being applied. Ad-hoc review tends to either
over-report (false-positive bugs re-asserted from memory) or under-report
(missing bugs that targeted probes would catch).

Complements `verify-before-assuming.md` (gates negative conclusions during
execution) and `pre-completion-review.md` (gates finishing a goal). Code
review is the third moment: BEFORE applying fixes derived from review.

## When this applies

- User asks for code review, fresh-eyes review, second look, or similar
- Agent decides on its own to review session or partner work for bugs
- Any moment where fixes are about to be applied based on review
  findings (NOT findings derived from a failing test or other
  ground-truth signal)
- Before a product-repo PR/merge, or any infrastructure operation
  (invoking a service directly, provisioning, cold-starting) — step 4
  applies there too; see "Scope beyond framework files" below

## Protocol

1. **Initial findings** — list suspected bugs/issues as hypotheses.
   State cleanly per `communication-clarity.md` rule 6 — do not hedge
   ("might be a bug"); either state or do not.

2. **Verification questions** — for each finding, write a concrete test
   that would falsify or confirm it. Phrase as questions, not assertions.

3. **Probe** — answer each verification question with concrete evidence:
   Python test for code behavior, env probe for shell/config state, regex
   tester for pattern bugs, file read for state claims. Do not re-state
   from memory. The probe IS the answer.
4. **Pre-apply consultation** (MANDATORY for framework-file fixes —
   `core/`, `.claude/`, `world/conventions/`, `core/config/` — AND for
   product-repo merges and infrastructure operations; see "Scope beyond
   framework files" below):
   For each fix about to be applied, run **TWO** queries — one phrased around
   the SUBJECT (what is being changed), one around the MECHANISM (how it is
   being changed, i.e. the shape of the edit operation):

   ```bash
   # 1. SUBJECT — what the fix is about
   bash core/scripts/retrieve.sh --category "<one-line fix description>" --depth shallow --include-framework
   # 2. MECHANISM — the edit operation you are about to perform
   bash core/scripts/retrieve.sh --category "<the operation, in its own words>" --depth shallow --include-framework
   ```

   Read returned reasoning_bank + guardrails + framework_rules from BOTH, look
   for entries that describe the SAME fix pattern. If any CONTRADICTS the
   intended fix, STOP — re-read, re-evaluate. Either apply the entry's pattern
   instead, OR retire the entry with justification if genuinely stale. If an
   entry REINFORCES the fix, increment its `utilization.times_helpful`.

   `--include-framework` is REQUIRED, not optional (g-115-3777): without it
   the response carries no `framework_rules` key at all, so the rules and
   conventions most likely to already prescribe the fix are silently absent.
   `--category` accepts free text (token-overlap, not a category key). Two
   queries, not one concatenated query: a subject query systematically misses
   guardrails indexed on the MECHANISM (measured 16/16 on framework goals;
   the cause is token DILUTION under the top-20 cap, so concatenation makes it
   worse). Evidence, worked examples, the rejected shortcut, and the incident
   that motivated the step (rb-774 / guard-165): `retrieval-triggers.md`
   § "Why TWO queries" (`load-conventions.sh retrieval-triggers`).

5. **Revise findings** — update the findings list based on probe +
   consultation results. Drop falsified ones. Add ones surfaced by
   consultation.

6. **Apply fixes** — only after steps 1-5. Surgical per
   `implementation-discipline.md`.

7. **Run tests** — verify no regressions.

8. **Report** — state findings, applied fixes, test results. Per
   `communication-clarity.md` rule 6: assert observed evidence.

## Scope beyond framework files (two measured extensions)

Step 4 was written for framework files, so work that did not LOOK like one
rode the honor system. Two lanes have been measured walking into rails that
were already encoded.

**Product/deployment repos** (2026-08-13, ZDS rb-1212): one merge missed a
guardrail naming the defect being fixed and the sibling PR that later
collided, plus another mandating the repo's pre-merge scanner. Run both
queries before designing a fix, opening a PR, or merging; for a merge the
MECHANISM query is the merge itself ("merging a PR to an auto-deploying
repo"), which surfaces the merge-readiness rails and repo scanners. Standing
merge grants untouched — no approval wait.

**Infrastructure operations** (2026-09-06, g-115-9291): invoking a service
directly, provisioning, cold-starting. The SUBJECT query ran; the
MECHANISM one ("invoking a service directly, bypassing its caller") was
skipped because this was infra, not a framework edit — walking into a
documented bypass that already carried two prior incidents.

Both share a shape: SUBJECT feels necessary, MECHANISM is what would have
fired. Honor-system: no gate counts these.

## Anti-patterns

- "I think there's a bug here" — state cleanly or do not state
- Re-asserting from in-context memory instead of running a probe
- Applying a framework-file fix without running step 4
- Opening or merging a product-repo PR without step 4's queries (and the
  domain's merge-readiness checklist where one exists)
- Stopping at "the fix passed tests" — if step 4 was skipped, a passing
  test does not falsify a guard violation
- Treating step 4 as a category lookup ("I'll check the
  scanner-authoring category") — use `--text` retrieval; that is what the
  canonical incident's category mismatch demonstrated

## Cross-references

- `rb-774` — verification-questions discipline (success pattern)
- `guard-165` — env-var injection vs. bash interpolation (the specific
  guard whose miss motivated this rule)
- `verify-before-assuming.md` — negative conclusions during execution
- `pre-completion-review.md` — re-read your own work before declaring done
- `implementation-discipline.md` — surgical fixes, no scope creep
- `communication-clarity.md` rule 6 — assert observed evidence
- `core/config/conventions/retrieval-triggers.md` § "Why TWO queries" —
  the measured evidence and worked examples for step 4 (moved from this rule)
