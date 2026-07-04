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

## Cross-references

- `rb-335`, `g-306-36` — bi-temporal reader origin
- `g-303-34`, `g-303-14` (zeta audit) — `resolves_when` requirement origin
- `guard-520` — high-surprise downstream reconciliation threshold (reused by Step 2.6c gate)
- `.claude/skills/reflect-on-outcome/SKILL.md` — consumer of this rationale
