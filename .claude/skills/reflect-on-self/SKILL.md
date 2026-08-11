---
name: reflect-on-self
description: "Runs deep self-model reflection: synthesizes patterns across recent outcomes, extracts strategies into the archive, updates the Level 2 self-model, and calibrates confidence scores. Use whenever the loop hits the self-reflection cadence, sq-012 fires (\"does this outcome change my core purpose?\"), cumulative learning warrants rewriting self.md, or /reflect dispatches with --extract-patterns / --calibration-check. Produces self-model updates, not outcome-level insights."
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-on-self"
  - "/reflect --extract-patterns"
  - "/reflect --calibration-check"
conventions: [pipeline, tree-retrieval, reasoning-guardrails, pattern-signatures]
minimum_mode: autonomous
revision_id: "skill-bootstrap-reflect-on-self-3b9f63"
previous_revision_id: null
---

# /reflect-on-self — Self-Model Reflection

This sub-skill implements self-model reflection modes for `/reflect`. It is invoked
by the parent router for two modes:

- **Patterns mode** (`--extract-patterns`): Mine resolved hypotheses for strategies, synthesize Level 1 patterns, build Level 2 strategic self-model
- **Calibration mode** (`--calibration-check`): Analyze confidence calibration across all hypotheses

Each mode section below is self-contained with its own step numbering.

---

## Mode: Extract Patterns (--extract-patterns)

This sub-skill implements Mode 2 of `/reflect`. It is invoked by the parent `/reflect` router when `--extract-patterns` is specified, or during `--full-cycle` after individual hypothesis reflections complete. It mines the full resolved population — `--stage resolved` AND `--stage archived`, per Step 1 — for reusable strategies, synthesizes Level 1 patterns and Level 2 strategic self-models, and updates the knowledge base.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Load the Full Resolved Population (resolved AND archived)

```
# BOTH stages are REQUIRED. `--stage resolved` alone is a SURVIVORSHIP FILTER:
# `resolved` is the small live holding area and records migrate to `archived`
# as they age, so most scoreable records sit in `archived`. Measured
# 2026-08-04T03:33 (bravo, hostname cc-05, uname -r 6.8.0-136-generic):
# resolved 86 vs archived 829 — resolved is 9.4% of the 915-record store.
# Pattern MINING is the worst possible consumer of that filter: a "recurring
# pattern" needs 3+ instances (Step 2), and mining 9.4% of the corpus both
# suppresses real patterns below threshold and lets a small recent cluster
# read as one. The `frequency: N` field emitted below, and the "Category
# accuracy clusters" in the pattern list, are computed straight off this
# fetch. (g-115-4866; template at review-hypotheses Mode 3 Step 1, g-115-3594.)
Bash: pipeline-read.sh --stage resolved
Bash: pipeline-read.sh --stage archived
# Mine the UNION of the two. Keep EXPIRED / UNRESOLVABLE out of any rate the
# patterns below assert (store-wide convention: only CONFIRMED and CORRECTED
# are scoreable) — but they remain legitimate inputs to non-rate pattern
# mining, so do not drop the records themselves.
Read all Level 0 reflections from journal entries
Read existing pattern files from $WORLD_DIR/knowledge/patterns/
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

Write strategies to extracted-strategies.md (resolve path via world-cat.sh first).

```
     # ── Compile strategy into tree node (wiki integration) ────────
     # Strategies in flat files are rarely consulted. The tree is the wiki.
     # Write the strategy as a Decision Rule in the relevant tree node.
     strategy_node = bash core/scripts/tree-find-node.sh --text "{strategy condition category or domain}" --leaf-only --top 1
     IF strategy_node found AND strategy_node.score > 0.3:
         Read strategy_node.file
         # Append to "## Decision Rules" section (create if missing):
         #   - IF {strategy.condition} THEN {strategy.action} — source: pattern-extraction, confidence: {strategy.confidence}, sample: {strategy.sample_size}
         Edit strategy_node.file with new decision rule
         # T21 PostToolUse hook (`tree-front-matter-sync.py`) auto-bumps
         # last_updated on every tree-node Edit — no explicit
         # `tree-update.sh --set last_updated` call needed (guard-531).
         Log: "▸ Strategy compiled to tree: {strategy_node.key}"
     # ── End tree compilation ──────────────────────────────────────
```

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
    Write to extracted-strategies.md (resolve path via world-cat.sh first)

    # ── Compile enabling strategy into tree node (wiki integration) ────────
    strategy_node = bash core/scripts/tree-find-node.sh --text "{enabling_strategy condition category or domain}" --leaf-only --top 1
    IF strategy_node found AND strategy_node.score > 0.3:
        Read strategy_node.file
        # Append to "## Decision Rules" section (create if missing):
        #   - IF {enabling_strategy.condition} THEN {enabling_strategy.action} — source: pattern-extraction (enabling), confidence: {enabling_strategy.confidence}, sample: {enabling_strategy.sample_size}
        Edit strategy_node.file with new decision rule
        # T21 PostToolUse hook auto-bumps last_updated — no explicit
        # `tree-update.sh --set last_updated` call needed (guard-531).
        Log: "▸ Enabling strategy compiled to tree: {strategy_node.key}"
    # ── End tree compilation ──────────────────────────────────────

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
        Write to trajectory-patterns.md (resolve path via world-cat.sh first):
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
            Write to extracted-strategies.md (resolve path via world-cat.sh first)

            # ── Compile trajectory strategy into tree node (wiki integration) ────────
            strategy_node = bash core/scripts/tree-find-node.sh --text "{enabling_trajectory_strategy condition}" --leaf-only --top 1
            IF strategy_node found AND strategy_node.score > 0.3:
                Read strategy_node.file
                # Append to "## Decision Rules" section (create if missing):
                #   - IF {enabling_trajectory_strategy.condition} THEN {enabling_trajectory_strategy.action} — source: pattern-extraction (trajectory), confidence: {enabling_trajectory_strategy.confidence}
                Edit strategy_node.file with new decision rule
                # T21 PostToolUse hook auto-bumps last_updated — no explicit
                # `tree-update.sh --set last_updated` call needed (guard-531).
                Log: "▸ Trajectory strategy compiled to tree: {strategy_node.key}"
            # ── End tree compilation ──────────────────────────────────────
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

Bash: meta-set.sh meta-knowledge/_index.yaml  # update self-model
Update aspirations meta via Bash: `aspirations-meta-update.sh --source agent <field> <value>`.

## Step 5: Meta-Strategy Synthesis *(metacognitive self-modification)*

Review extracted patterns and strategies for meta-level implications:

a. **Goal selection patterns**: Do accuracy patterns suggest weight changes?
   Example: "Same-category goal streaks improve accuracy by 15%" → increase
   context_coherence weight in meta/goal-selection-strategy.yaml.

b. **Reflection patterns**: Do some reflection modes consistently yield more?
   Bash: meta-read.sh reflection-strategy.yaml --field roi_history
   If a mode's ROI is consistently > 2x average → add trigger_override to run it more often.
   If a mode's ROI is consistently near 0 → add skip_condition.

c. **Encoding patterns**: Are certain types of insights retrieved more often?
   If violations encoded to tree nodes are retrieved 3x more → add priority rule
   to meta/encoding-strategy.yaml.

For each proposed change:
  Bash: curriculum-contract-check.sh --action allow_meta_edits
  IF permitted:
      Bash: meta-set.sh {file} {field} {new_value} --reason "{pattern evidence}"

## Step 5.3: Meta-Heuristic Usage Report

Surface which meta/*-strategy.yaml heuristics are actually being applied via
strategy-apply.sh (wired into execute-protocol-digest.md Step 4b). Without this
step, times_applied counters accumulate silently with no surfacing in /reflect
output — the consumer exists but the signal never reaches the reviewer.

Heuristics with times_applied==0 past a staleness window are retirement candidates,
analogous to reflect-maintain Step 1a for extracted-strategies.md.

```
# Read both strategy files (in migrations scope — see strategy-apply.py STRATEGY_FILES)
Bash: meta-read.sh goal-selection-strategy.yaml --field selection_heuristics --json
Bash: meta-read.sh aspiration-generation-strategy.yaml --field generation_heuristics --json

# Aggregate and classify
total_heuristics = len(selection_heuristics) + len(generation_heuristics)
applied = [h for h in all if h.get("times_applied", 0) > 0]
unused = [h for h in all if h.get("times_applied", 0) == 0]
stale_unused = [h for h in unused if (today - parse(h.added)).days > 30]

Output:
  "▸ Meta-heuristic usage: {len(applied)}/{total_heuristics} applied at least once"
  "  Top-used: {top-3 by times_applied}"
  IF stale_unused:
      "  Retirement candidates ({len(stale_unused)}): {ids} (zero applications, >30 days old)"
      FOR EACH h in stale_unused:
          Log: "META-HEURISTIC UNUSED: {h.id} added {h.added} — review for retirement"

# Strategic reclassification: if a heuristic has high times_applied AND correlates with
# outcome quality (from Step 2/3 patterns), promote it to convention or reasoning bank.
FOR EACH h in applied:
    IF h.times_applied >= 5 AND correlates_with_positive_outcome:
        Log: "META-HEURISTIC MATURE: {h.id} applied {h.times_applied}x — consider promoting to guardrail or reasoning bank"
```

## Step 6: Update Knowledge Base

- Write new patterns to `world/knowledge/patterns/` with proper YAML front matter
- Update `world/knowledge/patterns/_index.yaml`
- Update meta-memory in `meta/meta-knowledge/_index.yaml`

PLACEMENT CHECK (rules vs. conventions): If the extracted pattern or strategy
would be encoded into a `.claude/rules/*.md` or `core/config/conventions/*.md`
file (because the pattern is universal-behavioral or framework-structural),
verify the content stays domain-agnostic per `.claude/rules/domain-free-examples.md`.
Domain-specific patterns (named services, endpoints, branded workflows) belong
in `world/conventions/*.md` instead. See `core/config/conventions/learning-routing.md`
§ "Rules vs Conventions" for the decision tree.

## Tree Update Protocol

When tree nodes are updated during pattern extraction or strategy compilation (decision rules written to tree nodes in Steps 3/3.5), invoke `/reflect-tree-update` to propagate changes upward through the memory tree.

---

## Mode: Calibration (--calibration-check)

This sub-skill implements Mode 3 of `/reflect`. It is invoked by the parent `/reflect` router when `--calibration-check` is specified, or during `--full-cycle` when 10+ resolved hypotheses exist. It analyzes confidence calibration across all hypotheses, bins them by confidence level, computes actual accuracy per bin, recommends self-consistency checks, and updates calibration data.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Bin Hypotheses by Confidence Level

```
Group all resolved hypotheses into confidence bins:
  50-59%: [hypotheses]
  60-69%: [hypotheses]
  70-79%: [hypotheses]
  80-89%: [hypotheses]
  90-100%: [hypotheses]
```

## Step 2: Calculate Actual Accuracy Per Bin

```
For each bin:
  expected_accuracy = midpoint of bin (e.g., 75% for 70-79%)
  actual_accuracy = confirmed / total in bin
  calibration_error = abs(expected - actual)
```

## Step 3: Multi-Sample Self-Consistency Check

For future hypotheses, recommend using self-consistency:
```
1. Generate 3-5 independent assessments of the same question
2. Measure agreement level
3. High agreement (4/5 or 5/5) = high confidence
4. Moderate agreement (3/5) = moderate confidence
5. Low agreement (2/5 or less) = low confidence or skip
Bash: echo "reflect-on-self phase documented"
```

## Step 4: Update Calibration Data

Write calibration report to journal and update:
- Aspirations meta confidence_calibration_bias via Bash: `aspirations-meta-update.sh --source agent confidence_calibration_bias <value>` (read via `aspirations-read.sh --source agent --meta`)
- `meta/meta-knowledge/_index.yaml` category-level calibration data

---

## Chaining Map (All Modes)

| Direction | Skill/Script | Modes | How |
|-----------|-------------|-------|-----|
| Called by | `/reflect --extract-patterns` | Patterns | Mode routing from parent |
| Called by | `/reflect --calibration-check` | Calibration | Mode routing from parent |
| Calls | `/reflect-tree-update` | Patterns | Propagate tree changes upward |
| Calls | `pipeline-read.sh` | Both | Load resolved hypotheses |
| Calls | `experience-read.sh` | Patterns | Temporal credit patterns |
| Calls | `aspiration-trajectory.sh` | Patterns | Trajectory-level mining |
| Calls | `tree-find-node.sh` | Patterns | Strategy → tree compilation |
| Calls | `curriculum-contract-check.sh` | Patterns | Gate meta-strategy edits |
| Calls | `meta-set.sh` | Patterns | Meta-strategy updates |
| Calls | `aspirations-meta-update.sh` | Both | Calibration data, self-model |
| Updates | `world/knowledge/patterns/` | Patterns | Pattern files |
| Updates | `world/knowledge/strategies/` | Patterns | Strategy files |
| Updates | `meta/meta-knowledge/` | Both | Self-model, calibration |
| Updates | Tree node Decision Rules | Patterns | Strategy compilation |
| Updates | `agents/<agent>/journal/` | Calibration | Calibration report |

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is `aspirations-meta-update.sh` or a tree-node write. Never end
with a text summary of pattern synthesis or calibration.
