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
minimum_mode: assistant
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
1.5. invoke /reflect-on-outcome Mode: Execution for goals completed this session with notable outcomes
     (only if not already reflected via --on-hypothesis pathway — check goal IDs)
1.75. invoke /reflect-maintain Mode: Curate Aspirations (groom stuck goals before reflecting on hypotheses)
2. Bash: pipeline-read.sh --unreflected → get unreflected resolved hypotheses
3. For each unreflected hypothesis:
   invoke /reflect-on-outcome Mode: Hypothesis with: hypothesis-id
# Phase B: Self-Model Reflection
4. invoke /reflect-on-self Mode: Extract Patterns
5. invoke /reflect-on-self Mode: Calibration
# Phase C: Maintenance
5.5. invoke /reflect-maintain Mode: Curate Memory (light sweep scoped to categories touched this session)
5.55. **Weakness Analysis (AutoContext-inspired)**:
     # Aggregates signals from pattern signatures, guardrails, experience archive,
     # and backpressure rollbacks into a coherent weakness report. HIGH-severity
     # weaknesses auto-create investigation goals.
     # Only runs during --full-cycle.

     Read <agent>/weakness-report.yaml (create with {last_analyzed: null, analysis_count: 0, weaknesses: []} if missing)

     # Gather signals from multiple sources
     signals = []

     # 1. Pattern signatures with high false positive rate
     Bash: pattern-signatures-read.sh --active
     FOR EACH sig WHERE sig.false_positive_rate > 0.3 AND sig.times_triggered >= 3:
         signals.append({source: "pattern_signature", id: sig.id, detail: sig})

     # 2. Guardrails that fired frequently
     Bash: guardrails-read.sh --active
     FOR EACH guard WHERE guard.times_triggered >= 3:
         signals.append({source: "guardrail", id: guard.id, detail: guard})

     # 3. Experience records with negative relative_advantage clustered by approach
     Bash: experience-read.sh --recent 20
     negative_experiences = filter WHERE relative_advantage < -0.1
     IF len(negative_experiences) >= 3:
         # Cluster by category
         clusters = group_by(negative_experiences, "category")
         FOR EACH cluster WHERE len(cluster.items) >= 2:
             signals.append({source: "experience_cluster", category: cluster.key, count: len(cluster.items)})

     # 4. Backpressure rollback patterns
     Bash: meta-backpressure.sh status
     FOR EACH rollback in result.rollback_history:
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
                     description: "Weakness detected by aggregated failure analysis.\nType: {weakness.type}\nEvidence: {weakness.evidence}\nDiscovered by: Step 5.55 Weakness Analysis"
                 }
                 # Route to most relevant aspiration
                 target_asp = aspiration matching weakness category, or most recent active
                 echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh {target_asp}
                 weakness.remediation.goal_id = created_goal_id
                 Output: "▸ WEAKNESS ANALYSIS: Created investigation goal for {weakness.description}"

     weakness_report.last_analyzed = now
     weakness_report.analysis_count += 1
     Edit <agent>/weakness-report.yaml with updated content

     Output: "▸ Weakness analysis: {len(signals)} signals, {new_weakness_count} new weakness(es), {goal_count} investigation goal(s)"
5.7. **Meta-Reflection ROI Tracking**:
     For each reflection mode invoked in this cycle, track:
     - Did it produce a reasoning bank entry, guardrail, or pattern signature?
     - Did it add an encoding queue item?
     - Did it change a belief or knowledge node?
     Compute: reflection_roi = artifacts_produced / modes_invoked
     Append via meta-yaml.py append to reflection-strategy.yaml roi_history:
       {date: today, modes_invoked: N, artifacts_produced: N, roi: N, session: N}

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
     FOR EACH node where retrieval_count > 10 AND last_updated older than 5 sessions:
         echo '{"node_key": "<key>", "reason": "stale-high-retrieval", "retrieval_count": <N>, "sessions_since_update": <M>, "priority": "MEDIUM"}' | wm-append.sh knowledge_debt
         Log: "▸ Tree lint: {node.key} flagged stale (retrieved {retrieval_count}x, last updated {sessions_ago} sessions ago)"
     
     # Cross-reference discovery: nodes that share entities but aren't linked
     Bash: world-cat.sh knowledge/tree/_tree.yaml  # entity_index
     IF entity_index is non-empty:
         FOR EACH entity appearing in 2+ nodes:
             Check if those nodes have cross-references to each other in their .md files
             IF no cross-reference exists:
                 Add "See also: [{other_node}]({other_node.file})" to both nodes
                 Log: "▸ Tree lint: cross-reference added between {node_a.key} and {node_b.key} (shared entity: {entity})"
     
     # Width check: interior nodes exceeding K_max children
     Read core/config/tree.yaml for K_max
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
- **Updates `<agent>/developmental-stage.yaml`**: Schema operations (assimilation/accommodation)
- **Updates `meta/skill-gaps.yaml`**: Capability gap detection (Spark Q6) and skill underperformance (Spark Q7)

---

## Active Forgetting — Decay Formula Reference

retention_score = base_decay^days_since_last_access × (1 + retrieval_count × retrieval_boost)

Default parameters: base_decay=0.95, retrieval_boost=0.15.
See /reflect-maintain (Memory Curation mode) for full active forgetting procedures (interference detection,
reconsolidation windows, protection rules).

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
