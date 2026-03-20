---
name: aspirations-consolidate
description: "Session-End Consolidation — hippocampal sleep replay, encoding, debt sweep, tree rebalancing, skill health, archive, user recap, handoff, restart"
user-invocable: false
parent-skill: aspirations
triggers:
  - "Session-End Consolidation Pass"
conventions: [aspirations, pipeline, experience, journal, handoff-working-memory, session-state, tree-retrieval, goal-schemas]
---

# Session-End Consolidation Pass

Run when the aspirations loop stops (any stop condition). This is the hippocampal "sleep replay" that compresses session observations into long-term memory. Covers micro-hypothesis sweep, encoding queue processing, dynamic consolidation budget, overflow queue management, encoding competition, tree encoding, knowledge debt sweep, snapshot invalidation, experience archive maintenance, journal logging, working memory archival, tree rebalancing, skill health report, aspiration archive sweep, user goal recap, continuation handoff, and restart loop cycle.

Note: Consolidation MUST NOT call session-state-set.sh.
Only /start and /stop may change agent-state.

## Parameters

- `stop_mode` (boolean, default: false) — When true, skip Steps 6 (tree rebalancing),
  7 (skill gap review), 8 (skill health report), 8.7 (user goal recap), and 10 (restart).
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
1. Items NOT selected but with encoding score >= 0.25: write to `mind/session/overflow-queue.yaml`
   - Set `original_score`, `current_score` (same initially), `deferred_count: 1`, `first_seen`, `session_first_seen`, `category`, `source_goal`
2. Before consolidation, read existing `mind/session/overflow-queue.yaml`:
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
      See mind/conventions/precision-encoding.md for schema and extraction heuristics.
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
        - Update mind/developmental-stage.yaml highest_capability if exceeded

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
   Bash: experience-read.sh --meta → get by_type, by_category stats
   IF enough data (total_live + total_archived >= 10):
       Read core/config/memory-pipeline.yaml → encoding_weight_adaptation section
       Read mind/memory-pipeline.yaml → current weight_performance_log
       Compare: for experiences with high utility_ratio (>0.7), what encoding weights
       were used when those observations were originally encoded?
       For experiences with low utility_ratio (<0.3), what weights were used?
       Adjust encoding_gate weights ±adjustment_per_session (0.05) toward weights
       that produced high-utility encodings, bounded by min/max from config.
       Log adjustment to weight_performance_log in mind/memory-pipeline.yaml

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

3. Bash: wm-read.sh sensory_buffer --json
   Log consolidation to journal:
   "## Consolidation — {date}
   Observations processed: {total_sensory_buffer}
   Encoded to long-term: {encoded_count}
   Discarded: {discarded_count}
   Flagged for review: {review_count}
   Context gaps detected: {context_gap_count} (hypotheses where relevant context existed but wasn't loaded)
   Judgment quality: {total_conclusions} conclusions, {wrong_count} wrong
   Articles updated: {list}"

4. Bash: wm-read.sh --json
   Archive working memory to journal entry (summary only)
5. Bash: wm-reset.sh

6. Tree Rebalancing (skip in stop_mode):
   IF stop_mode != true:
     Invoke /tree maintain (run all checks: DECOMPOSE, REDISTRIBUTE, DISTILL, SPLIT, SPROUT, MERGE, PRUNE, RETIRE)
     All 8 ops must be listed — DECOMPOSE grows tree depth, DISTILL concentrates low-utility nodes, RETIRE removes dead ones.
     Report any structural changes to journal

7. Skill Gap Review (skip in stop_mode):
   IF stop_mode != true:
     Read mind/skill-gaps.yaml
     Report: new gaps registered, gaps meeting forge threshold, dismissed gaps
     Highlight any gaps ready for "/forge-skill skill <gap-id>"

8. Skill Health Report (skip in stop_mode):
   IF stop_mode != true:
     Read .claude/skills/_tree.yaml
     Report:
       Active skills: {count} (core: {N}, forged: {N})
       Gaps registered: {N} (ready to forge: {N})
       Skills forged this session: {list}
       Skills underperforming: {list}

8.5. Aspiration Archive Sweep:
   Bash: aspirations-archive.sh  (sweep completed/retired aspirations to archive)

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
   Write mind/session/handoff.yaml with:
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
       # Bash: wm-read.sh known_blockers --json
       # Carry forward unresolved blockers where resolution is null
       - blocker_id: "{id}"
         reason: "{reason}"
         affected_skills: ["{skills}"]
         detected_session: {N}
     knowledge_debts_pending:
       # Bash: wm-read.sh knowledge_debt --json
       # Carried from Step 2.25 — debts not resolved during this session's consolidation
       - node_key: "{node-key}"
         reason: "{why this node needs updating}"
         source_goal: "{goal-id}"
         priority: "{HIGH/MEDIUM}"
         created: "{ISO date}"
         sessions_deferred: {N}
     user_goals_pending:
       # From Step 8.7 — user action items for next boot to report
       count: {N}
       goals:
         - id: "{goal-id}"
           title: "{goal title}"

10. Restart Loop Cycle (skip in stop_mode):
    IF stop_mode == true:
        Output: "Consolidation complete (stop mode — no restart)."
        RETURN
    # INTENTIONAL RECURSIVE CALL: aspirations → boot → aspirations.
    # Context compression prevents unbounded growth. Do not "fix" this cycle.
    Invoke /boot
    Boot detects handoff.yaml → continuation mode (abbreviated report → fast loop handoff).
```
