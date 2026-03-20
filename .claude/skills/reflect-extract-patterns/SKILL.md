---
name: reflect-extract-patterns
description: "Pattern extraction — Level 1 pattern synthesis, strategy extraction, Level 2 strategic self-model, knowledge base update"
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-extract-patterns"
  - "/reflect --extract-patterns"
conventions: [pipeline, tree-retrieval, reasoning-guardrails]
---

# /reflect-extract-patterns — Pattern Extraction

This sub-skill implements Mode 2 of `/reflect`. It is invoked by the parent `/reflect` router when `--extract-patterns` is specified, or during `--full-cycle` after individual hypothesis reflections complete. It mines all resolved hypotheses for reusable strategies, synthesizes Level 1 patterns and Level 2 strategic self-models, and updates the knowledge base.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Load All Resolved Hypotheses

```
Bash: pipeline-read.sh --stage resolved  (all resolved records)
Read all Level 0 reflections from journal entries
Read existing patterns from mind/knowledge/patterns/
```

## Step 2: Level 1 Reflection — Pattern Synthesis

When 3+ Level 0 reflections exist, synthesize:

```yaml
level_1_reflection:
  level: 1
  date: "YYYY-MM-DD"
  synthesized_from: [list of hypothesis IDs]
  pattern: "Description of recurring pattern"
  frequency: N  # how many hypotheses fit this pattern
  conditions: "When does this pattern appear?"
  confidence: 0.0-1.0  # grows with more evidence
  action_recommendation: "What should we do when we see this pattern?"
  category: "politics/crypto/..."
```

Patterns to look for:
- Category accuracy clusters (good at politics, bad at crypto)
- Confidence calibration patterns (overconfident above 80%)
- Research depth correlations (deep research → better accuracy?)
- Time horizon patterns (better at short-term vs. long-term?)
- Source reliability patterns (which sources help most?)
- Hypothesis type patterns (binary vs. multi-outcome)

## Step 3: Strategy Extraction

Mine high-accuracy hypotheses for reusable strategies:

```yaml
strategy:
  condition: "When we see [specific conditions]"
  action: "Apply [specific skill chain / research approach]"
  expected_outcome: "We tend to hypothesize correctly when..."
  success_rate: 0.72  # from historical data
  sample_size: 7  # hypotheses supporting this strategy
  confidence: 0.6
  first_observed: "YYYY-MM-DD"
  last_reinforced: "YYYY-MM-DD"
  status: active         # Initialize on creation
  times_applied: 0       # Initialize on creation
  last_applied: null     # Initialize on creation
```

When creating new strategies:
- Set `status: active`
- Set `times_applied: 0`
- Set `last_applied: null`

Write strategies to `mind/knowledge/strategies/extracted-strategies.md`.

## Step 4: Level 2 Reflection — Strategic Self-Model

When 5+ Level 1 reflections exist, synthesize a strategic self-assessment:

```
We are [good/moderate/poor] at hypothesizing about [categories].
Our confidence is [well-calibrated / overconfident / underconfident] at [ranges].
Our best hypothesis approach is [skill chain].
Our biggest weakness is [pattern].
We should [strategic recommendation].
```

Write to `mind/knowledge/meta/_index.yaml` and update aspirations meta via Bash: `aspirations-meta-update.sh <field> <value>`.

## Step 5: Update Knowledge Base

- Write new patterns to `mind/knowledge/patterns/` with proper YAML front matter
- Update `mind/knowledge/patterns/_index.yaml`
- Update meta-memory in `mind/knowledge/meta/_index.yaml`

## Tree Update Protocol

When tree nodes are updated during pattern extraction, invoke `/reflect-tree-update` to propagate changes upward through the memory tree.
