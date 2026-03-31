---
name: aspirations-consolidate
description: "Session-End Consolidation — hippocampal sleep replay, encoding, debt sweep, tree rebalancing, experience-to-skill mining, skill health, archive, user recap, handoff, restart"
user-invocable: false
parent-skill: aspirations
triggers:
  - "Session-End Consolidation Pass"
conventions: [aspirations, pipeline, experience, journal, handoff-working-memory, session-state, tree-retrieval, goal-schemas]
minimum_mode: autonomous
---

# Session-End Consolidation Pass

Run when the aspirations loop stops (any stop condition). This is the hippocampal "sleep replay" that compresses session observations into long-term memory. Covers micro-hypothesis sweep, encoding queue processing, dynamic consolidation budget, overflow queue management, encoding competition, tree encoding, knowledge debt sweep, snapshot invalidation, experience archive maintenance, journal logging, working memory archival, tree rebalancing, skill health report, aspiration archive sweep, user goal recap, continuation handoff, and restart loop cycle.

Note: Consolidation MUST NOT call session-state-set.sh.
Only /start and /stop may change agent-state.

Note: minimum_mode is `autonomous` but /stop invokes this AFTER setting state to IDLE
and BEFORE setting mode to reader. The mode is still `autonomous` at invocation time.
If /stop's step ordering changes, this check will break. See /stop step 4 comment.

## Parameters

- `stop_mode` (boolean, default: false) — When true, skip Steps 6 (tree rebalancing),
  7 (skill gap review), 7.5 (experience-to-skill mining), 8 (skill health report),
  8.7 (user goal recap), and 10 (restart).
  Used by /stop to run proper consolidation without restarting the loop.

---

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

```
# Consolidation — run before session exit
Bash: wm-read.sh --json
Read core/config/memory-pipeline.yaml (replay_priority_order)

0. Micro-Hypothesis Sweep:
   Bash: wm-read.sh micro_hypotheses --json
   IF micro_hypotheses is non-empty:
     batch_micro_result = invoke /reflect --batch-micro
     # This computes batch stats, promotes surprises to encoding_queue,
     # updates pipeline micro_hypothesis_stats,
     # updates developmental-stage resolved count,
     # and writes journal batch summary.
     # Promoted micro-surprises are now in encoding_queue for step 1.

     # Actionable work from batch patterns
     IF batch_micro_result.actionable_discoveries is non-empty:
       FOR EACH discovery in actionable_discoveries:
         # Route using same logic as sq-013 handler step 2
         Determine target aspiration (current → other active → /create-aspiration)
         Build goal object with discovery_type: "micro_batch"
         Add goal to target aspiration via aspirations-update.sh
         Log: evolution-log-append.sh with event "micro-batch-discovery"

0.5. Unreflected Hypothesis Sweep:
   Bash: pipeline-read.sh --unreflected
   IF unreflected hypotheses exist:
     invoke /review-hypotheses --learn
     # This reflects on each unreflected hypothesis, sets reflected: true,
     # and pushes encoding items into encoding_queue for Step 1.
     Output: "▸ CONSOLIDATION: reflected on {count} unreflected hypotheses"

1. Bash: wm-read.sh encoding_queue --json
   Sort encoding_queue by replay_priority_order:
   - violations first, then context_gap_corrections, high_surprise, high_outcome_impact, goal_completions, routine
   - Context gap corrections: hypotheses where /reflect Step 7.7 found missed context that contributed to a correction
   - Within each priority class, sort by encoding_score descending
```

### Dynamic Consolidation Budget
Read `core/config/memory-pipeline.yaml` → `consolidation_budget` section.
Calculate: budget = min(15, max(5, 10 + violations_this_session*2 + new_domains_this_session*3 + surprise_gt7_count))
- violations_this_session: count of expectation violations detected
- new_domains_this_session: count of categories first touched this session
- surprise_gt7_count: count of items with surprise rating > 7
Use this budget instead of fixed top-10 for consolidation item selection.

### Overflow Queue Management
After selecting the top items for consolidation (based on dynamic budget):
1. Items NOT selected but with encoding score >= 0.25: write to `<agent>/session/overflow-queue.yaml`
   - Set `original_score`, `current_score` (same initially), `deferred_count: 1`, `first_seen`, `session_first_seen`, `category`, `source_goal`
2. Before consolidation, read existing `<agent>/session/overflow-queue.yaml`:
   - **IF file does not exist**: log "No overflow queue from prior sessions" and continue (no overflow items to merge)
   - IF file exists:
     - Items re-encountered this session: boost `current_score` by +0.15, reset `deferred_count`
     - Items with `deferred_count >= 3`: decay `current_score` by 0.8x
     - Items with `current_score < 0.25`: remove from queue
     - Merge overflow items into this session's consolidation candidates (they compete with new items)
3. After consolidation: update overflow queue with items that didn't make the cut
4. Max queue size: 20 items (oldest/lowest-score items drop off)

#### Encoding Competition (Top-K)

After collecting all encoding candidates (encoding_queue + qualifying overflow items above threshold):

```
# Merge all candidates
all_candidates = encoding_queue + [item for item in overflow if item.encoding_score >= 0.40]

# Rank uniformly
# Primary sort: replay_priority_order (violations > high_surprise > pattern_forming > reinforcement > routine)
# Secondary sort: encoding_score descending
all_candidates.sort(by=priority_class, then_by=encoding_score, descending=True)

# Budget cap (existing formula, unchanged)
budget = min(15, max(5, 10 + violations*2 + new_domains*3 + surprise_gt7_count))

# Select top-K — threshold is quality floor, budget is ceiling
selected = all_candidates[:budget]
deferred = all_candidates[budget:]  # return to overflow queue for next session
```

The encoding threshold (>= 0.40) remains the quality floor. The budget is the ceiling. When more candidates pass the threshold than the budget allows, only the top-ranked candidates encode. Deferred items return to the overflow queue and compete again next session.

```
2. For top items (up to dynamic budget) in encoding_queue:
   a. Determine target leaf node:
      node=$(bash core/scripts/tree-find-node.sh --text "{item.target_article}" --leaf-only --top 1)
      # Returns: {key, score, file, depth, summary, node_type}
   b. EXTRACT PRECISION from encoding queue item:
      IF item has precision_manifest AND it is non-empty:
          precision_data = item.precision_manifest
      ELIF item has source_experience:
          Bash: experience-read.sh --id {item.source_experience}
          Read content .md for full-fidelity context; extract precision items
      ELSE:
          Scan observation text for exact values; build precision manifest
      See core/config/conventions/precision-encoding.md for schema and extraction heuristics.
   c. IF precision_data non-empty:
        Append to node's "## Verified Values" section (create if missing):
          For each item: - **{label}**: `{value}` {unit} — {context}
   d. Append compressed narrative (1-3 sentences) to "Key Takeaways" section
   d.5. IF encoding item contains a behavioral rule (IF X THEN Y pattern):
        Append to "## Decision Rules" section (create if missing).
        Format: `- IF {observable condition} THEN {specific action} — source: {item.source_goal}`
        Same criteria as state-update Step 8e: concrete, testable, actionable, no duplicates.
   e. PRECISION AUDIT: Verify each precision item appears in Verified Values
   f. bash core/scripts/tree-update.sh --set <node.key> last_updated <today>
   g. If leaf node changed significantly:
      - Update the node via batch:
        echo '{"operations": [
          {"op": "set", "key": "<node.key>", "field": "confidence", "value": <new-value>},
          {"op": "set", "key": "<node.key>", "field": "capability_level", "value": "<new-value>"}
        ]}' | bash core/scripts/tree-update.sh --batch
      - Propagate changes up parent chain:
        result=$(bash core/scripts/tree-propagate.sh <node.key>)
        # Returns: {source_node, ancestors_updated: [...], capability_changes: [...]}
        IF result.capability_changes is non-empty:
          For each changed ancestor: Read ancestor.file (.md)
          Append 1-sentence compressed summary of the new insight to "Key Insights" section
          Set last_update_trigger: {type: "consolidation", source: "session-end encoding", session: N}
          Update .md body text (capability map table)
        If root-level domain summary changed:
          bash core/scripts/tree-update.sh --set root summary "<updated>"
        - Update <agent>/developmental-stage.yaml highest_capability if exceeded

2.25. Knowledge Debt Sweep:
   Bash: wm-read.sh knowledge_debt --json
   IF items exist:
       Sort by priority (HIGH first), then by age (oldest first)
       For each debt:
           Read target node .md file
           IF node was updated AFTER debt was created → mark resolved, skip
           IF priority is HIGH or sessions_deferred >= 2:
               Reconcile now: read node, update stale content, set last_update_trigger:
                   {type: "debt-reconciliation", source: debt.source_goal, session: N}
               Propagate up parent chain if significant
               Log: "KNOWLEDGE DEBT RESOLVED: {node_key} — {reason}"
           ELSE:
               Carry forward to handoff (increment sessions_deferred)
       Report: "Knowledge debts: {resolved} resolved, {carried} carried forward"

2.6. Experience Archive Maintenance + Encoding Weight Adjustment:
   # Sweep stale experiences to archive
   Bash: experience-archive.sh

   # Encoding weight adjustment based on experience utility data
   # MANDATORY: always attempt this step, even if encoding queue was empty
   Bash: experience-read.sh --meta → get by_type, by_category stats
   IF script errors or returns empty: log "No experience metadata available" and continue
   IF not enough data (total_live + total_archived < 10):
       Log: "Encoding weight adjustment: insufficient data ({total} experiences, need >= 10)"
   ELSE (enough data):
       Read core/config/memory-pipeline.yaml → encoding_weight_adaptation section
       Read world/memory-pipeline.yaml → current weight_performance_log
       IF world/memory-pipeline.yaml does not exist: log "No weight performance log yet — skipping adjustment"
       ELSE:
           Compare: for experiences with high utility_ratio (>0.7), what encoding weights
           were used when those observations were originally encoded?
           For experiences with low utility_ratio (<0.3), what weights were used?
           Adjust encoding_gate weights ±adjustment_per_session (0.05) toward weights
           that produced high-utility encodings, bounded by min/max from config.
           Log adjustment to weight_performance_log in world/memory-pipeline.yaml

2.7. Conclusion Quality Sweep:
   Bash: wm-read.sh conclusions --json
   IF conclusions is non-empty:
       total = len(conclusions)
       negative = count(c for c in conclusions if c.type == "negative")
       correct = count(c for c in conclusions if c.outcome == "correct")
       wrong = count(c for c in conclusions if c.outcome == "wrong")
       pending = count(c for c in conclusions if c.outcome is null)
       avg_signals = mean(sum(e.weight for e in c.evidence) for c in conclusions)
       # Extract lessons from wrong conclusions — encode INLINE (not queued,
       # because the main encoding loop in Step 2 has already finished and
       # wm-reset in Step 5 would discard queued items).
       FOR EACH conclusion WHERE outcome == "wrong":
           IF not already captured as guardrail or reasoning bank entry:
               Log: "JUDGMENT LESSON: concluded '{conclusion.conclusion}' but was wrong — {conclusion.outcome_source}"
               # Encode directly to tree: find the relevant node, append to Key Insights
               node=$(bash core/scripts/tree-find-node.sh --text "{conclusion category or related domain}" --leaf-only --top 1)
               IF node found:
                   Append to node's Key Insights: "Judgment correction: {conclusion.conclusion} was wrong — {outcome_source}"
                   bash core/scripts/tree-update.sh --set <node.key> last_updated <today>
       Log summary: "Judgment quality: {total} conclusions ({negative} negative), {correct} correct, {wrong} wrong, {pending} pending. Avg signals: {avg_signals:.1f}"

2.8. Insight Consolidation:
   Bash: insights-read.sh --count
   IF count > 0:
     insights_json = Bash: insights-read.sh  # returns unprocessed as JSON array
     encoded_count = 0
     buffered_count = 0
     FOR EACH insight in insights_json:
       node = bash core/scripts/tree-find-node.sh --text "{insight.content}" --leaf-only --top 1
       IF node found AND node.score > 0.3:
         Read node file
         Append compressed insight (1-2 sentences) to "Key Insights" section
         bash core/scripts/tree-update.sh --set <node.key> last_updated <today>
         Log: "INSIGHT ENCODED: {insight.id} → {node.key}"
         encoded_count += 1
       ELSE:
         echo '{"observation": "{insight.content}", "source": "insight-capture", "insight_id": "{insight.id}"}' | bash core/scripts/wm-append.sh sensory_buffer
         Log: "INSIGHT BUFFERED: {insight.id} — no matching tree node"
         buffered_count += 1
     Bash: insights-read.sh --mark-processed
     Output: "▸ CONSOLIDATION: {encoded_count} insights → tree, {buffered_count} buffered"
   ELSE:
     Log: "No unprocessed insights"

3. **MANDATORY** — run even if all earlier steps had empty data:
   Bash: wm-read.sh sensory_buffer --json
   Log consolidation to journal (use this EXACT format, with zeros for empty fields):
   "## Consolidation — {date}
   Observations processed: {total_sensory_buffer}
   Encoded to long-term: {encoded_count}
   Discarded: {discarded_count}
   Flagged for review: {review_count}
   Context gaps detected: {context_gap_count} (hypotheses where relevant context existed but wasn't loaded)
   Judgment quality: {total_conclusions} conclusions, {wrong_count} wrong
   Articles updated: {list}"

4. **MANDATORY** — must run BEFORE wm-reset (Step 5) to preserve state:
   Bash: wm-read.sh --json
   Archive working memory to journal entry (summary only).
   This captures any remaining WM state before it is destroyed by reset.
5. Bash: wm-reset.sh

6. Tree Rebalancing (skip in stop_mode):
   IF stop_mode != true:
     Invoke /tree maintain (run all checks: DECOMPOSE, REDISTRIBUTE, DISTILL, SPLIT, SPROUT, MERGE, PRUNE, RETIRE)
     All 8 ops must be listed — DECOMPOSE grows tree depth, DISTILL concentrates low-utility nodes, RETIRE removes dead ones.
     Report any structural changes to journal

7. Skill Gap Review (skip in stop_mode):
   IF stop_mode != true:
     Read meta/skill-gaps.yaml
     Report: new gaps registered, gaps meeting forge threshold, dismissed gaps
     Highlight any gaps ready for "/forge-skill skill <gap-id>"

7.5. Experience-to-Skill Mining (skip in stop_mode):
   IF stop_mode != true:
     # Mine experience records for repeated procedures that should be skills
     Bash: experience-read.sh --type goal_execution --recent 30 --summary
     Read meta/skill-gaps.yaml
     Read core/config/skill-gaps.yaml (experience_mining config)

     Group experience records by category + skill.
     FOR EACH cluster of 3+ successful executions sharing procedural patterns:
       IF no existing gap in meta/skill-gaps.yaml covers this procedure:
         Register new gap in meta/skill-gaps.yaml:
           id: gap-{next_id}
           status: registered
           times_encountered: {cluster_size}
           procedure_name: "{common procedure description}"
           estimated_value: "medium"
           source: "experience-mining"
           evidence_experiences: [list of experience IDs in cluster]
         Log: "EXPERIENCE MINING: registered gap {gap.id} from {cluster_size} similar executions in {category}"
       ELIF existing gap covers this AND gap.source != "experience-mining":
         Increment gap.times_encountered by cluster_size - 1
         Log: "EXPERIENCE MINING: strengthened existing gap {gap.id} with {cluster_size} experience records"
     # Cap: max 3 new gaps per mining pass (experience_mining.max_gaps_per_scan)
     Report: "Experience mining: {N} categories scanned, {M} new gaps registered, {K} gaps strengthened"

8. Skill Health Report (skip in stop_mode):
   IF stop_mode != true:
     Read .claude/skills/_tree.yaml
     # Quality-enriched report using skill analytics
     Bash: skill-evaluate.sh report
     Bash: skill-relations.sh discover
     Bash: skill-analytics.sh recommendations
     Report:
       Active skills: {count} (core: {N}, forged: {N})
       Gaps registered: {N} (ready to forge: {N})
       Skills forged this session: {list}
       Skills underperforming: {list from skill-evaluate report}
       Quality summary: avg={avg_overall}, min={min_overall}
       Relation discoveries: {proposed new relations from co-invocation patterns}
       Recommendations: {forge/retire/improve suggestions}

8.5. Aspiration Archive Sweep:
   Bash: aspirations-archive.sh  (sweep completed/retired aspirations to archive)

8.6. Curriculum Gate Evaluation:
   invoke /curriculum-gates
   # Evaluates graduation gates for the current curriculum stage.
   # If all gates pass: promotes to next stage, logs promotion.
   # If not all pass: reports gate status (informational only).
   # If curriculum not configured: skips silently.
   # Include curriculum_stage in handoff.yaml (Step 9):
   #   curriculum_stage: {current_stage}
   #   curriculum_gates_passed: {N}/{total}

8.65. **Meta-Strategy Session Review**:
   Read meta/meta.yaml
   IF meta/meta.yaml does not exist:
       Log: "Meta-strategy review: meta/meta.yaml not initialized — skipping"
       Continue to next step
   Read meta/improvement-velocity.yaml
   IF meta/improvement-velocity.yaml does not exist:
       Log: "Meta-strategy review: improvement-velocity.yaml not initialized — skipping"
       Continue to next step

   # Compute session-level metrics
   session_entries = filter improvement-velocity entries for this session's goal_ids
   session_imp_k = mean(session_entries.learning_value) if non-empty else 0.0

   # Compare to overall average
   overall_imp_k = meta.yaml.overall_imp_k
   delta = session_imp_k - overall_imp_k
   IF delta > 0.1:
       Log: "META SESSION: improvement velocity UP by {delta:.2f}"
   ELIF delta < -0.1:
       Log: "META SESSION: improvement velocity DOWN by {delta:.2f}"

   # Update meta.yaml rolling averages
   Edit meta/meta.yaml:
       overall_imp_k: recomputed rolling average
       last_session_imp_k: session_imp_k
       sessions_evaluated: N + 1

8.7. User Goal Recap (skip in stop_mode):
   IF stop_mode != true:
     Bash: load-aspirations-compact.sh → IF path returned: Read it
     (compact aspirations now in context for user goal recap)
     Filter goals where participants contains "user" and status != "completed"

     IF any user goals exist:
       Output visible recap:
       "═══ USER ACTION ITEMS ══════════════════════
       {N} goals waiting for your input:
       {for each goal}
       - {goal.id}: {goal.title}
         {goal.description (first line)}
       ═══════════════════════════════════════════════"

     Store user goal count for handoff (step 9)

9. Write Continuation Handoff:
   Bash: goal-selector.sh → get top-ranked goal for next session
   Read decisions_locked from current session context (if any)
   Write <agent>/session/handoff.yaml with:
     session_number: {session_count}
     timestamp: "{ISO 8601 now}"
     last_goal_completed: "{last goal id from this session}"
     goals_in_progress: [{list of in-progress goal IDs}]
     hypotheses_pending: {count from Bash: pipeline-read.sh --counts → active}
     next_focus: "{recommendation based on session results}"
     first_action:
       goal_id: "{top-ranked goal from goal-selector.sh}"
       score: {score from goal-selector.sh output}
       effort_level: "{estimated effort for this goal}"
       reason: "{why this goal is top priority for next session}"
     decisions_locked:
       # Carry forward unexpired decisions from previous handoff
       # Add any new strategic decisions made this session
       # Expire entries where current_session - made_session > 3
       # CLASSIFY each decision:
       #   kind: "strategy" if about approach/priority/sequencing
       #   kind: "world_claim" if about infrastructure/availability/external state
       #   For world_claims: evidence_strength "weak"|"moderate"|"strong"
       #   based on how the conclusion was reached (single error = weak)
       - decision: "{decision text}"
         made_session: {session_number}
         reason: "{rationale}"
         kind: "{strategy|world_claim}"
         evidence_strength: "{weak|moderate|strong}"  # world_claim only
     session_summary:
       goals_completed: {count of goals completed this session}
       goals_failed: {count of goals that failed this session}
       key_outcomes:
         - "{notable outcome 1}"
         - "{notable outcome 2}"
     known_blockers_active:
       # Use blocker data from Step 4 WM archive (WM was reset in Step 5)
       # Carry forward unresolved blockers where resolution is null
       - blocker_id: "{id}"
         reason: "{reason}"
         affected_skills: ["{skills}"]
         detected_session: {N}
     knowledge_debts_pending:
       # Use debt data carried forward from Step 2.25 (WM was reset in Step 5)
       - node_key: "{node-key}"
         reason: "{why this node needs updating}"
         source_goal: "{goal-id}"
         priority: "{HIGH/MEDIUM}"
         created: "{ISO date}"
         sessions_deferred: {N}
     user_goals_pending:
       # From Step 8.7, or from aspirations compact data if 8.7 was skipped (stop_mode)
       count: {N}
       goals:
         - id: "{goal-id}"
           title: "{goal title}"
     meta_state:
       improvement_velocity_trend: "{improving|stable|declining}"
       # Read active experiments for handoff
       Bash: meta-experiment.sh list --active → active_exp
       active_variant: "{active_exp variant_id or null}"
       meta_changes_this_session: {count of meta-log entries this session}

9.5. **Transfer Profile Update**:
   Read meta/experiments/completed-experiments.yaml
   IF file does not exist: log "Transfer profile: no completed experiments — skipping" and continue
   ELSE:
       adopted = filter where outcome == "adopted"
       IF adopted is empty: log "Transfer profile: no adopted experiments — skipping" and continue
       ELSE:
           Edit meta/transfer-profile.yaml (create if missing):
               validated_strategies: list of adopted strategy descriptions with imp@k data
               total_goals_at_export: total from aspirations-meta

### Execution Checklist (MANDATORY)

Before proceeding to Step 10, output a checklist accounting for EVERY step.
Each step must show one of: `done`, `empty` (ran but no data), `skipped (stop_mode)`, `skipped (file missing)`.
Do NOT proceed without outputting this checklist.

```
CONSOLIDATION CHECKLIST:
  Step 0  Micro-Hypothesis Sweep:  {done|empty}
  Step 0.5 Unreflected Hyp Sweep:  {done|empty}
  Step 1  Encoding Queue:          {done|empty}
  Overflow Queue:                  {done|empty|skipped (file missing)}
  Step 2  Tree Encoding:           {done|empty}
  Step 2.25 Knowledge Debt:        {done|empty}
  Step 2.6  Experience Archive:    {done}
  Step 2.6  Encoding Weights:      {done|skipped (insufficient data)|skipped (file missing)}
  Step 2.7  Conclusion Quality:    {done|empty}
  Step 2.8  Insight Consolidation: {done|empty}
  Step 3  Journal (structured):    {done}    ← MANDATORY
  Step 4  WM Archive:              {done}    ← MANDATORY
  Step 5  WM Reset:                {done}
  Step 6  Tree Rebalancing:        {done|skipped (stop_mode)}
  Step 7  Skill Gap Review:        {done|skipped (stop_mode)}
  Step 7.5 Experience Mining:      {done|skipped (stop_mode)}
  Step 8  Skill Health:            {done|skipped (stop_mode)}
  Step 8.5 Aspiration Archive:     {done}
  Step 8.6 Curriculum Gates:       {done}
  Step 8.65 Meta-Strategy Review:  {done|skipped (file missing)}
  Step 8.7 User Goal Recap:        {done|skipped (stop_mode)}
  Step 9  Handoff:                 {done}
  Step 9.5 Transfer Profile:       {done|skipped (file missing)}
```

10. Restart Loop Cycle (skip in stop_mode):
    IF stop_mode == true:
        Output: "Consolidation complete (stop mode — no restart)."
        RETURN
    # INTENTIONAL RECURSIVE CALL: aspirations → boot → aspirations.
    # Context compression prevents unbounded growth. Do not "fix" this cycle.
    Invoke /boot
    Boot detects handoff.yaml → continuation mode (abbreviated report → fast loop handoff).
```
