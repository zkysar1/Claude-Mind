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

4. **Pre-apply consultation** (MANDATORY for framework-file fixes:
   `core/`, `.claude/`, `world/conventions/`, `core/config/`):
   For each fix about to be applied, run
   `bash core/scripts/retrieve.sh --category "<one-line fix description>" --depth shallow`,
   read returned reasoning_bank + guardrails, look for entries that
   describe the SAME fix pattern. If any CONTRADICTS the intended fix,
   STOP — re-read, re-evaluate. Either apply the entry's pattern instead,
   OR retire the entry with justification if genuinely stale. If an
   entry REINFORCES the fix, increment its `utilization.times_helpful`.

   The `--category` parameter accepts free text — the engine runs
   token-overlap matching (Substring/Entity-index/Word-prefix/Concept)
   on whatever string is passed, not strict category-key equality. The
   parameter name is historical; the behavior is token-overlap. See
   `core/config/conventions/tree-retrieval.md` "Matching strategies"
   and `core/config/conventions/retrieval-triggers.md` G9 for the
   engine details.

5. **Revise findings** — update the findings list based on probe +
   consultation results. Drop falsified ones. Add ones surfaced by
   consultation.

6. **Apply fixes** — only after steps 1-5. Surgical per
   `implementation-discipline.md`.

7. **Run tests** — verify no regressions.

8. **Report** — state findings, applied fixes, test results. Per
   `communication-clarity.md` rule 6: assert observed evidence.

## Why step 4 matters

Without consultation, ad-hoc fixes can violate existing learnings.
Canonical incident (2026-05-09, rb-774): a fresh-eyes review fixed an
`os.environ['WORLD_DIR']` KeyError in a SKILL.md by switching to
`'$WORLD_DIR'` bash interpolation inside `python3 -c` source. That fix
DIRECTLY violated `guard-165` ("never interpolate bash variables into the
Python source text — pass values via env, single-quote python source").
The wrong-direction fix was caught only by a subsequent /encode-session
retrospective. Step 4 closes that gap.

Use `retrieve.sh --category "<free-text>"` for token-overlap retrieval,
not a strict category key. The parameter name is historical — the
engine treats the value as free text and runs Substring/Entity-index/
Word-prefix/Concept matching on tree nodes. Categorization is exactly
where the canonical incident failed: `guard-165` lives under
`framework-architecture`, not where one would naturally look for a
SKILL.md python-in-bash fix. Token-overlap on free-text retrieves
tree nodes regardless of category key.

For supplementary stores (reasoning bank, guardrails, pattern
signatures), the supplementary matcher historically required a
category-key substring match — free-text queries returned zero
supplementary hits. The fallback added 2026-05-12 (see
`core/config/conventions/retrieval-triggers.md` G9 / R3) now matches
free text against title / content / tags / when_to_use when category
match returns empty, restoring symmetry with tree-node matching.

## Anti-patterns

- "I think there's a bug here" — state cleanly or do not state
- Re-asserting from in-context memory instead of running a probe
- Applying a framework-file fix without running step 4
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
