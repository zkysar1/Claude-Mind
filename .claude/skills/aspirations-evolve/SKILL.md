---
name: aspirations-evolve
description: "Evolution engine — developmental stage assessment, config parameter tuning, gap analysis, novelty filter, cap enforcement, pattern calibration, strategy archive, forge check, skill curation"
user-invocable: false
parent-skill: aspirations
triggers:
  - "/aspirations evolve"
conventions: [aspirations, pipeline, pattern-signatures, reasoning-guardrails, spark-questions]
minimum_mode: autonomous
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
   Read <agent>/developmental-stage.yaml (current assessment, epsilon, schema log)
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
     If highest != <agent>/developmental-stage.yaml.highest_capability:
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

   Update <agent>/developmental-stage.yaml:
     overall_stage, average_competence, exploration_budget, evidence

   Run active forgetting pruning:
     Read core/config/memory-pipeline.yaml forgetting config
     For each leaf node, calculate retention score
     If retention < 0.4: archive (if validated) or deprecate
   ```

0.5. **Config Parameter Tuning**:
   ```
   Read core/config/ files with modifiable: sections (tree, memory-pipeline, aspirations, skill-gaps, evolution-triggers)
   Read meta/config-overrides.yaml (current overrides)

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
       Write override to meta/config-overrides.yaml:
         {param}: {value: new_value, previous: old_value, changed_date: today}
       Append to meta/config-changes.yaml:
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

0.7. **Meta-Strategy Evaluation** *(metacognitive self-modification — the HyperAgents core)*:
   ```
   # CONTRACT CHECK: Is the agent allowed to modify meta-strategies?
   Bash: curriculum-contract-check.sh --action allow_meta_edits
   IF exit code 1 (not permitted by curriculum stage):
       Log: "META EVAL: blocked by curriculum — read-only access to meta/"
       SKIP to Step 1

   # Read current state
   Read meta/goal-selection-strategy.yaml
   Read meta/reflection-strategy.yaml
   Read meta/evolution-strategy.yaml
   Read meta/encoding-strategy.yaml
   Read meta/improvement-instructions.md
   Read meta/improvement-velocity.yaml (last 20 entries)
   Bash: meta-impk.sh compute --window 10 --metric pipeline_accuracy
   Bash: meta-impk.sh compute --window 10 --metric goal_completion_rate

   # AutoContext-inspired pre-evaluation checks
   # 1. Backpressure cooldown check — don't modify recently-rolled-back fields
   Bash: meta-backpressure.sh cooldown-check --window 20
   cooldown_fields = parse result.in_cooldown
   # Fields in cooldown will be skipped when proposing changes below

   # 2. Dead end check — don't propose known-bad approaches
   Bash: meta-dead-ends.sh read --active
   active_dead_ends = parse result as JSON array
   # Dead ends will block specific value ranges when proposing changes below

   # 3. Credit assignment context — prioritize modifying low-attribution parameters
   Read meta/credit-assignment.yaml
   # High-attribution parameters should be preserved; low-attribution modified first

   # 4. Strategy generation history — what configurations performed best
   Bash: meta-generations.sh status
   gen_status = parse result
   # "Generation {gen_status.peak_generation} was peak (avg {gen_status.peak_score:.4f}). Current gen {gen_status.current_generation} at {gen_status.current_avg_lv:.4f}."

   # 5. Weakness report context
   IF file_exists(<agent>/weakness-report.yaml):
       Read <agent>/weakness-report.yaml
       high_weaknesses = filter weaknesses where severity == "HIGH" and status == "active"
       # Active HIGH weaknesses should inform meta-strategy changes

   # Evaluate: Are current meta-strategies working?
   IF imp@k is declining (direction == "declining") for ANY tracked metric:
       Log: "META ALERT: improvement velocity declining — review needed"
       # Diagnose: Which strategy area is underperforming?
       # Cross-reference meta-log signals with velocity segments.
       Read meta/meta-log.jsonl (last 20 entries)
       Cluster signals by strategy_file to identify problematic area.

       # Propose change or A/B experiment
       Bash: meta-experiment.sh list --active
       IF no active experiment AND a specific change is proposed:
           Bash: meta-experiment.sh create \
               --strategy {target_file} --field {target_field} \
               --baseline {current_value} --variant {proposed_value}
       ELIF specific fix is clear (high confidence):
           # AutoContext guards: check cooldown, dead ends, and credit before setting
           IF {dotpath} in cooldown_fields:
               Log: "META SKIP: {dotpath} in backpressure cooldown — skipping"
           ELIF dead_end_match = check_dead_ends(active_dead_ends, {file}, {dotpath}, {new_value}):
               Bash: meta-dead-ends.sh increment {dead_end_match.id} times_matched
               Log: "META BLOCKED: {dotpath} = {new_value} hits dead end {dead_end_match.id}: {dead_end_match.failure_pattern}"
           ELSE:
               Bash: meta-set.sh {file} {dotpath} {new_value} --reason "{justification}"

   ELIF imp@k is stable or improving:
       Log: "META STATUS: improvement velocity stable/improving — no changes needed"

   # Check active experiments
   Bash: meta-experiment.sh list --active
   FOR EACH active experiment past min_duration_goals (10):
       Bash: meta-experiment.sh status --id {exp_id}
       IF sufficient data for resolution:
           Bash: meta-experiment.sh resolve --id {exp_id}
           # Result: adopted (variant wins), reverted (baseline wins), or inconclusive

   # Update master meta-state
   Edit meta/meta.yaml: last_evaluation = today, evaluation_count += 1
   ```

1. Read all state: Bash: load-aspirations-compact.sh → IF path returned: Read it (compact aspirations data), pipeline, knowledge, meta-memory, journal

1.5. **Plateau Detection & Strategic Redirection** *(AVO-inspired self-supervision)*:
   Inspired by NVIDIA AVO (arXiv:2603.24517) — detects when aspirations keep completing
   goals but learning yield is stagnating, and redirects effort to fresh directions.
   ```
   Read core/config/aspirations.yaml → plateau_detection config
   velocity_window = plateau_detection.velocity_window (default 5)
   plateau_threshold = plateau_detection.plateau_threshold (default 0.2)
   diminishing_returns_window = plateau_detection.diminishing_returns_window (default 5)

   qualifying_asp_ids = [asp.id for asp in active_aspirations
                         where completed_goals >= velocity_window]

   IF qualifying_asp_ids:
       # Batch trajectory compilation — loads shared data once for all aspirations
       Bash: aspiration-trajectory.sh {qualifying_asp_ids joined by space}
       all_trajectories = parse JSON output
       # Single ID returns flat object; multiple IDs returns keyed object
       IF len(qualifying_asp_ids) == 1:
           all_trajectories = {qualifying_asp_ids[0]: all_trajectories}

   FOR EACH asp_id in qualifying_asp_ids:
       trajectory = all_trajectories[asp_id]

       IF trajectory.plateau_detected:
           Log: "PLATEAU DETECTED: {asp.id} '{asp.title}' — learning velocity {trajectory.current_velocity:.2f} over last {velocity_window} goals"

           # Review trajectory for strategic redirection
           last_inflection = trajectory.last_inflection_point
           IF last_inflection:
               Log: "Last inflection at {last_inflection.goal_id}: {last_inflection.description}"

           # Decision: redirect, archive, or continue
           IF trajectory.goals_since_inflection >= velocity_window * 2:
               # Prolonged plateau — recommend archival and pivot
               Log: "STRATEGIC REDIRECT: {asp.id} — prolonged plateau, recommending archival"
               Bash: aspirations-complete.sh {asp.id}
               invoke /create-aspiration from-self --plan with:
                   context: "Pivoting from '{asp.title}' after learning plateau. Prior trajectory: {trajectory.summary}. Explore directions NOT yet tried."
           ELSE:
               # Recent plateau — add investigation goal
               Log: "STRATEGIC REDIRECT: {asp.id} — adding exploration goal"
               echo '{"title":"Investigate: Fresh directions for {asp.title}","description":"Learning velocity has plateaued. Review trajectory, identify untried approaches, propose alternative exploration directions. Prior trajectory summary: {trajectory.summary}","priority":"HIGH","category":"{trajectory.primary_category}","participants":["agent"]}' | Bash: aspirations-add-goal.sh {asp.id}

       ELIF trajectory.diminishing_returns:
           Log: "DIMINISHING RETURNS: {asp.id} '{asp.title}' — learning yield declining monotonically over {diminishing_returns_window} goals"
           # Flag for review but don't auto-redirect — diminishing returns
           # may be acceptable near completion
           IF asp.progress.completed_goals / asp.progress.total_goals < 0.80:
               echo '{"title":"Investigate: Diminishing returns on {asp.title}","description":"Learning yield declining over last {diminishing_returns_window} goals despite continued effort. Check if approach needs adjustment or if aspiration is near its knowledge frontier.","priority":"MEDIUM","category":"{trajectory.primary_category}","participants":["agent"]}' | Bash: aspirations-add-goal.sh {asp.id}
   ```

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
   Read <agent>/self.md
   Ask: "Given this Self and the current aspirations, what is Self missing?
   What would a person with this purpose naturally want to do next that
   isn't covered? What data sources do I know about that I haven't accessed?"
   If gap found: invoke /create-aspiration from-self --plan
     with: default_scope = "project"  # gap-analysis aspirations default to project scope
   Accept "no gap" as valid — only create when genuinely needed.

   **Meta-gap analysis**: Given the current improvement velocity and meta-log signals,
   is there a procedural gap in how I improve? Am I missing a meta-strategy for some
   aspect of the learning loop? If meta-gap found, write initial content to the
   appropriate meta/ strategy file and log via meta-log-append.sh.

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
7. Update `<agent>/profile.yaml` if strategy parameters change
8. Update `meta/meta-knowledge/_index.yaml` with any new self-model insights
9. **Forge check**: Audit registries, then create goals for forge-ready gaps:
   - **Integrity audit**: invoke `/forge-skill check` (orphan detection, max_gaps, encounter log limits, tree cross-check)
   - **Forge-ready gap → goal creation**: Read `meta/skill-gaps.yaml`. For EACH gap where `status != "forged"`:
     - Read `core/config/skill-gaps.yaml` → `forge_threshold` (default: 2)
     - Read `<agent>/developmental-stage.yaml` → current stage
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

### Skill Curation (Step 9.5 — after forge check)

Quality-based skill curation using five-dimension evaluation data.
See `core/config/conventions/skill-quality.md` for dimension definitions.

```
Read core/config/skill-gaps.yaml (quality_thresholds section)
Bash: skill-evaluate.sh underperforming --threshold {quality_thresholds.retirement_floor}
underperforming = parse JSON output

FOR EACH skill in underperforming:
    IF skill.total_evaluations >= quality_thresholds.min_evaluations (5):
        Read <agent>/forged-skills.yaml
        IF skill is a forged skill (exists in forged-skills.yaml):
            # Check if any pending goals depend on this skill
            Bash: load-aspirations-compact.sh
            IF no pending goals use this skill:
                Log: "SKILL CURATION: Retiring {skill.name} — quality {skill.overall} after {skill.total_evaluations} evaluations"
                Add goal: "Retire forged skill: {skill.name}" to current/evolution aspiration
            ELSE:
                Log: "SKILL CURATION: {skill.name} underperforming but has pending goals — creating improvement goal"
                Add goal: "Improve forged skill: {skill.name} (quality {skill.overall})" to current aspiration
        ELSE:
            # Base skill — cannot retire. Flag for user attention.
            Log: "SKILL ALERT: Base skill {skill.name} quality {skill.overall} — user review recommended"
            Write to <agent>/session/pending-questions.yaml:
                question: "Base skill {skill.name} has quality {skill.overall}. Should I create a better forged alternative?"
                default_action: "Monitoring — will create improvement goal if quality drops further"
                status: pending

# Quality bar tightening: if average skill quality is high, raise expectations
Bash: skill-evaluate.sh report
avg_quality = parse summary.avg_overall
IF avg_quality > 0.80 AND summary.total_skills_evaluated >= 5:
    Read meta/skill-quality-strategy.yaml
    IF review_threshold < 0.60:
        Bash: meta-set.sh skill-quality-strategy.yaml review_threshold 0.60 \
            --reason "Average quality {avg_quality} supports higher bar"
```

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
1. Record the old strategy in `meta/strategy-archive.yaml`:
   - strategy name, category, active_from, active_to
   - performance data (accuracy, sample_size, roi_pct)
   - superseded_by (new strategy name + reason)
   - evolution_trigger (which trigger caused the change)
   - session_archived
2. This preserves history for rollback decisions and performance comparison.

### Curriculum Evaluation (after strategy archive)

Evaluate curriculum graduation gates and promote if all pass.

```
invoke /curriculum-gates

This will:
  - Evaluate all gates for the current curriculum stage
  - If all pass: promote to the next stage, log promotion
  - If not all pass: report gate status (no action needed)
  - If curriculum not configured: skip silently
```
