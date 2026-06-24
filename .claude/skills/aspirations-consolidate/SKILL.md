---
name: aspirations-consolidate
description: "Runs session-end consolidation — the hippocampal sleep-replay pass that compresses session observations into long-term memory. Handles micro-hypothesis sweep, encoding queue processing, knowledge debt sweep, tree rebalancing, experience-to-skill mining, skill health check, aspiration archive, user recap, handoff writeout, and restart loop cycle. Use whenever the aspirations loop stops (any stop condition), before agent-state transitions to IDLE, or when the orchestrator hits an explicit consolidation checkpoint."
user-invocable: false
parent-skill: aspirations
triggers:
  - "Session-End Consolidation Pass"
conventions: [aspirations, pipeline, experience, journal, handoff-working-memory, session-state, tree-retrieval, goal-schemas, coordination]
minimum_mode: autonomous
revision_id: "skill-bootstrap-aspirations-consolidate-ad0fdd"
previous_revision_id: null
---

# Session-End Consolidation Pass

Run when the aspirations loop stops (any stop condition). This is the hippocampal "sleep replay" that compresses session observations into long-term memory. Covers micro-hypothesis sweep, encoding queue processing, dynamic consolidation budget, overflow queue management, encoding competition, tree encoding, knowledge debt sweep, snapshot invalidation, experience archive maintenance, journal logging, working memory archival, tree rebalancing, skill health report, aspiration archive sweep, user goal recap, continuation handoff, and restart loop cycle.

Note: Consolidation MUST NOT call session-state-set.sh.
Only /start and /stop may change agent-state.

Note: minimum_mode is `autonomous` but /stop's deferred sequence (Phase -1.4 in aspirations/SKILL.md)
invokes this AFTER setting state to IDLE (D1) and BEFORE setting mode to reader (D7).
The mode is still `autonomous` at invocation time (D4). If Phase -1.4 step ordering changes, this breaks.

## Parameters

- `stop_mode` (boolean, default: false) — When true, skip Steps 7 (skill gap review),
  7.5 (experience-to-skill mining), 8 (skill health report),
  8.7 (user goal recap), and 10 (restart).
  Used by /stop to run proper consolidation without restarting the loop.

---

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

```
# Consolidation — run before session exit

-1. GENERALIZE-DOWN BODY-WM MERGE (Phase 1C, g-306-63):
   # The reducer IS this consolidating session (it holds running-session-id) —
   # the EXISTING reducer the Mind/Body design merges into. Before triage reads
   # WM, merge back every non-reducer worker Body that closed since the last
   # consolidation: each carries body_state=closed-pending-merge (set by the
   # stop-hook producer on Body close). generalize_down delta-merges each Body's
   # forked WM into the reducer's WM under per-slot policies (arrays
   # append+content-hash-dedup; active_context/session-identity reducer-wins;
   # numeric counters SUM; ISO-timestamp cadence-trackers latest-wins; loop_state
   # recurses), copies back to the reducer WM, re-scans once for late arrivals,
   # and marks each manifest body_state=merged (the engine's session-termination
   # memory-persistence merge). Running it FIRST means merged Body data (micro_hypotheses,
   # encoding_queue, conclusions, ...) lands in the triage counts below and gets
   # consolidated THIS pass.
   #
   # DORMANT in single-runner: no closed-pending-merge manifest exists, so this
   # is a no-op (empty summary, reducer WM untouched) until a 2nd worker Body
   # forks (Phase 2, g-306-65). Direct py -3 (NOT the .sh wrapper) per
   # rb-225/rb-247 (Windows bash-subprocess hang). FAIL-OPEN: a merge error must
   # never block consolidation — log and proceed to triage.
   Bash: py -3 core/scripts/body-merge.py generalize-down --agent "$MIND_AGENT" --output json
   # Parse the JSON summary. IF merged or noop non-empty:
   #   Log "▸ GENERALIZE-DOWN: merged {len(merged)} Body(ies), {len(noop)} no-op,
   #        {len(skipped)} skipped (scanned {scanned})".
   # On a non-zero exit: Log the stderr line and CONTINUE to triage (fail-open).

0.1. CONSOLIDATION TRIAGE GATE:
   # This logic is duplicated in core/scripts/consolidation-precheck.py.
   # If you change the checks here, update that script to match.
   # ── PRE-SCAN (2 script calls + 1 file check) ────────────────────────
   triage_wm      = Bash: wm-read.sh --json
   triage_unrefl  = Bash: pipeline-read.sh --unreflected --counts
   triage_overflow = test -f agents/<agent>/session/overflow-queue.yaml

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
   prior_lean = read agents/<agent>/session/consolidation-lean-streak (integer, default 0 if missing)

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

0.6. Source-Integrity Verifier (Gap-16 — never-summarize-summaries):
   # Invariant (GSD 2.0 / BRD Gap 16, guard-745): every summary level this
   # skill generates — journal batch summary (Step 0), handoff session_summary
   # (Step 9), team-state session summary (Step 8.87), tree-node summaries
   # (Step 1+) — MUST regenerate from the RAW level below (journal + actual
   # state), NEVER from a prior summary file. Summarizing a summary compounds
   # lossy re-compression across session boundaries (the summary-of-summary
   # degradation).
   #
   # The verifier fails LOUD (exit 1) when a source path is a summary artifact
   # (handoff.yaml, session-summary*, *-summary.*, paths under /summaries/);
   # raw sources (journal/, experience.jsonl, session state) pass (exit 0).
   journal_src="agents/<agent>/journal/{YYYY}/{MM}/{YYYY-MM-DD}.md"
   Bash: bash core/scripts/consolidate-source-verify.sh "$journal_src"
   IF exit 1:
       # A summary file was about to be consolidated AS a raw source — STOP.
       # Re-point the source at the raw journal/state before summarizing. This
       # is a fail-loud invariant (guard-745), not an advisory: do NOT proceed
       # to summarize from a summary.
       Output: "▸ CONSOLIDATION HALT (Gap-16): source '{journal_src}' is a summary artifact — re-point at raw journal+state before summarizing"
   # Apply the SAME check to any other path used as a summary-generation source
   # later in this skill (handoff session_summary inputs at Step 9, team-state
   # session summary inputs at Step 8.87): each such source MUST be raw.

0.65. Cluster-Then-Summarize the Session Journal (BRD Gap 1d, g-306-09):
   # Lyfe Agents cluster-then-summarize, applied to the session journal:
   # instead of ONE linear summary over all of today's journal entries, GROUP
   # entries by similarity FIRST, then write ONE summary per cluster. Per-cluster
   # summaries preserve the topical separation a single linear summary blurs, so
   # the downstream handoff key_outcomes (Step 9, boot-retrievable) stays
   # granular rather than mushy. This is the cheap half of the retrieval upgrade
   # (before Gap 1b/1c). FULL path only (lean/fast sessions have too few entries
   # for clustering to add value — this step lives inside `tier == "full"`).
   #
   # CLUSTERING METHOD = LEXICAL TOKEN-OVERLAP, NOT EMBEDDINGS (rb-1781 +
   # first-principles.md). The BRD framed this as "embedding similarity," but a
   # first-principles read of the premise rejects embeddings here:
   #   (a) No embedding infrastructure exists in this framework — the retrieval
   #       engine and goal_duplication.py both discriminate via IDF-weighted
   #       token-overlap, and building an embedding service is the inherited-
   #       solution anti-pattern g-305-05 already declined.
   #   (b) A single session's journal entries are a semantically-HOMOGENEOUS
   #       corpus (one agent, one session, shared framework vocabulary), exactly
   #       the shape where topical embeddings are a PRECISION TRAP (rb-1781:
   #       cosine>=0.7 fires between genuinely-different entries). Token-shape
   #       (which subsystem/file/mechanism an entry names) discriminates better.
   # The embedding->lexical decision is logged to pending-questions + the
   # decisions board for the BRD author to review (executed, override-if-disagree).
   Read today's RAW journal (already source-verified raw by Step 0.6):
     agents/<agent>/journal/{YYYY}/{MM}/{YYYY-MM-DD}.md
   entries = parsed per-goal journal entries for this session
   IF len(entries) < 4:
     # Too few to cluster meaningfully — fall back to the prior linear single
     # summary. No degradation: clustering a 3-entry log is noise, not signal.
     Output: "▸ Journal cluster-summarize: {len(entries)} entries < 4 — linear single summary (clustering skipped)"
     clusters = [{label: "session", summary: <one compressed summary over all entries>}]
   ELSE:
     # Cluster by LEXICAL token-overlap. Group entries that share, in priority:
     #   1. the same aspiration-prefix (the goal-id family, the strongest cheap
     #      signal that two entries are the same thread of work), AND/OR
     #   2. the same category, AND/OR
     #   3. high significant-token overlap — drop the IDF-zero framework
     #      stopwords (goal/deep/encoded/committed/verified/rb/exp) and keep the
     #      discriminating tokens (subsystem names, file paths, mechanism words);
     #      two entries naming the same surface merge even across aspirations.
     clusters = group entries into topical clusters by the rules above
     FOR EACH cluster:
       cluster.label   = a short topical label (the shared aspiration / surface)
       cluster.summary = ONE compressed summary of that cluster's entries only
     Output: "▸ Journal cluster-summarize: {len(entries)} entries -> {len(clusters)} cluster(s): {[c.label for c in clusters]}"
   # Retrievability (verification outcome 2): stash the per-cluster summaries in a
   # WM slot. Step 9 reads this slot and makes each cluster.summary one
   # key_outcomes entry in the handoff, so the per-cluster summaries survive to
   # the next session (boot reads handoff.yaml) — they remain retrievable.
   Bash: echo '<json array of {label, summary} per cluster>' | bash core/scripts/wm-set.sh journal_cluster_summaries
   # Retrieval-not-degraded (verification outcome 3): every entry is still
   # represented, now under a topical label — per-cluster summaries are strictly
   # MORE granular than the prior single linear summary, so the next session can
   # target a cluster instead of scanning one blob. No information is dropped.

0.7. Operational Gotcha Sweep (safety net):
   # Catch error-then-fix patterns that Phase 6.5 missed (e.g., errors during
   # boot, consolidation itself, or non-goal work). Budget: max 2 new entries.
   #
   Read today's journal: agents/<agent>/journal/{YYYY}/{MM}/{YYYY-MM-DD}.md
   Scan for co-occurring patterns:
     (error|exception|traceback|failed|refused) AND (fixed|resolved|workaround|solution|root cause|turned out)
   
   IF potential gotcha patterns found (max 2):
       FOR EACH pattern:
           # Dedup against existing reasoning bank
           Bash: reasoning-bank-read.sh --summary
           IF not already encoded (no semantic overlap):
               Determine store: prescriptive → guardrail, diagnostic → reasoning bank
               Create entry via reasoning-bank-add.sh or guardrails-add.sh
                 applies_to: <any|framework|domain|specific>  # REQUIRED on rb-add.
                   # Ops gotchas about external services / domain infra → domain
                   # (this agent's deployment-specific services, products, integrations).
                   # Framework-internal gotchas (skill protocols, gates, hooks) → framework.
                   # Cross-cutting methodological gotchas → any.
                   # Single-incident with no transferable shape → specific.
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
1. Items NOT selected but with encoding score >= 0.25: write to `agents/<agent>/session/overflow-queue.yaml`
   - Set `original_score`, `current_score` (same initially), `deferred_count: 1`, `first_seen`, `session_first_seen`, `category`, `source_goal`
2. Before consolidation, read existing `agents/<agent>/session/overflow-queue.yaml`:
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
          verify = bash core/scripts/tree-find-node.sh --node {item.target_node_key}
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
   # T21 PostToolUse hook (`tree-front-matter-sync.py`) atomically bumps
   # _tree.yaml last_updated on every Edit of a tree node .md — no
   # explicit `tree-update.sh --set last_updated` call needed here.
   f. If leaf node changed significantly:
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
        - Update agents/<agent>/developmental-stage.yaml highest_capability if exceeded

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
           Update agents/<agent>/developmental-stage.yaml highest_capability if exceeded

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
                   # T21 hook auto-bumps last_updated on the Append above.
       Log summary: "Judgment quality: {total} conclusions ({negative} negative), {correct} correct, {wrong} wrong, {pending} pending. Avg signals: {avg_signals:.1f}"

2.8. Pending Questions Re-evaluation:
   Read agents/<agent>/session/pending-questions.yaml
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
       Write updated agents/<agent>/session/pending-questions.yaml
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
       Read agents/<agent>/experience/{exp.content_file}
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

5.5. Presence file truncation (g-115-411 — cross-agent presence). For each
   `world/presence/<agent>.jsonl`, keep the last 1000 entries via locked
   rewrite. Bounds storage to ~140KB per agent at the 1000-entry cap
   (~672KB/day write rate is what motivates the truncation). Dormant when
   the PostToolUse * hook is not wired — the loop is idempotent on empty.

   Bash: for _PRES in "$WORLD_DIR"/presence/*.jsonl; do [ -f "$_PRES" ] || continue; py -3 -c "import sys,os,pathlib; sys.path.insert(0, os.environ['CORE_ROOT']+'/scripts'); from _fileops import locked_modify_jsonl; locked_modify_jsonl(pathlib.Path('$_PRES'), lambda recs: recs[-1000:])" 2>/dev/null || true; done; unset _PRES

6. Tree Rebalancing — runs always, including stop_mode. Symmetric with the
   FAST consolidation path in `core/config/consolidation-housekeeping.md`
   Step 6: both invoke `/tree maintain --stop-mode` (small caps from
   `core/config/tree.yaml` `stop_mode_caps`) under stop_mode=true so /stop
   stays fast. Skipping this step in any context is a guardrail violation.

   distill_json=$(bash core/scripts/tree-read.sh --distill-candidates)
   decompose_json=$(bash core/scripts/tree-read.sh --decompose-candidates)
   debt_count = length(distill_json) + length(decompose_json)
   debt_threshold = tree_debt_check.debt_threshold from core/config/tree.yaml (default 40)

   IF stop_mode == true:
     Invoke /tree maintain --stop-mode   # small caps (stop_mode_caps), fast /stop
   ELIF debt_count > debt_threshold * 3:
     Invoke /tree maintain --backlog     # elevated caps, largest-first
   ELSE:
     Invoke /tree maintain                # standard caps

   All three paths run all 8 ops: DECOMPOSE, REDISTRIBUTE, DISTILL, SPLIT, SPROUT, MERGE, PRUNE, RETIRE.
   DECOMPOSE grows tree depth, DISTILL concentrates low-utility nodes, RETIRE removes dead ones.
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
     # Skill-quality pipeline staleness guard (skill-telemetry-signal-master-plan
     # Layer 3). Catches Step 8.76 sampling-bias silences within 7 days instead
     # of the 25 days it took during the 2026-04-16 → 2026-05-12 silence.
     # With --file-goal, files an Investigate goal automatically when stale so
     # the next iteration probes it. Exit code 0=fresh, 2=stale (goal filed),
     # 3=missing file. Knowledge tree:
     # world/knowledge/tree/system/system-constraints-loop/skill-telemetry-signal-master-plan.md
     Bash: skill-quality-staleness-check.sh --file-goal --json
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
       Skill-quality pipeline: {fresh|stale (Investigate goal filed)|missing} from staleness-check JSON

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
   # Status filter excludes terminal-status goals — claim-clearing on completion
   # is enforced in cmd_complete_by/cmd_update_goal; this filter is defense in
   # depth so any future writer regression can't flood the release loop.
   Bash: MIND_AGENT={agent} aspirations-query.sh --goal-field claimed_by {agent_name} --goal-status pending,in-progress,blocked
   FOR EACH returned goal WHERE source == "world":
       Bash: aspirations-release.sh <goal-id>
       Log: "Released claim on {goal.id}"
   echo "Session ending: released all held claims" | Bash: board-post.sh --channel coordination --type status

9. Write Continuation Handoff:
   # Tier 0 phase-cost telemetry (plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md).
   # TWO-STEP under Windows bash. Previous single-pipeline (`script | py -c ... || echo`)
   # silently fell through to the `||` fallback under autocompact pressure, so alpha's
   # handoff showed "skipped (no markers)" despite 773 captured markers. The env-var
   # pattern below matches `iteration-close.sh:114` — robust against MSYS pipe issues.
   Bash: bash core/scripts/phase-cost-report.sh --write-report > "$MIND_AGENT/session/last-phase-cost.json" 2>/dev/null || echo '{}' > "$MIND_AGENT/session/last-phase-cost.json"
   Bash: MIND_AGENT="$MIND_AGENT" py -3 -c "import json,os; p=os.environ['MIND_AGENT']+'/session/last-phase-cost.json'; d=json.load(open(p)) if os.path.exists(p) and os.path.getsize(p)>2 else {}; print(d.get('written_to') or 'none'); print(d.get('summary') or 'skipped (no markers)')"
   → capture first line as phase_cost_report_path, second line as phase_cost_summary
   Bash: goal-selector.sh → get top-ranked goal for next session
   Read decisions_locked from current session context (if any)
   ### Scripted Handoff Build

   The LLM assembles a payload JSON with the prose fields (next_focus,
   reasons, key_outcomes, decisions_locked) plus the structured fields
   already gathered above (phase_cost_report, goal-selector output,
   blockers, debts, user_goals), then pipes it to `handoff-yaml-build.sh`
   which validates required fields, defaults optionals, and writes the
   handoff.yaml atomically via `locked_write_yaml`.

   `key_outcomes` sourcing (BRD Gap 1d, g-306-09): read the
   `journal_cluster_summaries` WM slot written by Step 0.65. Each cluster's
   `summary` becomes one `key_outcomes` entry (prefixed with its `label`), so the
   handoff carries the PER-CLUSTER journal summaries — topical and granular —
   rather than one linear blob. IF the slot is null/empty (lean session, or < 4
   journal entries so Step 0.65 produced a single "session" cluster), fall back
   to the prior single linear key_outcomes list. Clear the slot after reading
   (one-shot; next session's Step 0.65 re-populates it).

   Required payload fields (all others optional — script defaults them):
   `session_number`, `next_focus`, `first_action.goal_id`,
   `first_action.reason`, `session_summary.goals_completed`,
   `session_summary.goals_failed`,
   `consolidation_meta.triage_tier` (the `consolidation_tier` value from
   the triage block at Step 0.1 — `"lean"` or `"full"`),
   `consolidation_meta.consecutive_lean_sessions` (the NEW streak count:
   `prior_lean + 1` when triage_tier is lean, `0` when full — same value
   written to `agents/<agent>/session/consolidation-lean-streak` below).
   Without these two, handoff.yaml's `consolidation_meta` defaults to
   `{}`, which reads as "nothing happened" even when Phase 8 inline-
   encoded a full session's worth of work — boot's status line at
   `boot/SKILL.md:226` then silently omits the "Last consolidation"
   row, losing the signal.

   Decision-classification convention (LLM responsibility when composing
   `decisions_locked`):
   - `kind: "strategy"` for approach / priority / sequencing decisions
   - `kind: "world_claim"` for infrastructure / availability / external
     state conclusions; also set `evidence_strength` to `weak|moderate|strong`
     (single error or single probe = weak)
   - Carry forward unexpired entries from the previous handoff; expire
     entries where `current_session - made_session > 3`

   Critical-path sourcing convention (LLM responsibility when composing
   `critical_path`) — mandatory, no narrative freedom:
   - `primary_blocker.goal_id`, `.title`, `.cause` MUST be read directly
     from `world/aspirations.jsonl`. The `cause` field MUST be the goal's
     actual `defer_reason` (for `status: pending` with non-null
     `defer_reason`) OR `blocked_reason` (for `status: blocked`). If both
     are null/empty, the goal is NOT a blocker — do not list it.
   - Do NOT narrate a `cause` that cannot be quoted verbatim from the
     goal record. Hallucinating a plausible-sounding defer reason (e.g.,
     "blocked on user-initiated X") when the field is null is a
     capability-routing violation per `.claude/rules/probe-before-defer.md`
     and `.claude/rules/capability-before-user.md`. The consumer (`/boot`
     Step 0.5 sub-step 4c) displays this string to the resuming session;
     a fabricated cause steers the next session toward the wrong work.
   - `blocked_fraction` MUST be computed from a single pass over
     `world/aspirations.jsonl`: `pending_with_defer_reason +
     status_blocked + status_hypothesis_gate` over total
     non-completed-non-archived goals. No "~" approximation — the data
     is authoritative and cheap to count.
   - `top_bottlenecks[]` entries follow the same sourcing rule as
     `primary_blocker`. Rank by `downstream_count` computed from
     `blocked_by` / `depends_on` edges, not by subjective importance.
   - If no goal has a non-null `defer_reason` AND no goal has
     `status: blocked`, emit `critical_path: {}` — an empty object is
     the correct representation of "nothing blocks the frontier",
     not a fabricated placeholder.

   ```
   Bash: echo '<payload>' | bash core/scripts/handoff-yaml-build.sh
   Read JSON: summary + written_path for confirmation.
   ```
   # Update the streak file (source of truth for anti-suppression ceiling)
   Write agents/<agent>/session/consolidation-lean-streak:
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

9.7. **Notify the User About Session End** (stop_mode ONLY):

   IF stop_mode != true: SKIP (mid-loop consolidations recur every few
   iterations — emailing each one floods the user's inbox, and the subject
   would include a changing goal count so notify-user's 30-min rate limiter
   cannot dedupe. Progress reporting mid-session is the job of
   /agent-completion-report Phase 5.5, not consolidation.)

   ELSE (session is actually ending — /stop or productivity-stop-gate):
     Notify the user about the session end.
     (Check `world/forged-skills.yaml` for a skill whose triggers match
     "notify the user" and invoke it with:
     - subject: "Session ended — <goals_completed_this_session> goals closed"
     - message: a concise wrap-up — goals completed, aspirations
       completed/archived, tree nodes encoded, knowledge debt delta, any
       blockers surfacing in the handoff, and the fact that the loop has
       stopped (user can resume via /start).

     If no matching skill is registered, fall back to a
     `participants: [agent, user]` goal via `aspirations-add-goal.sh`
     titled `"User Notice: Session ended"` with
     `origin_signal: "idea:session-end-notification"`. Never block
     consolidation on notification failure — the handoff and archive
     are already persisted.)

### Execution Checklist (MANDATORY)

Before proceeding to Step 10, output a checklist accounting for EVERY step.
Each step must show a value from its own inline `{…}` enumeration (the row
defines the allowed values for that row — don't invent new ones).
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
  Step 9  Phase Cost Report:       {done|skipped (no markers)}
  Step 9.5 Transfer Profile:       {done|skipped (file missing)}
  Step 9.7 Notify User (stop_mode): {done|skipped (mid-loop)|skipped (no matching forged skill)}
```

10. Restart Loop Cycle (skip in stop_mode):
    IF stop_mode == true:
        # Message is emitted via the Bash echo itself — never as a preceding
        # text step. Return-protocol: last action must be a tool call.
        Bash: echo "Consolidation complete (stop mode — no restart)."
        RETURN
    # INTENTIONAL RECURSIVE CALL: aspirations → boot → aspirations.
    # Context compression prevents unbounded growth. Do not "fix" this cycle.
    Invoke /boot
    Boot detects handoff.yaml → continuation mode (abbreviated report → fast loop handoff).
```

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
Normal path: `Invoke /boot` (Skill call) is the terminal action. Stop-mode path:
the `Bash: echo ...` in Step 10 above IS the terminal action — the message is
emitted from inside the Bash call, not as a preceding text step.
