---
name: reflect-extract-patterns
description: "Pattern extraction — Level 1 pattern synthesis, strategy extraction, Level 2 strategic self-model, knowledge base update"
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-extract-patterns"
  - "/reflect --extract-patterns"
conventions: [pipeline, tree-retrieval, reasoning-guardrails]
minimum_mode: autonomous
---

# /reflect-extract-patterns — Pattern Extraction

This sub-skill implements Mode 2 of `/reflect`. It is invoked by the parent `/reflect` router when `--extract-patterns` is specified, or during `--full-cycle` after individual hypothesis reflections complete. It mines all resolved hypotheses for reusable strategies, synthesizes Level 1 patterns and Level 2 strategic self-models, and updates the knowledge base.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Load All Resolved Hypotheses

```
Bash: pipeline-read.sh --stage resolved  (all resolved records)
Read all Level 0 reflections from journal entries
Read existing patterns from world/knowledge/patterns/
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

Write strategies to `world/knowledge/strategies/extracted-strategies.md`.

### Enabling Strategy Detection (MR-Search Temporal Credit)

In addition to direct success strategies, mine for **enabling strategies** — approaches
that often precede later success even when their own immediate outcome was weak.

```
# Check experience records for temporal credit patterns
Bash: experience-read.sh --type goal_execution
# Filter for records with temporal_credit > 0.1 (received backward credit)
enabling_experiences = [exp for exp in experiences if exp.temporal_credit > 0.1]

IF len(enabling_experiences) >= 2:
    # Look for patterns across enabling experiences
    # What did they have in common? (approach, category, skill, timing)
    # These are fundamentally different from direct success strategies:
    # they represent FOUNDATION-LAYING work that pays off downstream.

    enabling_strategy = {
        condition: "When starting work in [category] with low capability_level",
        action: "Apply [enabling approach pattern]",
        expected_outcome: "Sets up context for later success (not immediate results)",
        success_rate: proportion of enabling experiences that led to downstream success,
        sample_size: len(enabling_experiences),
        confidence: 0.4,  # Lower initial confidence — enabling strategies are harder to verify
        strategy_type: "enabling",  # Distinct from "direct" success strategies
        first_observed: earliest enabling experience date,
        last_reinforced: latest enabling experience date,
        status: "active",
        times_applied: 0,
        last_applied: null,
        temporal_credit_total: sum of temporal_credit across enabling experiences
    }
    Write to world/knowledge/strategies/extracted-strategies.md
    Output: "▸ Enabling strategy extracted from {len(enabling_experiences)} temporal credit patterns"
```

## Step 3.5: Trajectory-Level Pattern Extraction (AVO-inspired)

When 3+ aspirations have 5+ completed goals each, extract trajectory-level patterns
across aspirations. Inspired by NVIDIA AVO (arXiv:2603.24517) — reasoning about the
SHAPE of progress over time, not just individual outcomes.

```
Bash: load-aspirations-compact.sh → IF path returned: Read it
mature_aspirations = [asp for asp in active where completed_goals >= 5]

IF len(mature_aspirations) >= 3:
    # Batch trajectory compilation — loads shared data once for all aspirations
    # (outer guard guarantees 3+ IDs, so output is always a keyed object)
    mature_asp_ids = [asp.id for asp in mature_aspirations]
    Bash: aspiration-trajectory.sh {mature_asp_ids joined by space}
    all_trajectories = parse JSON output
    trajectories = [all_trajectories[aid] for aid in mature_asp_ids]

    # Cross-trajectory pattern mining:
    #
    # 1. Velocity patterns: Which categories/scopes produce the highest
    #    learning velocity? Is there a correlation between aspiration scope
    #    (sprint/project/initiative) and sustained velocity?
    #
    # 2. Inflection point patterns: What do high-learning goals have in
    #    common? (research goals? audit goals? first contact with new code?)
    #    Are inflection points clustered early (exploration phase) or spread?
    #
    # 3. Plateau patterns: Do aspirations plateau at similar completion
    #    fractions? Is there a "natural stopping point" for learning?
    #
    # 4. Trajectory shape typology: Classify trajectories as:
    #    - Front-loaded (high learning early, tails off)
    #    - Back-loaded (slow start, accelerates)
    #    - Uniform (steady learning throughout)
    #    - Spike-and-plateau (discrete jumps separated by flat periods)
    #    Which shape correlates with highest total learning?

    trajectory_patterns = extract_cross_trajectory_patterns(trajectories)

    IF trajectory_patterns:
        Write to world/knowledge/patterns/trajectory-patterns.md:
            YAML front matter + pattern descriptions
        Output: "▸ Trajectory patterns extracted from {len(trajectories)} aspiration arcs"

        # Feed into strategy extraction: if a trajectory shape predicts
        # higher total learning, recommend that shape in goal planning
        IF trajectory_patterns.best_shape:
            enabling_trajectory_strategy = {
                condition: "When planning goals for a new aspiration",
                action: "Structure goal sequence to produce {best_shape} trajectory",
                expected_outcome: "Higher total learning yield based on {sample_size} prior aspirations",
                strategy_type: "trajectory",
                confidence: 0.35,  # Low initial — trajectory patterns need more data
                status: "active",
                times_applied: 0,
                last_applied: null
            }
            Write to world/knowledge/strategies/extracted-strategies.md
```

## Step 4: Level 2 Reflection — Strategic Self-Model

When 5+ Level 1 reflections exist, synthesize a strategic self-assessment:

```
We are [good/moderate/poor] at hypothesizing about [categories].
Our confidence is [well-calibrated / overconfident / underconfident] at [ranges].
Our best hypothesis approach is [skill chain].
Our biggest weakness is [pattern].
We should [strategic recommendation].
```

Write to `meta/meta-knowledge/_index.yaml` and update aspirations meta via Bash: `aspirations-meta-update.sh <field> <value>`.

## Step 5: Meta-Strategy Synthesis *(metacognitive self-modification)*

Review extracted patterns and strategies for meta-level implications:

a. **Goal selection patterns**: Do accuracy patterns suggest weight changes?
   Example: "Same-category goal streaks improve accuracy by 15%" → increase
   context_coherence weight in meta/goal-selection-strategy.yaml.

b. **Reflection patterns**: Do some reflection modes consistently yield more?
   Read meta/reflection-strategy.yaml roi_history.
   If a mode's ROI is consistently > 2x average → add trigger_override to run it more often.
   If a mode's ROI is consistently near 0 → add skip_condition.

c. **Encoding patterns**: Are certain types of insights retrieved more often?
   If violations encoded to tree nodes are retrieved 3x more → add priority rule
   to meta/encoding-strategy.yaml.

For each proposed change:
  Bash: curriculum-contract-check.sh --action allow_meta_edits
  IF permitted:
      Bash: meta-set.sh {file} {field} {new_value} --reason "{pattern evidence}"

## Step 6: Update Knowledge Base

- Write new patterns to `world/knowledge/patterns/` with proper YAML front matter
- Update `world/knowledge/patterns/_index.yaml`
- Update meta-memory in `meta/meta-knowledge/_index.yaml`

## Tree Update Protocol

When tree nodes are updated during pattern extraction, invoke `/reflect-tree-update` to propagate changes upward through the memory tree.
