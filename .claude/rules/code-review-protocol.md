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
- Before opening or merging a PR in a product/deployment repo — step 4's
  consultation applies at the pre-merge moment too (downstream-prod
  extension, 2026-08-13; see "Product-repo scope" below)

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
   product-repo changes at the pre-merge moment; see "Product-repo scope"
   below):
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

## Product-repo scope (back-ported from downstream prod, 2026-08-13)

Step 4 was scoped to framework files, so product/deployment-repo work (web
apps, product code) rode the honor system — and both misses that motivated
this extension happened in ONE merge (measured on downstream prod by omni,
ZDS rb-1212): a guardrail already described the exact defect being fixed AND
named the sibling PR that later collided mid-flight, and another guardrail
mandated the repo's pre-merge scanner, which was skipped and run only
retroactively. Neither was consulted, because nothing required it.

Rule: before designing a fix, opening a PR, or merging in a product/
deployment repo, run the same TWO queries (subject + mechanism). For a
merge, the MECHANISM query is the merge operation itself ("merging a PR to
an auto-deploying repo") — that is what surfaces the merge-readiness rails
and any repo-specific pre-merge scanner the domain registers. This does not
add an approval wait (standing merge grants are untouched); it adds the same
20-second consultation framework files already get. Honor-system like the
rest of step 4 — no gate counts these queries; it is written down because it
was measured.

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
