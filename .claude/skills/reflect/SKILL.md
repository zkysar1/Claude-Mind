---
name: reflect
description: "Reflexion-based learning from hypothesis outcomes — ABC chains, violation tracking, hierarchical reflection, strategy extraction"
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
---

# /reflect — Reflexion-Based Self-Learning Engine

Generates structured reflections from hypothesis outcomes, extracts reusable strategies, tracks violations of expectation, and synthesizes hierarchical insights. This is the core self-learning mechanism — it turns raw outcomes into institutional knowledge.

Based on: Reflexion (Shinn 2023), ABC Method, Generative Agents (Park 2023), VoE metacognitive framework.

## Quick Links

| Sub-skill | Mode | Purpose |
|-----------|------|---------|
| [/reflect-hypothesis](../reflect-hypothesis/SKILL.md) | `--on-hypothesis <id>` | Full single hypothesis reflection pipeline |
| [/reflect-execution](../reflect-execution/SKILL.md) | `--on-execution` | Pattern signatures + contradiction detection from execution |
| [/reflect-batch-micro](../reflect-batch-micro/SKILL.md) | `--batch-micro` | Batch micro-hypothesis reflection |
| [/reflect-extract-patterns](../reflect-extract-patterns/SKILL.md) | `--extract-patterns` | Mine resolved hypotheses for strategies |
| [/reflect-calibration](../reflect-calibration/SKILL.md) | `--calibration-check` | Confidence calibration analysis |
| [/reflect-curate-memory](../reflect-curate-memory/SKILL.md) | `--curate-memory` | Retire stale strategies/guardrails |
| [/reflect-curate-aspirations](../reflect-curate-aspirations/SKILL.md) | `--curate-aspirations` | Groom stuck goals via evidence cross-reference |
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

## Mode Routing

### `--on-hypothesis <hypothesis-id>` — Single Hypothesis Reflection

Reflect on one resolved hypothesis with full ABC chain analysis, pattern extraction,
belief updates, and knowledge reconciliation. Handles horizon gating (micro → error,
session → lightweight path, short/long → full pipeline).

invoke /reflect-hypothesis with: hypothesis-id, retrieval_context from Step 0
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
invoke /reflect-execution with: goal, result, outcome_class, retrieval_context from Step 0
# Sub-skill handles Steps 0.5-5: notability gate, pattern signatures, contradiction
# detection, investigation goal creation, experience archival, journal entry.
```

### `--batch-micro` — Batch Micro-Hypothesis Reflection

Batch-process micro-hypotheses from working memory (session-end).
Computes batch stats, promotes surprises, updates aggregate pipeline stats.

invoke /reflect-batch-micro with: retrieval_context from Step 0
# Sub-skill handles Steps 1-7: load micros, batch stats, surprise promotion,
# aggregate stats, journal, actionable work check, return batch result.

### `--extract-patterns` — Pattern Extraction

Mine all resolved hypotheses for reusable strategies and Level 2 strategic self-model.

invoke /reflect-extract-patterns with: retrieval_context from Step 0
# Sub-skill handles Steps 1-5: load resolved, Level 1 pattern synthesis,
# strategy extraction, Level 2 strategic self-model, update knowledge base.

### `--calibration-check` — Confidence Calibration

Analyze confidence calibration across all hypotheses.

invoke /reflect-calibration with: retrieval_context from Step 0
# Sub-skill handles Steps 1-4: bin by confidence, calculate accuracy,
# self-consistency check, update calibration data.

### `--curate-memory` — Memory Curation

Retire stale/low-utilization strategies, guardrails, reasoning bank entries,
and pattern signatures. Includes active forgetting reference formulas.

invoke /reflect-curate-memory with: retrieval_context from Step 0, scope (if provided)
# Sub-skill handles Steps 1-4: gather candidates, evaluate (agent judgment),
# execute retirements, journal. Plus active forgetting: decay model,
# retrieval strengthening, interference detection, reconsolidation.

### `--curate-aspirations` — Aspiration Grooming

Detect stuck goals whose evidence has converged. Cross-reference pending/blocked
goals against experience archive and knowledge tree. Complete, skip, or re-scope
goals that can be resolved from existing data.

invoke /reflect-curate-aspirations with: retrieval_context from Step 0, scope (if provided)
# Sub-skill handles Steps 1-4: gather candidates, evidence cross-reference,
# execute decisions, journal.

### `--full-cycle` — Full Reflection Cycle

Run all reflection modes in sequence. This is the comprehensive learning pass.

1. Bash: wm-read.sh micro_hypotheses --json → if non-empty, invoke /reflect-batch-micro
1.5. invoke /reflect-execution for goals completed this session with notable outcomes
     (only if not already reflected via --on-hypothesis pathway — check goal IDs)
1.75. invoke /reflect-curate-aspirations (groom stuck goals before reflecting on hypotheses)
2. Bash: pipeline-read.sh --unreflected → get unreflected resolved hypotheses
3. For each unreflected hypothesis:
   invoke /reflect-hypothesis with: hypothesis-id
4. invoke /reflect-extract-patterns
5. invoke /reflect-calibration
5.5. invoke /reflect-curate-memory (light sweep scoped to categories touched this session)
6. invoke /replay --sharp-wave --selective (if violations detected)

---

## Integration Points

- **Called by `/aspirations`**: After every goal completion (spark check); `--batch-micro` at session-end consolidation
- **Called by `/aspirations-state-update`**: Step 8.75 after productive goal execution with notable outcomes
- **Called by `/review-hypotheses --learn`**: For each resolved hypothesis with `reflected: false` (horizon gate routes session→lightweight, short/long→full)
- **Calls `/replay`**: During full-cycle mode (Step 2.5) for hippocampal replay
- **Calls `/research-topic`**: When a knowledge gap is identified
- **Calls `/aspirations add`**: When a new aspiration emerges from patterns
- **Calls `/reflect-tree-update`**: Shared tree update protocol used by reflect-hypothesis and reflect-extract-patterns
- **Calls `/tree maintain`**: When new categories detected or article counts cross thresholds (Step 8.5)
- **Updates discovery filters**: Adds new trap types to discovery lessons-learned
- **Updates evaluation calibration**: Adjusts evaluation weights based on calibration data
- **Updates pattern signatures** (via `pattern-signatures-add.sh`, `pattern-signatures-record-outcome.sh`): New signatures, accuracy updates, separation markers
- **Updates working memory** (via `wm-append.sh`): Encoding queue items from Step 2.5
- **Updates `mind/developmental-stage.yaml`**: Schema operations (assimilation/accommodation)
- **Updates `mind/skill-gaps.yaml`**: Capability gap detection (Spark Q6) and skill underperformance (Spark Q7)

---

## Active Forgetting — Decay Formula Reference

retention_score = base_decay^days_since_last_access × (1 + retrieval_count × retrieval_boost)

Default parameters: base_decay=0.95, retrieval_boost=0.15.
See /reflect-curate-memory for full active forgetting procedures (interference detection,
reconsolidation windows, protection rules).
