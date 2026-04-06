---
name: aspirations-state-update
description: "State Update Protocol — 9 steps + Step 3.5 Team State + Step 8.5 Actionable Findings Gate + Step 8.75 Execution Reflection + Step 8.76 Skill Quality + Step 8.11 Execution Feedback after every goal execution (routine: Steps 1-4 + abbreviated journal; deep: all steps, immediate tree encoding)"
user-invocable: false
parent-skill: aspirations
triggers:
  - "run_state_update()"
conventions: [aspirations, tree-retrieval, goal-schemas]
minimum_mode: autonomous
---

# State Update Protocol

Invoked after EVERY goal execution as Phase 8 of the aspirations loop. Accepts `outcome_class` (default: `"deep"`).

## Inputs (from orchestrator)

- `outcome_class`: Outcome tier (`"routine"` or `"deep"`) — default `"deep"`
- `source`: Queue origin (`"world"` or `"agent"`) — pass `--source {source}` to all `aspirations-*.sh` calls

For **deep** outcomes (default — all non-routine goals): all steps run (1-8, 8.5, 8.75). Step 8 performs IMMEDIATE tree encoding — the full precision manifest, curator quality gate, consistency scan, and tree write. This is the highest-fidelity path. Learning is the mission — every non-routine outcome gets immediate encoding.

For **routine** outcomes (recurring goal, no findings): Steps 1-4 run (bookkeeping), then an abbreviated Step 7 (journal), then RETURN. Steps 5-8.75 are skipped because there is genuinely no insight to encode, no triggers to check, and no capability to propagate.

The loop MUST NOT continue to Phase 9 until state update is complete.

---

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## State Update Protocol

After EVERY goal execution (Steps 1-8, plus Steps 8.5 and 8.75 for deep outcomes):

```
1. UPDATE goal status via Bash: `aspirations-update-goal.sh --source {source} <goal-id> status <status>`
   - `aspirations-update-goal.sh --source {source} <goal-id> completed_date <today>` if completed
   - `aspirations-update-goal.sh --source {source} <goal-id> achievedCount <N+1>`
   - Update streak counters via additional update-goal calls
   - If recurring: compute elapsed = hours_since(lastAchievedAt) BEFORE updating.
     Bash: `aspirations-update-goal.sh --source {source} <goal-id> lastAchievedAt "$(date +%Y-%m-%dT%H:%M:%S)"`
     If elapsed > 2 * interval_hours: new_streak = 1.
     Otherwise: new_streak = currentStreak + 1.
     ALWAYS update both: currentStreak = new_streak, longestStreak = max(new_streak, longestStreak).

2. Aspiration progress is updated automatically by update-goal when status changes

3. UPDATE last_session via Bash: `aspirations-meta-update.sh --source {source} last_updated <today>`
   - Append goal completion to goals_completed_this_session in working memory:
     # Keys read by goal-selector.py streak_momentum + recurring_saturation — do not rename
     Bash: echo '{"goal_id":"<goal-id>","aspiration_id":"<aspiration-id>","recurring":<true|false>}' | wm-append.sh goals_completed_this_session
   - Set aspiration_touched_last in working memory:
     Bash: echo '"<aspiration-id>"' | wm-set.sh aspiration_touched_last
   - Set current_goal_source in working memory:
     echo '"{source}"' | Bash: wm-set.sh current_goal_source
   - Set last_goal_category = goal's resolved category (from goal-selector.py output):
     Bash: echo '"<category>"' | wm-set.sh last_goal_category

3.5. TEAM STATE UPDATE (multi-agent situational awareness)
   # Update world/team-state.yaml with this goal completion.
   # Gives the other agent instant awareness of what just happened.
   Bash: team-state-update.sh --field recent_completions --operation append \
     --value '{"goal_id":"<goal-id>","title":"<goal.title>","completed_by":"<AGENT_NAME>","completed_at":"<now>","key_finding":"<one-line factual summary of what was produced/discovered>"}'
   Bash: team-state-update.sh --field agent_status.<AGENT_NAME>.last_active --value '"<now>"'
   Bash: team-state-update.sh --field agent_status.<AGENT_NAME>.current_focus --value '"<goal.category or aspiration title>"'
   # Increment session goal count in working memory (read current, +1)
   # The actual agent_status.session_goals_completed is updated at consolidation

4. INCREMENT session_count via Bash: `aspirations-meta-update.sh --source {source} session_count <N>`
   (Note: session_count increments once per /aspirations loop invocation,
    NOT once per goal. Goals within the same loop share a session.)

# ── Routine outcome learning path ──────────────────────────────────
# Principle: we are here to learn. Even routine outcomes deserve a creative lens.
# This replaces the bare early return with lightweight but meaningful reflection.
IF outcome_class == "routine":

    # Step 5r: Lightweight creative reflection (NOT skipped for routine)
    # Ask ONE creative question from the creative_lens template, rotating
    # deterministically so each routine execution gets a different question.
    Read core/config/reflection-templates.yaml → creative_lens.questions
    question_index = hash(goal.id) % len(creative_lens.questions)
    creative_q = creative_lens.questions[question_index]
    Evaluate: {creative_q} applied to this routine goal's outcome
    IF creative reflection produces a non-trivial insight (not a restatement of known facts):
        # Capture as a lightweight reasoning bank entry
        Bash: reasoning-bank-read.sh --category {goal.category}
        IF NOT semantically overlapping with existing entries:
            Create reasoning bank entry via reasoning-bank-add.sh:
              title: "Routine insight: {concise name}"
              type: success
              category: goal's category
              content: the creative insight
              when_to_use: when this insight applies
              source_goal: goal.id
              tags: ["routine-creative-insight"]
            Log: "ROUTINE CREATIVE INSIGHT: {title} from {goal.id}"
        routine_insight_found = true
        routine_insight_text = the creative insight (1-2 sentences)
    ELSE:
        routine_insight_found = false
        routine_insight_text = "No new insight."

    # 7r. WRITE enhanced journal entry
       - Append to <agent>/journal/YYYY/MM/YYYY-MM-DD.md:
         "## {timestamp} — Routine: {goal.title}\nNo new items. Streak: {currentStreak}. Creative lens ({creative_q[:60]}...): {routine_insight_text}"
       - Update journal index via scripts (same merge/add pattern as full journal):
         - If session entry exists: pipe update JSON to `bash core/scripts/journal-merge.sh <session-num>`
         - If session entry does not exist: pipe new entry JSON to `bash core/scripts/journal-add.sh`

    # Step 8r: Routine accumulation check (every 5th routine for this goal)
    # Over time, accumulated routine executions can reveal trends worth encoding.
    IF goal.achievedCount >= 5 AND goal.achievedCount % 5 == 0:
        node=$(bash core/scripts/tree-find-node.sh --text "{goal.category}" --leaf-only --top 1)
        IF node found:
            Read node.file
            # Check: does the pattern of routine execution reveal a trend?
            # e.g., "always succeeds", "timing is consistent", "never produces findings"
            IF routine execution pattern reveals an encodable trend:
                Append one-line trend observation to node's Key Insights
                bash core/scripts/tree-update.sh --set <node.key> last_updated $(date +%Y-%m-%d)
                Log: "ROUTINE ACCUMULATION: {goal.category} trend encoded to {node.key}"

    RETURN  # Remaining deep-only steps (5-8.75) still skipped
# ── End routine outcome learning path ─────────────────────────────

5. CHECK evolution triggers — Read `core/config/evolution-triggers.yaml` (definitions) and `world/evolution-triggers.yaml` (state) and check performance-based triggers (see Phase 9 for full protocol). Fixed session-count trigger is DEPRECATED.
   # MR-Search exploration mode gating (Priority 3):
   # Exploration goals are shielded from negative evolution triggers.
   IF goal.execution_mode == "exploration":
       SKIP accuracy_drop and consecutive_losses trigger checks
       Log: "▸ Evolution triggers: SKIPPED (exploration mode — shielded from negative triggers)"
   # Standard goals: check all triggers normally

6. UPDATE readiness gates via Bash: `aspirations-meta-update.sh --source {source} readiness_gates '<JSON>'`
   - Re-run gate checks after any state change

7. WRITE journal entry
   - Append to <agent>/journal/YYYY/MM/YYYY-MM-DD.md
   - Format: "## {timestamp} — Goal: {goal.title}\nSkill: {skill}\nResult: {outcome}\nSpark: {spark_result}"
   - Update journal index via scripts (AUTHORITATIVE OWNER — only exception: /stop step 3.e for emergency cleanup):
     - If session entry exists: pipe update JSON to `bash core/scripts/journal-merge.sh <session-num>`:
       `echo '{"goals_completed":["g-001-01"],"key_events":["..."],"tags":["..."]}' | bash core/scripts/journal-merge.sh <session-num>`
     - If session entry does not exist: pipe new entry JSON to `bash core/scripts/journal-add.sh`:
       `echo '{"session":<N>,"date":"YYYY-MM-DD","journal_file":"<agent>/journal/YYYY/MM/YYYY-MM-DD.md"}' | bash core/scripts/journal-add.sh`
     - /boot Step 11 writes journal .md content only — it does NOT touch the journal index

8. REFRESH memory tree (dynamic lookup)
   - Find target node:
     node=$(bash core/scripts/tree-find-node.sh --text "{goal.category}" --leaf-only --top 1)
     # Returns: {key, score, file, depth, summary, node_type}
   - Navigate to node.file (use `Edit` to update — never `Write` on existing node files)
   # MR-Search episode chain encoding (Priority 1):
   # When episode_history is present, encode the PROGRESSION of understanding,
   # not just the final outcome. The chain shows how the agent's approach evolved.
   # episode_history is already in context from Phase 4-chain (set via aspirations-update-goal.sh --source {source}).
   IF goal has episode_history AND episode_history is non-empty (multi-episode goal):
       Include episode progression in tree encoding:
       - "## Episode Progression" section: summarize how approach evolved
       - Note which approach finally succeeded and why prior attempts failed
       - Extract transferable lesson: "For similar goals, start with approach N"
       Log: "▸ Tree encoding: including {len(episode_history)} episode progression"
   # Meta-strategy encoding guidance
   Bash: meta-read.sh encoding-strategy.yaml
   # Apply precision_emphasis_categories, narrative_compression_level,
   # and decision_rule_preference as advisory guidance for tree encoding.

   # ── Investigation encoding obligation ──────────────────────────────
   # Investigation/research/audit goals produce findings as their PRIMARY output.
   # Unlike development goals that change external state, investigation goals
   # produce understanding. The "new insight" gate must account for this:
   # findings in the execution trace ARE the insight, even if nothing external changed.
   is_investigation_goal = goal.title starts with (Investigate, Research, Audit, Analyze, Diagnose, Trace, Review)
                        OR goal.category in ("analysis", "diagnosis", "research")
   IF is_investigation_goal AND len(result_text) > 500:
       force_encoding = true
       Log: "▸ Investigation encoding obligation: forcing insight branch ({len(result_text)} chars)"
   ELSE:
       force_encoding = false

   # ── Encoding anti-drift override (set by orchestrator Phase 8.0.6) ──
   Bash: wm-read.sh force_tree_encoding 2>/dev/null
   IF force_tree_encoding == "true":
       force_encoding = true
       echo '"false"' | Bash: wm-set.sh force_tree_encoding
       Log: "▸ Step 8: forced encoding from anti-drift safeguard"
   # ── End encoding obligation checks ─────────────────────────────────

   # ── Encoding decision (default: ENCODE) ─────────────────────────
   # The default is to ENCODE. Skip ONLY for structural reasons.
   # "Nothing new learned" is NOT a valid skip reason — the curator gate (c.5) filters quality.
   skip_encoding = false
   IF len(result_text) < 100:
       skip_encoding = true
       Log: "▸ Step 8: skipping encoding — insufficient output ({len(result_text)} chars)"
   ELIF goal.status in ("blocked", "skipped"):
       skip_encoding = true
       Log: "▸ Step 8: skipping encoding — goal was {goal.status}"
   # ── End encoding decision ──────────────────────────────────────

   - If NOT skip_encoding OR force_encoding:
     # ── Load experience record for full-fidelity encoding ──────────
     # Context window content fades by the time encoding runs.
     # The experience file preserves verbatim_anchors and full reasoning traces.
     IF goal has experience_ref (set by Phase 4.25):
         experience_content = Read <agent>/experience/{experience_ref}.md
         # Use verbatim_anchors and full reasoning trace for precision extraction below,
         # supplementing whatever remains in the context window.
         Log: "▸ Step 8: loaded experience {experience_ref} for full-fidelity encoding"
     # ── End experience record loading ──────────────────────────────

     a. EXTRACT PRECISION: Scan execution context for exact values. Build precision manifest —
        each item: {type, label, value (VERBATIM), unit, context}. Types: threshold, formula,
        constant, reference, measurement, config_value. Include ALL numbers, code refs, error codes,
        thresholds, formulas, config values, commit hashes, line numbers. When in doubt, INCLUDE.
        See core/config/conventions/precision-encoding.md for schema and extraction heuristics.
     b. COMPOSE PRECISION BLOCK: Build candidate "## Verified Values" entries from manifest.
        Format: - **{label}**: `{value}` {unit} — {context}
        # DO NOT write to tree yet — the branch below controls when writing happens.
     c. COMPOSE NARRATIVE: Compress qualitative insight into candidate "Key Insights" text.

     # ══ ENCODING COORDINATION CHECK (multi-agent semantic overwrite prevention) ══
     # Before writing to a shared tree node, check if the other agent is encoding
     # to the same node. If so, defer to consolidation queue instead of writing immediately.
     # This prevents semantic overwrites where both agents encode different findings
     # to the same node and the second write silently loses the first agent's insights.
     # (Informed by "LLM Teams as Distributed Systems" Finding 3: concurrent writes.)
     encoding_deferred_by_coordination = false
     IF target node is identified (node.key is known):
         Bash: board-read.sh --channel coordination --type encoding --since 30m --json
         IF any message has a tag matching node.key AND author != current agent:
             Output: "▸ Step 8: ENCODING DEFERRED — {other_agent} recently encoded to {node.key}"
             encoding_deferred_by_coordination = true
             # Defer encoding to consolidation when another agent recently wrote to same node
     # Post own encoding intent BEFORE writing (other agent will see this in their check)
     IF NOT encoding_deferred_by_coordination:
         echo "Encoding: {node.key}" | Bash: board-post.sh --channel coordination --type encoding --tags {node.key}

     # ══ DEEP tree encoding (immediate write) ══════════════════════
     # All non-routine outcomes get immediate tree encoding.
     # Learning is the mission — never defer encoding.
     # Exception: coordination deferral queues to encoding_queue to prevent
     # semantic overwrites when another agent recently wrote to the same node.

     IF outcome_class == "deep" AND NOT encoding_deferred_by_coordination:
         # ── IMMEDIATE TREE WRITE (full inline encoding) ──
         c.5. CURATOR QUALITY GATE (AutoContext-inspired):
        Read core/config/memory-pipeline.yaml curator_gate section
        IF curator_gate.enabled:
            Evaluate the compressed insight (from step c) against the target node:
            CURATOR Q1 (Coverage, 0-1): "What specific info does this add that isn't
              already in the target node?" Vague/reinforcing → 0.2, concrete new info → 0.8+
            CURATOR Q2 (Specificity, 0-1): "Can I state a concrete fact, threshold, or
              procedure from this insight?" Exact values → 0.8+, vague feelings → 0.2
            CURATOR Q3 (Actionability, 0-1): "What specific action does this tell me to
              take in similar situations?" "be more careful" → 0.1, "check X before Y" → 0.8
            # Investigation-aware scoring: reweight for investigation goals
            # (higher coverage weight, lower actionability — understanding IS the output)
            IF is_investigation_goal:
                curator_score = (coverage * 0.50) + (specificity * 0.30) + (actionability * 0.20)
            ELSE:
                curator_score = (coverage * 0.40) + (specificity * 0.35) + (actionability * 0.25)
            IF curator_score < pass_threshold (default 0.45):
                Output: "▸ CURATOR GATE: REJECTED (score {curator_score:.2f} < {pass_threshold}) — demoted to overflow"
                echo '{"observation": "<insight_text>", "target_node": "<node.key>", "curator_score": <score>, "reason": "below_threshold"}' | wm-set.sh curator_overflow
                SKIP steps d through f for this insight (do NOT write to tree)
                # Overflow items get second chance during session-end consolidation
            ELSE:
                Output: "▸ CURATOR GATE: PASSED (score {curator_score:.2f})"
                # Proceed to step d (PRECISION AUDIT)
     d. PRECISION AUDIT: Re-read node. Verify each manifest item appears in Verified Values.
     e. EXTRACT DECISION RULES: IF execution produced a clear behavioral rule (IF X THEN Y):
        Append to or create "## Decision Rules" section in node.
        Format: `- IF {observable condition} THEN {specific action} — source: {goal.id}`
        Rules must be concrete (no vague "consider"), testable (condition is observable),
        and actionable (action is a specific step, not "be careful").
        Do NOT duplicate existing rules — check for semantic overlap first.
        Not every goal produces decision rules — only write when a clear IF-THEN emerges.
        See core/config/conventions/decision-rules.md for full format spec.
     f. CONSISTENCY SCAN: If the insight changes a factual claim already stated elsewhere
        in the node (count, threshold, formula, status), search the full node for stale
        references to the old value and update them. Use Edit replace_all for unambiguous
        strings. For ambiguous cases (e.g., "19" appears in unrelated contexts), fix each
        occurrence individually.
   - Update node metadata via batch:
     echo '{"operations": [
       {"op": "set", "key": "<node.key>", "field": "confidence", "value": <new-value>},
       {"op": "set", "key": "<node.key>", "field": "capability_level", "value": "<new-value>"}
     ]}' | bash core/scripts/tree-update.sh --batch
     # article_count: only increment here if Phase 8 itself wrote new content.
     # Skills that write to tree nodes (research-topic, reflect) handle their own increment.
     If Phase 8 wrote new insight above AND executing skill was NOT research-topic or reflect:
       Add {"op": "increment", "key": "<node.key>", "field": "article_count"} to the batch
   - Check growth triggers on the updated node:
     Read core/config/tree.yaml for decompose_threshold, split_threshold
     line_count = count lines in node .md body (excluding YAML front matter)
     If line_count > decompose_threshold AND node depth < D_max:
       bash core/scripts/tree-update.sh --set <node.key> growth_state ready_to_decompose
       Invoke /tree maintain
     Elif article_count > split_threshold:
       bash core/scripts/tree-update.sh --set <node.key> growth_state ready_to_split
       Invoke /tree maintain
   - Propagate changes up parent chain:
     result=$(bash core/scripts/tree-propagate.sh <node.key>)
     # Returns: {source_node, ancestors_updated: [...], capability_changes: [...]}
     IF result.capability_changes is non-empty:
       For each changed ancestor: update .md body text (capability map table in Key Insights)
   - If capability_level threshold crossed (check result.capability_changes):
     a. bash core/scripts/tree-update.sh --set root summary "<updated domain summary>"
     b. Log capability event via `echo '<json>' | bash core/scripts/evolution-log-append.sh`
     c. Announce: "CAPABILITY UNLOCK: {topic} → {new_level}"
     d. Read <agent>/developmental-stage.yaml
     e. If new level > highest_capability → update highest_capability

   # CRITICAL — set flags at the deep branch level, not inside capability check.
   # Orchestrator drift tracking (Phase 8.0.5) gates on step_8_tree_encoded.
   # Step 8.5 and Step 8.75 gate on step_8_wrote_insight.
   step_8_wrote_insight = true
   step_8_tree_encoded = true

   ELIF encoding_deferred_by_coordination:
     # ── COORDINATION DEFERRAL: queue encoding for consolidation ──────
     # Another agent recently encoded to the same node. Writing now would
     # silently overwrite their insight. Queue for consolidation instead.
     # DO NOT remove this branch — without it, insight is silently dropped
     # and the learning gate forces inline encoding, causing the exact
     # overwrite this check exists to prevent.
     encoding_payload = {
         source_goal: goal.id,
         source_type: "coordination_deferred",
         target_node_key: node.key,
         target_node_file: node.file,
         observation: compressed_narrative,
         precision_manifest: precision_manifest,
         decision_rules: candidate_rules,
         encoding_score: 0.65,
         curator_score: curator_score,
         target_article: node.file,
         metadata_updates: {
             confidence: <computed new value>,
             capability_level: <computed new value>
         },
         timestamp: now
     }
     echo '<encoding_payload_json>' | wm-append.sh encoding_queue
     Output: "▸ Step 8: COORDINATION DEFERRED — encoding queued for {node.key}"
     step_8_wrote_insight = true
     step_8_tree_encoded = false

   ELSE:
     # No new insight from this goal
     step_8_wrote_insight = false
     step_8_tree_encoded = false

# ── Step 8.5: Actionable Findings Gate ──────────────────────────────
# Catches findings encoded to tree (Step 8) that need their own goal.
# Runs AFTER tree encoding so the insight is already articulated.
# sq-013 (Phase 6) is complementary — it catches obvious work BEFORE
# encoding; this gate catches what crystallizes DURING encoding.
#
# Design: structural keyword scan, not open-ended LLM judgment.
# Investigation goals get a mandatory binary fallback check.

IF step_8_wrote_insight:   # True when Step 8 entered "compress into Key Insights" branch
    insight_text = the compressed insight just written to the tree node  # Already in context
    is_investigation = goal.title.startsWith("Investigate:")

    # ── Signal detection (keyword scan on insight_text) ─────────────
    # Negative filters prevent false positives when the insight itself reports the issue as resolved.
    # "Within 50 chars" window prevents distant resolution language from suppressing real signals.
    signals = []
    IF insight_text matches (root cause|caused by|due to|because of|stems from) + specific reference
       AND NOT within 50 chars followed by (fixed|resolved|applied|addressed|patched|corrected|updated|removed):
        signals.append({type: "root_cause", match: extracted_reference})
    IF insight_text matches (bug|defect|mismatch|incorrect|wrong|broken) + (in|at|of) + location
       AND NOT within 50 chars followed by (fixed|resolved|patched|corrected):
        signals.append({type: "bug_identified", match: extracted_reference})
    IF insight_text matches (fix by|should be changed|needs to be|replace with|update to)
       AND NOT within 50 chars followed by (done|completed|applied|implemented|changed|updated):
        signals.append({type: "proposed_fix", match: extracted_reference})
    IF insight_text matches (needs|requires|must|should) + (to be|updating|fixing|adding|removing)
       AND NOT within 50 chars followed by (done|completed|applied|implemented|resolved):
        signals.append({type: "unimplemented_action", match: extracted_reference})

    # ── Investigation override ──────────────────────────────────────
    # Completed investigations with findings are inherently actionable.
    # If no keywords matched, ask the reduced binary question.
    IF is_investigation AND len(signals) == 0:
        # Binary: "Is this finding purely informational, or does it need action?"
        IF finding_requires_action(insight_text):
            signals.append({type: "investigation_finding", match: insight_text})

    # ── Goal creation from signals ──────────────────────────────────
    IF len(signals) > 0:
        # Dedup: scan goal titles to avoid duplicates
        Bash: load-aspirations-compact.sh → IF path returned: Read it
        (compact aspirations now in context for dedup)
        active_titles = extract goal titles with status pending/in-progress from ALL aspirations
        # Cross-goal sibling check: completed goals in the SAME aspiration may have already fixed the finding
        parent_asp = goal's parent aspiration
        sibling_completed_titles = extract goal titles with status completed from parent_asp ONLY
        dedup_titles = active_titles + sibling_completed_titles

        FOR EACH signal:
            IF signal.type in ("root_cause", "bug_identified", "investigation_finding"):
                title = "Unblock: Fix {signal.match (50 chars)}"
                priority = "HIGH"
            ELSE:
                title = "Idea: {signal.match (50 chars)}"
                priority = "MEDIUM"

            IF similar title already exists in dedup_titles:
                Output: "▸ Step 8.5: {signal.type} detected but goal already exists — skipped"
                continue

            goal_json = {
                title, status: "pending", priority,
                skill: null, participants: ["agent"],
                category: goal.category,
                description: "Found during {goal.id}: {signal.match}\n\nSource: {insight_text}\n\nDiscovered by: Step 8.5 Actionable Findings Gate",
                verification: {
                    outcomes: ["Finding addressed — fix applied or determined not actionable with reasoning"],
                    checks: []
                },
                discovered_by: goal.id,
                discovery_type: signal.type
            }
            target_asp = goal's parent aspiration
            echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh --source {source} <target_asp>
            Output: "▸ FINDINGS GATE: Created {title} in {target_asp} from {goal.id}"

        Append to journal: "Findings gate: {N} signal(s) → {M} new goal(s)"
    ELSE:
        Output: "▸ Step 8.5: No actionable signals — passed"
# If Step 8 did not write insight: silent pass (no output)
```

```
# ── Step 8.75: Execution Reflection ──────────────────────────────────
# Cross-references execution outcomes against institutional knowledge:
# pattern signature matching/creation, contradiction detection against
# tree nodes, and investigation goal creation for findings that need
# follow-up. Complements Phase 6.5 (guardrails + reasoning bank) and
# Step 8.5 (actionable findings from keywords). This step handles
# pattern-level and contradiction-level analysis.
#
# Runs AFTER Step 8 (tree encoding) and Step 8.5 (actionable findings)
# so the insight text and tree state are fresh.
# Skipped by routine outcome early return (line 51-59).
# Skipped when Step 8 did NOT write insight (nothing to reflect on).

IF step_8_wrote_insight:
    invoke /reflect --on-execution with: goal, result, outcome_class
    # Sub-skill handles: notability gate (cheap early-exit if nothing notable),
    # pattern signature matching/creation, contradiction detection,
    # investigation goal creation, experience archival, journal entry.
    # Guardrails and reasoning bank are NOT created here (Phase 6.5 owns those).
```

```

```
# ── Step 8.76: Skill Quality Assessment ───────────────────────────
# SkillNet-inspired five-dimension quality scoring for the skill that
# just executed. Accumulates in meta/skill-quality.yaml as a rolling
# window of the last 20 evaluations per skill.
# Skipped by routine outcome early return (same as 8.75).
# Skipped when goal has no linked skill.

IF goal.skill is set AND outcome_class != "routine":
    # Map execution signals to five quality dimensions
    # See core/config/conventions/skill-quality.md for dimension definitions
    skill_name = goal.skill stripped of "/" prefix and any parameters

    safety = "good"
    IF guardrail violations occurred during this execution:
        safety = "average"  # caught violation
    IF uncaught harmful side effect detected:
        safety = "poor"

    completeness = "good"
    IF all verification.outcomes were met: completeness = "good"
    ELIF partial results achieved: completeness = "average"
    ELSE: completeness = "poor"

    executability = "good"
    IF episode_chain_count == 0: executability = "good"
    ELIF episode_chain_count == 1: executability = "average"
    ELSE: executability = "poor"

    maintainability = "good"  # Default for base skills; forged skills assessed at forge time

    cost_awareness = "good"
    # Assess from retrieval manifest: items loaded vs items actually used
    IF retrieval was disproportionate to task (loaded >> used): cost_awareness = "average"
    IF excessive redundant reads or bloated context: cost_awareness = "poor"

    Bash: skill-evaluate.sh score --skill {skill_name} --goal {goal.id} \
        --safety {safety} --completeness {completeness} --executability {executability} \
        --maintainability {maintainability} --cost-awareness {cost_awareness}
```

```
# ── Step 8.8: Improvement Velocity Update ──────────────────────────
# Track learning output per goal for meta-strategy evaluation.
# Runs for all non-routine outcomes. Lightweight: compute one score, append one line.

IF outcome_class != "routine":
    learning_value components (0-1 each):
      tree_updated: 1.0 if Step 8 wrote insight to tree, 0.0 otherwise → weight 0.3
      artifacts_created: min(1.0, count(reasoning_bank + guardrails + pattern_sigs created) × 0.2) → weight 0.3
      encoding_score: from Step 2.7 encoding gate if available, else 0.0 → weight 0.2
      findings_gated: min(1.0, count(Step 8.5 findings) × 0.25) → weight 0.2

    learning_value = (tree_updated × 0.3) + (artifacts_created × 0.3) + (encoding_score × 0.2) + (findings_gated × 0.2)

    # MR-Search exploration mode flag (Priority 3):
    IF goal.execution_mode == "exploration":
        Add exploration_mode: true to the snapshot
    # Get active backpressure monitors for credit assignment tagging
    bp_status = Bash: meta-backpressure.sh status
    active_change_ids = comma-join [m.meta_change_id for m in bp_status.active_monitors]
    Bash: meta-impk.sh snapshot --goal-id {goal.id} --learning-value {learning_value} --category {goal.category} --active-changes "{active_change_ids}"
```

```
# ── Step 8.85: Backpressure Gate Check (AutoContext-inspired) ──────────
# Checks if any active meta-strategy monitors detect regression.
# Auto-reverts changes that consistently degrade performance.
# Runs after imp@k snapshot (Step 8.8) so learning_value is fresh.

IF outcome_class != "routine":
    # active_change_ids already computed in Step 8.8 (from bp_status)

    # Check for regression — processes all active monitors internally
    bp_result = Bash: meta-backpressure.sh check --learning-value {learning_value}
    parse bp_result as JSON

    # Execute any rollback actions
    FOR EACH action in bp_result.rollback_actions:
        Bash: meta-set.sh {action.strategy_file} {action.field} {action.rollback_to} --reason "BACKPRESSURE ROLLBACK: {action.reason}"
        echo '{"date":"...","event":"backpressure_rollback","details":"{action.meta_change_id}: {action.field} reverted from {action.failed_value} to {action.rollback_to}","trigger_reason":"regression detected"}' | bash core/scripts/evolution-log-append.sh
        Output: "▸ BACKPRESSURE ROLLBACK: {action.field} reverted to {action.rollback_to} ({action.reason})"

    # Register dead end candidates (only reported when a NEW rollback pushes field to 2+ rollbacks)
    FOR EACH candidate in bp_result.dead_end_candidates:
        # failed_values contains the actual values that caused regression
        value_lo = min(candidate.failed_values)
        value_hi = max(candidate.failed_values)
        echo '{"strategy_file":"{candidate.strategy_file}","field":"{candidate.field}","value_range":[{value_lo},{value_hi}],"evidence":{candidate.evidence},"failure_pattern":"Rolled back {candidate.rollback_count} times","category":"meta_weight"}' | bash core/scripts/meta-dead-ends.sh add
        Output: "▸ DEAD END REGISTERED: {candidate.field} (rolled back {candidate.rollback_count}x)"

    # Report graduations
    FOR EACH grad_id in bp_result.graduated:
        Output: "▸ Backpressure graduated: {grad_id} (sustained improvement)"

    # Update generation metrics
    Bash: meta-generations.sh update --learning-value {learning_value}
```

```
# ── Step 8.9: Temporal Credit Propagation (MR-Search) ─────────────
# When this goal succeeded because of a prior goal's research/exploration,
# propagate discounted credit backward. Inspired by MR-Search's discounted
# temporal credit: A_{i,n} = Σ γ^(n'-n) × r̃_{i,n'}.
#
# This creates a feedback signal for "enabling strategies" — approaches
# that set up later success even when their own immediate outcome was weak.

IF outcome_class != "routine":
    # Read the experience record just created in Phase 4.25
    Bash: experience-read.sh --goal {goal.id}
    IF experience has enabled_by entries (non-empty):
        gamma = 0.9  # Discount factor per temporal distance unit
        FOR EACH enabler in enabled_by:
            credit = learning_value * gamma^(enabler.temporal_distance)
            IF credit > 0.01:  # Minimum meaningful credit
                # Accumulate credit on the enabling experience
                Bash: experience-read.sh --id {enabler.experience_id}
                current_credit = enabler_record.temporal_credit or 0.0
                new_credit = current_credit + credit
                Bash: experience-update-field.sh {enabler.experience_id} temporal_credit {new_credit}
                Output: "▸ Temporal credit: {enabler.experience_id} += {credit:.3f} (γ^{enabler.temporal_distance} × {learning_value:.3f})"
```

```
# ── Step 8.10: Relative Advantage Scoring (MR-Search) ────────────
# Compare this goal's learning_value against historical baselines for
# similar goals in the same category. MR-Search uses RLOO to compare
# same-position episodes across parallel trajectories. Since we execute
# sequentially, we compare against historical category means.
#
# This provides a relative (not absolute) quality signal for strategy
# evaluation — did this approach outperform the typical approach?

IF outcome_class != "routine":
    # Uses learning_value computed in Step 8.8 (same execution context).
    # Compares against imp@k snapshots from prior goals in same category.
    Bash: meta-read.sh improvement-velocity.yaml --field entries --json
    similar_snapshots = filter entries for category == {goal.category}, last 10
    IF len(similar_snapshots) >= 3:
        mean_learning_value = mean(snapshot.learning_value for snapshot in similar_snapshots)
        relative_advantage = learning_value - mean_learning_value
        # Store advantage on experience record
        Bash: experience-update-field.sh {experience_id} relative_advantage {relative_advantage}
        IF relative_advantage > 0.1:
            Output: "▸ Relative advantage: +{relative_advantage:.3f} (above category mean)"
        ELIF relative_advantage < -0.1:
            Output: "▸ Relative advantage: {relative_advantage:.3f} (below category mean)"
        # else: within normal range, no output
```

```
# ── Step 8.11: Execution Feedback (Cross-Agent Goal Quality) ─────
# When executing a goal created by another agent, post structured quality
# feedback to world/board/feedback.jsonl. Creates a backward learning signal
# so the goal creator can improve future goal descriptions.
# See board.md Execution Feedback Schema for field definitions.

IF outcome_class != "routine" AND source == "world":
    # Determine who created the goal. For world goals, check if there's a
    # created_by field or infer from the aspiration author / board handoff.
    goal_created_by = goal.get("created_by") or goal.get("discovered_by") or "unknown"

    IF goal_created_by != AGENT_NAME AND goal_created_by != "unknown":
        # Rate the goal on three dimensions (1-5 each)
        # Based on actual execution experience:
        clarity = <1-5: was the description clear and actionable?>
        scope_accuracy = <1-5: was the effort estimate right? 1=wildly off, 5=spot on>
        verification_quality = <1-5: were checks testable and sufficient?>
        friction = <"low"|"medium"|"high": overall execution friction>
        notes = <optional: what was missing or wrong — only if friction >= medium>

        feedback_json = {
            "goal_id": goal.id,
            "created_by": goal_created_by,
            "executed_by": AGENT_NAME,
            "clarity": clarity,
            "scope_accuracy": scope_accuracy,
            "verification_quality": verification_quality,
            "friction": friction,
            "notes": notes
        }

        echo '<feedback_json>' | Bash: board-post.sh --channel feedback \
            --type execution-feedback --tags "{goal.id},created_by:{goal_created_by}"
        Output: "▸ Execution feedback: clarity={clarity} scope={scope_accuracy} verify={verification_quality} friction={friction}"
```

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.

## Chaining

- **Called by**: `/aspirations` orchestrator (Phase 8)
- **Calls**: `aspirations-update-goal.sh --source {source}`, `aspirations-meta-update.sh --source {source}`, `aspirations-add-goal.sh --source {source}`, `wm-set.sh`, `wm-append.sh`, `skill-evaluate.sh`, `meta-impk.sh`, `meta-backpressure.sh`, `experience-update-field.sh`, `team-state-update.sh`, `board-post.sh`
- **Reads**: Goal object, execution result, `core/config/evolution-triggers.yaml`, `core/config/memory-pipeline.yaml`, `meta/encoding-strategy.yaml`
