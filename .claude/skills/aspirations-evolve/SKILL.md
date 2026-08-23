---
name: aspirations-evolve
description: "Runs the autonomous evolution engine: assesses developmental stage, tunes config parameters, performs self-driven gap analysis, applies novelty filters, enforces caps, calibrates pattern signatures, archives strategies, runs the /forge-skill check, and curates skill health. Use whenever Phase 9 performance-based triggers fire, the user runs /aspirations evolve, or the orchestrator schedules a mandatory evolution pass. Internal sub-skill of /aspirations."
user-invocable: false
parent-skill: aspirations
triggers:
  - "/aspirations evolve"
conventions: [aspirations, pipeline, pattern-signatures, reasoning-guardrails, spark-questions]
minimum_mode: autonomous
revision_id: "skill-bootstrap-aspirations-evolve-26d9dc"
previous_revision_id: null
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
   Read agents/<agent>/developmental-stage.yaml (current assessment, epsilon, schema log)
   old_stage = agents/<agent>/developmental-stage.yaml.overall_stage
   old_highest = agents/<agent>/developmental-stage.yaml.current_assessment.highest_capability

   # Stage-block computation is SCRIPT-ENFORCED (g-115-2624). The former inline
   # formula here (tree-leaf capability mean at depth >= 2, competence_mapping
   # EXPLORE=0.15/CALIBRATE=0.45/EXPLOIT=0.70/MASTER=0.90, stage bands
   # 0.30/0.55/0.80, exploration_budget = clamp(1 - tree_maturity, 0.15, 0.85))
   # now lives in core/scripts/_competence.py::assess_stage — the SAME math,
   # deterministic and refreshed at every curriculum-evaluate chokepoint too.
   # WHY: LLM-discretionary producers drift silent (rb-3171); this exact block
   # fired once in 2 months on zeta, pinning ZDS agents at 'exploring' with
   # forge gated and resolved_hypotheses contradicting its own sibling
   # evidence (ZDS omni code-read 2026-07-18). The one call below computes AND
   # writes: overall_stage, current_assessment.{stage,tree_maturity,
   # highest_capability,lowest_capability,exploration_budget,
   # resolved_hypotheses(=pipeline_resolved),average_competence,components,
   # evidence,producer,assessed_at}, exploration.epsilon. Do NOT re-inline the
   # math here.
   Bash: py -3 core/scripts/competence-assess.py
   Parse JSON stdout → stage_assessment.{stage, tree_maturity, highest_capability, exploration_budget}

   If stage_assessment.highest_capability != old_highest:
     Log: "DEVELOPMENTAL UPDATE: highest_capability → {stage_assessment.highest_capability}"

   If stage_assessment.stage != old_stage:
     # Numbers are already written by the script; the LLM applies the
     # stage-definition SIDE EFFECTS (behavioral envelope from
     # core/config/developmental-stage.yaml stages.<stage>.behaviors):
     Update allowed hypothesis_types and max_commitment (per stage definition)
     Log: "DEVELOPMENTAL TRANSITION: {old_stage} → {stage_assessment.stage}"

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

   Update agents/<agent>/developmental-stage.yaml:
     schema_operations.log + equilibration_state ONLY (from the two blocks above).
     # ALL numeric assessment fields (overall_stage, current_assessment.*,
     # exploration.epsilon) are script-written by competence-assess.py — the
     # single producer since g-115-2624. Evolve MUST NOT hand-write any of
     # them; hand-writing re-creates the g-115-2028 last-writer-wins collision
     # AND the rb-3171 silent-drift class this delegation eliminated. Evolve's
     # write surface here is the schema_operations narrative alone.

   Run active forgetting pruning:
     Read core/config/memory-pipeline.yaml forgetting config
     For each leaf node, calculate retention score
     If retention < 0.4: archive (if validated) or deprecate
   ```

0.5. **Config Parameter Tuning**:
   ```
   Read core/config/ files with modifiable: sections (tree, memory-pipeline, aspirations, skill-gaps, evolution-triggers)
   Bash: meta-read.sh config-overrides.yaml  # current overrides

   For each modifiable parameter:
     Assess: Does performance data suggest this parameter should change?
     Consider:
       - Is the tree hitting K_max limits frequently? → increase K_max
       - Is encoding_gate rejecting too many observations? → lower threshold
       - Are evolution triggers firing too often/rarely? → adjust thresholds
       - Is the consolidation budget consistently maxing out? → increase max
       - Are skills hitting max_skills ceiling? → the cap is RATCHET-DOWN ONLY:
         tighten it, or retire skills. Do NOT try to raise it. `modifiable.max_skills`
         in core/config/skill-gaps.yaml is {min: 10, max: 100, default: 100}, so the
         modifiable MAXIMUM equals the default and step 104's bounds validation
         refuses every increase. This line used to read "increase if quality is high",
         which sent readers after a change the bounds cannot express (g-115-5533).
         The one-way bound is deliberate, not an oversight: every skill's name and
         description is loaded into the system prompt at startup, so the corpus is a
         pure additive ratchet — forging adds and nothing subtracts — and a ceiling
         that can only tighten is the counter-pressure (learning-philosophy.md rule 5).
         Measured 2026-08-11: 116 skill dirs against a cap of 100.

     If change warranted:
       Validate new_value is within [min, max] bounds from config modifiable section
       Write override via meta-set.sh to config-overrides.yaml:
         {param}: {value: new_value, previous: old_value, changed_date: today}
       Append via meta-yaml.py append to config-changes.yaml:
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

0.5b. **Structural Modifiable Surfacing (S8 — tree taxonomy changes)**:
   ```
   # The `modifiable:` block above is numeric-only. Categorical / structural
   # changes (e.g., renaming an L1, adding a new L1) live in
   # `structural_modifiable:` blocks and require user approval before any
   # apply script runs. Evolution engine PROPOSES; the user GATES.
   #
   # This step surfaces strong candidates as pending-questions — never as
   # auto-writes. The candidates come from passive evidence accumulated
   # elsewhere (S1 board posts, S9 l1-pick-log).
   #
   # Trigger conditions for filing a pending-question:
   #   - The latest l1-skew-check board post carries a flagged finding
   #     (flag_reason: dominance / share_creep / empty_l1 — g-115-2455
   #     recalibration; ratio rides along as evidence, not the gate)
   #     AND no l1-taxonomy-* pending-question is currently open
   #   - OR the l1-pick-log shows the same NEW node-cluster going to one
   #     L1 repeatedly while not fitting siblings (signal for ADD)
   #   - OR a fresh-eyes-tree briefing surfaced a candidate the user has
   #     not yet been asked about

   Read core/config/tree.yaml `structural_modifiable.l1_domains`
   IF block missing OR requires_user_approval != true:
     SKIP — feature disabled or misconfigured
   # Anti-stacking guard. FLEET-WIDE and FAIL-CLOSED (g-115-3100).
   #   --all-agents is load-bearing, not defensive: an L1 taxonomy change is a
   #   WORLD-GLOBAL structural proposal, but the questions live in per-agent
   #   files. A bound-agent read lets two agents on different boxes each see
   #   "nothing open" in their OWN file and both file competing proposals at
   #   the user. Measured 2026-07-25: the 2 open l1-taxonomy- questions belong
   #   to echo and zeta, so an alpha-bound read returns 0 and does NOT suppress
   #   while a fleet read returns 2 and does.
   #   This call previously passed --prefix to a parser that did not accept it
   #   (exit 2, EMPTY stdout), and the empty read was taken as "nothing open" —
   #   so the guard was VACUOUS and never suppressed. --prefix now exists.
   Bash: pending-questions-read.sh --all-agents --prefix l1-taxonomy- --status pending
   IF the call exits non-zero OR its output is not parseable JSON:
     SKIP — treat an unreadable probe as SUPPRESSED, never as "nothing open".
     Suppression gates fail CLOSED (guard-487); the inverse is what made this
     guard vacuous. Emit a stderr WARN so the broken probe is visible.
   IF any pending l1-taxonomy- question already open:
     SKIP — do not stack proposals; let the user clear the queue first.

   Read meta/l1-pick-log.jsonl (last 200 entries) for ADD candidates.
   Read latest l1-skew-check board post (channel=coordination, tag=l1-skew)
     for RENAME / boundary candidates.

   IF strong candidate found:
     Compose proposal text (current state, proposed state, evidence,
     blast radius — see l1-taxonomy-changes.md "Propose" section).
     Bash: pending-questions-add.sh \
         --id "l1-taxonomy-$(date +%Y-%m-%d)-{rename|add}-{key}" \
         --question "<proposal>" \
         --default-action "no-change" \
         --priority HIGH
     Log: "STRUCTURAL PROPOSAL: filed l1-taxonomy- pending-question"
   ELSE:
     SKIP silently — no candidates ripe for proposal this iteration.
   ```

0.7. **Meta-Strategy Evaluation** *(metacognitive self-modification — the HyperAgents core)*:
   ```
   # CONTRACT CHECK: Is the agent allowed to modify meta-strategies?
   Bash: curriculum-contract-check.sh --action allow_meta_edits
   IF exit code 1 (not permitted by curriculum stage):
       Log: "META EVAL: blocked by curriculum — read-only access to meta/"
       SKIP to Step 1

   # Read current state
   Bash: meta-read.sh goal-selection-strategy.yaml
   Bash: meta-read.sh reflection-strategy.yaml
   Bash: meta-read.sh evolution-strategy.yaml
   Bash: meta-read.sh encoding-strategy.yaml
   Bash: meta-cat.sh improvement-instructions.md
   Bash: meta-read.sh improvement-velocity.yaml  # last 20 entries
   # ONE call — the store holds a single per-goal learning_value series; the
   # old two-metric form (--metric pipeline_accuracy / goal_completion_rate)
   # returned identical output because --metric was decorative (g-115-2441).
   # NOISE CAVEAT: entries before 2026-07-17 include false 0.0s from unflagged
   # deep closes (the producer now SKIPS unmeasured closes instead of writing
   # 0.0) — a "declining" direction whose window spans that era is suspect
   # until ~20 measured closes flush it.
   Bash: meta-impk.sh compute --window 10

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
   Bash: meta-read.sh credit-assignment.yaml
   # High-attribution parameters should be preserved; low-attribution modified first

   # 4. Strategy generation history — what configurations performed best
   Bash: meta-generations.sh status
   gen_status = parse result
   # "Generation {gen_status.peak_generation} was peak (avg {gen_status.peak_score:.4f}). Current gen {gen_status.current_generation} at {gen_status.current_avg_lv:.4f}."

   # 5. Weakness report context
   IF file_exists(agents/<agent>/weakness-report.yaml):
       Read agents/<agent>/weakness-report.yaml
       high_weaknesses = filter weaknesses where severity == "HIGH" and status == "active"
       # Active HIGH weaknesses should inform meta-strategy changes

   # Evaluate: Are current meta-strategies working?
   IF imp@k is declining (direction == "declining") on the learning_value series:
       Log: "META ALERT: improvement velocity declining — review needed"
       # Diagnose: Which strategy area is underperforming?
       # Cross-reference meta-log signals with velocity segments.
       Bash: meta-cat.sh meta-log.jsonl  # review last 20 entries
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
   Bash: meta-set.sh meta.yaml last_evaluation "$(date +%Y-%m-%d)" && meta-set.sh meta.yaml evaluation_count {+1}
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
   # Maintenance queues (recurring-upkeep aspirations) carry plateau_exempt: true on
   # their record — the trajectory compiler suppresses both flags for them (g-115-2387),
   # so a trajectory.plateau_exempt==true never enters the branches below. Zero learning
   # velocity is a maintenance queue's normal operating point, not a stalled direction.

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
               # Guard: aspirations with recurring goals cannot be archived (data layer blocks it)
               IF any(g.recurring for g in asp.goals):
                   Log: "Cannot archive {asp.id} — contains recurring goals. Adding exploration goal instead."
               ELSE:
                   Log: "STRATEGIC REDIRECT: {asp.id} — prolonged plateau, recommending archival"
                   Bash: aspirations-complete.sh --source {asp.source} {asp.id}
               invoke /create-aspiration from-self --plan with:
                   context: "Learning plateau in '{asp.title}' after {trajectory.goals_since_inflection} goals. Prior trajectory: {trajectory.summary}. FIRST: identify what existing work can be strengthened, deepened, or made more robust. Only pivot to genuinely new directions if deepening is exhausted."
           ELSE:
               # Recent plateau — add investigation goal
               Log: "STRATEGIC REDIRECT: {asp.id} — adding exploration goal"
               echo '{"title":"Investigate: Root causes of plateau in {asp.title}","description":"Learning velocity has plateaued. Diagnose WHY velocity dropped — is the approach wrong, are prerequisites missing, or is the problem harder than expected? Try a different approach within the same domain before pivoting to new directions. Prior trajectory summary: {trajectory.summary}","priority":"HIGH","category":"{trajectory.primary_category}","participants":["agent"],"origin_signal":"investigate:{asp.id}"}' | Bash: aspirations-add-goal.sh --source {asp.source} {asp.id}

       ELIF trajectory.diminishing_returns:
           Log: "DIMINISHING RETURNS: {asp.id} '{asp.title}' — learning yield declining monotonically over {diminishing_returns_window} goals"
           # Flag for review but don't auto-redirect — diminishing returns
           # may be acceptable near completion
           IF asp.progress.completed_goals / asp.progress.total_goals < 0.80:
               echo '{"title":"Investigate: Diminishing returns on {asp.title}","description":"Learning yield declining over last {diminishing_returns_window} goals despite continued effort. Check if approach needs adjustment or if aspiration is near its knowledge frontier.","priority":"MEDIUM","category":"{trajectory.primary_category}","participants":["agent"],"origin_signal":"investigate:{asp.id}"}' | Bash: aspirations-add-goal.sh --source {asp.source} {asp.id}
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
             Bash: aspirations-retire.sh --source {asp.source} {asp.id}
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

2.75. **Portfolio Health Review** *(periodic portfolio-level assessment)*:
   The loop handles individual aspirations well (completion review, plateau detection),
   but portfolio-level drift — priority inflation, completed aspirations lingering,
   recurring goals trapped in project aspirations — requires a holistic sweep.

   ```
   Read core/config/aspirations.yaml → portfolio_review config

   # Check if strategic scan flagged portfolio issues (runs more frequently than evolve)
   Bash: wm-read.sh portfolio_health_signal --json 2>/dev/null
   IF signal exists and non-null:
       Log: "PORTFOLIO REVIEW: triggered with scan signal — inflation:{signal.priority_inflation} unarchived:{signal.completed_unarchived}"
       Bash: wm-clear.sh portfolio_health_signal  # consumed — clear for next cycle

   # --- 2.75a: Archive Sweep ---
   # Find aspirations where all non-recurring goals are terminal (completed/skipped/expired)
   # but the aspiration itself hasn't been archived yet. This catches cases missed by
   # per-goal completion review (e.g., goals completed across sessions or by other agents).
   FOR EACH active aspiration:
       non_recurring = [g for g in asp.goals if not g.recurring]
       terminal = [g for g in non_recurring if g.status in (completed, skipped, expired)]
       IF len(non_recurring) > 0 AND len(terminal) == len(non_recurring):
           # All non-recurring goals are done — check for recurring goals
           recurring = [g for g in asp.goals if g.recurring]
           IF len(recurring) == 0:
               Log: "PORTFOLIO ARCHIVE: {asp.id} '{asp.title}' — all goals terminal, archiving"
               Bash: aspirations-complete.sh --source {asp.source} {asp.id}
           ELSE:
               # Has recurring goals — flag for relocation (Step 2.75c), don't archive yet
               Log: "PORTFOLIO FLAG: {asp.id} '{asp.title}' — non-recurring complete but {len(recurring)} recurring goals remain"

   # --- 2.75b: Recurring-Only Demotion ---
   # Aspirations whose only remaining work is recurring goals don't need HIGH priority.
   # Recurring urgency scoring ensures they execute on cadence at any priority level.
   FOR EACH active aspiration where priority == "HIGH":
       non_recurring = [g for g in asp.goals if not g.recurring]
       terminal = [g for g in non_recurring if g.status in (completed, skipped, expired)]
       IF len(non_recurring) > 0 AND len(terminal) == len(non_recurring):
           # All non-recurring goals done, only recurring remain
           Log: "PORTFOLIO DEMOTE: {asp.id} '{asp.title}' — HIGH→MEDIUM (recurring-only)"
           Demote priority — field-merge, single positional call (daemon merges only this field):
           Bash: aspirations-update.sh {asp.id} priority MEDIUM

   # --- 2.75c: Misplaced Recurring Goal Detection ---
   # Recurring monitoring goals in sprint/project aspirations should live in the
   # maintenance aspiration (asp-001 pattern). They prevent archival and inflate
   # active aspiration count.
   FOR EACH active aspiration where scope in (sprint, project):
       non_recurring = [g for g in asp.goals if not g.recurring]
       terminal = [g for g in non_recurring if g.status in (completed, skipped, expired)]
       IF len(non_recurring) > 0 AND len(terminal) == len(non_recurring):
           recurring = [g for g in asp.goals if g.recurring]
           IF len(recurring) > 0:
               Log: "PORTFOLIO RELOCATE: {asp.id} has {len(recurring)} recurring goals preventing archival"
               # Find the agent's maintenance aspiration (asp-001 or equivalent)
               # Create equivalent recurring goals there, then stop+complete originals
               relocated = 0
               skipped_referenced = 0
               FOR EACH recurring_goal in recurring:
                   # --- INBOUND-REFERENCE PRECONDITION (g-115-3096, DEFECT 2) ---
                   # Relocation creates a COPY under a NEW goal id and completes
                   # the original — every inbound reference still points at the
                   # OLD id and is orphaned. A long-lived recurring goal
                   # accumulates them across the reasoning bank, guardrails,
                   # pattern signatures, pipeline, override ledgers AND non-store
                   # surfaces (source comments, tests, rationale docs). Archiving
                   # one aspiration does not buy enough to break them.
                   #
                   # Script-enforced, not eyeballed (guard-399: never add an "LLM
                   # must check X" step without the executable behind it). The
                   # scanner splits LIVE referents from append-only narration
                   # (changelog / evolution-log / journal / board / *-archive /
                   # telemetry) and blocks only on the former — counting
                   # narration would refuse EVERY relocation, since any goal that
                   # has ever run has thousands of log lines.
                   #   exit 0 = clear, safe to relocate
                   #   exit 1 = referenced, SKIP this goal
                   Bash: py -3 core/scripts/goal-reference-scan.py {recurring_goal.id}
                   IF exit code == 1:
                       Log: "PORTFOLIO RELOCATE SKIP: {recurring_goal.id} has live inbound references — relocating would orphan them; leaving in place"
                       skipped_referenced += 1
                       CONTINUE   # next recurring goal; do NOT relocate this one
                   # Create copy in maintenance aspiration
                   new_goal = copy recurring_goal fields (title, description, category,
                       skill, interval_hours, lastAchievedAt, achievedCount,
                       currentStreak, longestStreak, priority, participants)
                   new_goal.description += " Relocated from {asp.id}."
                   new_goal.origin_signal = "recurring_cadence:{recurring_goal.id}"
                   echo '{new_goal as JSON}' | Bash: agent-aspirations-add-goal.sh asp-001
                   # Stop recurring on original and complete it
                   Bash: aspirations-update-goal.sh --source {asp.source} {goal.id} recurring false
                   Bash: aspirations-update-goal.sh --source {asp.source} {goal.id} status completed
                   relocated += 1
               # Archive ONLY if every recurring goal actually moved. A partial
               # relocation leaves non-terminal recurring goals behind, so
               # archiving here would bury live work — the aspiration stays open
               # by design until the references are migrated deliberately.
               IF skipped_referenced == 0:
                   Bash: aspirations-complete.sh --source {asp.source} {asp.id}
               ELSE:
                   Log: "PORTFOLIO RELOCATE PARTIAL: {asp.id} keeps {skipped_referenced} referenced recurring goal(s) ({relocated} relocated) — NOT archiving"

   # --- 2.75d: Priority Distribution Check ---
   # If too many aspirations share the same priority, the signal is meaningless.
   # HIGH should mean "strategically important right now" — not the default.
   IF portfolio_review.priority_concentration_warn > 0:
       active_count = count of active aspirations
       high_count = count where priority == "HIGH"
       IF active_count > 0 AND (high_count / active_count) > portfolio_review.priority_concentration_warn:
           Log: "PORTFOLIO WARN: {high_count}/{active_count} aspirations are HIGH — priority inflation detected"
           # Identify demotion candidates: HIGH aspirations with no recent activity.
           # last_selected is the LIVE recency field (stamped by aspirations.py /
           # aspirations_write.py when a goal is selected from the aspiration).
           # It is NOT last_worked: that field has ZERO writers anywhere and is
           # absent from every live record, so this branch read permanently null
           # (g-115-3097, measured 2026-08-10 — 0/28 world aspirations carry the
           # key; it survives only as a seeded null in the bootstrap templates,
           # which is what made it look real). The comment formerly here claimed
           # "last_worked is available in compact data" — that claim was FALSE and
           # was the thing making the wrong field look justified to a reader: 0 of
           # 23 rendered compact records carried the key (measured 2026-07-29).
           stale_hours = portfolio_review.stale_threshold_sessions * 24
           FOR EACH HIGH aspiration:
               IF asp has no in-progress goals:
                   # The recency field must be POPULATED to be evidence of staleness.
                   # The former `is null OR ...` form demoted every HIGH aspiration
                   # on deployments where the field is never written (measured
                   # 2026-07-30, g-029-82: null on all 9 HIGH aspirations INCLUDING
                   # ones at 78/97 and 78/92 goals completed — heavily worked, not
                   # idle). KEEP that guard: it remains correct now that the field
                   # is a real one, and it still covers records that legitimately
                   # carry null — 10 of 28 on 2026-08-10, ALL of them asp-xw-*
                   # cross-world imports, which per guard-2829 are an inbound queue
                   # rather than portfolio entries and must not be read as idle HIGH
                   # work. For a null last_selected, derive recency from the goals'
                   # own completed_date / last_modified, or skip the aspiration.
                   IF asp.last_selected is NOT null AND hours_since(asp.last_selected) > stale_hours:
                       Log: "PORTFOLIO DEMOTE: {asp.id} '{asp.title}' — HIGH with no activity for {stale_hours}+ hours"
                       Demote priority — field-merge, single positional call (daemon merges only this field):
                       Bash: aspirations-update.sh {asp.id} priority MEDIUM

   # --- 2.75e: Log portfolio health snapshot ---
   active_after = count of active aspirations (after archival/demotion)
   high_after = count where priority == "HIGH" (after changes)
   echo '{"date":"<today>","event":"portfolio_review","details":"active:{active_after} high:{high_after} archived:{archived_count} demoted:{demoted_count} relocated:{relocated_count}","trigger_reason":"evolve-portfolio-review"}' | bash core/scripts/evolution-log-append.sh
   ```

3. **Self-driven gap analysis**:
   ```
   # Retrieve broad domain context for gap analysis.
   # Read tree summary to identify the primary domain L1 node(s), then retrieve.
   Bash: tree-read.sh --summary
   # Pick the L1 node(s) with most children/articles as the primary domain category
   Bash: retrieve.sh --category {primary_domain_node} --depth medium
   ```
   Read agents/<agent>/self.md
   Ask: "Given this Self and the current aspirations, what is Self missing?
   What would a person with this purpose naturally want to do next that
   isn't covered? What data sources do I know about that I haven't accessed?"
   # Consolidation guard — gate aspiration creation, not the analysis itself
   Bash: wm-read.sh consolidation_health --json 2>/dev/null
   IF consolidation_health exists AND consolidation_health.avg_completion < 0.25 AND consolidation_health.active_count >= 3:
       Log: "GAP ANALYSIS: aspiration creation deferred — consolidation health poor (avg {avg_completion:.0%}). Existing aspirations need attention first."
   ELSE:
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

3.5. **Convention Health Audit**:
   Evaluate the health of domain execution conventions. Are steps earning their cost?
   Are there recurring failures a convention should catch? Are pending proposals mature?

   ```
   # 3.5a: Load convention state
   Bash: source core/scripts/_paths.sh
   pre_exists=$(test -f "$WORLD_DIR/conventions/pre-execution.md" && echo "yes")
   post_exists=$(test -f "$WORLD_DIR/conventions/post-execution.md" && echo "yes")

   IF neither exists:
       Log: "CONVENTION HEALTH: no domain conventions configured — skip audit"
       SKIP to Step 4

   IF pre_exists: Read $WORLD_DIR/conventions/pre-execution.md
   IF post_exists: Read $WORLD_DIR/conventions/post-execution.md

   # 3.5b: Step utilization analysis
   # Scan recent journal/experience for evidence of each convention step executing
   Bash: journal-read.sh --recent 20
   FOR EACH convention step in loaded conventions:
       Estimate: times_relevant (step's IF condition was true), times_executed (step ran)
       IF times_relevant == 0 AND goals_analyzed >= 10:
           Log: "CONVENTION HEALTH: {convention}/{step} condition never matched in {goals_analyzed} goals — candidate for removal"
       IF times_relevant >= 5 AND (times_executed / times_relevant) < 0.50:
           Log: "CONVENTION HEALTH: {convention}/{step} skipped {skipped}/{times_relevant} times — needs clarification"

   # 3.5c: Missing convention gap detection
   # Find the guardrails that fire MOST and are universal/procedural → convention candidates.
   #
   # RANK-BASED, NOT VALUE-BASED (g-115-3098, fixed 2026-08-10). This filter used to
   # be an absolute `utilization.times_active >= 5`. times_active is a CUMULATIVE,
   # never-windowed, fleet-wide counter, so on a mature store an absolute threshold
   # stops discriminating. Measured on world/guardrails.jsonl: 822/1143 active (71.9%)
   # on 2026-07-25, 862/1315 (65.6%) on 2026-07-27, and 2067/2935 (70.4%) on
   # 2026-08-10 — where the median times_active was 10, i.e. the threshold of 5 sat at
   # HALF the median and selected the majority BY CONSTRUCTION. Emitting 2067
   # "candidates" is operationally identical to emitting none: nothing triages that
   # list, so the sub-phase produces no signal and its silence reads as "no convention
   # gaps exist". Pattern signature sig-27 (cumulative-counter-saturation-needs-windowing).
   #
   # WHY TOP-N RATHER THAN WINDOWING — and the answer is NOT "windowing is better but
   # unavailable". WINDOWING THIS COUNTER CANNOT WORK, for a reason that outlives any
   # telemetry you might add. times_active is incremented by guardrail-check.py on
   # KEYWORD-SCAN MATCHES, not on genuine fires: the precheck mass-matches tens of rules
   # every iteration, which is why the top entries sit in the thousands. The framework
   # already reached this conclusion once and acted on it — core/scripts/weakness-signals.py
   # computes a windowed baseline-delta over exactly this field, and its guardrail_signals
   # output was RETIRED from /reflect Step 5.55 consumption (g-115-2141, re-applied by
   # g-115-2470) with the note that "even a windowed delta carries no genuine-fire
   # information". Its stated re-add condition is a real-fires field distinct from
   # keyword-scan matches. That field does not exist, so a windowed 3.5c would be a more
   # expensive way to rank the same noise.
   #
   # So do NOT "upgrade" this sub-phase to windowing on the strength of the saturation
   # story alone — the saturation is real and the windowing cure is not. What top-N buys
   # is honest and bounded: it cannot saturate by construction (the candidate count is N
   # whatever the values grow to), it needs no new telemetry, and it puts a triageable
   # slate in front of the universal/procedural judgment that actually decides. The
   # ranking signal remains scan-volume, which is a proxy — that limitation is inherited
   # from the counter and is not repaired here.
   #
   # ORDER IS LOAD-BEARING — rank the whole active store FIRST, take the top N, and
   # only THEN drop the already-proposed. The reverse (drop-then-rank) refills the
   # slate from further down the list every pass and eventually proposes the entire
   # store — the same saturation on a slower clock. Rank-first CONVERGES: once the
   # genuine head is proposed the sub-phase goes quiet and re-fires only when a new
   # guardrail climbs into the top N. That quiet is a real measurement; the current
   # silence is structural.
   #
   # rb-245 pre-read gate: verify schema before aggregating. If the gate fails,
   # SKIP this sub-phase (3.5d continues). Do NOT --override: fix field paths instead.
   # The probe covers EVERY field the emission template reads, not just the ranking
   # key — guard-1665 step 2 (confirm the projection actually returns each field a
   # filter/template reads). This is what the pre-fix template violated: it read
   # guard.title (17.3% present), guard.description (0/2935 — the field does not
   # exist in this schema at all) and guard.when_to_use.conditions (4.7%), so it
   # would have emitted a null title, the literal string "IF None:" and a null action
   # for ~92-100% of records had it ever fired.
   Bash: source core/scripts/_paths.sh && bash core/scripts/audit-schema-gate.sh \
           --jsonl-path "$WORLD_DIR/guardrails.jsonl" \
           --field-names "utilization.times_active,rule,when_to_use,action_hint"
   Bash: guardrails-read.sh --active
   Read core/config/aspirations.yaml → modifiable.convention_learning.promotion_top_n (default 10)
   #   Read the bound at run time. Never re-hardcode it here (guard-2805).
   ranked = active guardrails sorted by utilization.times_active DESC
   candidates = first {promotion_top_n} entries of ranked        # RANK FIRST — see above
   Log: "CONVENTION GAP SCAN: top {promotion_top_n} of {len(ranked)} active by times_active (range {candidates[0].utilization.times_active}x..{candidates[-1].utilization.times_active}x)"
   FOR EACH guard in candidates:
       IF guard rule is universal (applies to most goals) AND procedural (a step to perform):
           Log: "CONVENTION GAP: guardrail {guard.id} fires frequently ({guard.utilization.times_active}x) and is procedural — convention candidate"
           # Check if already tracked in convention-changes.jsonl
           IF NOT already_proposed(guard.id):
               target = "pre-execution" if rule maps to pre-execution else "post-execution"
               # Emission template reads ONLY fields this schema actually carries
               # (measured 2026-08-10 over 2935 active records): rule 100%,
               # when_to_use 100%, action_hint 34.3%. action_hint falls back to rule.
               step_title     = first 80 chars of guard.rule (append "…" if truncated)
               step_action    = guard.action_hint if non-empty else guard.rule
               # Condition must come from a field that carries MEANING. when_to_use is
               # present on 100% of records but has TWO SHAPES and is usually worthless.
               # Measured 2026-08-10 over 2937 active records:
               #     dict, empty shell {"conditions": [], "category": ""}   2720  92.6%
               #     dict with non-empty conditions                          139   4.7%
               #     plain STRING of trigger prose                            75   2.6%
               #     dict, category only                                       3   0.1%
               # Handle the string shape explicitly — it is real, usable trigger text
               # (e.g. guard-359, guard-317). A dict-only branch silently refuses it, and
               # that miss is concentrated exactly where it hurts: 2 of the live top 10.
               IF guard.when_to_use is a string AND non-empty:
                   step_condition = "IF " + guard.when_to_use + ":"
               ELIF guard.when_to_use.conditions is non-empty:
                   step_condition = "IF " + join(guard.when_to_use.conditions, ", ") + ":"
               ELIF guard.when_to_use.category is non-empty:
                   step_condition = "IF category=" + guard.when_to_use.category + ":"
               ELSE:
                   step_condition = null        # empty shell — refused below, deliberately
               # Fail loud rather than appending a degenerate proposal. A bad proposal is
               # worse than no proposal: 3.5d will happily mature and auto-apply it into a
               # convention file.
               #
               # NON-NULL IS NOT ENOUGH, and this is the trap the fix itself fell into
               # (measured 2026-08-10 on the live top 10): rendering the whole when_to_use
               # object as a fallback produced the literal condition
               # `IF {'conditions': [], 'category': ''}:` for 6 of 10 candidates. Every one
               # passed a null check and every one was garbage. NEVER let a raw object repr
               # satisfy this gate — that is the same class as the pre-fix "IF None:",
               # wearing a non-null disguise. This is exactly what outcome (g) of
               # g-115-3098 warns about: a candidate COUNT in single digits proves nothing
               # about proposal CONTENT.
               IF any of step_title / step_condition / step_action is null or empty:
                   Log: "CONVENTION GAP: REFUSED to emit proposal for {guard.id} — unusable field (title={step_title!r} condition={step_condition!r} action={step_action!r}). A refusal here is a finding about the GUARDRAIL, not a bug: it means that entry carries no when_to_use trigger metadata and cannot become a convention step until it does."
                   CONTINUE to the next candidate — do NOT append.
               echo '{"date":"<today>","type":"promote_guardrail","target":"{target}","proposed_step":{"title":"{step_title}","condition":"{step_condition}","action":"{step_action}"},"source":"evolve-health-audit","source_hypothesis":null,"source_guardrails":["{guard.id}"],"reinforcement_count":1,"confidence":0.7,"status":"pending"}' >> $WORLD_DIR/conventions/convention-changes.jsonl
   # ACCEPTANCE (g-115-3098 outcome g): judge this sub-phase by the CONTENT of what it
   # emits, never by the candidate COUNT. A count in single digits proves nothing if
   # every proposal is null-filled — that was the pre-fix failure mode exactly. The
   # direct catch metric is the chain OUTPUT artifact: entries in
   # $WORLD_DIR/conventions/convention-changes.jsonl carrying source="evolve-health-audit"
   # (guard-1665 step 1b). That count was 0 across the store's entire history at fix time.
   #
   # CAPACITY (measured 2026-08-10): pre-execution.md 7 steps, post-execution.md 9 —
   # against convention_learning.max_steps_per_convention default 8, post-execution is
   # already OVER and pre-execution has one slot. Deliberate decision, do not "fix" by
   # raising the cap or displacing a step: 3.5d's existing pending_capacity flag is the
   # correct behavior. A saturated convention is a real signal that a human should
   # decide what to REMOVE; auto-raising the cap converts that signal into unbounded
   # growth (learning-philosophy.md rule 5 — the complexity ratchet).

   # 3.5d: Pending proposal review — auto-apply mature proposals
   IF file_exists($WORLD_DIR/conventions/convention-changes.jsonl):
       Read convention-changes.jsonl
       Read core/config/aspirations.yaml → modifiable.convention_learning.auto_apply_confidence (default 0.8)
       pending = [c for c in changes where c.status == "pending"]
       FOR EACH change in pending:
           IF change.confidence >= auto_apply_confidence OR change.reinforcement_count >= 2:
               # Read target convention, check for duplication and cost cap
               current_step_count = count steps in target convention file
               max_steps = aspirations.yaml → convention_learning.max_steps_per_convention (default 8)
               IF current_step_count >= max_steps:
                   Update change.status = "pending_capacity" in convention-changes.jsonl
                   Log: "CONVENTION HEALTH: proposal '{change.proposed_step.title}' mature but at capacity — flagged"
               ELIF proposed step overlaps with existing convention step:
                   Update change.status = "rejected" in convention-changes.jsonl
                   Log: "CONVENTION HEALTH: proposal '{change.proposed_step.title}' overlaps existing — rejected"
               ELSE:
                   Bash: bash core/scripts/history-save.sh "$WORLD_DIR/conventions/{change.target}.md" {agent_name} "Evolve: auto-applying convention step"
                   Edit $WORLD_DIR/conventions/{change.target}.md:
                       Append step at appropriate position
                   Update change.status = "applied" in convention-changes.jsonl
                   # Retire subsumed guardrails
                   FOR EACH guard_id in change.source_guardrails:
                       Bash: guardrails-update-field.sh {guard_id} status retired
                   Log: "CONVENTION APPLIED: '{change.proposed_step.title}' added to {change.target} (confidence {change.confidence}, reinforcements {change.reinforcement_count})"

   # 3.5e: Cost-benefit review — flag underperforming steps
   FOR EACH convention step:
       IF step has been active for 20+ goals AND rarely helpful (efficiency ratio < 0.1):
           Write to agents/<agent>/session/pending-questions.yaml:
               question: "Convention step '{step_title}' in {convention} has run {times} times but rarely prevents failures. Remove?"
               default_action: "Will remove after 1 more evolution cycle if no improvement"
               status: pending

   # 3.5f: Log health snapshot
   echo '{"date":"<today>","event":"convention_health_audit","details":"pre_steps:{N} post_steps:{N} utilization_flags:{flag_count} gap_candidates:{gap_count} pending_proposals:{pending_count}","trigger_reason":"evolve-convention-audit"}' | bash core/scripts/evolution-log-append.sh
   ```

4. **Interestingness filter** (OMNI-EPIC two-stage): All aspiration creation in evolve
   routes through `/create-aspiration`, which runs the two-stage interestingness filter
   (Stage 1: generation-time criteria, Stage 2: post-generation archive comparison).
   See create-aspiration Step 5. No separate filtering needed here.
5. **Aspiration cap enforcement**: If > `max_active` aspirations exist:
   - FIRST exclude `asp-xw-*` cross-world imports from cap-driven retirement entirely.
     They are an inbound queue, not portfolio entries (guard-2829), and each arrives as
     a single-goal aspiration with a null recency field — precisely the shape the
     never-started branch below retires. Retiring one DISCARDS unprocessed work sent by
     a peer deployment. 10 of 28 active aspirations were this shape on 2026-08-10.
   - Use `aspirations-retire.sh --source {asp.source} <asp-id>` for never-started aspirations (no goals completed, `last_selected` is null)
     — NOT `last_worked`: that field has zero writers and read null on every record, so
     this branch was taken unconditionally and an aspiration WITH progress would have
     been retired rather than completed (g-115-3097, measured 2026-08-10)
   - Use `aspirations-complete.sh --source {asp.source} <asp-id>` for aspirations that had progress
   - Then `aspirations-archive.sh` for sweep
   - Never exceed the cap
6. Log all changes via `echo '{"date":"...","event":"...","details":"...","trigger_reason":"..."}' | bash core/scripts/evolution-log-append.sh` with:
   - `date`, `event`, `details`, `trigger_reason`
   - `aspirations_created`, `aspirations_completed`, `aspirations_archived`
   - Update last_evolution timestamp: `Bash: aspirations-meta-update.sh --source {asp.source} last_evolution "$(date +%Y-%m-%d)"`

   **ANNECS metric update** (OMNI-EPIC-inspired open-ended progress tracking):
   ```
   # ANNECS = Accumulated Novel Aspirations Created and Solved
   # "Novel" = passed interestingness filter during creation
   # "Solved" = completed with at least 1 tree node updated (learning happened)
   #
   # Read current counters (meta-update is SET, not increment — read first):
   Bash: aspirations-read.sh --meta → extract annecs_created, annecs_solved
   current_created = annecs_created (default 0 if missing)
   current_solved = annecs_solved (default 0 if missing)
   #
   # For each aspiration CREATED this evolution cycle that passed interestingness filter:
   new_created = current_created + {count_created_this_cycle}
   Bash: aspirations-meta-update.sh --source {asp.source} annecs_created {new_created}
   #
   # For each aspiration COMPLETED this cycle with learning artifacts:
   #   (check: did completion produce tree node updates, pattern signatures, or reasoning bank entries?)
   new_solved = current_solved + {count_solved_this_cycle}
   Bash: aspirations-meta-update.sh --source {asp.source} annecs_solved {new_solved}
   #
   # Log ANNECS snapshot:
   echo '{"date":"<today>","event":"annecs_update","details":"created={new_created} solved={new_solved} ratio={new_solved/new_created} trend={improving|stable|declining}","trigger_reason":"evolve-annecs"}' | bash core/scripts/evolution-log-append.sh
   #
   # If ratio is declining over last 3 evolution cycles:
   #   Log: "ANNECS STAGNATION: novel aspiration solve rate declining"
   #   This signals aspiration quality issues — interestingness filter may need tuning
   ```
7. Update `agents/<agent>/profile.yaml` if strategy parameters change
8. Update `meta/meta-knowledge/_index.yaml` with any new self-model insights
9. **Forge check**: Audit registries, then create goals for forge-ready gaps:
   - **Integrity audit**: invoke `/forge-skill check` (orphan detection, max_gaps, encounter log limits, tree cross-check)
   - **Curriculum contract gate (g-115-1801)**: `Bash: curriculum-contract-check.sh --action allow_forge_skill`.
     IF exit code 1 (forging not permitted by the current curriculum stage): SKIP the entire
     forge-ready loop below and continue to Skill Curation. This is the stricter gate `/forge-skill`
     enforces at its OWN Step 1 — filing a forge goal now would only ABORT there, queuing
     un-executable work. The developmental-stage check below is a SEPARATE, complementary axis
     (competence-based); dev-stage >= EXPLOIT can pass while the curriculum contract still blocks,
     so gate on BOTH. (Mirrors the `allow_meta_edits` contract check already used at Step 2's META
     EVAL above.) Log one line: `"FORGE CHECK: curriculum blocks allow_forge_skill at {stage_name} — skipping forge-ready loop"`.
   - **Forge-ready gap → goal creation**: Read `meta/skill-gaps.yaml`. For EACH gap whose status
     does NOT suppress forging — suppressing set is the three TERMINAL statuses
     `{forged, dismissed, satisfied-by-extension}` plus the NON-terminal `deferred-to-goal`:
     - `forged` — a skill was created (`/forge-skill` Step, sets it + a `forged-skills.yaml` entry).
     - `dismissed` — explicitly declined (`/forge-skill` sets this; see its "Set gap `status: dismissed`").
     - `satisfied-by-extension` — the capability shipped by EXTENDING an existing script/skill rather
       than forging a new one. A legitimate and often preferable outcome (implementation-discipline:
       no single-use abstractions), and the honest label when nothing was in fact forged.
     - `deferred-to-goal` — **not terminal.** A resolution path is DECIDED and tracked by an open
       goal named in the gap's `resolution_tracked_by` field. Forging is wrong (the capability is
       not missing in the shape a new skill would fill), but nothing has shipped either, so no
       terminal status is honest yet. This status carries a re-check obligation — see below.

     Test the whole SET, never `status != "forged"` alone. Every non-excluded suppressing status makes
     its gap re-qualify as forge-ready on EVERY evolve pass, forever: the forge goal gets re-filed,
     the goal-duplication gate blocks it on the completed original via `origin_signal_completed`, and
     the pass burns the same dead-end investigation again. Observed 2026-07-27 on gap-026, whose
     `satisfied-by-extension` status (set the previous day, when the capability shipped as an
     extension to `tree-body-presence-audit.py` under g-115-3237) was invisible to the old
     one-value test — that status appears NOWHERE else in `core/` or `.claude/`, so no other layer
     caught it either. `dismissed` has the identical hole and is only latent because no gap
     currently carries it.

     **`deferred-to-goal` re-check (MANDATORY — a deferral must not suppress forever).** On seeing
     this status, probe its tracker before honoring it:
     `Bash: aspirations-query.sh --goal-field id "{gap.resolution_tracked_by}" --full`
     — EITHER NAME WORKS: `goal_id` is a REGISTERED alias of `id` (`_goal_fields.py:113`).
     SUPERSEDES the pre-2026-08-22 "only `id` is queryable" claim — re-measured on that
     claim's own goal g-115-3767: 1 row BOTH ways (g-115-4430). Fail-CLOSED below unchanged.
     Then: `resolution_tracked_by` ABSENT or empty → the deferral names nothing to re-check and so
     could never self-clear; treat it as MALFORMED, set the gap back to `registered`, note why, and
     evaluate forge criteria normally. This is the ONE case that fails OPEN, deliberately — a
     suppression with no expiry path is strictly worse than one re-filed goal. Tracker NAMED but the
     probe ERRORS or returns 0 rows → WARN + SKIP, failing CLOSED (guard-487; an unreadable tracker
     is not a dead one, and a spurious re-file does not self-heal). Tracker `skipped`/`expired` →
     the deferral is VOID: set the gap back to `registered`, note why, and evaluate forge criteria
     normally. Tracker `completed` → set `satisfied-by-extension` and skip. Tracker still open →
     skip; the deferral is live.

     **WHY this status exists (g-115-4457, measured 2026-08-01).** gap-028's description had carried
     an explicit DO-NOT-FILE directive naming its tracker (g-115-3767) since 2026-07-28, and a forge
     goal was filed anyway. Every suppression gate in this step reads STATUS or GOAL RECORDS; none
     reads the gap's own description — which is where that decision had to go, because no status
     value fit "resolution decided, tracker still open". The dedup probes were not wrong: they
     correctly found no duplicate, BECAUSE the prior decision was to file nothing. Absence of a forge
     goal was the EVIDENCE OF THE DECISION, and the sweep read it as evidence of starvation. A prose
     disposition that no reader reads is not a disposition — give the decision a field, or it does
     not exist.

     **The status vocabulary is DECLARED in `core/config/skill-gaps.yaml` → `gap_statuses`
     (g-115-3517, 2026-08-02).** That block is the authoritative source; the set named above is
     an inline copy kept for its explanations, which guard-426 permits only with a pointer to
     the source and a check that diffs the copy against it. Both now exist: declare a new status
     THERE first (with a writer, per guard-334, then sweep `meta/skill-gaps.yaml` for records
     that should already carry it and report the count), and `skill-gaps-validate.py` will
     refuse any gap whose status is undeclared. Until 2026-08-02 there was no declaration at all,
     which is why `satisfied-by-extension` could be coined ad hoc and go unnoticed here.

     When adding a new suppressing status anywhere, add it to ALL THREE readers, which must
     agree: HERE, the sibling filter in `aspirations-spark/SKILL.md` Phase 6.5 (same forge
     block, same defect), AND `core/scripts/coordination_merge.py`
     (`_SKILL_GAP_TERMINAL` / `_gap_status_rank`) — the cross-box merge handler. Agreement is
     no longer honor-system: `test_skill_gaps_hardening.py` pins all three against the
     declaration (`test_terminal_set_matches_declared_vocabulary`,
     `test_forge_filters_name_every_declared_suppressing_status`), so a copy left un-updated
     fails the suite instead of silently mis-classifying gaps.

     THREE, not two. Until 2026-08-01 this sentence read "the two are the only readers of gap
     status", and the spark sibling said the same thing — two copies of one unverified claim,
     which is not corroboration even though it reads like it. The merge handler was found only
     by grepping the VALUE across `core/` and `mind_api/` (g-115-4457). The consequence of
     missing it is not cosmetic: a status absent from the merge handler loses to a peer box
     still holding the old value, silently reverting the decision and re-arming the exact loop
     the new status was added to prevent. VERIFY BY GREP, not by this list — a fourth reader
     added later will be just as invisible to this sentence as the third was.
     (guard-2283: a document's claim about its own completeness is an unverified claim.
     guard-821 is the reinforcing rule: it already treats "set a status with a resolution
     note" as the way to stop re-qualification; this makes the reader honor every such
     status, not just one.)
     - **Registry cross-check (g-326-09 incident, 2026-07-16)**: before trusting `gap.status`,
       grep `world/forged-skills.yaml` for `gap_ref: {gap.id}`. If a forged skill already
       references this gap, the gap's `status: registered` is STALE (another agent forged it;
       the local meta mirror served an old copy — observed 11 days stale for gap-006 despite a
       daemon-routed read). SKIP the gap — do not file a forge goal. forged-skills.yaml is the
       authoritative cross-agent registry; skill-gaps.yaml status is per-store and can lag.
       (guard-1163 family: never act on a single possibly-stale read when the authoritative
       registry is one grep away.)
     - Read `core/config/skill-gaps.yaml` → `forge_threshold` (default: 2)
     - Read `agents/<agent>/developmental-stage.yaml` → current stage
     - IF `gap.times_encountered >= forge_threshold`
          AND `gap.estimated_value >= "medium"`
          AND developmental stage >= EXPLOIT (developing+):
       - **Live-store dedup (g-115-2284 — replaces compact-search)**: the in-context compact is
         doubly stale (context-read dedup serves an hours-old copy, and the compact renders from
         the box's local mirror). Probe the live store instead:
         - `Bash: aspirations-query.sh --goal-field origin_signal "idea:forge-ready-{gap.id}"`
         - `Bash: aspirations-query.sh --title-contains "Forge skill: {gap.procedure_name}"`
           (catches legacy datestamped origin_signal variants)
         - IF either probe returns a pending/in-progress goal: SKIP this gap — duplicate exists.
         - IF either probe ERRORS (non-zero exit or unparseable output): WARN loudly + SKIP this
           gap — suppression gates fail CLOSED (guard-487). A missed filing re-detects next
           evolve pass; a cross-box duplicate does not self-heal.
       - IF both probes returned clean-empty (`[]`):
         - Route to target aspiration (current → matching category → `/create-aspiration from-self`)
         - Build goal: title `"Forge skill: {gap.procedure_name}"`,
           skill `"/forge-skill"`, args `"skill {gap.id}"`, priority `"MEDIUM"`,
           origin_signal EXACTLY `"idea:forge-ready-{gap.id}"` — canonical form, NO datestamp
           suffix (a datestamped variant defeats the duplication-gate's Strategy-1 exact match
           against the canonical form; g-115-2284 incident g-115-2279-vs-g-307-54)
         - Add via `aspirations-add-goal.sh` (goal JSON on stdin; asp-id as arg)
         - **Post-filing read-back**: re-run the origin_signal probe above; only log
           "forge goal filed" when the goal reads back (own-cloud can silently swallow the write
           while echoing success — insight msg-20260714-213836-echo-3288). IF read-back empty:
           WARN + retry the add once, then file-or-fail loudly.
         - Log: `echo '{"date":"...","event":"forge-ready","details":"Gap {gap.id} met criteria in evolve Phase 9.2","trigger_reason":"evolve-forge-check"}' | bash core/scripts/evolution-log-append.sh`

### Skill Curation (Step 9.5 — after forge check)

Quality-based skill curation using five-dimension evaluation data.
See `core/config/conventions/skill-quality.md` for dimension definitions.

```
Read core/config/skill-gaps.yaml (quality_thresholds section)
Bash: skill-evaluate.sh underperforming --threshold {quality_thresholds.retirement_floor}
underperforming = parse JSON output

FOR EACH skill in underperforming:
    # FIELD NAME (g-115-1403): skill-evaluate.sh emits the skill name under
    # key "skill" (record["skill"]), NOT "name". Use skill.skill — skill.name
    # is None (silent schema drift, guard-359). Do NOT "correct" it back.
    IF skill.total_evaluations >= quality_thresholds.min_evaluations (5):
        Bash: world-cat.sh forged-skills.yaml
        IF skill is a forged skill (exists in world/forged-skills.yaml):
            # Check if any pending goals depend on this skill
            Bash: load-aspirations-compact.sh
            IF no pending goals use this skill:
                Log: "SKILL CURATION: Retiring {skill.skill} — quality {skill.overall} after {skill.total_evaluations} evaluations"
                Add goal: "Retire forged skill: {skill.skill}" to current/evolution aspiration
            ELSE:
                Log: "SKILL CURATION: {skill.skill} underperforming but has pending goals — creating improvement goal"
                Add goal: "Improve forged skill: {skill.skill} (quality {skill.overall})" to current aspiration
        ELSE:
            # Base skill — cannot retire. Flag for user attention.
            Log: "SKILL ALERT: Base skill {skill.skill} quality {skill.overall} — user review recommended"
            Write to agents/<agent>/session/pending-questions.yaml:
                question: "Base skill {skill.skill} has quality {skill.overall}. Should I create a better forged alternative?"
                default_action: "Monitoring — will create improvement goal if quality drops further"
                status: pending

# Quality bar tightening: if average skill quality is high, raise expectations
Bash: skill-evaluate.sh report
avg_quality = parse summary.avg_overall
IF avg_quality > 0.80 AND summary.total_skills_evaluated >= 5:
    Bash: meta-read.sh skill-quality-strategy.yaml
    IF review_threshold < 0.60:
        # DEAD-END GUARD (g-115-4938, 2026-08-04). This write is a meta-strategy
        # threshold change like any other, so it needs the SAME guard Step 0.7
        # already applies to its own writes — it did not have one, and the value
        # it hardcodes is a REGISTERED dead end. Measured this pass: de-004
        # (skill-quality-strategy.yaml / review_threshold / 0.6) carries
        # failure_pattern "Rolled back 2 times" and times_matched 5, while the
        # live file sits at review_threshold 0.5 and avg_quality reads 0.9706 —
        # so the unguarded branch fires EVERY evolve pass and had already made
        # five attempts at a twice-reverted value. The guard is the fix; do NOT
        # "fix" this by raising the literal, which only renames the dead end.
        # Reuse Step 0.7's active_dead_ends if that step ran; re-read otherwise
        # (it is SKIPPED whole when curriculum-contract-check.sh denies
        # allow_meta_edits, so the variable cannot be assumed present).
        Bash: meta-dead-ends.sh read --active
        active_dead_ends = parse result as JSON array
        dead_end_match = check_dead_ends(active_dead_ends,
                             "skill-quality-strategy.yaml", "review_threshold", 0.60)
        IF dead_end_match is not null:
            Bash: meta-dead-ends.sh increment {dead_end_match.id} times_matched
            Log: "SKILL CURATION BLOCKED: review_threshold = 0.60 hits dead end {dead_end_match.id}: {dead_end_match.failure_pattern} — bar left at {review_threshold}"
        ELSE:
            Bash: meta-set.sh skill-quality-strategy.yaml review_threshold 0.60 \
                --reason "Average quality {avg_quality} supports higher bar"
```

### Skill Discovery Audit (Step 9.5.5 — after Skill Curation)

Behavioral counterpart to per-execution Skill Curation above. Per-execution
quality scoring cannot detect the rb-314 failure mode: a skill that never
fires produces zero evaluations, so the five quality dimensions have no data
to score. This step measures absence of invocation against time-since-forge
and prior-rate-of-use, then files Investigate/Idea goals for skills past
their action window.

Strategy + thresholds: `meta/skill-discovery-strategy.yaml`.
Static authoring counterpart (description quality, XML-safety): already
enforced by verify-learning Section FSR-D — do NOT duplicate here.

Calibration caveat: a flagged skill is "zero invocations across all logged
sources," which is an UPPER BOUND on real silence — under-logging (skill
fires but neither skill-quality.yaml, co_invocation_log, nor journal.jsonl
captures the call) is indistinguishable from genuine silence. The
triage_hints emitted by the script already direct the agent to rule out
under-logging in step 1; this is by design. For utility-type wrapper skills
the under-logging is STRUCTURAL, not incidental — see the type-aware triage
branch below and `meta/skill-discovery-strategy.yaml` → `type_triage` +
`known_blind_spots` (g-115-2289).

```
Bash: skill-discovery.sh flagged --action-required-only
flagged = parse JSON output → {generated_at, count, skills: [...]}

# Read goals.target_aspiration + goals.priority from strategy. The script
# does not echo these into its output (they are filing policy, not
# measurement), so read the YAML directly.
Read meta/skill-discovery-strategy.yaml → strategy.goals

FOR EACH skill in flagged.skills:
    # FIELD NAME (g-115-1403): skill-discovery.py emits the skill name under
    # key "skill" (record["skill"]), NOT "name". Use skill.skill — skill.name
    # is None (silent schema drift, guard-359). Do NOT "correct" it back.
    # Bounded: one goal per skill at a time. The goal-selector and
    # aspirations-execute prevent duplicate execution; this guard prevents
    # duplicate FILING while a prior follow-up is still active. Resolved
    # goals do not block re-filing — if a prior investigation closed
    # without addressing the silence, we want a fresh signal.
    # PROBE THE SKILL NAME ALONE — not a keyword list, not an exact
    # origin_signal (its status suffix drifts). Both miss owners. rb-8741.
    Bash: aspirations-query.sh --title-contains "{skill.skill}"
    IF any returned goal has status in (pending, in-progress, blocked):
        Log: "DISCOVERY AUDIT: {skill.skill} already has active follow-up — skipping"
        continue

    # Type-aware triage (g-115-2289, 2026-07-16): utility-type wrapper skills
    # are exercised via direct Bash calls to their companion scripts — a usage
    # mode invisible to ALL invocation sources including the skill-invocations
    # ledger (the Skill tool never fires for a bash call). Zero counts are
    # WEAK evidence for this type; do NOT file on measurement absence alone.
    # Capability loss at the script layer is caught elsewhere (infra-health,
    # loud failures in consuming goals). Policy + full blind-spot catalog:
    # meta/skill-discovery-strategy.yaml → type_triage.utility, known_blind_spots.
    IF skill.type == "utility" AND strategy.type_triage.utility.downgrade_action_required:
        Log: "DISCOVERY AUDIT: {skill.skill} type=utility — flag downgraded to advisory (wrapper-mediated blind spot, g-115-2289); no goal filed on measurement absence. Verify companion-script health via infra-health / consuming goals if in doubt."
        continue

    # Map status → primitive + title. Primitives are encoded by title prefix
    # ("Investigate:" / "Idea:") per CLAUDE.md "Cognitive Primitives".
    IF skill.status == "silently_undertriggering":
        title = "Investigate: forged skill " + skill.skill + " silent for " + skill.days_since_forge + "d (0 invocations logged)"
    ELIF skill.status == "cold_after_use":
        title = "Idea: review forged skill " + skill.skill + " — last invoked " + skill.days_since_last_invocation + "d ago"
    ELIF skill.status == "declining":
        # decline_signal.ratio is a fraction (0.0-1.0); multiply for display.
        decline_pct = round(skill.decline_signal.ratio * 100)
        title = "Idea: investigate declining usage of " + skill.skill + " (rate " + decline_pct + "% of prior window)"
    ELSE:
        continue

    priority = strategy.goals.priority[skill.status]
    target_asp = strategy.goals.target_aspiration  # default asp-115

    # Build description with the triage hints emitted by the script. The
    # hints already cross-reference FSR-D, gap analysis, etc — copying them
    # verbatim avoids re-deriving the triage path inside this pseudocode.
    description = (
        "Forged skill: " + skill.skill + "\n"
        "Forged: " + skill.forged_date + " (" + skill.days_since_forge + " days ago)\n"
        "Total invocations: " + skill.total_invocations + " (sources: " + json(skill.invocation_sources) + ")\n"
        "Confidence: " + skill.confidence + "\n"
        "Last invocation: " + (skill.last_invocation_date or "never") + "\n"
        "Invocations/week: " + skill.invocations_per_week + "\n"
        "\n"
        "Triage:\n"
        + skill.triage_hints + "\n"
        "Source: aspirations-evolve Step 9.5.5 (Skill Discovery Audit)\n"
        "Strategy: meta/skill-discovery-strategy.yaml\n"
    )

    # aspirations-add-goal.sh reads goal JSON from stdin and takes asp_id
    # positional. NO --title / --priority / --category flags — those are
    # JSON fields in the stdin payload (see CLAUDE.md "Cognitive Primitives"
    # and the script's --schema output for the full field list).
    goal_json = {
        "title": title,
        "description": description,
        "priority": priority,
        "category": "framework-meta",
        "participants": ["agent"],
        "origin_signal": "skill-discovery-audit:" + skill.skill + ":" + skill.status,
    }
    Bash: echo '<goal_json>' | aspirations-add-goal.sh --source world {target_asp}

    Log: "DISCOVERY AUDIT: filed goal for " + skill.skill + " (" + skill.status + ")"
    echo '{"date":"<today>","event":"discovery-audit-fire","details":"discovery audit filed goal for {skill.skill} (status: {skill.status})","trigger_reason":"evolve-discovery-audit"}' \
        | bash core/scripts/evolution-log-append.sh
```

### Pattern Signature Calibration (during evolve or weekly)
# rb-245 pre-read gate: verify schema before aggregating. If the gate fails,
# SKIP this calibration block (other sub-phases continue). Do NOT --override:
# fix the field paths below instead.
`source core/scripts/_paths.sh && bash core/scripts/audit-schema-gate.sh \
        --jsonl-path "$WORLD_DIR/pattern-signatures.jsonl" \
        --field-names "outcome_stats.accuracy,outcome_stats.total,outcome_stats.confirmed"`
(`utilization.retrieval_count` REMOVED 2026-07-30, g-029-82: pattern-signature
records carry NO `utilization` block, so including it made this gate block on
every run — measured `would_block:true, fields_missing:[utilization.retrieval_count]`
across all 19 records, which silently skipped the whole calibration block. The
three fields above were verified present.)

`bash core/scripts/pattern-signatures-read.sh --active` → get active patterns as JSON. For each pattern:
1. Check calibration rules from the `calibration_protocol` section (live schema):
   - **`last_matched is not None` AND** `outcome_stats.accuracy < 0.80 AND outcome_stats.total >= 3` → flag for condition tightening (was false_positives/times_triggered > 0.20) <!-- DRIFT-EXEMPT: rename-documentation -->
   - **`last_matched is not None` AND** `outcome_stats.total == 0 AND sessions_since_creation > 10` → flag as stale, consider loosening
   - `outcome_stats.confirmed >= 5 AND outcome_stats.accuracy >= 0.90` → graduate to `validated`, increase weight

   <!-- The `last_matched is not None` gate on rules 1 and 2 (g-115-3516 SITE 2,
   applied 2026-08-10 by echo, hostname cc-03, uname -r 6.8.0-136-generic).
   Rule 4 below already carries the g-115-1441 caveat that a count-like field
   reading low means MISSING TRACKING, not pattern failure; rules 1 and 2 read
   the SAME counters with no equivalent gate. That asymmetry is not theoretical
   — a calibration pass flagged sig-49 for TIGHTENING when it had never been
   measured at all. `last_matched` is the recorder's own unconditional stamp, so
   it is a proof-of-execution discriminator already present in the record (no new
   plumbing). Measured this run: non-null on 50 of 94 records. guard-1641 is the
   general class. -->

   <!-- GRADUATE (rule 3) IS EXPOSED, AND THE PROPOSED FIX IS REFUTED — MEASURED,
   DO NOT BUILD IT. A seeder setting BOTH total=5 and confirmed=5 auto-satisfies
   rule 3 and reaches validation_status=validated with ZERO independent tests.
   g-115-3516's appendix proposed gating GRADUATE on `outcome_stats.total >
   sample_size` (a seeded record has total == sample_size by construction) and
   explicitly asked that the sample_size population be surveyed BEFORE wiring it,
   because "a graduate-gate that silently passes when sample_size is absent would
   be worse than none." That survey is now done and it comes back negative on
   BOTH halves the caveat named:
     * PRESENT on only 17 of 94 records (18%) — absent on 82% of the corpus.
     * NOT consistently meant as formation-instance-count — sig-12 carries
       sample_size=81 against outcome_stats.total=3.
   So `total > sample_size` cannot be the graduate discriminator. Fail-closed on
   an absent field (guard-487) would refuse to graduate 82% of the store; fail-open
   restores the exact hole. The durable fix is SITE 1 (stop `add` from seeding
   counters at all), after which total>0 is itself proof of recording — see the
   guard-2475 constraint recorded on g-115-3516 before attempting it. -->

   <!-- ⚠ RULE 4's STATED REASON IS FALSIFIED — ITS CONCLUSION STILL STANDS.
   Re-measured 2026-08-10 (echo, cc-03, `--all`): the strikethrough below says
   "Pattern-signature records have no `utilization` block at all" and enumerates a
   field list without it. That is no longer true. 86 of 94 records carry a
   `utilization` block and ALL 86 carry `retrieval_count` (keys: retrieval_count,
   times_matched, last_retrieved; values to 386); 68 of 73 ACTIVE records have it.
   The record count in that note is also stale — 19 then, 94 now (73 active).
   The rule-4 predicate as written would fire on 22 active records TODAY.
   DO NOT read that as licence to un-retire the rule. Its CONCLUSION — never prune
   on `total < 2` unless outcome-recording is WIRED for that pattern — is unchanged
   and still load-bearing, because a low total still reflects missing tracking
   rather than pattern failure. What changed is only the reason: the rule is no
   longer *unmeasurable*, it is *unsafe*. That distinction is why this note exists
   — the next reader who checks the schema will find `utilization` present,
   conclude the strikethrough is stale, and un-retire a rule that would prune 22
   patterns on missing tracking. A right conclusion resting on a falsified premise
   fails in exactly that direction (guard-1922, guard-1641). -->

   <!-- Not applied here, deliberately: SITE 1 (refuse seeded counters in `add`).
   The live path is the daemon `/v1/store/append`, NOT the cited
   `core/scripts/pattern-signatures.py:155-161` (guard-2323), and `set_field`
   calls the SAME `spec.validate(ctx, rec)` over the WHOLE record — so adding the
   refusal to the shared spec freezes every record that already has total>0:
   measured 55 of 94 (58.5%), which would break the validation_status transitions
   this very block performs. That is guard-2475 exactly. It needs a create-only
   validation hook, which does not exist today. -->

   - ~~`utilization.retrieval_count >= 10 AND outcome_stats.total < 2`~~ → **INERT, DO NOT USE (g-029-82, 2026-07-30).** Pattern-signature records have no `utilization` block at all: the 19 live records carry exactly `category, conditions, confused_with, created, description, expected_outcome, id, last_matched, name, outcome_stats, retrieval_cues, separation_markers, status, tags, validation_status`. The rule appears to have conflated TREE-NODE schema (tree nodes DO carry `retrieval_count` / `utility_ratio`) with pattern-signature schema, so it could never fire. There is no retrieval counter on these records, so the retrieval-VOLUME half is unmeasurable; the never-matched half is already covered by rule 2 above (`outcome_stats.total == 0 AND sessions_since_creation > 10`), and `last_matched` is the nearest real field. MUST NOT be used to justify pruning until a retrieval counter exists. The retained guidance below remains correct in spirit: **PRUNE ONLY IF outcome-recording is WIRED** for this pattern (it is a meta-pattern recorded via hypothesis resolution per guard-575, OR a `sig-NNN-auto-detect.py` auto-probe exists). If NO recording mechanism exists, `total < 2` reflects MISSING TRACKING, not pattern failure → RETAIN (a high `retrieval_count` is positive reference-value signal — LLMs keep finding it relevant). Audit 2026-06-14 (g-115-1441): 14/19 active patterns are unwired and ZERO auto-probe scripts exist, so this rule false-flags valuable reference patterns unless the wiring check gates it.
2. For flagged patterns: propose specific condition changes
3. Update `validation_status` based on current stats
4. Log changes to pattern calibration via `echo '<json>' | bash core/scripts/evolution-log-append.sh`

### Gate Retirement Evaluation (during evolve or weekly)
Phase 5 of the gate audit/retirement plan — gate-side parallel of Pattern
Signature Calibration above. Reads `meta/gate-firings.jsonl` +
`core/config/gates.yaml` and produces per-gate recommendations.

`bash core/scripts/gate-retirement-eval.sh --output json` → JSON with
per-gate recommendation in {retire | tighten | widen | investigate |
inert_candidate | keep | insufficient_data | uninstrumented} plus the raw
counts that justify it.

For each `recommendation` in {retire, tighten, widen, investigate, inert_candidate}:
1. Append the full record to `meta/gate-eval-recommendations.jsonl`
   (append-only journal — preserves the recommendation lineage so trends
   over evolutions are visible). Append via:
   `echo '<json-record>' >> "$META_DIR/gate-eval-recommendations.jsonl"`
2. For `retire`: file an Investigate goal naming the gate. Do NOT
   auto-delete — gate code paths often have downstream consumers; review
   first. Goal title: `"Investigate: gate-retirement-eval recommends retiring {gate_id}"`.
3. For `tighten`: file an Idea goal proposing tighter trigger patterns.
   Include the override-rate evidence in the description.
4. For `widen`: file an Idea goal proposing additional trigger patterns.
   Quote the gate's `fn_description` from gates.yaml as a hint about what's
   escaping.
5. For `investigate`: READ the record's `reason` field before choosing a
   priority — the evaluator emits `investigate` for at least THREE situations
   that need OPPOSITE responses, and collapsing them into the urgent reading
   is wrong (g-029-82):
   - `fail_open` count > 0 (gate-retirement-eval.py ~L63) → the gate MAY have
     a code bug. **Do NOT file HIGH on this counter alone — verify the gate
     actually threw. See the third bullet.**
   - an all-noop gate whose `fn_cost` outranks `fp_cost` → its own `reason`
     says this is "typically a WORKING preventive guard whose rare guarded
     condition did not occur in-window, NOT a dead gate." File a MEDIUM Idea
     goal asking whether the gate's SCOPE is right. Do NOT report a healthy
     guard as a code bug.
   - **`fail_open` that is really a correct DECLINE mislabelled by the
     decision taxonomy** — the gate refused bad input exactly as designed and
     logged that refusal as `fail_open`. This is a taxonomy defect one layer
     up from the gate code, and the first bullet's wording sends you at the
     code. It has now manufactured a phantom investigation TWICE on the same
     gate: a peer deployment filed HIGH g-001-292, and g-115-4180 (pending)
     reproduced the premise as INVERTED — `printf '' | py -3
     core/scripts/store_dupe_warn.py --store guardrails` returns rc=0 and logs
     `{"decision":"fail_open","extra":{"reason":"JSONDecodeError"}}`,
     byte-identical to the records under investigation. File NOTHING for a
     gate whose taxonomy defect is already tracked.

   **THE DISCRIMINATOR, and it is one read.** Before filing on `fail_open`,
   open the firing records in `meta/gate-firings.jsonl` and look for evidence
   the gate THREW — not the recommendation's own `reason` text. Read
   `extra.reason`: `JSONDecodeError` on empty stdin is a DECLINE, not a crash.
   Measured 2026-08-21 (zeta, cc-02, 6.8.0-137-generic): **120 of 120**
   `store-dupe-warn` fail_open records carry `extra.reason: JSONDecodeError`.
   `gate_error` — the field the emitted reason tells you to inspect, ABSENT
   from all 119 records on 2026-08-11 (echo, cc-03) — now exists on exactly
   ONE, and it CORROBORATES the decline (`Expecting value: line 1 column 1`,
   i.e. empty stdin). Its arrival does not make this marker stale; it supplies
   the evidence the marker had to infer. (guard-1675: an absent field is not
   a zero. rb-245: probe the schema before concluding from a count.)
6. For `inert_candidate`: file an Investigate goal to verify the gate is
   truly inert and route it to retirement or telemetry re-enable. The
   evaluator emits this when a prior retire/tighten/widen recommendation
   rests on ZERO recent-window firings AND the gate's implementation was
   modified after its last firing (see `evidence.inert_prior_recommendation`,
   `evidence.last_firing_ts`, `evidence.gate_impl_modified_ts`) — the stale
   recommendation describes a gate that no longer exists in that form. Do
   NOT file the prior recommendation's goal type; quote the evidence fields
   in the description. Goal title:
   `"Investigate: gate {gate_id} appears inert — stale {prior_rec} recommendation"`.
   (Canonical incident: stale-read-gate re-spawned duplicate tighten goals
   g-115-1796 → g-115-2106 across evolutions after its firing lane went quiet.)

Bounded: only one goal per gate per evolution pass. If a goal already
exists for `{gate_id}` from a prior evolution, skip — wait for that goal
to resolve before re-flagging. Check via grep on the live aspirations file.

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

### Maintenance Cadence Write (before return)

Before returning to the caller, write the wall-clock timestamp to the maintenance
cadence slot. The aspirations loop Phase 8.8 reads this to decide whether a
time-based evolution fire is due on subsequent iterations. Any evolve invocation
(cadence-triggered, performance-triggered, all-blocked idle, maintenance tick)
updates the clock — preventing double-fires.

<!-- RESOLVED 2026-04-20 (asp-246 g-246-02): last_evolution_at_time now has a
     reader in core/config/aspirations-loop-digest.md Phase 8.8 MAINTENANCE TICK
     block (see the `wm-read.sh last_evolution_at_time` call). Writer + reader
     are now paired — this line is the single source of truth for evolution
     cadence timestamps (rb-254 single-writer principle). -->
```
echo '"'"$(date +%Y-%m-%dT%H:%M:%S)"'"' | bash core/scripts/wm-set.sh last_evolution_at_time
# g-115-1561: bash-owned single-writer for the evolution ACCUMULATORS
# (loop_state.evolutions + loop_state.last_evolution_at). Co-located with the
# cadence-TIMESTAMP write above so exactly ONE place records "an evolution
# fired", on EVERY path that reaches aspirations-evolve (Phase 8.8 cadence,
# Phase 9 triggers, all-blocked B3 idle). Replaces the retired in-context
# `evolutions_this_session += 1` (which lived ONLY in all-blocked B3 — the main
# Phase 8.8/9 path never incremented it) plus the learning-gate / B3 overlays
# that were discarded at LOOP_CONTINUE → evolutions frozen at 0 → the
# per-session evolution cap was silently defeated. Fail-open (exits 0 on any
# error); --evolution-fired needs no --outcome.
py -3 core/scripts/loop-state-bump-counters.py --evolution-fired
```

### Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.

### Chaining

- **Called by**: `/aspirations` orchestrator (Phase 9 evolution triggers)
- **Calls**: `aspirations-complete.sh --source`, `aspirations-retire.sh --source`, `aspirations-add-goal.sh --source`, `aspirations-update.sh`, `aspirations-update-goal.sh`, `agent-aspirations-add-goal.sh`, `aspirations-meta-update.sh --source`, `wm-read.sh`, `wm-clear.sh`, `/create-aspiration`, `/forge-skill check`, `/curriculum-gates`
- **Reads**: Working memory (`portfolio_health_signal` — written by strategic scan S3c)
- **Source routing**: All `aspirations-*.sh` calls receive `--source {asp.source}` from the aspiration's compact data

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is `wm-set.sh last_evolution_at_time` or the last
`aspirations-*.sh` call. Never end with a text summary of evolution decisions.
