---
name: aspirations-consolidate
description: "Session-End Consolidation — hippocampal sleep replay, encoding, debt sweep, tree rebalancing, experience-to-skill mining, skill health, archive, user recap, handoff, restart"
user-invocable: false
parent-skill: aspirations
triggers:
  - "Session-End Consolidation Pass"
conventions: [aspirations, pipeline, experience, journal, handoff-working-memory, session-state, tree-retrieval, goal-schemas, coordination]
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

- `stop_mode` (boolean, default: false) — When true, skip Steps 7 (skill gap review),
  7.5 (experience-to-skill mining), 8 (skill health report),
  8.7 (user goal recap), and 10 (restart).
  Used by /stop to run proper consolidation without restarting the loop.

---

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

```
# Consolidation — run before session exit

0.1. CONSOLIDATION TRIAGE GATE:
   # This logic is duplicated in core/scripts/consolidation-precheck.py.
   # If you change the checks here, update that script to match.
   # ── PRE-SCAN (2 script calls + 1 file check) ────────────────────────
   triage_wm      = Bash: wm-read.sh --json
   triage_unrefl  = Bash: pipeline-read.sh --unreflected --counts
   triage_overflow = test -f <agent>/session/overflow-queue.yaml

   # ── EXTRACT COUNTS ──────────────────────────────────────────────────
   micro_count       = len(triage_wm.slots.micro_hypotheses)     # null/[] → 0
   encoding_count    = len(triage_wm.encoding_queue)              # null/[] → 0
   debt_count        = len(triage_wm.slots.knowledge_debt)        # null/[] → 0
   conclusions_count = len(triage_wm.slots.conclusions)           # null/[] → 0
   violations_count  = len(triage_wm.slots.recent_violations)     # null/[] → 0
   unreflected_count = triage_unrefl.active_unreflected           # 0 if none
   has_overflow      = triage_overflow                            # boolean

   data_total = micro_count + encoding_count + debt_count + conclusions_count + unreflected_count

   # ── SAFETY RAILS ────────────────────────────────────────────────────
   # Anti-suppression ceiling: max 3 consecutive lean sessions.
   # Stored in a standalone file (NOT handoff.yaml, which boot deletes).
   prior_lean = read <agent>/session/consolidation-lean-streak (integer, default 0 if missing)

   IF prior_lean >= 3:
       consolidation_tier = "full"
       Log: "▸ TRIAGE: OVERRIDE → full (ceiling: {prior_lean} consecutive lean sessions)"
   ELIF violations_count > 0:
       consolidation_tier = "full"
   ELIF has_overflow:
       consolidation_tier = "full"
   ELIF any pre-scan script call failed or returned unparseable output:
       consolidation_tier = "full"
   ELIF data_total == 0:
       consolidation_tier = "lean"
   ELSE:
       consolidation_tier = "full"

   Log: "▸ TRIAGE: {consolidation_tier} (micro={micro_count} enc={encoding_count} debt={debt_count} concl={conclusions_count} unrefl={unreflected_count} overflow={has_overflow} violations={violations_count})"

# ── LEAN FAST PATH ─────────────────────────────────────────────────────
# When all data queues are verified empty, skip Steps 0–2.8 entirely.
# Step 2.9 (Experience Distillation) and mandatory steps (3+) still run.
IF consolidation_tier == "lean":
   Output: "▸ CONSOLIDATION: lean path — no session data to encode"
   # Experience archive still runs (timer-based sweep, not session-dependent)
   Bash: experience-archive.sh
   # JUMP → Step 2.9 (experience distillation — runs on both paths)

# ── FULL PATH ──────────────────────────────────────────────────────────
IF consolidation_tier == "full":

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
         Add goal to target aspiration via aspirations-update.sh --source {asp.source}
         Log: evolution-log-append.sh with event "micro-batch-discovery"

0.5. Unreflected Hypothesis Sweep:
   Bash: pipeline-read.sh --unreflected
   IF unreflected hypotheses exist:
     invoke /review-hypotheses --learn
     # This reflects on each unreflected hypothesis, sets reflected: true,
     # and pushes encoding items into encoding_queue for Step 1.
     Output: "▸ CONSOLIDATION: reflected on {count} unreflected hypotheses"

0.7. Operational Gotcha Sweep (safety net):
   # Catch error-then-fix patterns that Phase 6.5 missed (e.g., errors during
   # boot, consolidation itself, or non-goal work). Budget: max 2 new entries.
   #
   Read today's journal: <agent>/journal/{YYYY}/{MM}/{YYYY-MM-DD}.md
   Scan for co-occurring patterns:
     (error|exception|traceback|failed|refused) AND (fixed|resolved|workaround|solution|root cause|turned out)
   
   IF potential gotcha patterns found (max 2):
       FOR EACH pattern:
           # Dedup against existing reasoning bank
           Bash: reasoning-bank-read.sh --summary
           IF not already encoded (no semantic overlap):
               Determine store: prescriptive → guardrail, diagnostic → reasoning bank
               Create entry via reasoning-bank-add.sh or guardrails-add.sh
                 tags: ["ops-gotcha", "consolidation-sweep"]
               Log: "CONSOLIDATION GOTCHA: {title} — encoded from session journal"
   Output: "▸ CONSOLIDATION: gotcha sweep — {N} new entries encoded"

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
      # Legacy: standard_tier_deferred items may exist from previous sessions.
      # All items now use the same target resolution path.
      IF item.target_node_key:
          node = {key: item.target_node_key, file: item.target_node_file}
          verify = bash core/scripts/tree-find-node.sh --key {item.target_node_key}
          IF verify is empty:
              node=$(bash core/scripts/tree-find-node.sh --text "{item.observation}" --leaf-only --top 1)
      ELSE:
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

   # Legacy: apply metadata updates for items that have them (e.g., from previous sessions)
   IF item.metadata_updates:
       echo '{"operations": [
         {"op": "set", "key": "<node.key>", "field": "confidence", "value": <item.metadata_updates.confidence>},
         {"op": "set", "key": "<node.key>", "field": "capability_level", "value": "<item.metadata_updates.capability_level>"}
       ]}' | bash core/scripts/tree-update.sh --batch
       # Growth triggers
       Read core/config/tree.yaml for decompose_threshold, split_threshold
       line_count = count lines in node .md body (excluding YAML front matter)
       If line_count > decompose_threshold AND node depth < D_max:
           bash core/scripts/tree-update.sh --set <node.key> growth_state ready_to_decompose
       # Capability event logging
       IF item.metadata_updates.capability_level crosses threshold:
           Log capability event via evolution-log-append.sh
           Update <agent>/developmental-stage.yaml highest_capability if exceeded

2.25. Knowledge Debt Sweep:
   Bash: wm-read.sh knowledge_debt --json
   IF items exist:
       Sort by priority (HIGH first), then by age (oldest first)
       For each debt:
           Read target node .md file
           IF node was updated AFTER debt was created → mark resolved, skip

           # ATTEMPT RESOLUTION — don't just check, actually try
           IF priority is HIGH or sessions_deferred >= 2:
               Reconcile now: read node, attempt the data acquisition that created this debt.
               If the debt references infrastructure (shared filesystem, API, external service, etc.):
                   Actually invoke the relevant skill/script to get the data.
                   Do not assume infrastructure is still down — try it.
               If data acquired: update node, set last_update_trigger:
                   {type: "debt-reconciliation", source: debt.source_goal, session: N}
               Propagate up parent chain if significant
               Log: "KNOWLEDGE DEBT RESOLVED: {node_key} — {reason}"
               If data acquisition fails: carry forward (increment sessions_deferred)

           ELSE:
               Carry forward to handoff (increment sessions_deferred)

           # MAX-DEFER CEILING: drop stale debts that never resolve
           IF sessions_deferred >= 10:
               Log: "KNOWLEDGE DEBT DROPPED: {node_key} — {reason} (deferred {sessions_deferred} sessions, ceiling reached)"
               Remove from debt list (do not carry forward)

       Report: "Knowledge debts: {resolved} resolved, {carried} carried forward, {dropped} dropped"

<!-- Steps 2.6-10 are mirrored in core/config/consolidation-housekeeping.md (fast-path digest) -->
<!-- If editing steps below, update that file to match. Sync date: 2026-04-04 -->

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
       Bash: world-cat.sh memory-pipeline.yaml  # current weight_performance_log
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

2.8. Pending Questions Re-evaluation:
   Read <agent>/session/pending-questions.yaml
   IF file exists AND has entries with status == "pending":
       FOR EACH pending question:
           # Re-evaluate: can the agent now answer this itself?
           # Check knowledge tree, experience archive, and session learnings.
           node=$(bash core/scripts/tree-find-node.sh --text "{question.question}" --leaf-only --top 3)
           IF relevant knowledge found that answers the question:
               Update question status to "resolved"
               Set question.resolution = "Self-resolved: {one-line answer from knowledge}"
               Set question.resolved_at = "$(date +%Y-%m-%dT%H:%M:%S)"
               Log: "PENDING QUESTION RESOLVED: {question.id} — answered from accumulated knowledge"
           ELIF question is still relevant but unanswerable:
               # Keep pending — it genuinely needs user input
               pass
           ELIF question references infrastructure/state that has changed:
               Update question status to "resolved"
               Set question.resolution = "Stale: conditions changed since question was created"
               Log: "PENDING QUESTION STALE: {question.id} — conditions changed"
       Write updated <agent>/session/pending-questions.yaml
       Report: "Pending questions: {resolved_count} self-resolved, {stale_count} stale, {remaining_count} still pending"

# ── END FULL PATH (Steps 0–2.8 above only run when consolidation_tier == "full") ───

# ── ALWAYS-RUN STEPS (both full and lean paths) ──────────────────────

2.9. Experience Distillation (compile experiences into tree wiki):
   # Reads from experience archive, NOT WM queues — runs on both full and lean paths.
   # Experiences are raw data. The tree is the compiled wiki.
   Bash: experience-read.sh --type goal_execution --recent 30 --summary
   Group experiences by tree_nodes_related field.
   
   FOR EACH tree node with 3+ related experiences since last distillation:
     # Read the FULL experience content files (not just JSONL summaries)
     FOR EACH experience in cluster:
       Read <agent>/experience/{exp.content_file}
       Extract: verbatim_anchors, key findings, exact values, failure sequences
     
     # Synthesize into deep tree content (NOT 1-3 sentence compression)
     Read the target tree node .md file
     Compose a multi-paragraph synthesis that:
       - Preserves specific technical detail (exact error messages, thresholds, sequences)
       - Identifies patterns across experiences (what worked, what failed, why)
       - Extracts decision rules with concrete conditions
       - Notes contradictions or evolution in understanding over time
     
     Edit target node .md with synthesized content
     Update node metadata via batch:
       echo '{"operations": [
         {"op": "set", "key": "<node-key>", "field": "last_updated", "value": "<today>"},
         {"op": "increment", "key": "<node-key>", "field": "article_count"}
       ]}' | bash core/scripts/tree-update.sh --batch
     Set last_update_trigger: {type: "experience-distillation", session: N}
     Check growth triggers (same as Step 2 growth trigger block):
       line_count = count lines in node .md body
       If line_count > decompose_threshold AND depth < D_max:
         bash core/scripts/tree-update.sh --set <node-key> growth_state ready_to_decompose
     Log: "EXPERIENCE DISTILLATION: {node_key} enriched from {count} experiences"
   
   Budget: max 5 nodes per consolidation (largest clusters first)
   Report: "Experience distillation: {distilled_count} nodes enriched, {skipped_count} clusters below threshold"

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
   Articles updated: {list}
   Triage: {consolidation_tier} — {reason summary}"

4. **MANDATORY** — must run BEFORE wm-reset (Step 5) to preserve state:
   Bash: wm-read.sh --json
   Archive working memory to journal entry (summary only).
   This captures any remaining WM state before it is destroyed by reset.
5. Bash: wm-reset.sh

6. Tree Rebalancing:
   Invoke /tree maintain (run all checks: DECOMPOSE, REDISTRIBUTE, DISTILL, SPLIT, SPROUT, MERGE, PRUNE, RETIRE)
   All 8 ops must be listed — DECOMPOSE grows tree depth, DISTILL concentrates low-utility nodes, RETIRE removes dead ones.
   Report any structural changes to journal

7. Skill Gap Review (skip in stop_mode):
   IF stop_mode != true:
     Bash: meta-read.sh skill-gaps.yaml
     Report: new gaps registered, gaps meeting forge threshold, dismissed gaps
     Highlight any gaps ready for "/forge-skill skill <gap-id>"

7.5. Experience-to-Skill Mining (skip in stop_mode):
   IF stop_mode != true:
     # Mine experience records for repeated procedures that should be skills
     Bash: experience-read.sh --type goal_execution --recent 30 --summary
     Bash: meta-read.sh skill-gaps.yaml
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
   Bash: meta-read.sh meta.yaml
   IF meta/meta.yaml does not exist:
       Log: "Meta-strategy review: meta/meta.yaml not initialized — skipping"
       Continue to next step
   Bash: meta-read.sh improvement-velocity.yaml
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
   Bash: meta-set.sh meta.yaml
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

8.87. Team State Session Summary:
   # Update shared team state with session-end summary
   goals_this_session = count goals_completed_this_session from working memory

   # Gather blocked data for critical path (used by both Step 8.87 and Step 9)
   Bash: goal-selector.sh blocked
   Parse JSON → blocked_data

   Bash: team-state-update.sh --field agent_status.<AGENT_NAME> \
     --value '{"last_active":"<now>","current_focus":"session ended","session_goals_completed":<goals_this_session>}'
   Output: "▸ Team state: updated agent status (session ended, {goals_this_session} goals)"

   IF blocked_data.bottlenecks is non-empty:
       critical_blockers_payload = top 3 bottlenecks as JSON array with fields: goal_id, title, cause, downstream_count, updated_by, updated_at
       Bash: team-state-update.sh --field critical_blockers --value '<critical_blockers_json>'
       Output: "▸ Team state: updated critical blockers ({N} entries)"

8.9. Release Held Claims (world goals only):
   # Prevent stale claims when session ends normally. See coordination convention.
   Bash: AYOAI_AGENT={agent} aspirations-query.sh --goal-field claimed_by {agent_name}
   FOR EACH returned goal WHERE source == "world":
       Bash: aspirations-release.sh <goal-id>
       Log: "Released claim on {goal.id}"
   echo "Session ending: released all held claims" | Bash: board-post.sh --channel coordination --type status

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
     critical_path:
       # Populated from blocked_data gathered in Step 8.87 (or gathered here if 8.87 was skipped in stop_mode)
       # IF blocked_data was not gathered yet: Bash: goal-selector.sh blocked → parse JSON → blocked_data
       primary_blocker:
         goal_id: "{bottlenecks[0].goal_id}"
         title: "{bottlenecks[0].title}"
         cause: "{bottlenecks[0].cause}"
         downstream_count: {bottlenecks[0].downstream_count}
         affected_aspirations: ["{asp_ids}"]
       blocked_fraction: "{total_blocked}/{total_active_goals}"
       top_bottlenecks:
         - goal_id: "..."
           title: "..."
           downstream_count: N
           cause: "..."
       estimated_unblock_impact: "Resolving {primary_blocker.title} would unblock {N} goals across {M} aspirations"
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
     consolidation_meta:
       triage_tier: "{lean|full}"
       consecutive_lean_sessions: {N}  # informational copy (source of truth: consolidation-lean-streak file)
   # Update the streak file (source of truth for anti-suppression ceiling)
   Write <agent>/session/consolidation-lean-streak:
     IF consolidation_tier == "lean": prior_lean + 1
     IF consolidation_tier == "full": 0

9.5. **Transfer Profile Update**:
   Bash: meta-read.sh experiments/completed-experiments.yaml
   IF file does not exist: log "Transfer profile: no completed experiments — skipping" and continue
   ELSE:
       adopted = filter where outcome == "adopted"
       IF adopted is empty: log "Transfer profile: no adopted experiments — skipping" and continue
       ELSE:
           Edit transfer-profile.yaml via meta-set.sh (create if missing):
               validated_strategies: list of adopted strategy descriptions with imp@k data
               total_goals_at_export: total from aspirations-meta

### Execution Checklist (MANDATORY)

Before proceeding to Step 10, output a checklist accounting for EVERY step.
Each step must show one of: `done`, `empty` (ran but no data), `skipped (stop_mode)`, `skipped (file missing)`, `skipped (lean)`.
Do NOT proceed without outputting this checklist.

```
CONSOLIDATION CHECKLIST:
  Triage:                          {lean|full}
  Step 0  Micro-Hypothesis Sweep:  {done|empty|skipped (lean)}
  Step 0.5 Unreflected Hyp Sweep:  {done|empty|skipped (lean)}
  Step 0.7 Gotcha Sweep:           {done|empty|skipped (lean)}
  Step 1  Encoding Queue:          {done|empty|skipped (lean)}
  Overflow Queue:                  {done|empty|skipped (file missing)|skipped (lean)}
  Step 2  Tree Encoding:           {done|empty|skipped (lean)}
  Step 2.25 Knowledge Debt:        {done|empty|skipped (lean)}
  Step 2.6  Experience Archive:    {done}
  Step 2.6  Encoding Weights:      {done|skipped (insufficient data)|skipped (file missing)|skipped (lean)}
  Step 2.7  Conclusion Quality:    {done|empty|skipped (lean)}
  Step 2.8  Pending Questions:     {done|empty|skipped (lean)}
  Step 2.9  Experience Distill:    {done|empty}              ← runs on both paths
  Step 3  Journal (structured):    {done}    ← MANDATORY
  Step 4  WM Archive:              {done}    ← MANDATORY
  Step 5  WM Reset:                {done}
  Step 6  Tree Rebalancing:        {done}
  Step 7  Skill Gap Review:        {done|skipped (stop_mode)}
  Step 7.5 Experience Mining:      {done|skipped (stop_mode)}
  Step 8  Skill Health:            {done|skipped (stop_mode)}
  Step 8.5 Aspiration Archive:     {done}
  Step 8.6 Curriculum Gates:       {done}
  Step 8.65 Meta-Strategy Review:  {done|skipped (file missing)}
  Step 8.7 User Goal Recap:        {done|skipped (stop_mode)}
  Step 8.87 Team State + Blockers: {done}
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
