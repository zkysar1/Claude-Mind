---
name: reflect
description: "Orchestrates Reflexion-based learning: dispatches to /reflect-on-outcome (hypothesis ABC chains, execution patterns, batch micro-hypotheses), /reflect-on-self (pattern synthesis, Level 2 self-model, calibration), or /reflect-maintain (memory curation, active forgetting, aspiration grooming) based on --mode. Use whenever the aspirations loop hits a reflection cadence, a hypothesis resolves, the user asks to \"reflect on recent work\", or after major outcomes that warrant pattern extraction."
user-invocable: false
triggers:
  - "/reflect"
parameters:
  - name: mode
    description: "Reflection mode: --on-hypothesis, --on-execution, --extract-patterns, --calibration-check, --full-cycle, --curate-memory, --curate-aspirations, --batch-micro"
    required: false
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [pipeline, reasoning-guardrails, pattern-signatures, handoff-working-memory]
minimum_mode: assistant
revision_id: "skill-bootstrap-reflect-8f539f"
previous_revision_id: null
---

# /reflect — Reflexion-Based Self-Learning Engine

Generates structured reflections from hypothesis outcomes, extracts reusable strategies, tracks violations of expectation, and synthesizes hierarchical insights. This is the core self-learning mechanism — it turns raw outcomes into institutional knowledge.

Based on: Reflexion (Shinn 2023), ABC Method, Generative Agents (Park 2023), VoE metacognitive framework.

## Quick Links

| Sub-skill | Mode | Purpose |
|-----------|------|---------|
| [/reflect-on-outcome](../reflect-on-outcome/SKILL.md) | `--on-hypothesis`, `--on-execution`, `--batch-micro` | Outcome reflection: hypothesis ABC chains, execution patterns, batch micro |
| [/reflect-on-self](../reflect-on-self/SKILL.md) | `--extract-patterns`, `--calibration-check` | Self-model: pattern synthesis, strategy extraction, calibration |
| [/reflect-maintain](../reflect-maintain/SKILL.md) | `--curate-memory`, `--curate-aspirations` | Maintenance: memory curation, aspiration grooming |
| [/reflect-tree-update](../reflect-tree-update/SKILL.md) | *(shared protocol)* | Propagate tree changes upward |

**Related skills:** [/replay](../replay/SKILL.md) (hippocampal replay), [/aspirations-spark](../aspirations-spark/SKILL.md) (Phase 6.5 immediate learning)

## Parameters

- `--on-hypothesis <hypothesis-id>` — Reflect on a single resolved hypothesis (session/short/long horizon)
- `--on-execution` — Reflect on a goal execution outcome (pattern signatures, contradiction detection, investigation goals)
- `--batch-micro` — Batch-reflect on micro-hypotheses from working memory (session-end)
- `--extract-patterns` — Mine all resolved hypotheses for reusable strategies
- `--calibration-check` — Analyze confidence calibration across all hypotheses
- `--full-cycle` — Run all reflection modes in sequence (includes --batch-micro)
- `--curate-memory` — Retire stale/low-utilization strategies, guardrails, reasoning bank entries, and pattern signatures
- `--curate-aspirations` — Groom stuck goals whose evidence has converged (backlog grooming)
- `--level N` — Reflection depth (0=episode, 1=pattern, 2=strategic). Default: auto-detect

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 0.1: Stamp reflection cadence (G5 of Phase 1.5)

**Step 0.1: Stamp reflection cadence** — `Bash: reflection-cadence-stamp.sh <mode>` (mode is the --mode arg, e.g. "on-hypothesis", "on-execution", "full-cycle", "batch-micro", etc.). Records this reflection in `wm.last_reflection_at` AND appends to `world/reflection-history.jsonl`. Signal #13 of the Self/Program evolution metric vector (world/conventions/self-program-evolution.md) reads the history file to compute "reflection_cadence — fraction of expected reflection windows that contained ≥1 fire over the last 50 goals." Fail-silent — never blocks /reflect. Runs BEFORE Step 0.5 context retrieval so cadence accounting captures the reflection regardless of downstream success.

## Step 0.5: Load Context for Reflection

Before loading the hypothesis, load background knowledge for informed analysis.

Bash: retrieve.sh --category {hypothesis.category} --depth medium
# Returns JSON with tree_nodes, reasoning_bank, guardrails, pattern_signatures,
# experiences, beliefs, experiential_index. All retrieval counters already incremented.

Use retrieved context to:
- Compare ABC chain against known patterns (did we use the right strategy?)
- Check if any guardrail should have fired (failure prevention analysis)
- Assess whether beliefs need updating based on this outcome
- Identify if a pattern signature matched (or should have matched)

Step 0 runs ONCE per /reflect invocation. Context is available to the invoked sub-skill. For --full-cycle mode with multiple hypotheses, cache context per category — don't re-retrieve for same category.

## Step 0.3: Load Meta-Reflection Strategy

```
# Step 0.3: Load Meta-Reflection Strategy
Bash: meta-read.sh reflection-strategy.yaml
# The agent's learned reflection preferences:
# - depth_allocation: episode/pattern/strategic weight distribution
#   (overrides developmental stage defaults when non-default)
# - trigger_overrides: conditions that modify reflection behavior
# - skip_conditions: conditions where reflection can be safely skipped
# - category_depth_overrides: per-category depth preferences
# - reflection_effectiveness_by_type: MR-Search quality tracking (Priority 2)
# - adaptive_depth: MR-Search adaptive reflection scaling (Priority 6)
# - roi_history: READ THE LAST 2-3 ENTRIES' `note` FIELD. This is the only
#   place a prior cycle records what it FOUND, and this step already pays to
#   load it (it is the bulk of the file), so reading it is free. guard-2095:
#   without it, consecutive cycles re-derive the same finding — measured
#   2026-07-31, a cycle independently re-derived a defect the previous day had
#   already encoded as rb-5577, and the duplicate filing was avoided only
#   because an unrelated dedup retrieval happened to run.
#   PAIR THIS WITH guard-1868, WHICH POINTS THE OPPOSITE WAY: the prior note is
#   a CLAIM, not a verdict, and because it reads as settled it is the note
#   LEAST likely to be re-checked. Consult the prior finding, then re-measure
#   its load-bearing assertion; never inherit it whole. Both halves are
#   load-bearing and the same day proved each — consulting rb-5577 prevented a
#   duplicate goal, AND re-measuring it showed its stated cause was wrong.
# These are advisory — structural rules (horizon gating) still apply.

# MR-Search reflection quality-driven depth allocation (Priority 2):
# Use reflection_effectiveness_by_type to allocate more depth to reflection
# types that historically produce downstream improvement.
IF reflection_effectiveness_by_type exists AND has data:
    # Only adjust depth for types with sufficient data (total >= 3).
    # total == 0 means "no data" not "ineffective" — use default allocation.
    # Currently only spark reflections are tracked (Phase 6.5 tags artifacts).
    # Hypothesis and execution types will show total=0 until those sub-skills
    # also tag their artifacts with source_reflection_id.
    types_with_data = {type: data for type, data in reflection_effectiveness_by_type if data.total >= 3}
    IF types_with_data is non-empty:
        Apply effectiveness rates as advisory weight on depth_allocation for those types only
        # Types without sufficient data: keep default depth_allocation unchanged

# MR-Search adaptive reflection depth (Priority 6):
# Scale reflection effort dynamically based on task properties.
# Only applies when goal context is available (--on-execution, --on-hypothesis from Phase 8.75).
# Skipped in --full-cycle mode where reflect iterates over hypotheses without goal context.
IF adaptive_depth exists AND goal context is available:
    depth_multiplier = 1.0
    IF adaptive_depth.scale_on_surprise AND surprise_level > 7:
        depth_multiplier = min(depth_multiplier * 1.5, adaptive_depth.max_depth_multiplier)
    IF adaptive_depth.scale_on_chain_length AND goal has episode_history with length > 1:
        depth_multiplier = min(depth_multiplier * 1.25, adaptive_depth.max_depth_multiplier)
    IF adaptive_depth.scale_on_importance AND goal.priority == "HIGH":
        depth_multiplier = min(depth_multiplier * 1.25, adaptive_depth.max_depth_multiplier)
    # Apply multiplier as advisory guidance to sub-skill invocations
```

## Step 0.35: Adversarial Review Check (rb-356 origin)

Originated from the PreCompact-gate release-scope bug: tests passed 15/15 while
the release predicate was a strict subset of the acquire predicate. The bug
was only caught by adversarial self-review with verification questions. This
step automates the trigger so the protocol fires without the user having to
ask for it.

```
# Step 0.35: Adversarial Review trigger check
# Only applies when goal context is available (--on-execution, --on-hypothesis)
IF adversarial_review exists AND goal context is available:
    goal_category = goal.category OR ""
    goal_topic    = goal.description OR goal.title OR ""
    combined      = lowercase(goal_category + " " + goal_topic)
    files_touched = length(goal.files_changed OR [])

    matched_substring = first(s for s in adversarial_review.triggers.category_substrings
                              if s in combined)
    IF matched_substring is not null
       AND files_touched >= adversarial_review.triggers.min_files_touched:
        # Surface the protocol questions so the sub-skill incorporates them
        # into its ABC chain / pattern synthesis / encoded lessons.
        Emit: "ADVERSARIAL_REVIEW_TRIGGERED (matched: {matched_substring}, files: {files_touched})"
        Emit: adversarial_review.protocol  # the 4 questions

        # Bump counters — tracks whether the pattern pays for itself over time.
        Bash: meta-set.sh reflection-strategy.yaml adversarial_review.last_triggered "$(date +%Y-%m-%dT%H:%M:%S)"
        Bash: meta-set.sh reflection-strategy.yaml adversarial_review.times_fired $(($OLD_VALUE + 1))
        # If the sub-skill surfaces a bug through the protocol, the sub-skill
        # bumps adversarial_review.times_caught_bug via the same mechanism.

# These questions are advisory guidance for the reflection sub-skill — they do
# not block the reflection or change routing. They join retrieval_context so
# the sub-skill naturally addresses them during pattern extraction.
```

## Step 0.36: Two-Pass Review Check (rb-8720 origin)

Originated from the 2026-08-21 temp/scratchpad shipment review: a first
fresh-eyes pass probed every shipped predicate live and came back green over
territory where a second pass — asking WHEN each recorded value is computed
versus WHAT WINDOW its consumer treats it as covering — found two real
defects (a completion-time watermark stamp licensing an unobserved window,
and a bare-variable step). The passes caught DISJOINT defect sets; neither
question class reviews the other's.

```
# Step 0.36: Two-pass review trigger check — same shape and gating as Step 0.35
IF two_pass_review exists AND goal context is available:
    combined      = lowercase((goal.category OR "") + " " + (goal.description OR goal.title OR ""))
    files_touched = length(goal.files_changed OR [])

    matched_substring = first(s for s in two_pass_review.triggers.category_substrings
                              if s in combined)
    IF matched_substring is not null
       AND files_touched >= two_pass_review.triggers.min_files_touched:
        Emit: "TWO_PASS_REVIEW_TRIGGERED (matched: {matched_substring}, files: {files_touched})"
        Emit: two_pass_review.protocol  # the two passes' question classes

        # Bump counters — same telemetry pattern as Step 0.35. The three leaves
        # are audit_only in core/config/meta.yaml (mc-800/801/802 class), so
        # backpressure cannot erase the record of a fire.
        Bash: meta-set.sh reflection-strategy.yaml two_pass_review.last_triggered "$(date +%Y-%m-%dT%H:%M:%S)"
        Bash: meta-set.sh reflection-strategy.yaml two_pass_review.times_fired $(($OLD_VALUE + 1))
        # A bug surfaced through the protocol bumps two_pass_review.times_caught_bug
        # via the same mechanism.

# Advisory, like Step 0.35 — the emitted passes join retrieval_context. The one
# non-negotiable: run pass 1 and pass 2 as SEPARATE sweeps over the same files,
# never merged into one walkthrough — merging is what re-blinds pass 2.
```

## Mode Routing

### `--on-hypothesis <hypothesis-id>` — Single Hypothesis Reflection

Reflect on one resolved hypothesis with full ABC chain analysis, pattern extraction,
belief updates, and knowledge reconciliation. Handles horizon gating (micro → error,
session → lightweight path, short/long → full pipeline).

invoke /reflect-on-outcome Mode: Hypothesis with: hypothesis-id, retrieval_context from Step 0
# Sub-skill handles Steps 0.5-9: horizon gate, load, ABC chain, differentiated extraction,
# contrastive extraction, experience archival, encoding score, textual reflection,
# domain-specific, violation, source tracking, journal, accuracy, pattern signatures,
# entities, beliefs, contradictions, process-outcome, context gaps, strategy tracking,
# experiential index, spark check, knowledge reconciliation, tree growth, snapshot invalidation.

### `--on-execution` — Execution Outcome Reflection

Reflect on a goal execution outcome that was notable (mistake, surprise, recurring
pattern). Handles pattern signatures and contradiction detection that Phase 6.5
(immediate learning) does not cover. Creates investigation goals for findings
that need follow-up. Lightweight — no ABC chains, no horizon gating.

```
invoke /reflect-on-outcome Mode: Execution with: goal, result, outcome_class, retrieval_context from Step 0
# Sub-skill handles Steps 0.5-5: notability gate, pattern signatures, contradiction
# detection, investigation goal creation, experience archival, journal entry.
```

### `--batch-micro` — Batch Micro-Hypothesis Reflection

Batch-process micro-hypotheses from working memory (session-end).
Computes batch stats, promotes surprises, updates aggregate pipeline stats.

invoke /reflect-on-outcome Mode: Batch Micro with: retrieval_context from Step 0
# Sub-skill handles Steps 1-7: load micros, batch stats, surprise promotion,
# aggregate stats, journal, actionable work check, return batch result.

### `--extract-patterns` — Pattern Extraction

Mine all resolved hypotheses for reusable strategies and Level 2 strategic self-model.

invoke /reflect-on-self Mode: Extract Patterns with: retrieval_context from Step 0
# Sub-skill handles Steps 1-5: load resolved, Level 1 pattern synthesis,
# strategy extraction, Level 2 strategic self-model, update knowledge base.

### `--calibration-check` — Confidence Calibration

Analyze confidence calibration across all hypotheses.

invoke /reflect-on-self Mode: Calibration with: retrieval_context from Step 0
# Sub-skill handles Steps 1-4: bin by confidence, calculate accuracy,
# self-consistency check, update calibration data.

### `--curate-memory` — Memory Curation

Retire stale/low-utilization strategies, guardrails, reasoning bank entries,
and pattern signatures. Includes active forgetting reference formulas.

invoke /reflect-maintain Mode: Curate Memory with: retrieval_context from Step 0, scope (if provided)
# Sub-skill handles Steps 1-4: gather candidates, evaluate (agent judgment),
# execute retirements, journal. Plus active forgetting: decay model,
# retrieval strengthening, interference detection, reconsolidation.

### `--curate-aspirations` — Aspiration Grooming

Detect stuck goals whose evidence has converged. Cross-reference pending/blocked
goals against experience archive and knowledge tree. Complete, skip, or re-scope
goals that can be resolved from existing data.

invoke /reflect-maintain Mode: Curate Aspirations with: retrieval_context from Step 0, scope (if provided)
# Sub-skill handles Steps 1-4: gather candidates, evidence cross-reference,
# execute decisions, journal.

### `--full-cycle` — Full Reflection Cycle

Run all reflection modes in sequence. This is the comprehensive learning pass.

# Phase A: Outcome Reflection
1. Bash: wm-read.sh micro_hypotheses --json → if non-empty, invoke /reflect-on-outcome Mode: Batch Micro
   # ⚠ RUN THIS BEFORE ANY STORE-MINING STEP — THE ORDER IS LOAD-BEARING, NOT
   # COSMETIC. Resolved micro-hypotheses are where THIS agent records fixes it
   # has already shipped. Every store you mine later (rollback_history,
   # changelog, alert logs, pipeline) is a DATED HISTORICAL LOG, so a fix that
   # landed mid-window leaves pre-fix entries sitting in it that read exactly
   # like current state. The micros are the fix-date index that disambiguates.
   # MEASURED 2026-08-19 (echo, cc-03, fire #104): mining backpressure
   # rollback_history first produced a HIGH finding + guardrail + filed goal
   # asserting roi_history and adversarial_review counters were being reverted
   # live. Micro #8 in this very slot — outcome CONFIRMED, resolved
   # 2026-08-17T14:45 — recorded that fire #101 of THIS SAME GOAL had shipped
   # the allowlist that fixed it, production-verified. The guardrail and goal
   # had to be rewritten and the goal downgraded HIGH -> LOW. Reading this slot
   # first costs one wm-read and would have scoped the finding correctly the
   # first time. (rb-2585 / rb-1204 — compare against fix dates before
   # investigating; guard-3399 for the inverse direction.)
1.5. invoke /reflect-on-outcome Mode: Execution for goals completed this session with notable outcomes
     (only if not already reflected via --on-hypothesis pathway — check goal IDs)
1.75. invoke /reflect-maintain Mode: Curate Aspirations (groom stuck goals before reflecting on hypotheses)
2. Bash: pipeline-read.sh --unreflected → get unreflected resolved hypotheses
   # ⛔ EXPECT ZERO REFLECTABLE, AND DO NOT RE-DERIVE WHY — the zero is CORRECT.
   # Recorded as a bare count for five consecutive fires of g-001-01 (#100-#104,
   # population 390 -> 404 -> 405), each pass re-deriving it as new because
   # nothing here said it was known (rb-7613; guard-1984 — a guardrail cannot
   # outvote the instrument it guards, which is why this lives HERE).
   #
   # MEASURED 2026-08-22 (echo, hostname cc-03, uname -r 6.8.0-137-generic), on
   # the live payload — 418 records, 1,232,091 bytes:
   #   by STAGE   : archived 417, resolved 1
   #   by OUTCOME : UNRESOLVABLE 187, EXPIRED 183, none 47, CONFIRMED 1
   #                (conservation: 370 + 47 + 1 = 418)
   # (prior reading 2026-08-19: 405 records, archived 404, EXPIRED 173 /
   #  UNRESOLVABLE 185 / none 47 — the population only grows, so a count here is
   #  a dated observation, never a standing fact. guard-1818.)
   # The EXPIRED + UNRESOLVABLE + none mass carries no ABC input: a chain needs a
   # prediction that MET REALITY; an EXPIRED hypothesis aged out unmeasured and an
   # UNRESOLVABLE one hit a denominator floor, so neither ever produced one.
   # There is nothing to chain in that mass — this step is not broken.
   # But do NOT read that as "the whole payload is inert" — run the control below.
   #
   # ⚠ THE WORD "resolved" IN THIS STEP'S OWN TEXT IS THE TRAP. It invites a
   # filter on `status`, and pipeline records have NO `status` field — the
   # discriminator is `stage` (guard-2869: status is null on 709 of 733 rows,
   # and `pipeline-read.sh` has no `--status` flag, so a status-keyed filter
   # returns a confident structural zero). guard-2869 did NOT prevent this on
   # the 2026-08-19 pass: the wrong count was computed first and caught only by
   # the rb-245 one-record schema probe afterwards.
   #
   # POSITIVE CONTROL — the ONLY number that should ever alarm:
   #   [r for r in payload if r.get("outcome") in _reflectable.REFLECTABLE_OUTCOMES
   #                       and not r.get("reflected")]
   # RUN IT. Do not read a number off this comment — it was 0 on 2026-08-19,
   # 1 on 2026-08-22 (2026-08-09_prose-mandate-rate-generalizes, CONFIRMED,
   # stage=resolved, resolved_by zeta 6 min before that pass reached this step),
   # and 0 again on 2026-08-23 (echo, cc-03; payload 418, all stage=archived,
   # UNRESOLVABLE 188 / EXPIRED 183 / none 47, conservation 418).
   # A nonzero value means genuine ABC input is going unreflected and IS the
   # work; a large `--unreflected` count means nothing on its own. The two
   # numbers move independently — the mass above grew 405 -> 418 across the same
   # window in which the control went 0 -> 1, so neither predicts the other.
   # WHEN IT IS NONZERO, CHECK OWNERSHIP BEFORE CHAINING: these records are the
   # shared target of g-001-08 ("Resolve unreflected hypotheses"), claimed 16x in
   # 4 days across three agents. A record another LIVE agent resolved minutes ago
   # is theirs — `liveness-check.sh --agent <resolved_by> --json`, and abstain on
   # `alive`. Reflecting it anyway races a conflicting ABC chain onto one record.
   #
   # THAT ABSTENTION IS NOW MEASURED, NOT JUST PRESCRIBED (2026-08-23, echo).
   # The 08-22 pass abstained on the CONFIRMED record above because zeta was
   # alive and held g-001-08. Read back one day later: that record is
   # `reflected: True`, `resolved_by: zeta`, `resolved_at: 2026-08-22T12:40` —
   # the owner did the work, and the control returned to 0 by that record
   # LEAVING the payload (the count held at 418 because a new archived
   # UNRESOLVABLE arrived, 187 -> 188). So "abstain on `alive`" costs nothing
   # and loses nothing; it is not merely the safe choice. Most abstentions are
   # never checked — check yours, the read is one `pipeline-read.sh --id`.
   #
   # ⚠ THE CONTROL ABOVE IS NECESSARY AND NOT SUFFICIENT — guard-2236's MIRROR
   # DIRECTION. A zero here is ambiguous in the direction that reads as
   # all-clear, because `archive_sweep` flips resolved -> archived on
   # outcome_date age alone (>= 3d) WITHOUT consulting `reflected`, so a record
   # left unreflected for three days can leave the queryable window while the
   # count keeps reading 0. ALSO RUN, and REPORT even when zero (guard-1760 — a
   # completeness tool must not report what it declined to look at as coverage):
   #   pipeline-read.sh --stage archived   -> outcome in {CONFIRMED,CORRECTED} AND not reflected
   #   pipeline-read.sh --stage resolved   -> same predicate
   # Measured 2026-08-23 (echo, cc-03), with the rb-245 schema probe FIRST so the
   # zeros are genuine absences and not misspelled-field artifacts:
   #   archived: 1279 records, `reflected` present 1279/1279, 823 scoreable
   #             (CONFIRMED 463 + CORRECTED 360) -> 0 unreflected
   #   resolved:   67 records, `reflected` present   67/67,  57 scoreable
   #             (CONFIRMED 41 + CORRECTED 16)   -> 0 unreflected
   # Clean on both stages. Note WHY the primary control cannot substitute: all
   # 418 rows it returned are the NON-scoreable mass, so on a healthy box it
   # never sees the 823 records that carry ABC input at all.
   #
   # AND DO NOT INFER THE PREDICATE FROM guard-2236's LINE NUMBERS — MEASURE IT.
   # That guardrail describes `--unreflected` as filtering `stage=="resolved"`
   # on the live file only (mind_api/src/world/pipeline.py:119-123). Its
   # CONCLUSION (run the archived scan explicitly) stands and is why this block
   # exists. Its MECHANISM was accurate when written (2026-08-01) and was
   # SUPERSEDED a week later: g-115-5358 (2026-08-08) widened the branch to
   # union live+archive with a live-wins dedup and to admit `stage: archived`
   # alongside `resolved` — age-driven archiving (ARCHIVE_AGE_DAYS=3) had made
   # every record left unreflected for 3+ days permanently invisible to the very
   # backlog meant to catch it, a 7.9x under-report. READ THE BRANCH AT :272.
   #
   # FIRE #111 (2026-08-23) recorded "returned 418 rows ALL stage=archived" and
   # read it as the guardrail misdescribing the code. The OBSERVATION was right;
   # the DIAGNOSIS was wrong — the code had changed under it. A stale mechanism
   # and a wrong mechanism call for opposite responses (re-date vs re-derive),
   # so name which one you found. That reading also described the PAYLOAD, not
   # the PREDICATE: fire #112 (2026-08-24, echo, cc-03) got 419 rows from the
   # same predicate — 418 archived + 1 resolved, the resolved row closed that
   # morning and not yet 3 days old. A stage histogram tells you what is IN the
   # store; only the source tells you what the predicate COVERS. Print the
   # histogram (it is what caught this), then read the branch before claiming
   # coverage.
   #
   # THE LANE IS HEALTHY, which inverts the severity every open owner implies:
   # precheck-eval the same hour counted 51 resolved hypotheses in the pipeline
   # and exactly ONE was unreflected (2026-08-11_move-desync-classifies-as-stale-path,
   # itself UNRESOLVABLE). So 50 of 51 were reflected promptly. The defect is only
   # that terminal-outcome records are never MARKED reflected, so they accumulate
   # here permanently and the count grows monotonically — a drain/marking problem
   # with no correctness impact on learning.
   #
   # ROUTE NOTHING. Owned FIVE times over: g-115-4335, g-115-4558, g-115-6338,
   # g-115-6543, g-001-08. Attach a fresh measurement to the newest rather than
   # filing #6 (their counts are stale by construction — rb-5818).
3. For each unreflected hypothesis:
   invoke /reflect-on-outcome Mode: Hypothesis with: hypothesis-id
# Phase B: Self-Model Reflection
4. invoke /reflect-on-self Mode: Extract Patterns
5. invoke /reflect-on-self Mode: Calibration
# Phase C: Maintenance
5.5. invoke /reflect-maintain Mode: Curate Memory (light sweep scoped to categories touched this session)
5.55. **Weakness Analysis (AutoContext-inspired)**:
     # Aggregates signals from pattern signatures, experience archive, and
     # backpressure rollbacks into a coherent weakness report. HIGH-severity
     # weaknesses auto-create investigation goals. (The guardrail times_active
     # source is RETIRED — g-115-2141, re-applied by g-115-2470; see the
     # retirement block below.)
     # Only runs during --full-cycle.

     Read agents/<agent>/weakness-report.yaml (create with {last_analyzed: null, analysis_count: 0, weaknesses: [], signal_baseline: {captured_at: null, guardrail_times_active: {}, signature_outcomes: {}}} if missing)

     # WINDOWED SIGNAL SOURCES (g-115-2002 / sig-27 CONFIRMED): several raw signals
     # below are MONOTONIC CUMULATIVE counters that never stop firing once they
     # cross an absolute threshold — they measure lifetime accumulation, not recent
     # activity, so they are non-discriminating. utilization.times_active is the
     # worst: Phase 0.5a runs `guardrail-check.sh --context any --phase
     # pre-selection`, matches_context("any") is unconditionally True, and
     # matches_phase filters on STATIC rule-text keywords (PHASE_PRE_SELECTION_KEYWORDS)
     # — NOT the runtime condition. So every guardrail whose text matches those
     # keywords increments ~once per iteration whether or not it genuinely fired,
     # making times_active iteration-correlated (top ~3500). backpressure
     # rollback_history is lifetime-cumulative the same way. THE FIX: snapshot each
     # signal's value at the END of every pass into weakness_report.signal_baseline;
     # this pass thresholds on the DELTA since that baseline — and for the guardrail
     # counter, on the delta ABOVE the iteration-correlated BACKGROUND (median delta),
     # so a guardrail signals only when it fired MORE than the per-iteration
     # keyword-match rate. Windowing keeps agent-judgment synthesis unchanged below;
     # only the signal INPUTS change. (The guardrail half of this fix is now
     # HISTORY — even windowed, the counter carries no fire information; the
     # source is retired outright in the g-115-2141 block below. The windowing
     # doctrine stands for pattern totals + any future counter-derived source.)
     baseline = weakness_report.signal_baseline (default {captured_at: null, guardrail_times_active: {}, signature_outcomes: {}})
     first_pass = (baseline.captured_at is null)  # no history yet → cumulative-counter sources emit nothing; baseline is written at end-of-pass

     # Gather signals — 3 CONSUMED sources: pattern_signatures (windowed,
     # script-side), experience, backpressure. Signature windowing (g-115-1905):
     # lifetime counters stop discriminating on a mature store (127/722
     # guardrails matched the old lifetime `times_active >= 3`; guard-054 sat
     # at 3520 because the precheck guardrail-check mass-matches ~58 rules
     # every iteration). The baseline lives in weakness-report.yaml
     # `signal_baseline:` (advisory-ratchet shape, same pattern as
     # meta/audit-baselines.yaml) and is advanced by the script. The guardrail
     # source is computed by the script but NOT consumed — retired, block below.
     signals = []

     # 1. Pattern signatures (windowed accuracy) — computed by
     # core/scripts/weakness-signals.py (guard-399: mechanical arithmetic is
     # script baseline; LLM synthesis on top). A sig flags when window
     # accuracy < 0.70 over >=3 window outcomes. First run seeds the baseline
     # and emits no signals. (The script also computes guardrail windowed
     # deltas — those are baseline-bookkeeping only, NOT consumed as signals;
     # see the retirement block below.)
     # rb-245 pre-read gates: verify schema before the script consumes the
     # fields. If a gate fails, SKIP this sub-phase (other phases continue).
     # Do NOT --override: fix field paths in the script instead.
     Bash: source core/scripts/_paths.sh && bash core/scripts/audit-schema-gate.sh \
             --jsonl-path "$WORLD_DIR/pattern-signatures.jsonl" \
             --field-names "outcome_stats.accuracy,outcome_stats.total"
     Bash: source core/scripts/_paths.sh && bash core/scripts/audit-schema-gate.sh \
             --jsonl-path "$WORLD_DIR/guardrails.jsonl" \
             --field-names "utilization.times_active"
     # g-115-2002 windowing is SCRIPT-SIDE (weakness-signals.py, g-115-1905):
     # the two implementations met at the 2026-07-11 g-115-2022 merge — upstream
     # carried equivalent LLM-side pseudocode (signature total-advance gate;
     # guardrail delta-minus-median-background). The script supersedes it:
     # signatures get true WINDOW accuracy (delta_confirmed/delta_total >= 3
     # outcomes, < 0.70 signals); guardrails get windowed deltas with ambient
     # discrimination (>= ambient_mult x median of nonzero deltas — the
     # iteration-correlated keyword-match background sig-27 warned about);
     # baseline seeding + storage owned by the script (signal_baseline section).
     # Script-enforced beats LLM-discretionary (Phase 3.7 doctrine) — do NOT
     # re-inline the windowing math here.
     Bash: py -3 core/scripts/weakness-signals.py --agent $MIND_AGENT
     Parse JSON: seeded, guardrail_signals[], signature_signals[], window_start, notes
     # READ `notes` FIRST — it is the ONLY channel that distinguishes a genuine
     # clean from a degraded run, and both render as `signature_signals: []`
     # (g-115-4974, measured 2026-08-04 echo/cc-03). The script fails SAFE on an
     # unreadable weakness-report.yaml: it computes without a baseline, sets
     # baseline_updated=false so it will not clobber recoverable content, and says
     # so — in `notes`, which this parse list did not name, so nothing downstream
     # ever saw it. Live instance: a prose note in weakness-report.yaml had been
     # written as an unquoted multi-line plain scalar whose continuation line began
     # `into it: this pass...`; YAML reads that `: ` as a mapping key, so the whole
     # 48KB report (2 weaknesses, 2202 guardrail + 61 signature baselines) went
     # unparseable and this lane returned a VACUOUS zero for ~27h while looking
     # exactly like a healthy pass. guard-465 / guard-1091 class: a wrapper
     # reporting "no signal" when the probe never ran is not a measurement of zero.
     IF notes is non-empty:
         Output: "▸ ⚠ Weakness signals DEGRADED: {notes} — the zero below is NOT a clean; fix the named cause before reading any count"
         # Repair, then re-run, before treating any signal count as evidence.
         # A report file that fails to parse is repairable: convert multi-line
         # plain scalars to block literals (`key: |-`), verify every parsed string
         # still appears verbatim in the pre-repair text, and keep an off-tree
         # backup first (archive-before-delete.md).
     IF seeded == true:
         Log: "▸ Weakness signals: baseline seeded — windowed signals available from the next analysis"
     FOR EACH sig in signature_signals:
         signals.append({source: "pattern_signature", id: sig.id, detail: sig})   # window_accuracy < 0.70, window_total >= 3

     # Guardrails that fired frequently — RETIRED as a weakness signal source
     # (g-115-2141, 2026-07-14; re-applied 2026-07-17 by g-115-2470 after the
     # ac3730ea31d7 sync merge silently dropped the retirement). The script's
     # guardrail_signals[] output is deliberately NOT consumed here.
     #
     # WHY RETIRED (rationale updated for the script-side impl this lineage runs):
     #   (a) guard-841 spirit: a weakness signal must reflect GENUINE activity.
     #       utilization.times_active increments on keyword-scan matches
     #       (Phase 0.5a guardrail-check.sh: matches_context("any") is always
     #       True; matches_phase filters on STATIC rule-text keywords) — so even
     #       a WINDOWED delta measures keyword-match rate. Windowing satisfies
     #       the freshness letter of guard-841, but the counter never contained
     #       fire information to begin with.
     #   (b) times_active cannot distinguish a real fire from a keyword-scan
     #       bump — no arithmetic downstream of the counter can recover a
     #       distinction the counter never recorded.
     #   (c) residual false-signal paths survive the script's nonzero-median x2
     #       ambient filter (which DOES defeat the uniform-cohort shape that
     #       killed the inline variant — pass 19: 61 guards all delta=31 →
     #       nonzero-median 31 → threshold 62 → zero signals): the <6-movers
     #       path (AMBIENT_MIN_COHORT) thresholds at bare min_delta, and a
     #       guard whose rule text matches ~2x more scan keywords than the
     #       cohort clears 2x-median EVERY pass — a chronic FP carrying zero
     #       fire content. Empirical yield: 19 passes → zero real weaknesses
     #       (origin lineage, inline variant); 6 passes → zero (this box,
     #       script variant).
     #
     # RE-ADD CONDITION: only when guardrail-check.py logs REAL fires
     # (action_hint ran AND surfaced an issue) as a field DISTINCT from
     # keyword-scan matches. A real_fires_since_baseline delta can then replace
     # this block; a keyword-bump-derived signal never can (guard-841).
     # (weakness-signals.py still computes + baselines guardrail deltas for
     # schema stability and a clean future re-add; CONSUMPTION alone is
     # retired. The rb-245 schema gate on guardrails.jsonl above stays — it
     # protects the script's computation, not this retired consumption.)

     # 3. Experience records with negative relative_advantage clustered by approach
     #    (already windowed — recent 20; unchanged by g-115-1905)
     #
     # ⛔ THIS SOURCE IS INERT, AND ITS ZERO IS STRUCTURAL — DO NOT RE-DERIVE IT.
     # `relative_advantage` IS NOT A FIELD ON EXPERIENCE RECORDS. Measured
     # 2026-08-22 (echo, cc-03): the key is present on 0 of 20 recent records and
     # no similarly-named key exists anywhere in the schema, which is
     # {archived, archived_date, category, content_path, created, goal_id,
     #  hypothesis_id, id, reasoning_chain, retrieval_stats, summary, type,
     #  tree_nodes_related, verbatim_anchors}. So the filter below reads a
     # missing key and yields 0 on EVERY pass — "no negative experiences" is not
     # what it means (guard-1641: a 0 is ambiguous between counted-zero and
     # never-produced; here it is provably the latter).
     # ALREADY OWNED, THREE TIMES OVER — file nothing, cite these: rb-8048
     # ("weakness analysis gates on >=2 signals while 3 of its 4 sources cannot
     # contribute"), rb-7831 (the general mechanism: an AND-threshold silently
     # becomes unreachable as its sources are individually and CORRECTLY
     # retired), and goal g-115-5115. With source 2 retired (g-115-2141) and
     # this one inert, only sources 1 and 4 can fire, so the `>= 2` synthesis
     # gate below needs BOTH — which is why passes routinely end at 1 signal and
     # synthesise nothing. That is the known state, not a fresh discovery.
     Bash: experience-read.sh --recent 20
     negative_experiences = filter WHERE relative_advantage < -0.1
     IF len(negative_experiences) >= 3:
         # Cluster by category
         clusters = group_by(negative_experiences, "category")
         FOR EACH cluster WHERE len(cluster.items) >= 2:
             signals.append({source: "experience_cluster", category: cluster.key, count: len(cluster.items)})

     # 4. Backpressure rollback patterns — windowed to the last 14 days
     #    (lifetime history counted months-old rollbacks as current weakness
     #    signal; entries carry timestamps — filter, no baseline needed)
     Bash: meta-backpressure.sh status
     # (g-115-2002 upstream variant used a baseline.rollback_count slice; the
     # timestamp filter below is self-contained — entries carry timestamps, no
     # baseline dependency — and was kept at the g-115-2022 merge.)
     FOR EACH rollback in result.rollback_history WHERE rollback timestamp within last 14d:
         signals.append({source: "backpressure_rollback", id: rollback.meta_change_id, detail: rollback})

     # Synthesize weaknesses from signals
     IF len(signals) >= 2:
         # Detect weakness types
         # regression: declining performance in a category over time
         # stagnation: category with many goals but no capability improvement
         # dead_end: same approach keeps failing (feeds into dead end registry)
         # systematic_bias: agent consistently over/under-estimates

         FOR EACH detected weakness:
             existing = find in weakness_report.weaknesses WHERE description matches
             IF existing:
                 existing.last_confirmed = now
                 existing.times_confirmed += 1
             ELSE:
                 new_weakness = {
                     id: "wk-{next_num}",
                     type: detected_type,
                     description: synthesized_description,
                     evidence: {
                         pattern_signatures: [relevant sig IDs],
                         guardrail_triggers: [relevant guard IDs],
                         experience_ids: [relevant exp IDs],
                         meta_log_entries: count_of_relevant
                     },
                     severity: HIGH if regression/dead_end else MEDIUM,
                     first_detected: now,
                     last_confirmed: now,
                     times_confirmed: 1,
                     status: "active",
                     remediation: {proposed: null, applied: null, goal_id: null}
                 }
                 weakness_report.weaknesses.append(new_weakness)

         # Create investigation goals for HIGH-severity active weaknesses
         FOR EACH weakness WHERE severity == "HIGH" AND status == "active" AND remediation.goal_id is null:
             # Check dedup against existing goals
             Bash: load-aspirations-compact.sh → IF path returned: Read it
             IF no existing "Investigate: {weakness.description}" goal:
                 goal_json = {
                     title: "Investigate: {weakness.description (60 chars)}",
                     status: "pending", priority: "MEDIUM",
                     skill: null, participants: ["agent"],
                     description: "Weakness detected by aggregated failure analysis.\nType: {weakness.type}\nEvidence: {weakness.evidence}\nDiscovered by: Step 5.55 Weakness Analysis",
                     origin_signal: "investigate:weakness-{weakness.type}"
                 }
                 # Route to most relevant aspiration
                 target_asp = aspiration matching weakness category, or most recent active
                 echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh {target_asp}
                 weakness.remediation.goal_id = created_goal_id
                 Output: "▸ WEAKNESS ANALYSIS: Created investigation goal for {weakness.description}"

     weakness_report.last_analyzed = now
     weakness_report.analysis_count += 1
     # Snapshot every windowed signal's CURRENT value so the NEXT pass diffs
     # against it (g-115-2002). This write is what turns the cumulative counters
     # above into windowed deltas — without it the delta math has no baseline and
     # every source degrades to first_pass (emits nothing). Snapshot ALL active
     # guardrails/patterns (not just the ones that signalled) so a guardrail that
     # crosses threshold NEXT window is measured from its value NOW. Runs on EVERY
     # pass — including passes with < 2 signals that skip synthesis — so the window
     # advances every cycle, never stalls at a stale baseline.
     # THE SCRIPT ALREADY DID THIS — do NOT hand-write the baseline (g-115-4110).
     # `weakness-signals.py` writes signal_baseline itself on every non-seeding run
     # (unless --no-baseline-update), which is what "baseline seeding + storage owned
     # by the script" above means. The schema it writes is THREE keys:
     #     captured_at          — ISO timestamp of this pass
     #     guardrail_times_active — {guard_id: utilization.times_active} for active guards
     #     signature_outcomes   — {sig_id: outcome_stats} for active signatures
     # There is NO `rollback_count` and NO `pattern_totals`. Those two names appeared
     # only in this pseudocode and were never written by anything; a reader who queried
     # them got an empty dict and could reasonably conclude the pattern source was
     # structurally silent. It is not — measured 2026-07-30: signature_outcomes carried
     # 55 entries while `pattern_totals` read as absent. Backpressure is windowed by
     # TIMESTAMP (see the 14d filter above), so it needs no baseline count at all.
     # rb-245 applies to reading this file as much as to the stores it describes:
     # enumerate the artifact's REAL keys before concluding a source is inert.
     Only update the LLM-owned fields (the script does not touch these):
         weakness_report.last_analyzed  = now
         weakness_report.analysis_count += 1
         weakness_report.weaknesses      = <synthesized list, if any>
     Edit agents/<agent>/weakness-report.yaml with those fields only —
     leave signal_baseline exactly as the script wrote it.

     Output: "▸ Weakness analysis: {len(signals)} signals, {new_weakness_count} new weakness(es), {goal_count} investigation goal(s)"
5.7. **Meta-Reflection ROI Tracking**:
     For each reflection mode invoked in this cycle, track:
     - Did it produce a reasoning bank entry, guardrail, or pattern signature?
     - Did it add an encoding queue item?
     - Did it change a belief or knowledge node?
     Compute: reflection_roi = artifacts_produced / modes_invoked
     Append via meta-yaml.py append to reflection-strategy.yaml roi_history:
       {date: today, modes_invoked: N, artifacts_produced: N, roi: N, session: N}

     # CALL SHAPE — THE RECORD GOES ON STDIN. `append` takes exactly two
     # positional args (file, dotpath); passing the JSON as a third is refused
     # with a bare argparse `unrecognized arguments` line. Canonical form
     # (core/config/conventions/stdin-json-inputs.md, same as reasoning-bank-add.sh):
     #   printf '%s' '<json>' | py -3 core/scripts/meta-yaml.py append reflection-strategy.yaml roi_history
     #
     # ⚠ VERIFY WITH `meta-read.sh`, AND DO NOT RETRY ON A FAILED VERIFICATION —
     # A FAILED VERIFICATION IS NOT A FAILED WRITE. `meta-yaml.py read` takes NO
     # dotpath, so `read <file> roi_history` errors for its own reasons and reads
     # as "the append did not land". Fire #110 (2026-08-22) hit exactly this and
     # retried: roi_history carries TWO byte-identical entries for that date,
     # same session id, same note — a duplicate telemetry row created by a
     # verification error, not a write error. Its own note documents the
     # misdiagnosis, which is how the mechanism is known rather than guessed.
     # Verify by COUNTING (`meta-read.sh reflection-strategy.yaml` -> len(roi_history)
     # should be exactly +1) and confirming the tail entry is yours. Same family
     # as rb-8976: a probe that fails for its own reasons is not evidence about
     # the thing it was probing.

     # BOUND THE LOG — KEEP THE LAST 20 IN PLACE (g-115-4231, 2026-08-27).
     # roi_history is an append-only log living inside a config file that
     # Step 0.3 reads WHOLE on every cycle, and NOTHING has ever trimmed it:
     # neither this step nor `meta-yaml.py append` carries a cap (verified by
     # reading both, 2026-08-27). After appending, if len(roi_history) > 20,
     # drop the OLDEST entries until 20 remain. Index 0 is the oldest — derived
     # empirically from the stored dates, not assumed (guard-2496: an
     # append-only file can have more than one append region, in different
     # orders).
     # IN PLACE, NOT A SIBLING ARCHIVE, and the reason is that the full
     # narrative ALREADY has a durable home: the journal. The 2026-08-18 entry
     # says so in its own note — "FULL NOTE IS IN THE JOURNAL, NOT HERE".
     # roi_history is the INDEX, not the archive, so sharding the tail would
     # create exactly the second unbounded file this bound exists to avoid.
     # ARCHIVE BEFORE DELETE APPLIES TO THE EVICTION. An entry about to be
     # dropped must already be represented in that date's journal entry; if it
     # is not, write the journal entry FIRST, then evict. A trim is a
     # destructive store operation, not bookkeeping.

5.8. **Reflection Quality Consolidation (MR-Search Priority 2)**:
     Update reflection_effectiveness_by_type from reflection_quality_log:
     For each entry in reflection_quality_log:
       Derive type from reflection_id prefix (ref-{goal_id} → look up goal's spark/execution context)
       Count by type: entries where helpful == true are "effective"
     For each reflection type (execution, hypothesis, spark):
       total = count entries of this type
       effective = count entries of this type where helpful == true
       rate = effective / total (or 0.0 if total == 0)
     Bash: meta-set.sh reflection-strategy.yaml reflection_effectiveness_by_type '<updated_json>'
     This closes the meta-learning loop: reflection quality → depth allocation → better reflections
6. invoke /replay --sharp-wave --selective (if violations detected)
7. **Tree Health Lint (wiki integrity check)**:
     # Periodically verify the knowledge tree's structural and content health.
     # Inspired by Karpathy's wiki "health checks": find inconsistencies,
     # flag stale data, discover missing cross-references.
     Bash: tree-read.sh --stats
     
     # Staleness check: heavily-used nodes that haven't been updated recently
     #
     # READ `_tree.yaml`, NOT `tree-read.sh --summary` (g-115-4110, measured
     # 2026-07-30 on cc-02 / Linux 6.8.0-136-generic). --summary exposes exactly
     # eight fields — file, summary, depth, capability_level, confidence,
     # last_updated, article_count, children — and `retrieval_count` is NOT among
     # them, so a lint driven off --summary evaluates `retrieval_count > 10`
     # against a missing key and reports ZERO stale nodes, every time, on a tree
     # where the true answer is 779. The field is real and fully populated: 1296
     # of 1299 nodes carry a nonzero count in `_tree.yaml`, max 433. This step
     # already reads `_tree.yaml` below for cross-references — use the same read.
     #
     # `nodes` IS A DICT (key -> node object), NOT A LIST — iterate `.values()`.
     # This line read "nodes[] carries retrieval_count" until 2026-08-16, and the
     # bracket notation plus the `for n in nodes` on the staleness line below both
     # say list. Iterating a dict yields its KEYS (strings), so `n.retrieval_count`
     # is absent on every element and the lint reports `retrieval_count present on
     # 0 of 1407` — a structural zero, which is the EXACT failure this g-115-4110
     # note was written to fix, reproduced one layer down. A correction can carry
     # the defect forward in a new costume: this one was right about the FILE and
     # wrong about the SHAPE, so following it literally still returns zero.
     # Measured cc-02 2026-08-16 (zeta, uname -r 6.8.0-137-generic): as `.values()`,
     # retrieval_count present on 1407/1407, 1265 at rc>10, 980 stale >14d.
     Bash: world-cat.sh knowledge/tree/_tree.yaml   # d["nodes"] is a DICT of node objects
     #
     # BOUND THE OUTPUT. At real numbers this predicate matches 984 nodes with
     # retrieval_count > 10, of which 779 are >14d stale — an unbounded
     # `FOR EACH -> wm-append.sh knowledge_debt` would write hundreds of debt
     # items in one pass and bury every other signal in working memory. Flag the
     # TOP 5 by retrieval_count and report the full count alongside, so the
     # backlog stays visible without being transcribed.
     stale = [(k, n) for k, n in nodes.items() if n.retrieval_count > 10 and days_since(n.last_updated) > 14]
     Log: "▸ Tree lint: {len(stale)} stale high-retrieval nodes (of {len(hi)} with rc>10) — flagging top 5"
     FOR EACH node in sorted(stale, by retrieval_count desc)[:5]:
         echo '{"node_key": "<key>", "reason": "stale-high-retrieval", "retrieval_count": <N>, "days_since_update": <M>, "total_stale_at_scan": <len(stale)>, "priority": "MEDIUM"}' | wm-append.sh knowledge_debt
         IF exit 0: Log: "▸ Tree lint: {node.key} flagged stale (retrieved {retrieval_count}x, last updated {days_ago}d ago)"
     
     # Cross-reference discovery: nodes that share entities but aren't linked
     Bash: world-cat.sh knowledge/tree/_tree.yaml  # entity_index
     # EXPECT EMPTY, and read that as "this sub-step is inert", not "no shared
     # entities exist". Measured cc-02 2026-08-16: entity_index has 0 entries and
     # cross_references 0, against 1407 nodes — so nothing populates it and the
     # join below has no input. Said here so the next reader does not investigate
     # why a correctly-guarded step produced nothing.
     IF entity_index is non-empty:
         FOR EACH entity appearing in 2+ nodes:
             Check if those nodes have cross-references to each other in their .md files
             IF no cross-reference exists:
                 Add "See also: [{other_node}]({other_node.file})" to both nodes
                 Log: "▸ Tree lint: cross-reference added between {node_a.key} and {node_b.key} (shared entity: {entity})"
     
     # Width check: interior nodes exceeding K_max children
     # K_max is NESTED at `config.K_max` in core/config/tree.yaml (currently 40),
     # not top-level — a top-level lookup returns None and the comparison below
     # throws rather than reporting zero, which is at least the loud failure.
     Read core/config/tree.yaml → config.K_max
     FOR EACH interior node where child_count > K_max * 2:
         Log: "▸ Tree lint: {node.key} has {child_count} children (K_max={K_max}) — consider reorganization"
         bash core/scripts/tree-update.sh --set <node.key> growth_state ready_to_decompose
     
     Report: "Tree lint: {stale_count} stale nodes flagged, {xref_count} cross-references added, {wide_count} wide nodes flagged"

---

## Integration Points

- **Called by `/aspirations`**: After every goal completion (spark check); `--batch-micro` at session-end consolidation
- **Called by `/aspirations-state-update`**: Step 8.75 after productive goal execution with notable outcomes
- **Called by `/review-hypotheses --learn`**: For each resolved hypothesis with `reflected: false` (horizon gate routes session→lightweight, short/long→full)
- **Calls `/replay`**: During full-cycle mode (Step 2.5) for hippocampal replay
- **Calls `/research-topic`**: When a knowledge gap is identified
- **Calls `/aspirations add`**: When a new aspiration emerges from patterns
- **Calls `/reflect-tree-update`**: Shared tree update protocol used by reflect-on-outcome (Hypothesis mode) and reflect-on-self (Patterns mode)
- **Calls `/tree maintain`**: When new categories detected or article counts cross thresholds (Step 8.5)
- **Updates discovery filters**: Adds new trap types to discovery lessons-learned
- **Updates evaluation calibration**: Adjusts evaluation weights based on calibration data
- **Updates pattern signatures** (via `pattern-signatures-add.sh`, `pattern-signatures-record-outcome.sh`): New signatures, accuracy updates, separation markers
- **Updates working memory** (via `wm-append.sh`): Encoding queue items from Step 2.5
- **Updates `agents/<agent>/developmental-stage.yaml`**: Schema operations (assimilation/accommodation)
- **Updates `meta/skill-gaps.yaml`**: Capability gap detection (Spark Q6) and skill underperformance (Spark Q7)

---

## Active Forgetting — Decay Formula Reference

retention_score = base_decay^days_since_last_access × (1 + retrieval_count × retrieval_boost)

Default parameters: base_decay=0.95, retrieval_boost=0.15.
See /reflect-maintain (Memory Curation mode) for full active forgetting procedures (interference detection,
reconsolidation windows, protection rules).

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
