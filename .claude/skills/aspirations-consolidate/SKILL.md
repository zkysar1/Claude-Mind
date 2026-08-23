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

- `goals_completed_this_session` (int) — close-EVENT count for the session,
  passed by `/aspirations` (SKILL.md L727/L735). The orchestrator derives it
  from `loop_state.goals_completed`, NOT from working memory.
- `session_count` (int), `evolutions_this_session` (int) — passed in the same
  two calls.

  These three were passed by the caller but UNDECLARED here until g-115-4935,
  and that gap is the root cause of the defect it fixes. Undeclared, Step 8.87
  reached for a working-memory field that merely SHARES THE NAME
  `goals_completed_this_session` — a top-level WM key (wm.py TOP_LEVEL_KEYS)
  holding a LIST, not this int. That read also lands AFTER Step 5's wm-reset,
  which returns the list to its `[]` template value because the only top-level
  field surviving reset is SESSION_IDENTITY_FIELDS = {"session_start"}. So the
  team-state field published a stale prior-session figure: measured 2026-08-04
  (alpha, cc-04) at 125 where the true count was 270 — and 270 is exactly what
  `loop_state.goals_completed`, i.e. this parameter, already held.

  Prefer this parameter over any working-memory read for the session count.
  The two rejected alternatives, recorded so the choice is not silently
  re-litigated: adding the WM field to SESSION_IDENTITY_FIELDS would make it
  survive until `wm clear-identity` at /stop D4.5, so a session ending without
  a graceful stop leaks its count into the next one — reproducing the same
  stale-count symptom by another route. Recomputing from the world store via
  `completed_by` would change what the field MEANS (currently-completed goals
  rather than close events; measured 255 vs 270 on the same session, the gap
  being recurring-goal closes) and is a deliberate semantic change, not a bug
  fix.

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
   # KEEP `merged_goal_ids` from this summary — Step -0.9 consumes it, and this
   # is the ONLY moment it can be read. See that step for why.

-0.9. WORKER-GOAL RETROSPECTIVE (Phase B, g-306-198):
   # A WORKER Body executes goals but never runs the reducer-only close phases,
   # so a goal it completed reaches the shared store with `outcome_note` written
   # and every reducer lane (the script's `run_lanes`) simply ABSENT, with
   # nothing downstream to fill them. This step fills them.
   #
   # RUN IT NOW OR NOT AT ALL. `merged_goal_ids` names the completed goals that
   # arrived from a Body, and it is derivable ONLY here: the WM rows carry no
   # session id, `claimed_by`/`claimed_by_sid` are erased at close (0 of 4015
   # completed goals carry either — deliberate, g-115-3176), and
   # body-manifest.yaml records no goal list. Re-running generalize-down does
   # NOT recover them: the Bodies are already marked merged, so the second run
   # returns an empty list. If this step is skipped, those goals keep their
   # missing lanes permanently.
   IF the Step -1 summary's `merged_goal_ids` is empty:
       SKIP — no Body contributed a completed goal this merge (the dormant
       single-runner case, and the common one). Continue to triage.
   ELSE:
       Bash: py -3 core/scripts/worker_retrospective.py --agent "$MIND_AGENT" \
               --goal-ids "{comma-joined merged_goal_ids}" --apply --output json
       # (`--from-merge-summary <path|->` accepts the generalize-down JSON
       #  directly when a scripted caller has it on disk or on stdin.)
       # Idempotent: each goal is marked `retrospective_encoded` on its own
       # record once its lanes land, and a marked goal is never re-run. The
       # marker is deliberately WITHHELD when every lane failed, so a transient
       # store fault retries next pass instead of being recorded as done.
       # Parse the JSON. Log "▸ WORKER-RETROSPECTIVE: {planned} planned,
       #   {len(applied)} applied, {len(skipped)} skipped".
       # On a non-zero exit: log and CONTINUE to triage (fail-open — a
       # retrospective failure must never block consolidation).
   #
   # THE THREE LANES IT DOES NOT RUN ARE YOURS. The script calls every lane
   # writer taking a goal id; read `run_lanes` for the list, never a count here
   # (this one said "four" through two additions). It reports
   # `pending_judgment_lanes`
   # — verification, execution_feedback, user_notable — and does NOT fill them:
   # their writers exist but consume LLM RATINGS, and a script supplying its own
   # scores would fabricate the measurement the lane records (the same reason
   # state-update-audit SKIPS the imp@k snapshot on an unmeasured close rather
   # than writing a false 0.0, g-115-2441). For each applied goal, read its
   # `outcome_note` and decide those three yourself — post-hoc verification,
   # a spec-quality rating via `state-update-audit.sh execution-feedback`, and
   # whether the user would want to know.

0.1. CONSOLIDATION TRIAGE GATE:
   # This logic is duplicated in core/scripts/consolidation-precheck.py.
   # If you change the checks here, update that script to match. Parity on the
   # unreflected field is MEASURED, not assumed (g-115-6173): both instruments
   # must yield the SAME count on one store snapshot — the script mirrors the
   # endpoint's union (live+archive, dedup by id, live wins) and both apply
   # the shared core/scripts/_reflectable.py filter.
   # ── PRE-SCAN (2 script calls + 1 file check) ────────────────────────
   triage_wm      = Bash: wm-read.sh --json
   # g-115-4878: was `--unreflected --counts`, which returned the stage-counts
   # object (branch precedence: counts is tried before unreflected) and carried
   # NO `active_unreflected` key on any response shape. So unreflected_count was
   # permanently 0 for every agent on every session end, silently, and the `# 0
   # if none` comment is how it hid -- a permanent zero reads as "none exist"
   # rather than "this key never existed". The endpoint now REFUSES the pair
   # with 400 ambiguous_selectors instead of answering the wrong one.
   triage_unrefl  = Bash: pipeline-read.sh --unreflected
   triage_overflow = test -f agents/<agent>/session/overflow-queue.yaml

   # ── EXTRACT COUNTS ──────────────────────────────────────────────────
   micro_count       = len(triage_wm.slots.micro_hypotheses)     # null/[] → 0
   encoding_count    = len(triage_wm.encoding_queue)              # null/[] → 0
   debt_count        = len(triage_wm.slots.knowledge_debt)        # null/[] → 0
   conclusions_count = len(triage_wm.slots.conclusions)           # null/[] → 0
   violations_count  = len(triage_wm.slots.recent_violations)     # null/[] → 0
   # g-115-6173: count the REFLECTABLE subset, not the raw array length. The
   # g-115-5358 widening made the raw length the truthful never-reflected
   # BACKLOG (measured 2026-08-14: 383 = 181 UNRESOLVABLE + 150 EXPIRED + 47
   # no-outcome + 5 reflectable) — dominated by records /reflect can never
   # learn from (g-115-4558), so len() here kept data_total nonzero forever
   # and the lean fast path was structurally dead. Filter SSOT:
   # core/scripts/_reflectable.py (outcome in {CONFIRMED, CORRECTED}).
   unreflected_count = count of triage_unrefl records where str(outcome).upper() in {"CONFIRMED", "CORRECTED"}   # reflectable only; [] → 0
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
   # g-115-6173: gate on the REFLECTABLE subset (outcome CONFIRMED/CORRECTED —
   # SSOT core/scripts/_reflectable.py), not array non-emptiness. The widened
   # array carries a permanent floor of structurally-unreflectable records
   # (UNRESOLVABLE/EXPIRED/no-outcome), so a bare existence check would invoke
   # the sweep on every full consolidation forever with nothing to reflect on.
   IF any record has outcome in {CONFIRMED, CORRECTED}:
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
   # The slot crosses the Step-5 wm-reset boundary BY DESIGN: wm.py
   # RESET_SURVIVING_SLOTS exempts it (g-115-1992 — reset previously wiped it,
   # so Step 9 always read null on the full path and silently fell back to the
   # linear summary).
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
          verify = bash core/scripts/tree-read.sh --node {item.target_node_key}
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
       Read core/config/tree.yaml for split_threshold
       Decompose is STRUCTURAL, not line-count (g-306-13; board msg-20260619-075228-bravo-086).
       Do NOT compute a line count and do NOT set growth_state ready_to_decompose:
       tree.py get_decompose_candidates selects on leaves-under-node >
       K_max^(D_retrieval-1) and never reads decompose_threshold, so a line-count
       flag is INERT — it writes a field no reader acts on. Ask the tool instead:
         bash core/scripts/tree-read.sh --decompose-candidates
         If the node is listed: Invoke /tree maintain
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

           # NULL-KEY LANE (g-115-5150) — mirrors encode-session Lane 1.6.
           # The node-update check above CANNOT fire without a node_key, and
           # null is the MAJORITY shape rather than an anomaly: /respond Step 6
           # files debt precisely when a correction has BROADER implications
           # than any single node the _tree.yaml scan found, so "no one node" is
           # the DESIGNED common case here. Such entries fall through to the
           # carry-forward increment on every sweep until the ceiling below
           # DISCARDS them. Measured 2026-08-06 (alpha): all five live entries
           # had node_key null, all HIGH, all at sessions_deferred 6 of 10.
           IF debt.node_key is null or empty:
               Extract the first goal id matching g-\d+-\d+ from debt.reason
                 (also debt.routed_goal / debt.source_goal if present).
               IF found:
                   # asp-<NNN> is derived from g-<NNN>-<NN>. aspirations-read.sh
                   # --id takes an ASPIRATION id; handing it a GOAL id returns
                   # {"error":"not_found"}, which reads like "goal is gone".
                   Bash: bash core/scripts/aspirations-read.sh --id asp-<NNN>
                   Locate the goal in goals[] and read status.
                   IF status == "completed":
                       Mark resolved, resolution_method = "auto_resolved_by_routed_goal"
                       Log: "KNOWLEDGE DEBT RESOLVED (null-key): routed goal {gid} completed — {reason}"
                       skip
               # Fall through when no goal id, or the goal is not yet completed.
               # NEVER resolve a null-key debt on AGE alone: a debt whose
               # condition is still TRUE must stay open. Age is not evidence the
               # gap was filled.

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
               # DURABLE DROP FIRST (g-115-5150) — mirrors encode-session.
               # This is a DISCARD, not a resolution: the gap is still open and
               # nobody was told. The Log line dies with the session, and for
               # the null-key majority it rendered as "DROPPED: null", naming
               # nothing recoverable. Preserve the reason BEFORE removing.
               Bash: echo '{"entry_type":"observation","content":"KNOWLEDGE DEBT DROPPED (ceiling {sessions_deferred}): node_key={node_key or \"null\"} priority={priority} source_goal={source_goal} — {reason}"}' | bash core/scripts/execution-diary.sh append
               IF priority == "HIGH":
                   # 10 sweeps failed to resolve a HIGH debt — a finding about
                   # the resolver, not only about this entry.
                   Bash: echo '{"title":"Investigate: HIGH knowledge debt hit the max-defer ceiling","description":"Dropped at sessions_deferred={N}. node_key={node_key or null}. source_goal={source_goal}. Verbatim reason, preserved because the entry is being discarded: {reason}","priority":"MEDIUM","participants":["agent"],"category":"framework-maintenance","tags":["knowledge-debt","max-defer-drop"],"origin_signal":"investigate:knowledge-debt-ceiling"}' | bash core/scripts/aspirations-add-goal.sh --source world asp-115
               Log: "KNOWLEDGE DEBT DROPPED: {node_key} — {reason} (deferred {sessions_deferred} sessions, ceiling reached; reason preserved to execution-diary)"
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
       Decompose is STRUCTURAL, not line-count (g-306-13; board msg-20260619-075228-bravo-086).
       Do NOT compute a line count and do NOT set growth_state ready_to_decompose:
       tree.py get_decompose_candidates selects on leaves-under-node >
       K_max^(D_retrieval-1) and never reads decompose_threshold, so a line-count
       flag is INERT — it writes a field no reader acts on. Ask the tool instead:
         bash core/scripts/tree-read.sh --decompose-candidates
         If the node is listed: Invoke /tree maintain
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

   Do NOT source the session goal count from this read (g-115-4935). It
   arrives as the `goals_completed_this_session` PARAMETER — see ## Parameters
   — which is immune to the reset below. Step 8.87 and Step 9.7 both use that
   parameter.
5. Bash: wm-reset.sh
   # journal_cluster_summaries survives this reset (wm.py RESET_SURVIVING_SLOTS,
   # g-115-1992) — Step 9 consumes it one-shot and clears it.

5.5. Presence file truncation (g-115-411 — cross-agent presence). For each
   `world/presence/<agent>.jsonl`, keep the last 1000 entries via locked
   rewrite. Bounds storage to ~140KB per agent at the 1000-entry cap
   (~672KB/day write rate is what motivates the truncation). Dormant when
   the PostToolUse * hook is not wired — the loop is idempotent on empty.

   THIS STEP NEVER TRUNCATED ANYTHING, ON ANY BOX, FROM g-115-411 UNTIL
   2026-08-07 (g-115-5023, filed from ZDS g-001-347). Three compounding
   defects, each individually silent, which is why it survived:
     1. It read `os.environ['CORE_ROOT']`, but `_paths.sh:18` ASSIGNS
        CORE_ROOT without exporting it — line 19 directly below exports
        PROJECT_ROOT and even comments why. So the child `py` raised
        KeyError even in a shell where $CORE_ROOT was set and correct.
     2. `2>/dev/null` swallowed the traceback.
     3. `|| true` forced rc=0, and there was no &&-gated success line, so
        the command COULD NOT FAIL. An observer narrating success was
        narrating rc=0, never truncation.
   Measured verbatim on three boxes/two platforms before the fix — cc-06
   Linux 4,916,690 B / 30,411 lines; zak-win MSYS2 2,919,977 B / 18,067;
   cc-07 Linux 1,172,356 B / 7,183 lines, unchanged by a run that returned
   rc=0. The 1000-line bound had never held anywhere.

   Values now pass through the ENVIRONMENT and the Python source is
   SINGLE-quoted, so bash expands nothing inside it (guard-165) — the old
   line interpolated `pathlib.Path('$_PRES')` straight into the source,
   which was a parse-injection surface as well as unreadable. stderr is no
   longer suppressed, and a failure now emits a WARN naming the file. Do
   NOT reintroduce `2>/dev/null` or `|| true` here: the whole defect above
   is that the error path rendered identically to success, so no amount of
   reading the output could ever have found it.

   Bash: for _PRES in "$WORLD_DIR"/presence/*.jsonl; do [ -f "$_PRES" ] || continue; CORE_ROOT="$CORE_ROOT" PRES="$_PRES" py -3 -c 'import sys,os,pathlib; sys.path.insert(0, os.environ["CORE_ROOT"]+"/scripts"); from _fileops import locked_modify_jsonl; locked_modify_jsonl(pathlib.Path(os.environ["PRES"]), lambda recs: recs[-1000:])' || echo "[consolidate] WARN: presence truncation FAILED for $_PRES — file left unbounded"; done; unset _PRES

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
           type: <utility|analytical>   # REQUIRED (g-115-3131)
           source: "experience-mining"
           evidence_experiences: [list of experience IDs in cluster]
         # `type` gates the forge developmental bar, so an absent value hands
         # that decision to a default rather than to you. Classify against
         # core/config/skill-gaps.yaml gap_types: utility = mechanizes an
         # ALREADY-DERIVED procedure (deterministic steps, known in->out) ->
         # CALIBRATE; analytical = the OUTPUT needs domain-mature judgment
         # (pattern recognition, evaluation) -> EXPLOIT, the higher bar.
         # Unsure -> write `utility` (it IS the default, so stating it costs
         # nothing and keeps the gate's input meaningful rather than absent).
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
   goals_this_session = the `goals_completed_this_session` PARAMETER (g-115-4935).
   # Do NOT read it from working memory here. The WM key of the SAME NAME is a
   # different object (a list, not this int) and Step 5's wm-reset has already
   # returned it to `[]`, so a read here publishes an empty or stale count to
   # every partner that reads team-state. See ## Parameters for the measurement
   # and for the two rejected alternatives.
   # NOT affected by the same reset, verified 2026-08-10: `current_focus` below
   # is the literal "session ended", and `blocked_data` comes from the live
   # `goal-selector.sh blocked` call — neither reads working memory. Step 9's
   # handoff reads `journal_cluster_summaries`, which survives reset by design
   # (wm.py RESET_SURVIVING_SLOTS), so it is unaffected too.

   # Gather blocked data for critical path (used by both Step 8.87 and Step 9)
   Bash: goal-selector.sh blocked
   Parse JSON → blocked_data

   # WHOLE-ROW HAZARD — set NESTED LEAVES, never the bare row path (guard-2769,
   # g-115-5079). `--field agent_status.<AGENT_NAME>` IS the row path, and the
   # row path REPLACES: mind_api/src/world/team_state_write.py:200-201 does
   # `row = dict(parsed)` when the subpath is empty, so a 3-key value leaves a
   # 3-key row and every key you did not name is gone. A nested-leaf subpath
   # takes the other branch (`_set_nested`, :202-203) and is additive. Both
   # print "Updated agent_status.<agent>", so the destructive write's success
   # message is IDENTICAL to the correct one — nothing surfaces the loss.
   # Measured 2026-08-05 (alpha, hostname cc-04) running the prior partial-dict
   # form: `beliefs` (4 entries — the Theory-of-Mind store the Phase 0-pre.0a
   # contradiction detector reads, carrying prior_confidence / revised_at
   # bi-temporal history) and `last_fresh_eyes_run` were destroyed in one call.
   # Re-measured 2026-08-06 (zeta, cc-02) against a live 11-key row: the same
   # form would have dropped SIX keys, not two — the other four (in_flight,
   # live_phase, current_focus_updated_at, session_ended) happen to be re-set by
   # other writers, which is why only two showed up as durable loss.
   # Do NOT collapse these back into one call, and do NOT &&-chain a sidecar
   # (board-post, log) onto them — guard-409.
   FOR EACH (leaf, value) in [("last_active",            "<now>"),
                              ("current_focus",          "session ended"),
                              ("session_goals_completed", <goals_this_session>)]:
       Bash: team-state-update.sh --field agent_status.<AGENT_NAME>.<leaf> --value '<value>'

   # MANDATORY re-read. Recovery is only possible if you NOTICE, and the write
   # itself will never tell you (guard-2769 step 3, guard-2305).
   Bash: team-state-read.sh --field agent_status.<AGENT_NAME> --json
   ASSERT the returned row still carries BOTH `beliefs` AND `last_fresh_eyes_run`.
   IF either key is absent:
       # The pre-write copy-on-write snapshot in world/.history is the recovery
       # layer. Use DIFF, not restore — restore is in-place and would clobber the
       # session_summary this step just wrote.
       Bash: bash core/scripts/history-list.sh "world/team-state/agents/<AGENT_NAME>.yaml"
       Bash: bash core/scripts/history-diff.sh "world/team-state/agents/<AGENT_NAME>.yaml" "<version-name>"
       Re-set ONLY the lost leaves using the nested-leaf form above, then re-read again.
   Output: "▸ Team state: updated agent status (session ended, {goals_this_session} goals; beliefs + last_fresh_eyes_run verified intact)"

   IF blocked_data.bottlenecks is non-empty:
       critical_blockers_payload = top 3 bottlenecks as JSON array with fields: goal_id, title, cause, downstream_count, updated_by, updated_at
       Bash: team-state-update.sh --field critical_blockers --value '<critical_blockers_json>'
       Output: "▸ Team state: updated critical blockers ({N} entries)"

8.9. Release Held Claims (BOTH queues):
   # Prevent stale claims when session ends normally. See coordination convention.
   # Status filter excludes terminal-status goals — claim-clearing on completion
   # is enforced in cmd_complete_by/cmd_update_goal; this filter is defense in
   # depth so any future writer regression can't flood the release loop.
   #
   # BOTH QUEUES, AND THE SOURCE IS THREADED (g-306-258, from fresh-eyes-code F2
   # msg-20260807-060726-zeta-5173). This step was world-only in two independent
   # ways, either sufficient alone to strand a claim: it discarded the agent rows
   # at the FOR EACH, and it passed no --source so the wrapper's `world` default
   # applied. Both were CORRECT BY CONSTRUCTION until g-306-238 taught claim() to
   # accept &source=agent — agent-queue goals carried no claims before that, so a
   # world-only predicate was complete. The gate's correct operation is what made
   # the gap invisible (guard-1802 shape, arriving through a SCHEMA change, which
   # is why no predicate diff would have surfaced it).
   #
   # WHY THIS IS THE WORST SITE TO HAVE MISSED: it is the SESSION-END sweep, and
   # its whole job is to stop claims outliving the session. The goals it could not
   # see are g-001-01..g-001-10, the recurring cadence — exactly the population
   # g-306-249's rationale names as the reason a release capability must exist. An
   # agent that stops while holding one left it stranded across sessions while
   # this step logged success. Note the mid-session net does NOT cover this:
   # stranded-claim-sweep.py runs at orchestrator Phase -0.5c.1 on loop RE-ENTRY,
   # and a session that is ending has no next re-entry. (That sweep is already
   # source-correct — /v1/aspirations/query reads BOTH queues and tags each row
   # with `source`, which is the same property the enumeration below relies on.)
   #
   # THIS WIDENING ENLARGED g-306-194's BLAST RADIUS — that goal is now CLOSED
   # and its guard is the block immediately below, so the "do not fix it inline
   # here" instruction this comment used to carry is RETIRED rather than merely
   # stale. Kept as history because the reasoning still binds the next editor:
   # the widening made this step reach agent-queue claims too, and under the
   # Mind/Body split a worker Body runs as the same agent name against the same
   # synced agent queue, so a second live holder is a real configuration and not
   # a hypothetical. The over-release is aggravated by g-306-260 (release()
   # never resets status, so a wrongly-released in-progress goal is unclaimed
   # AND unselectable until someone flips it) — which is why the guard below
   # errs toward SKIPPING rather than toward releasing.
   # ── FOREIGN-SID GUARD (g-306-194) ─────────────────────────────────────────
   # Release ONLY claims held by THIS session. The query keys on the AGENT NAME
   # alone, and under the Mind/Body split a worker Body runs as the same agent
   # name against the same synced queues — so "another live instance of me" is a
   # standing configuration, not an edge case. Measured 2026-08-07: this query
   # returned 53 rows for alpha while a worker Body on another box held ~40 of
   # them. Releasing those is a silent cross-box theft, aggravated by g-306-260
   # (release() never resets status, so the victim is left unclaimed AND
   # unselectable — the over-release does not self-heal).
   #
   # THE GUARD IS `== MY SID`, NEVER `!= running-session-id`, and that is
   # load-bearing: running-session-id is BOX-LOCAL, so the `!=` form is ALSO true
   # of a genuine live peer and causes the very defect it targets. Skipping every
   # not-mine claim needs no liveness inference at all and cannot fail in the
   # dangerous direction — a stale claim left behind is collected by
   # stranded-claim-sweep.py at the next session's Phase -0.5c.1, which OWNS the
   # foreign-SID decision and its 120m grace, whereas a wrongly-released peer
   # claim is destroyed with nothing to restore it. Do NOT "improve" this into a
   # liveness test here; that would be a second, competing foreign-SID policy.
   #
   # A SECOND READ IS REQUIRED — the query cannot answer this (measured
   # 2026-08-07). aspirations-query.sh projects exactly
   # {goal_id, asp_id, source, status, title, category} and omits BOTH
   # claimed_by and claimed_by_sid. That omission is deliberate ("the query
   # endpoint omits both — query is identity info only",
   # stranded-claim-sweep._read_goal_claim_fields), so the guard is not a filter
   # over the rows below; it needs the active aggregate, which carries every
   # field. One read per SOURCE, not one per GOAL: the sweep pays N aggregate
   # walks because it inspects a handful of goals, while this step runs over
   # every claim the agent holds.
   Bash: MIND_AGENT={agent} aspirations-query.sh --goal-field claimed_by {agent_name} --goal-status pending,in-progress,blocked
   FOR EACH distinct source among the returned rows:
       Bash: aspirations-read.sh --source <source> --active
       → sid_map[goal.id] = goal.claimed_by_sid   # for goals with claimed_by == {agent_name}
   FOR EACH returned goal:                      # NOT `WHERE source == "world"`
       held_sid = sid_map.get(goal.goal_id)
       IF held_sid AND held_sid != $MIND_SID:
           Log: "Skipped {goal.goal_id} — held by session {held_sid[:8]}, not this one ({MIND_SID[:8]})"
           continue
       # A NULL held_sid falls through to release DELIBERATELY: claims predating
       # g-115-3176 carry no sid, and preserving their pre-existing behaviour is
       # the same legacy rule _read_goal_claim_fields applies.
       Bash: aspirations-release.sh <goal-id> --source {goal.source}
       Log: "Released claim on {goal.id} ({goal.source} queue)"
       # ── PAIRABLE RELEASE POST (g-306-194, guard-1610) ─────────────────────
       # WORLD ONLY, mirroring _announce_release's own scope decision: an
       # agent-source queue is private, so a post about one is pure noise on the
       # shared channel. The RELEASE above still covers BOTH queues (g-306-258) —
       # announce scope and release scope deliberately differ, do not unify them.
       IF goal.source == "world":
           echo "RELEASING {goal.goal_id} -- returned to the world queue as pending because the session holding it ({MIND_SID[:8]}) is ending. Any agent may claim it now." \
             | Bash: board-post.sh --channel coordination --type release --tags "{goal.goal_id},{agent_name}"
   # WHY PER-GOAL AND WHY THIS EXACT SHAPE. The old single post — "Session
   # ending: released all held claims", --type status, no tags — matched NONE of
   # goal-pickup-coordination-check._released_ids' three legs: not
   # type=="release"+goal-id tags, not the ^RELEASING <id> text prefix, not a
   # release-marker tag paired with id tags. Every claim it released therefore
   # stayed an unpaired lien, and because a post EXISTED, a reader checking "did
   # we announce?" saw yes — worse than silence. The type, the tags and the
   # "RELEASING <id>" prefix are each independently load-bearing and are bound to
   # the REAL consumer by core/scripts/tests/test_release_announce_pairing.py.
   # Change any one of them and that test goes red.

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
   reasons, `session_summary.key_outcomes`, decisions_locked) plus the structured fields
   already gathered above (phase_cost_report, goal-selector output,
   blockers, debts, user_goals), then pipes it to `handoff-yaml-build.sh`
   which validates required fields, defaults optionals, and writes the
   handoff.yaml atomically via `locked_write_yaml`.

   `key_outcomes` sourcing (BRD Gap 1d, g-306-09): read the
   `journal_cluster_summaries` WM slot written by Step 0.65. The slot survives
   Step 5's wm-reset by design (wm.py RESET_SURVIVING_SLOTS, g-115-1992), so a
   null read here means Step 0.65 genuinely didn't run — not the reset wiping
   it. Each cluster's `summary` becomes one `key_outcomes` entry (prefixed with
   its `label`), so the handoff carries the PER-CLUSTER journal summaries —
   topical and granular — rather than one linear blob. IF the slot is
   null/empty (lean session — the lean fast path skips Step 0.65; on the full
   path even a < 4-entry session writes a single "session" cluster), fall back
   to the prior single linear key_outcomes list. Clear the slot after reading
   (one-shot; next session's Step 0.65 re-populates it).

   **FIELD PATH — write it NESTED, at `session_summary.key_outcomes` (g-115-3385).**
   NOT top-level `key_outcomes`. Three sources agree the nested path is canonical:
   the schema example in `core/config/conventions/handoff-working-memory.md`, the
   only consumer (`boot/SKILL.md` auto-continuation status line, which renders
   `{session_summary.key_outcomes}`), and the "prior single linear key_outcomes
   list" named in the fallback above — that prior list was already the nested one,
   so the cluster summaries REPLACE its contents rather than creating a new
   top-level field. `handoff-yaml-build.py::_assemble()` is a fixed 17-key
   allowlist that carries `session_summary` through WHOLE but has no top-level
   `key_outcomes` slot, so a top-level emission is silently discarded: it
   validates cleanly, writes 17 fields, reports `flags: []`, and the per-cluster
   summaries never reach the next session. That is not hypothetical — it happened
   on the 2026-07-26 consolidation. The builder now reports any unrecognized
   top-level key in `dropped_keys` plus a stderr WARN, so a repeat is loud rather
   than silent, but the correct emission is nested in the first place.

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
     from `world/aspirations.jsonl`. Source `cause` by STATUS:
     - `status: pending` with non-null `defer_reason` → quote
       `defer_reason` verbatim.
     - `status: blocked` → compose from the fields that actually exist on
       a blocked record. Measured 2026-08-11 across all 5 live blocked
       goals: `blocked_since` 5/5, `blocked_by` 4/5, `blocker_ref` 2/5,
       `defer_reason` 0/5. Prefer, in order:
         1. `blocker_ref.why` (dict form only — the richest text);
         2. `blocker_ref.type` + `.external_id` (e.g.
            `partner-response:g-326-118`);
         3. `blocked by <blocked_by joined>` — the most widely populated;
         4. `blocked since <blocked_since>` — universal, so this arm
            always yields a quotable string.
     - **`blocker_ref` HAS TWO SHAPES AND BOTH ARE LIVE.** It is a dict
       (`type`/`external_id`/`unblock_goal`/`why`/`created_at`/`expires_at`)
       on some records and a BARE STRING holding an unblocking goal id on
       others — both measured on the same day. Test the type before
       subscripting; on a string, treat it as `blocked_by` and use arm 3.
     - **A `status: blocked` goal IS a blocker. Always list it.** Never
       drop one for want of a cause string. The previous wording named
       `blocked_reason`, WHICH IS NOT A FIELD ON GOAL RECORDS (0 of 4,208
       asp-115 records carry the key), and paired it with "if both are
       null/empty, the goal is NOT a blocker — do not list it". Those two
       clauses composed into silent under-reporting in the healthy-looking
       direction: a compliant author found every blocked goal "empty",
       listed none, and the handoff read as an unblocked frontier
       (g-115-3361). Fixing only the field NAMES would leave that escape
       clause live and re-break on the next schema change, so the default
       is inverted here: status decides whether a goal is listed, and the
       cause string is best-effort.
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
   - Emit `critical_path: {}` ONLY when no goal has a non-null
     `defer_reason` AND no goal has `status: blocked`. An empty object is
     the correct representation of "nothing blocks the frontier", not a
     fabricated placeholder — but it is also the single most dangerous
     value in this payload, because `/boot` Step 0.5 sub-step 4c shows it
     to the resuming session as an all-clear. Before emitting `{}`, COUNT
     the blocked goals (`aspirations-query.sh --goal-status blocked`); a
     non-zero count means `{}` is wrong no matter how empty the cause
     fields look. This is the check that would have caught g-115-3361.

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
     - subject: "Session ended — <goals_completed_this_session parameter> goals closed"
       # The PARAMETER, not a working-memory read (g-115-4935). Same post-reset
       # staleness as Step 8.87, and NOT among the siblings that goal named —
       # it is the most user-visible one, since the count lands in the subject
       # line of the session-end email at every /stop.
     - message: a concise wrap-up — goals completed, aspirations
       completed/archived, tree nodes encoded, knowledge debt delta, any
       blockers surfacing in the handoff, and the fact that the loop has
       stopped (user can resume via /start).

     The message MUST be a real multi-line body built via
     `core/scripts/notify-build-payload.py` (notify-user Step 2). NEVER
     hand-write the email-send.sh JSON here — the 2026-07-07 delta stop
     email delivered as title + border + EMPTY body because a Title-only
     payload was hand-built at the transport: the SendInfoAlert renderer
     IGNORES InfoMessage whenever Title is present (structured mode renders
     Body/Sections only). email-send.sh now refuses bodyless payloads
     (exit 2, empty-body guard); on refusal use the fallback below — do
     not retry with a thinner payload.

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
