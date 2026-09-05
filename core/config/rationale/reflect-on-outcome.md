# Rationale: reflect-on-outcome

Referenced from `.claude/skills/reflect-on-outcome/SKILL.md`. WHY reasoning for
five structural choices across the Hypothesis, Execution, and Batch Micro modes.

## Why `--as-of` instead of bare `retrieve.sh` (Step 1.5 — bi-temporal reader)

The ABC antecedent asks "what was believed when the hypothesis was formed?"
Reading the CURRENT reasoning bank / guardrails / beliefs answers the wrong
question — those stores have evolved since (records falsified, retired, or
superseded via close-old/insert-new). The bi-temporal reader returns the record
VERSIONS that were valid at formation time T, so the antecedent reflects what
was ACTUALLY believed then, not what is believed now.

`--as-of` returns the RB / guardrails / pattern_signatures / beliefs versions
whose validity interval contained T (`valid_from <= T < valid_to`; `valid_to`
null = still-current). It is status-agnostic (a since-retired record that was
active at T still surfaces) and never bumps retrieval counters (a historical
read is observational). Contrast a bare `retrieve.sh` (no `--as-of`), which
returns the CURRENT active set.

Use the as-of result to populate `abc_chain.antecedents.data_signals /
source_signals` with the beliefs that were live at T. When the as-of belief
DIFFERS from the current belief, that delta IS the learning: a belief that was
confidently held at T and has since been falsified is the highest-value ABC
antecedent — it shows the reasoning operated on a premise later proven wrong.
Note the divergence explicitly in the reflection (Step 5 text).

**SCOPE NOTE (sq-009):** beliefs use a bounded one-per-partner supersede-DROP
snapshot (g-306-35), NOT close-old/insert-new, so the belief store retains at
most the CURRENT version per partner — an as-of belief query returns the current
snapshot unless a closed interval was explicitly written. RB and guardrails carry
true append-only version history, so point-in-time reads over THOSE stores are
exact. Prefer RB/guardrail as-of evidence when the formation-time premise must
be reconstructed precisely.

**DIVERGENCE IS NOT AN ANTECEDENT UNTIL IT IS CONTROLLED (guard-3788, which names
Step 1.5 by name).** A zero `guardrails` / `reasoning_bank` array from the `--as-of`
read is NOT evidence the store held nothing at T. Step 1.5 therefore issues the
bare read as a CONTROL in the same block: if the bare read is non-empty and the
as-of read is zero, the divergence is 100% BY CONSTRUCTION — report "the as-of
supplementary read was unusable" and do NOT log it as an ABC antecedent. Only a
divergence that survives that comparison is the high-value antecedent described
above. Measured 2026-08-14 (echo, cc-03): category `npc-believability` returned
22/40 at `--depth medium` and 0/0 with `--as-of 2026-08-09T15:14:56`, while
guard-120 (created 2026-04-15, still active) proves records existed at T.

**ENFORCEMENT CLASS, stated rather than implied (guard-399 amendment 2).** Both
lines are LLM-elected, so pairing them REDUCES two elections to one — it does not
make the control unconditional. Prose and a `Bash:` line are the same enforcement
class; the operative test is "WHO executes it, a script or a model reading a
file?" A bash gate that catches the skip is still owed, and the shape that would
work keys on the ARTIFACT rather than on the elected call: refuse an ABC
antecedent whose evidence cites an `--as-of` retrieval with a zero supplementary
array unless a same-turn bare read of the same category is cited too — the shape
`zero-count-gate.py` already uses for statistical negations. Relayed as sq-013
from g-115-7106 (2026-09-05, alpha worker Body, cc-07); durable copy in findings
`msg-20260905-001450-alpha-5327`.

The guardrail-protocol conflict detector flags this step's `retrieve.sh --as-of`
line against guard-3788 on every cadence fire. That row is a signature-level
FALSE POSITIVE and cannot be cleared by editing this step: the signature matches
the INVOCATION, while guard-3788's prohibition verb is READ/REPORT — an inference
no signature can see. Verified with a guard-4166 positive control on 2026-09-05
(row count 2 before the fix and 2 after, while controls guard-3980 x replay and
guard-3572 stayed static). Do not "fix" the row; it is already dispositioned.

## Why counterfactual rollout is gated (Step 2.6c)

Decoupled-simulator extraction (transfer from Qwen-AgentWorld, arXiv
2606.24597): after a real outcome, simulate 1-2 alternative behaviors against
the SAME antecedent and extract the predicted delta as extra learning. The real
trajectory already paid its cost; a counterfactual mines additional signal from
it. Gated to outcomes worth the extra reasoning (modest ROI by design — do NOT
run it on every reflection).

The gate reuses `surprise_level >= 7` (same threshold guard-520 uses for
high-surprise downstream reconciliation) so the two high-surprise paths stay
consistent. `high_cost` catches expensive-to-be-wrong outcomes that may not
register as high-surprise but still repay the simulation overhead.

## Why divergent alternatives run before convergent textual reflection (Step 2.8)

Before the convergent textual reflection, generate alternative explanations.
This step is the antidote to premature convergence — the pipeline produces a
single ABC chain (Step 2) which becomes THE explanation. But there may be other
explanations that are equally or more valid. Running the divergent step BEFORE
Step 3 means the alternatives can enrich the textual reflection (`divergent_context`
passed forward) rather than being generated in retrospect and having no effect
on what gets encoded.

## Why depth calibration runs for every notable outcome (Execution Step 0.75)

Compare the pre-execution depth estimate (Phase 3.95 of aspirations-execute)
against the actual `outcome_class` and execution duration. Mismatches > 1 tier
are logged to `meta/depth-calibration.jsonl` so bias drift can be surfaced in
later reflect-on-self passes.

This step runs for EVERY notable outcome (it's cheap — no retrieval, just a
comparison and a file append). Over time the JSONL produces a per-category
calibration signal: "recurring goals in category X tend to be deeper than
estimated — default to standard" becomes an advisory in
`meta/goal-selection-strategy.yaml`. Running unconditionally ensures the signal
accumulates steadily rather than being skipped on routine outcomes where the
bias is most likely to hide.

## Why auto-settle requires `resolves_when` and runs before Step 2 (Batch Step 1.5)

The dominant micro-hyp failure (zeta audit g-303-14: 71% never-resolve, 0%
consumed) is NON-RESOLUTION — nothing ever evaluates them, so they accrue as
permanent nulls. Filing sites now write a REQUIRED `resolves_when` (the concrete
later signal that settles the micro-hyp) and `consumer` (which decision/encoding
uses the resolution). This step CONSUMES `resolves_when`: it turns the batch
processor from a stats-only pass into an actual resolver.

Running BEFORE Step 2 ensures the batch statistics reflect the freshly-settled
outcomes — without this ordering, accuracy counts would exclude the auto-settled
resolutions from the session's learning signal.

## The four discriminators are read by CONTENT, not by field name (Steps 2.5 guard-3427 / guard-2724)

Both override blocks tell the reader to consult `outcome_detail`,
`resolution_method` and `measurement_channel`. Those are the fields the content
*usually* occupies — not the fields it is guaranteed to occupy. The pipeline store
accepts a record carrying none of the three, so nothing at write time forces the
placement, and the writers are LLMs rather than a schema validator.

Measured 2026-09-04 (bravo, hostname cc-05, uname -r 6.8.0-138-generic) on two
hypotheses formed by the SAME author on the SAME day, 2026-08-05:

| record | `measurement_channel` / `resolution_criteria` / `premortem` | where the content actually is |
|---|---|---|
| `2026-08-05_health-live-repoint-ends-spurious-heals` | all populated | the named fields |
| `2026-08-05_lot-a-backfill-produces-no-product-activity` | all **NULL** | `verification` (table `A#<sub>/K#A`, field `balanceUSD`, four named subs, the `efs-ssh` wrapper) and `rationale` (the discriminating-power argument rejecting `lastLoginAt`) |

A field-keyed reading therefore scores discriminator (a) NEGATIVE on a fully
pre-registered record. Three properties make that worth a rail rather than a
one-line note:

1. **Nothing errors.** The checklist still runs and still returns a confident
   verdict, so the failure is invisible at the moment it happens.
2. **The error direction is set by the override's own bias.** guard-3427 is
   deliberately biased *toward not* attaching its do-not-reinforce note, so a
   missed discriminator re-arms a stamp that poisons a validated strategy — and
   per that guardrail's own text, nothing downstream ever contradicts it.
3. **The guardrail was already right; only the prose citing it was wrong.**
   guard-3427's `action_hint` names `rationale` and the formation experience.
   When an `action_hint` and the SKILL.md prose quoting it disagree, the
   `action_hint` is the authority — it is the guardrail's own words — and the
   prose is the defect (guard-1984: edit the passage; guard-4058: do not re-word
   a guardrail that needs no correction). Extracted as `rb-10111`.

### guard-3427 SOUND-METHOD OVERRIDE — the measured cases (4 of 4)

Every one would have been poisoned by the literal `lucky_confirmed` branch. The
misread is not confined to reflection: the goal-selector scores
`lucky_confirmed: +0.5`.

- `2026-08-04_widened-dispatch-surfaces-daemon-findings` — conf 0.40; explicit
  pre-mortem, a NAMED counter-mechanism priced at -0.15, a calibration gate it
  deliberately came in under.
- `2026-07-29_usage-lane-coverage-stays-thin` — conf 0.55; concrete pre-registered
  scan channel, Count 3 / ScannedCount 3955 completeness recorded, and
  `outcome_detail` goes on to ask which narrowing dominates (discriminator (d)).
- `2026-08-05_warned-budget-trap-reaches-implementer` — conf 0.55; a THREE-surface
  pre-registered channel, verified by a DIFFERENT agent than the implementer,
  precondition gated before resolving.
- `2026-08-05_lot-a-backfill-produces-no-product-activity` — conf 0.47 (cut from
  0.62 by a named premortem); the case above, and the one where discriminator (c)
  carried the most weight: the 30-day gate refused a day-13 answer another agent
  had already written into the goal description and endorsed.

### guard-2724 PROCESS-DEFECT OVERRIDE — the measured case

`2026-07-18_c2-idle-variety-nondiscriminating-at-current-coverage`, measured
2026-08-05: confidence 0.60 -> `unlucky_corrected`, and its own resolution
documented all four defects — the literal skip would have suppressed a real
guardrail. Contrast `guard-1604`, which covers a HAND-WRITTEN value being wrong,
remedy "the script wins"; here the script's value is CORRECT and only its gloss
is false, so re-running the script cannot surface it.

## Cross-references

- `rb-335`, `g-306-36` — bi-temporal reader origin
- `g-303-34`, `g-303-14` (zeta audit) — `resolves_when` requirement origin
- `guard-520` — high-surprise downstream reconciliation threshold (reused by Step 2.6c gate)
- `.claude/skills/reflect-on-outcome/SKILL.md` — consumer of this rationale
- `rb-10111`, `g-335-823` — field-placement hazard: read discriminators by content
