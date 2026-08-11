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

   `--include-framework` is load-bearing here, not optional (g-115-3777,
   measured 2026-07-29). Without it the response has no `framework_rules`
   key AT ALL — the `.claude/rules/*` and `core/config/conventions/*` files
   are simply absent from the result, silently. That is the single worst
   omission for THIS step, because a framework-file fix is exactly the case
   where a convention or rule is most likely to already prescribe the
   pattern. Measured on the query "fix the drain-temp purge glob so cited
   evidence files are not deleted": bare call returned 15 tree nodes / 20 rb
   / 20 guardrails and no framework key; adding the flag returned
   `temp-store.md` — the convention that actually governs that fix.

   The two-query requirement is measured, not stylistic — the evidence,
   worked examples, and the rejected one-query shortcut are in
   "Why TWO queries" below.

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

## Why TWO queries (g-115-3174, measured 2026-08-02)

A subject-phrased query systematically misses guardrails indexed on the
MECHANISM. Measured over the 16 most recent completed framework goals carrying
a recorded consult query (`world/retrieval-trace.jsonl`, joined on
`work_class: framework`), holding flags constant and varying only phrasing:
**16/16 had at least one mechanism-only guardrail that would have changed how
the fix was made** — the bar was fixed before looking, and topical adjacency did
not count. Threshold for acting was 30%.

The cause is **token DILUTION under the top-20 cap, not token disjointness.**
This distinction is load-bearing and the originating hypothesis had it wrong:
g-115-4521's subject query *contained the word "daemon"* ("refuse claim on
terminal-status goal in **daemon** claim endpoint...") and still did not return
guard-742; the mechanism query returned it at rank 1. One mechanism token among
twelve subject tokens cannot lift its guardrail past the cap. This matters for
the fix direction: were the cause disjointness, the remedy would be a retrieval
change (synonym expansion). Because it is dilution, a dedicated second query —
which gives the mechanism clause 100% of a query's weight — is the right shape,
and raising the cap would not reach it.

**Do not collapse this into one combined query — measured and rejected.**
Concatenating subject + mechanism recovered the target in only 4 of 6 spot
checks, and where it survived the rank collapsed: 1→18 (one slot from falling
off), 1→12, 4→9, 2→7, with two rank-1 hits vanishing entirely. Adding subject
tokens dilutes the mechanism tokens by the very mechanism the defect describes.

Worked examples — the mechanism is the OPERATION, not the topic:

| Fix (subject) | Mechanism query | Recovered |
|---|---|---|
| refuse claim on terminal-status goal in daemon claim endpoint | `editing logic behind a daemon-routed wrapper where the live path is the daemon reimplementation` | guard-742, guard-547 |
| align a date-only deadline comparison between two sweeps | `changing a shared predicate that two independent consumers both read` | guard-2275 |
| conf-based test world-isolation defeated by .mind-data | `editing pytest conftest fixtures and test environment isolation` | guard-1165, guard-588 |
| phantom tree nodes recurred | `writing a knowledge tree node and registering it in _tree.yaml` | guard-2317, guard-1195, guard-610 |
| pre-apply-consult-gate skips self-filed goals | `editing a gate predicate under core/scripts/gates` | guard-502, guard-142 |
| generalize the embedded-python-block compile guard | `authoring a new repo-wide scanner and wiring it into the pre-commit gate chain` | guard-1426, guard-914 |

Control run: pairing each goal's subject query against a MISMATCHED goal's
mechanism query returned guardrails relevant to the other goal's operation and
irrelevant to this one — so the effect is the mechanism framing, not an artifact
of the 20-cap. (The one partial overlap was two genuinely adjacent mechanisms,
both "editing a bash wrapper", which is the control behaving correctly.)

**Enforcement reality (guard-302 — name the real mechanism, not an inferred
one):** no gate counts queries. `pre-apply-consult-gate.py` fires per-goal on
framework-file prose, and `pre-apply-consult-drift-gate.py` keys the Phase
0-pre6 sentinel on `retrieval-summary: performed=false` — a boolean. Running one
query satisfies both gates exactly as running two does. The second query is
honor-system, like the rest of step 4; it is written down because it was
measured, not because anything will refuse you for skipping it.

Baseline for whoever checks whether this rule changed anything (measured
2026-08-02 from `world/retrieval-trace.jsonl`, all 9,309 rows): of 2,125 goals
with at least one goal-tied, non-read-only consult, **508 — 23.9% — issued 2+
distinct queries; 76.1% issued exactly one.** Recent windows run higher (39.9%
over 7d, 32.4% over 14d, 25.0% over 30d), so re-measure the window rather than
diffing against the all-time figure. Caveat that biases this number DOWNWARD:
only goal-tied rows can be attributed, so a second query issued without
`--goal` is invisible here — treat 23.9% as a floor, not a point estimate. The
comparison that survives the bias is before/after on the same window width,
since the undercount applies equally to both.

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
