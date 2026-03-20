---
name: aspirations-evolve
description: "Evolution engine — developmental stage assessment, config parameter tuning, gap analysis, novelty filter, cap enforcement, pattern calibration, strategy archive, forge check"
user-invocable: false
parent-skill: aspirations
triggers:
  - "/aspirations evolve"
conventions: [aspirations, pipeline, pattern-signatures, reasoning-guardrails, spark-questions]
---

# Evolution Engine (`evolve` sub-command)

Trigger evolution check — the system evaluates its own strategy and generates new aspirations. Invoked by Phase 9 performance-based evolution triggers, or directly via `/aspirations evolve`. Covers developmental stage assessment, config parameter tuning, state reading, evolve-first aspiration review, constraint-aware rebalancing, self-driven gap analysis, novelty filter, cap enforcement, logging, profile/meta update, forge check, pattern signature calibration, and strategy archive.

---

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

### `evolve`

Trigger evolution check — the system evaluates its own strategy and generates new aspirations:

0. **Developmental Stage Assessment** (competence-based):
   ```
   Read core/config/developmental-stage.yaml (stage definitions, competence_mapping)
   Read mind/developmental-stage.yaml (current assessment, epsilon, schema log)
   leaves_json=$(bash core/scripts/tree-read.sh --leaves)
   # Filter to entries where depth >= 2, extract capability_level from each

   Compute average_competence:
     competence_mapping: EXPLORE=0.15, CALIBRATE=0.45, EXPLOIT=0.70, MASTER=0.90
     For each leaf at depth >= 2: map capability_level → numeric value
     average_competence = mean(all competence values)
     If no leaves at depth >= 2: average_competence = 0.0

   Compute exploration_budget:
     exploration_budget = max(0.15, min(0.85, 1.0 - average_competence))

   Determine stage label from average_competence:
     exploring:  avg < 0.30
     developing: 0.30 <= avg < 0.55
     applying:   0.55 <= avg < 0.80
     mastering:  avg >= 0.80

   Also update highest_capability and lowest_capability:
     # Use same leaves_json from above (already has all depth >= 2 nodes)
     highest = max(leaf.capability_level for all leaves at depth >= 2)
     lowest = min(leaf.capability_level for all leaves at depth >= 2)
     If highest != mind/developmental-stage.yaml.highest_capability:
       Update highest_capability
       Log: "DEVELOPMENTAL UPDATE: highest_capability → {highest}"

   If stage has CHANGED since last check:
     Update exploration_budget (epsilon)
     Update allowed hypothesis_types and max_commitment (per stage definition)
     Log: "DEVELOPMENTAL TRANSITION: {old} → {new}"

   Metacognitive self-check (every 5th goal via sq-010):
     "Based on my knowledge, accuracy, and experience — what capability level am I at?
      Does it match the computed level?"
     If divergence: log as ACCOMMODATION in schema_operations.log

   Schema operation detection:
     Read recent reflections and violations
     For each finding that contradicts existing framework:
       Log as ACCOMMODATION in schema_operations.log
       Set equilibration_state = "disequilibrium"
     For each finding that confirms existing framework:
       Log as ASSIMILATION in schema_operations.log

   Update mind/developmental-stage.yaml:
     overall_stage, average_competence, exploration_budget, evidence

   Run active forgetting pruning:
     Read core/config/memory-pipeline.yaml forgetting config
     For each leaf node, calculate retention score
     If retention < 0.4: archive (if validated) or deprecate
   ```

0.5. **Config Parameter Tuning**:
   ```
   Read core/config/ files with modifiable: sections (tree, memory-pipeline, aspirations, skill-gaps, evolution-triggers)
   Read mind/config-overrides.yaml (current overrides)

   For each modifiable parameter:
     Assess: Does performance data suggest this parameter should change?
     Consider:
       - Is the tree hitting K_max limits frequently? → increase K_max
       - Is encoding_gate rejecting too many observations? → lower threshold
       - Are evolution triggers firing too often/rarely? → adjust thresholds
       - Is the consolidation budget consistently maxing out? → increase max
       - Are skills hitting max_skills ceiling? → increase if quality is high

     If change warranted:
       Validate new_value is within [min, max] bounds from config modifiable section
       Write override to mind/config-overrides.yaml:
         {param}: {value: new_value, previous: old_value, changed_date: today}
       Append to mind/config-changes.yaml:
         - param: "{param}"
           config_file: "{source config file}"
           old_value: {old}
           new_value: {new}
           reason: "one-line justification"
           date: "{ISO 8601}"
           session: {N}
           triggered_by: "{what performance signal prompted this}"

   Log summary: "CONFIG TUNING: {N} parameters adjusted"
   ```

1. Read all state: Bash: load-aspirations-compact.sh → IF path returned: Read it (compact aspirations data), pipeline, knowledge, meta-memory, journal
2. **Evolve-first**: For each active aspiration, ask:
   - Should priority change based on performance data?
   - Should goals be added/removed based on what we've learned?
   - Is this aspiration still relevant or should it be archived?
   - **Scope check**: Is `scope` set? If missing, classify using create-aspiration Step 1.5 logic and set it.
   - **Merge check**: Scan for sprint-scope aspirations (or legacy aspirations with ≤4 goals and no scope)
     that share a category. If 2+ cluster in the same domain, consider merging into a project-scope aspiration:
     ```
     sprint_aspirations = [a for a in active where a.scope == "sprint"
                           or (a.scope is None and len(a.goals) <= 4)]
     clusters = group_by_category(sprint_aspirations)
     FOR EACH cluster where len(cluster) >= 2:
         Log: "MERGE CANDIDATE: {len(cluster)} sprint aspirations in {domain}"
         # Create merged project-scope aspiration with combined goals
         invoke /create-aspiration from-self --plan with:
             merge_context: {cluster asp-ids, combined titles, combined goals}
         # Retire the small aspirations (goals migrated to new aspiration)
         FOR EACH asp in cluster:
             Bash: aspirations-retire.sh {asp.id}
     ```
2.5. **Constraint-Aware Rebalancing**:
   Bash: wm-read.sh known_blockers --json
   Count pending goals blocked by each known_blocker
   IF any blocker blocks >30% of pending goals:
       Log constraint to evolution-log
       Deprioritize blocked HIGH→MEDIUM goals
       Invoke /create-aspiration from-self with constraint_context:
           blocked_resource, avoid_skills, focus on executable alternatives
       echo '<json>' | wm-set.sh active_constraints
3. **Self-driven gap analysis**:
   ```
   # Retrieve broad domain context for gap analysis.
   # Read tree summary to identify the primary domain L1 node(s), then retrieve.
   Bash: tree-read.sh --summary
   # Pick the L1 node(s) with most children/articles as the primary domain category
   Bash: retrieve.sh --category {primary_domain_node} --depth medium
   ```
   Read mind/self.md
   Ask: "Given this Self and the current aspirations, what is Self missing?
   What would a person with this purpose naturally want to do next that
   isn't covered? What data sources do I know about that I haven't accessed?"
   If gap found: invoke /create-aspiration from-self --plan
     with: default_scope = "project"  # gap-analysis aspirations default to project scope
   Accept "no gap" as valid — only create when genuinely needed.

   **Idea goal signal check**: Scan active aspirations for accumulated idea goals
   (title starts with "Idea:"). If 3+ idea goals cluster in one domain/category,
   this signals a potential new aspiration direction. Consider creating an aspiration
   to explore that cluster. Ideas are proto-aspirations.
4. **Novelty filter** (stepping stones): Before creating new aspirations:
   - Compare candidate against existing aspirations
   - If too similar to an existing aspiration → reject (prevents sprawl)
   - If sufficiently novel → accept (encourages exploration)
5. **Aspiration cap enforcement**: If > `max_active` aspirations exist:
   - Use `aspirations-retire.sh <asp-id>` for never-started aspirations (no goals completed, last_worked is null)
   - Use `aspirations-complete.sh <asp-id>` for aspirations that had progress
   - Then `aspirations-archive.sh` for sweep
   - Never exceed the cap
6. Log all changes via `echo '{"date":"...","event":"...","details":"...","trigger_reason":"..."}' | bash core/scripts/evolution-log-append.sh` with:
   - `date`, `event`, `details`, `trigger_reason`
   - `aspirations_created`, `aspirations_completed`, `aspirations_archived`
   - Update last_evolution timestamp: `Bash: aspirations-meta-update.sh last_evolution "$(date +%Y-%m-%d)"`
7. Update `mind/profile.yaml` if strategy parameters change
8. Update `mind/knowledge/meta/_index.yaml` with any new self-model insights
9. **Forge check**: Audit registries, then create goals for forge-ready gaps:
   - **Integrity audit**: invoke `/forge-skill check` (orphan detection, max_gaps, encounter log limits, tree cross-check)
   - **Forge-ready gap → goal creation**: Read `mind/skill-gaps.yaml`. For EACH gap where `status != "forged"`:
     - Read `core/config/skill-gaps.yaml` → `forge_threshold` (default: 2)
     - Read `mind/developmental-stage.yaml` → current stage
     - IF `gap.times_encountered >= forge_threshold`
          AND `gap.estimated_value >= "medium"`
          AND developmental stage >= EXPLOIT (developing+):
       - Bash: load-aspirations-compact.sh → IF path returned: Read it (search compact data for this gap's ID)
       - IF no pending forge goal exists:
         - Route to target aspiration (current → matching category → `/create-aspiration from-self`)
         - Build goal: title `"Forge skill: {gap.procedure_name}"`,
           skill `"/forge-skill"`, args `"skill {gap.id}"`, priority `"MEDIUM"`
         - Add via `aspirations-update.sh`
         - Log: `echo '{"date":"...","event":"forge-ready","details":"Gap {gap.id} met criteria in evolve Phase 9.2","trigger_reason":"evolve-forge-check"}' | bash core/scripts/evolution-log-append.sh`

### Pattern Signature Calibration (during evolve or weekly)
`bash core/scripts/pattern-signatures-read.sh --active` → get active patterns as JSON. For each pattern:
1. Check calibration rules from the `calibration_protocol` section:
   - `false_positives / times_triggered > 0.20` → flag for condition tightening
   - `times_triggered == 0 AND sessions_since_creation > 10` → flag as stale, consider loosening
   - `true_positives >= 5 AND false_positive_rate < 0.10` → graduate to `validated`, increase weight
   - `utility_stats.utility_ratio < 0.2 after 10+ retrievals` → prune candidate
2. For flagged patterns: propose specific condition changes
3. Update `validation_status` based on current stats
4. Log changes to pattern calibration via `echo '<json>' | bash core/scripts/evolution-log-append.sh`

### Strategy Archive
When a strategy changes during evolution:
1. Record the old strategy in `mind/strategy-archive.yaml`:
   - strategy name, category, active_from, active_to
   - performance data (accuracy, sample_size, roi_pct)
   - superseded_by (new strategy name + reason)
   - evolution_trigger (which trigger caused the change)
   - session_archived
2. This preserves history for rollback decisions and performance comparison.
