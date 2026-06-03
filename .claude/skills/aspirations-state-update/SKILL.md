---
name: aspirations-state-update
description: "Runs the State Update Protocol (Phase 8 of the aspirations loop) after EVERY goal execution: 9 steps plus Step 3.5 Team State, Step 8.5 Actionable Findings Gate, Step 8.75 Execution Reflection, Step 8.76 Skill Quality, Step 8.77 User-Notable Event Push Classifier, and Step 8.11 Execution Feedback. Use whenever a goal finishes executing. Outcome_class controls depth — routine: Steps 1-4 + abbreviated journal; deep: full protocol with immediate tree encoding. MANDATORY — skipping or abbreviating kills the learning loop."
user-invocable: false
parent-skill: aspirations
triggers:
  - "run_state_update()"
conventions: [aspirations, tree-retrieval, goal-schemas]
minimum_mode: autonomous
revision_id: "skill-bootstrap-aspirations-state-update-4155f1"
previous_revision_id: null
---

# State Update Protocol

Invoked after EVERY goal execution as Phase 8 of the aspirations loop. Accepts `outcome_class` (default: `"deep"`).

## Abbreviation Policy

Mandatory writes for this obligation: see `core/config/obligation-schema.yaml`
→ `obligations.state`. Abbreviation is permitted only when
`context_budget.zone == tight`. Zone is distance-to-autocompact, not raw
usage — see `core/scripts/context-budget-status.py` `classify_zone` for the
source of truth. Before abbreviating, `Bash: bash
core/scripts/context-budget-banner.sh` and quote its output; that line is
the evidence that makes the "tight" claim verifiable. (Routine outcomes
follow the Steps 1-4 + 7r path below — that is a scope-of-work distinction,
NOT the abbreviated pattern.) When abbreviating, log TWO lines in the
journal entry (in this order):
  `OBLIGATION ABBREVIATED: state — {condition}`
  `CTX: raw N% | of-autocompact N% | zone tight | headroom N tokens | env ... | updated ...`
The banner line must be the actual output captured from the banner script.
`core/scripts/context-citation-audit.sh` audits the pair. Then satisfy
`minimum_inline`.
Otherwise invoke this Skill literally. The learning-gate audit (Phase 9.5d)
scans those journal lines and logs false claims to
`agents/<agent>/session/obligation-audit.jsonl`.

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

1.1. CLEAR iteration-checkpoint (goal is finished — anchor no longer applies).
     The checkpoint was written by `/aspirations-select` Phase 2.95 and must be
     cleared now so a post-autocompact restart doesn't resume a completed goal.
     Fires for ALL outcome classes (routine and deep) — Step 1's goal-status
     update is the point-of-no-return. See `core/scripts/postcompact-restore.py`
     for the read side.
     Bash: rm -f agents/<agent>/session/iteration-checkpoint.json

2. Aspiration progress is updated automatically by update-goal when status changes

3. UPDATE last_session via Bash: `aspirations-meta-update.sh --source {source} last_updated <today>`
   - Append goal completion to goals_completed_this_session in working memory:
     # Keys read by goal-selector.py streak_momentum + recurring_saturation +
     # per_goal_saturation + class_balance_bonus — do not rename.
     # work_class is optional (Tranche C, rb-390): scorer's class_balance_bonus
     # reads it when present, excludes the entry from the distribution when absent.
     # Goals with no work_class field on the goal record: omit the key entirely
     # (do NOT write "work_class":"unclassified" — empty key = excluded).
     Bash: echo '{"goal_id":"<goal-id>","aspiration_id":"<aspiration-id>","recurring":<true|false>[,"work_class":"<class>"]}' | wm-append.sh goals_completed_this_session
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

# ── Step 4.5: Outcome-Metrics Cadence Guard (routine outcomes) ─────
# Step 8.12 fires the outcome-observation hook for non-routine outcomes only.
# Routine-only sessions would otherwise skip the hook indefinitely and leave
# outcome-metrics.yaml silently stale (20-day window observed 2026-04-24 to
# 2026-05-14, see world/conventions/outcome-observation.md "Cadence Guarantee").
# This guard fires the same wrapper opportunistically when the metrics file
# is missing or > 24h stale. Wrapper exits 0 unconditionally — fail-open.
IF outcome_class == "routine":
    Bash: source core/scripts/_paths.sh && \
          METRICS_FILE="$WORLD_DIR/outcome-metrics.yaml"; \
          if [ ! -f "$METRICS_FILE" ] || \
             [ $(( ( $(date +%s) - $(stat -c %Y "$METRICS_FILE") ) / 3600 )) -ge 24 ]; then \
              bash core/scripts/outcome-observation-run.sh "{goal.id}" routine; \
          fi

# ── Routine outcome learning path ──────────────────────────────────
# Principle: we are here to learn. Even routine outcomes deserve operational scrutiny.
# This replaces the bare early return with lightweight but meaningful reflection.
IF outcome_class == "routine":

    # Step 5r: Lightweight operational reflection (NOT skipped for routine)
    # Ask ONE question from the routine_lens template, rotating so each
    # execution of a recurring goal gets a DIFFERENT question over time.
    Read core/config/reflection-templates.yaml → routine_lens.questions
    question_index = hash(goal.id + str(goal.achievedCount)) % len(routine_lens.questions)
    routine_q = routine_lens.questions[question_index]
    Evaluate: {routine_q} applied to this routine goal's outcome
    IF reflection produces a non-trivial insight (not a restatement of known facts):
        # Capture as a lightweight reasoning bank entry
        Bash: reasoning-bank-read.sh --category {goal.category}
        IF NOT semantically overlapping with existing entries:
            Create reasoning bank entry via reasoning-bank-add.sh:
              title: "Routine insight: {concise name}"
              type: success
              category: goal's category
              content: the operational insight
              applies_to: <any|framework|domain|specific>  # REQUIRED. routine ops insights about external services → domain; framework-internal → framework; cross-cutting → any
              when_to_use: when this insight applies
              source_goal: goal.id
              tags: ["routine-operational-insight"]
            Log: "ROUTINE OPERATIONAL INSIGHT: {title} from {goal.id}"
        routine_insight_found = true
        routine_insight_text = the operational insight (1-2 sentences)
    ELSE:
        routine_insight_found = false
        routine_insight_text = "No new insight."

    # 7r. WRITE enhanced journal entry — delegated to journal-append.sh
    #     (g-248-35, rb-428 family). The wrapper handles templating + citation
    #     scan + journal-index merge/add fallback as a single bash-enforced unit.
    #     iteration-close.sh do_state_update calls it automatically — this
    #     pseudocode documents the LLM-residue path for /reflect and ad-hoc
    #     callers that bypass iteration-close.
    Bash: `journal-append.sh --goal {goal.id} --outcome-class routine --summary "{routine_q[:60]}: {routine_insight_text}"`
    # The wrapper appends to agents/<agent>/journal/YYYY/MM/YYYY-MM-DD.md with
    # stable section headers (## title, Outcome:, Summary:) AND tries
    # journal-merge.sh first, falling back to journal-add.sh on no-existing-
    # session-record. NEVER inline the markdown assembly + index call here —
    # /verify-learning Section BE checks the wrapper is the single writer.

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
                # T21 PostToolUse hook (tree-front-matter-sync.py) bumps
                # both the .md front matter AND _tree.yaml last_updated
                # automatically — no explicit register call needed.
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

7. WRITE journal entry — delegated to journal-append.sh (g-248-35, rb-428 family)
   The wrapper handles markdown templating + citation scan + journal-index
   merge/add fallback as a single bash-enforced unit. iteration-close.sh
   do_state_update calls it automatically — this pseudocode documents the
   LLM-residue path for /reflect, /aspirations-consolidate, and ad-hoc
   callers that bypass iteration-close.

   Bash: `journal-append.sh --goal {goal.id} --outcome-class {routine|deep} --summary "..."`

   The wrapper:
     - Appends to agents/<agent>/journal/YYYY/MM/YYYY-MM-DD.md with stable section
       headers (## title, Outcome:, Summary:) — /verify-learning Section BE
       checks this contract.
     - Scans summary for rb-NNN / guard-NNN citations and increments
       utilization.times_cited via reasoning-bank-increment.sh /
       guardrails-increment.sh.
     - Tries journal-merge.sh first, falls back to journal-add.sh on no
       existing session record (AUTHORITATIVE OWNER — only exception is
       /stop step 3.e for emergency cleanup).
     - Fail-open: write failures log to stderr but never block the iteration.

   NEVER inline the markdown assembly + index call here. Drift between this
   pseudocode and the wrapper is the failure mode rb-428 was filed against;
   keeping the wrapper as the single source of truth is the fix.
   /boot Step 11 writes journal .md content only — it does NOT touch the
   journal index.

8. REFRESH memory tree (dynamic lookup)

   The encoding mechanics from this step onward — scoring formula, curator
   gate, precision-manifest extraction, decision-rule append, capability
   propagation, and cross-agent coordination — live in
   `core/config/encoding-protocol-digest.md` (E16). This step is the
   autonomous-mode consumer of that digest; `/encode-session` Lane 1 is
   the chat-mode consumer. **Edit the digest BEFORE editing this step —
   drift between the two skills is what the digest exists to prevent.**

   PLACEMENT CHECK (rules vs. conventions): Before writing to
   `.claude/rules/*.md` or `core/config/conventions/*.md` (as part of an
   encoding side-effect), determine scope. If the content mentions a
   brand, product, domain-specific identifier, or external service name,
   it belongs in `world/conventions/` (the Pattern B hook slot per
   `domain-hooks.md`), NOT in a core rule or core convention. Core rules
   and conventions must remain domain-agnostic per
   `.claude/rules/domain-free-examples.md`.

   - Find target node:
     node=$(bash core/scripts/tree-find-node.sh --text "{goal.category}" --leaf-only --top 1)
     # Returns: {key, score, file, depth, summary, node_type}
   - Read node.file
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
         experience_content = Read agents/<agent>/experience/{experience_ref}.md
         # Use verbatim_anchors and full reasoning trace for precision extraction below,
         # supplementing whatever remains in the context window.
         Log: "▸ Step 8: loaded experience {experience_ref} for full-fidelity encoding"
     # ── End experience record loading ──────────────────────────────

     # ── E13 chunked-encoding consumer ──────────────────────────────
     # Phase 4.05 may have emitted chunked observations to sensory_buffer
     # when the execution was long-form. Each chunk is scored independently
     # against the encoding gate so multi-finding executions surface as
     # multiple tree updates instead of one collapsed paragraph. Schema
     # and pipeline: encoding-protocol-digest.md Section F.
     Bash: wm-read.sh sensory_buffer --json
     chunks_for_this_goal = [e for e in sensory_buffer
                             if e.source_goal == goal.id AND e.chunk_idx is defined]
     IF chunks_for_this_goal is non-empty:
         Log: "▸ Step 8: E13 found {len(chunks)} chunks for this goal — encoding each independently"
         For each chunk in chunks_for_this_goal:
             Run Section A encoding-gate against chunk.scores
             IF passes:
                 Build a per-chunk precision payload from chunk.chunk_text
                 Run Section B (curator) + Section C (tree write) on this chunk
             ELSE:
                 Drop (overflow handles re-consideration)
         # The chunked path REPLACES the single-bundle encoding below for
         # this goal. SKIP steps a-f after the chunk loop completes.
     # If no chunks: continue to step a (single-bundle encoding, the
     # original path).
     # ── End E13 chunked consumer ───────────────────────────────────

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
     e. EXTRACT DECISION RULES: Bash-enforced via decision-rules-append.sh
        (rb-428 / guard-365). LLM residue: compose the IF clause (observable
        condition) and THEN clause (specific action). The wrapper does the
        format, token-overlap dedup (>=70% → skip), and insert. Empty stdin
        is legitimate ("no rule emerged") and emits a staleness signal for
        aggregate drift detection — see core/config/conventions/decision-rules.md.

        Rules must be concrete (no vague "consider"), testable (condition
        is observable), and actionable (a specific step, not "be careful").
        Not every goal produces decision rules — only emit when a clear
        IF-THEN emerges.

        IF a clear behavioral rule emerged:
            echo '{"if": "<observable condition>", "then": "<specific action>"}' \
              | bash core/scripts/decision-rules-append.sh \
                  --goal {goal.id} --node-path <node-md-path>
            # For multiple rules: echo a JSON array instead of a single object.
        ELSE:
            # Signal "no rule" explicitly so the staleness probe counts it.
            echo '' | bash core/scripts/decision-rules-append.sh \
                --goal {goal.id} --node-path <node-md-path>

        # Wrapper emits one line per rule:
        #   ▸ DECISION RULE: appended: - IF ... THEN ... — source: g-NNN-NN
        #   ▸ DECISION RULE: skipped (duplicate): - IF ... THEN ...
        # Final line: decision_rules_count=<N> appended=<M> skipped=<K>
        #   (reason=no_rule_passed when stdin was empty)
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
     d. Read agents/<agent>/developmental-stage.yaml
     e. If new level > highest_capability → update highest_capability

   # CRITICAL — set flags at the deep branch level, not inside capability check.
   # Orchestrator drift tracking (Phase 8.0.5) gates on step_8_tree_encoded.
   # Step 8.5 and Step 8.75 gate on step_8_wrote_insight.
   step_8_wrote_insight = true
   step_8_tree_encoded = true

   # ── Knowledge-debt auto-detect fallback (anti-pattern 2) ─────────
   # A goal may resolve a knowledge_debt without having declared it at
   # decomposition time. If the node we just wrote matches an entry in
   # working-memory knowledge_debt[], back-populate closes_knowledge_debt
   # on the goal so the classifier's semantic override (in the NEXT goal's
   # Phase 4-post, or in reflection) can see the link. Also log for the
   # current session's debt-sweep.
   debt_match = $(bash core/scripts/wm-read.sh knowledge_debt --json | \
                  jq -r --arg k "<node.key>" '.[] | select(.node_key == $k) | .node_key')
   IF debt_match is non-empty:
     existing_field = goal.closes_knowledge_debt or []
     IF node.key not in existing_field:
       updated = existing_field + [node.key]
       bash core/scripts/aspirations-update-goal.sh --source {source} <goal.id> \
            closes_knowledge_debt "$(jq -c -n --argjson v '<updated_json>' '$v')"
       Log: "▸ Step 8: auto-detected debt closure — back-populated closes_knowledge_debt=[{node.key}] on {goal.id}"

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
# Bash-enforced (rb-428 / guard-365). The wrapper does the 4-signal
# keyword scan with resolution filters, dedup against active + sibling-
# completed titles (reads aspirations-compact.json), and dispatches via
# aspirations-add-goal.sh. Emits `findings_count=N created=M` for
# downstream accounting. LLM residue: (a) the insight text itself (comes
# from Step 8), (b) the investigation-override binary — for
# Investigate: goals where no keyword fires, answer "does this finding
# require action?" and pass it as a flag.

IF step_8_wrote_insight:   # True when Step 8 entered "compress into Key Insights" branch
    insight_text = the compressed insight just written to the tree node  # Already in context
    is_investigation = goal.title.startsWith("Investigate:")
    parent_asp = goal's parent aspiration

    # Write the insight to a temporary file for --insight-file.
    Write agents/<agent>/session/findings-gate-insight.tmp.md with insight_text

    # LLM residue: investigation-override binary. Only answer if
    # is_investigation AND the insight contains NO keyword signals
    # (the wrapper runs the scan too — answer here just provides the
    # override). Conservative default: omit the flag if not certain.
    investigation_needs_action = (is_investigation AND finding_requires_action(insight_text))

    # Single dispatch — wrapper handles scan + dedup + goal creation.
    flags = "--goal {goal.id} --aspiration {parent_asp} --category {goal.category} --source {source} --insight-file agents/<agent>/session/findings-gate-insight.tmp.md"
    IF is_investigation: flags += " --is-investigation"
    IF investigation_needs_action: flags += " --investigation-needs-action"

    Bash: bash core/scripts/findings-gate.sh <flags>

    # Wrapper output format (one line per signal + final summary):
    #   ▸ FINDINGS GATE: created <title> in <asp_id> from <goal.id>
    #   ▸ FINDINGS GATE: skipped (dup) <title>
    #   findings_count=<N> created=<M>
    # If M > 0, append to journal: "Findings gate: N signal(s) → M new goal(s)"

    Delete agents/<agent>/session/findings-gate-insight.tmp.md
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
    #
    # Positive-state claim discipline (verify-before-assuming.md
    # Positive File-State Claims): any file-state assertion the reflection
    # makes ("handoff.yaml reflects N", "X.jsonl was updated to Y") must be
    # backed by an in-turn Read. Belt-and-suspenders — the verify gate
    # (aspirations-verify Q1) is the hard check; this note is the advisory
    # reminder for reflection narratives. If a file-state claim appears
    # without a supporting Read in this turn, re-read before stating.
```

```

```
# ── Step 8.76: Skill Quality Assessment ───────────────────────────
# SkillNet-inspired five-dimension quality scoring for the skill that
# just executed. Accumulates in meta/skill-quality.yaml as a rolling
# window of the last 20 evaluations per skill.
# Skipped by routine outcome early return (same as 8.75).
# Skipped when goal has no linked skill.
#
# Bash-enforced auto-derivation (g-248-34, rb-428 pattern): the wrapper
# computes 4-of-5 dimensions mechanically from execution signals, and
# the LLM supplies ONLY cost_awareness (semantic retrieval-proportionality
# judgment that genuinely cannot be derived from counters).
#
#   Mechanical:     safety (from guardrail violations),
#                   completeness (from outcomes-met/total ratio),
#                   executability (from episode-chain count),
#                   maintainability (base → "good"; forged → registry lookup)
#   LLM residue:    cost_awareness
#
# See core/config/conventions/skill-quality.md for dimension definitions
# and core/scripts/skill-quality-score.py for derivation rules.

# Sampling-bias fix (skill-telemetry-signal-master-plan Layer 3, 2026-05-12):
# The original gate excluded routine outcomes AND null goal.skill, causing the
# pipeline to silently dry up when the workload shifted to recurring/infra
# goals (2026-04-16 silence root cause). Per the master plan, routine outcomes
# still produce mechanical quality signal — only cost_awareness (LLM judgment)
# is skipped; everything else fires. Null goal.skill on a non-routine outcome
# now surfaces as an explicit warning (goal-creation gap upstream).
#
# Name canonicalization: skill-quality-score.py strips leading "/", drops
# post-whitespace, validates against canonical name list. LLM passes goal.skill
# raw — no pre-normalization needed. See rb-885, guard-535.

IF goal.skill is set:
    skill_arg = goal.skill  # raw; script.canonicalize_skill_name handles it

    # Four mechanical execution-signal inputs (LLM gathers from this iteration):
    outcomes_met   = count of goal.verification.outcomes met during verify
    outcomes_total = len(goal.verification.outcomes)
    episodes       = episode_chain_count from execute phase (0 if clean)
    violations     = guardrail-fire count during execute (0 if none)

    # cost_awareness: deep outcomes earn full LLM judgment; routine outcomes
    # get the default "good" (fast-path — keeps eval firing without spending
    # judgment cycles on expected-routine work).
    IF outcome_class == "routine":
        cost_awareness = "good"  # default — routine doesn't warrant LLM cycles
    ELSE:
        # One semantic-judgment input (LLM decides; no mechanical proxy exists):
        #   good    = items loaded proportional to items used; no wasted reads
        #   average = loaded >> used, OR redundant reads of the same file
        #   poor    = excessive bloat, repeated reads of unused context
        cost_awareness = LLM judgment on retrieval proportionality this iteration

    Bash: skill-quality-score.sh score \
        --skill {skill_arg} --goal {goal.id} \
        --outcomes-met {outcomes_met} --outcomes-total {outcomes_total} \
        --episode-chain-count {episodes} --guardrail-violations {violations} \
        --cost-awareness {cost_awareness}
    # Wrapper acquires <meta>/skill-quality.yaml.lock before invoking
    # skill-evaluate.py — protects against concurrent-agent write races.
    # Emits a human-readable score line plus a machine-readable JSON trailer
    # with the derived grades (auditable + consumable by downstream steps).
    # On invalid skill name: script exits 1 with explicit error rather than
    # silently writing a denormalized YAML key (previous bad-state mode).

ELIF outcome_class != "routine":
    # Deep outcome with null goal.skill = goal-creation gap. Surface it loud
    # so upstream goal creators learn to tag skill on creation. This was the
    # 2026-04-16 silence root cause — pipeline silently dried up on null-skill
    # workload. Do not silently skip when the outcome is deep.
    Report: "WARN: Step 8.76 skipped — deep outcome on goal {goal.id} with no goal.skill. Tag the skill at goal-creation time for proper quality signal."

# IF outcome_class == "routine" AND goal.skill is null: silently skip (expected
# for some routine infra goals; no novel signal to score, no warning needed).
```

### Scripted Audit Pass (Steps 8.8-8.10)

`state-update-audit.sh run-all` runs velocity (8.8), backpressure (8.85),
temporal credit (8.9), and relative advantage (8.10) in one Python pass —
pure arithmetic, no LLM judgment. This is the ONLY path. The previous
toggle + LLM-fallback pseudocode was removed 2026-04-20 — the script is
the single source of truth. Any bug in `state-update-audit.py` MUST be
fixed in the script, not patched around by reintroducing a shadow LLM
path here.

Steps 8.5 (Actionable Findings Gate), 8.75 (Execution Reflection), 8.76
(Skill Quality), and 8.11 (Execution Feedback) stay on the LLM path —
those require judgment that `run-all` does not cover.

```
IF outcome_class != "routine":
    # LLM gathers four inputs from this iteration's state. These MUST be
    # passed — omitting any flag silently produces learning_value=0
    # (state-update-audit.compute_learning_value uses argparse defaults).
    # The script owns the arithmetic; the LLM owns the inputs.
    tree_updated    = true iff Step 8 wrote an insight node to the tree
    artifacts_count = count(reasoning_bank + guardrails + pattern_sigs created this iteration)
    encoding_score  = from Step 2.7 encoding gate if it fired, else 0.0
    findings_count  = count(Step 8.5 Actionable Findings gated this iteration)

    Bash: bash core/scripts/state-update-audit.sh run-all \
        --goal {goal.id} \
        --outcome-class {outcome_class} \
        --category {goal.category} \
        --experience-id {experience_id} \
        [--tree-updated if tree_updated] \
        --artifacts-count {artifacts_count} \
        --encoding-score {encoding_score} \
        --findings-count {findings_count} \
        [--exploration if goal.execution_mode == "exploration"]
    # Reads the JSON. Flags to watch for (each prefixed by subcommand):
    #   velocity:impk_snapshot_failed  → meta-impk.sh unreachable; investigate stderr
    #   backpressure:rollbacks_applied → one or more meta-strategy fields
    #                                    auto-reverted — note in output
    #   backpressure:check_failed      → meta-backpressure.sh unreachable; investigate stderr
```

```
# ── Step 8.86: Self/Program/Skill/Rule Evolution Backpressure ───────
# Per world/conventions/self-program-evolution.md
# Runs AFTER 8.10 (meta-strategy backpressure) and BEFORE 8.77.
#
# Re-samples the metric vector for every active evolution monitor
# (monitor_kind ∈ {self_evolution, program_evolution, skill_evolution,
#  rule_evolution}). Fires auto-rollback when consecutive_below_baseline
# crosses the per-kind regression_window. Fires graduation when
# consecutive_above_baseline crosses graduation_window.
#
# Skips entirely when outcome_class == "routine" — routine goals do not
# perturb the metric vector enough to be informative, and sampling on
# every routine iteration would flood the script. The §14.5 windows
# (6-15 iterations) are sized for deep/exploration iterations only.

IF outcome_class == "routine":
    SKIP silent

Bash: bash core/scripts/meta-backpressure.sh evolution-check
    # Returns JSON with:
    #   rollback_actions[]    → each: {revision_id, monitor_kind, file_path,
    #                                  worst_signal, worst_drop, rolled_back,
    #                                  rollback_error?}
    #   graduated[]           → each: {revision_id, monitor_kind, file_path,
    #                                  samples_collected}
    #   active_monitors_count → total post-check (rolled_back + graduated removed)
    #
    # If rollback_actions is non-empty: surface in iteration close output —
    # e.g., "ROLLBACK: skill_evolution {rev_id} reverted — worst signal
    # {worst_signal} dropped {worst_drop}". The rollback record is already
    # appended to the corresponding <kind>-evolution.jsonl by the engine.
    #
    # If subprocess fails (timeout, ImportError): log "WARN: 8.86 evolution-
    # check failed: {stderr}" and continue. NEVER block iteration close on
    # backpressure infra failure — the monitors will resume on the next
    # successful iteration.
```

```
# ── Step 8.77: User-Notable Event Push Classifier ────────────────────
# Proactive user notification on goal outcomes that cross a "user would
# want to know" threshold. Catches the gap Alpha named 2026-04-23:
# coordination-board updates flow constantly, but user-facing signal
# fires only via fresh-eyes-review (cadence 25) + blocker alerts +
# explicit invocations. No per-goal middle tier — until now.
#
# This step is a CLASSIFIER, not a new transport. Dispatch routes through
# the existing /notify-user skill (which owns self-identification,
# 30m same-subject dedup, email + fallback cascade, and
# wm.notification_log append). This step decides WHEN to fire; /notify-user
# decides HOW to deliver.
#
# Runs AFTER Step 8.76 (skill quality) and BEFORE Step 8.78 (fresh-eyes
# gate) — placed so reflection/quality outputs are in context but the
# more expensive fresh-eyes dispatch is still downstream.

# ── Skip conditions (fail-closed; default is NO push) ──
IF outcome_class == "routine":
    SKIP silent  # routine outcomes already degate via Part A's auto-deep at 8
IF NOT goal_succeeded:
    SKIP silent  # failures route via CREATE_BLOCKER → /notify-user category=blocker
IF goal.category == "blocker" OR goal.title starts with "Blocker:":
    SKIP silent  # double-push avoidance — CREATE_BLOCKER already dispatched
# ── Skip condition #4: user-only mute ──
# User can mute Step 8.77 entirely by writing the slot:
#   bash core/scripts/wm-set.sh suppress_user_push true
# Useful when on-call / focusing — agent keeps working but stops pushing
# user-notable events. Clear with `... wm-set.sh suppress_user_push false`
# (or wm-clear.sh suppress_user_push). Intentionally has no programmatic
# writer — agent must never auto-mute itself. Whitelisted in
# signal-lifecycle-gate.py phantom-reads CATEGORY 2.
Bash: wm-read.sh suppress_user_push
IF signal is not null AND signal == "true":
    Output: "▸ Step 8.77: suppress_user_push set (user-muted) — skipping classifier"
    SKIP

# ── Trigger evaluation (match any — multiple triggers per goal is valid) ──
triggers = []   # list of (trigger_id, category, subject, message)

# ---- Trigger 1: shipped — a new capability landed ----
# Signal: deep outcome AND ship-verb title OR insight_text contains shipping language.
# Keeps false-positive rate low by requiring the deep-outcome precondition
# (only non-routine, verified-successful, material outcomes reach here).
IF outcome_class == "deep":
    title_matches_ship = re.match(r"^(ship|deploy|release|build|create|add)\b", goal.title, re.I)
    insight_has_ship = any(s in (insight_text or "").lower()
                            for s in ["shipped", "deployed", "released to", "landed in"])
    IF title_matches_ship OR insight_has_ship:
        subject = f"Alpha shipped: {goal.title[:60]}"
        message = (insight_text or goal.title)[:400]
        triggers.append(("shipped", "info", subject, message))

# ---- Trigger 2: resolved-long-blocker — Unblock goal closed after >= 7d open ----
# Single source of truth for age = goal.created_at (aspirations schema).
# DO NOT add fallback fields (created, created_date, blocked_since) — if
# created_at is missing, the age signal is untrustworthy and the trigger
# MUST skip. Fail-open matches user-stated design rule ("when in doubt,
# don't protect"). 40/50 Unblock goals in the live store lack created_at
# as of 2026-04-23 — guard-first or crash (datetime - None → TypeError).
IF goal.title starts with "Unblock:" AND goal.created_at is not None:
    age_days = (now - parse_iso(goal.created_at)).days
    IF age_days >= 7:
        title_trimmed = goal.title[9:65].strip() if len(goal.title) > 9 else goal.title
        subject = f"Unblocked after {age_days}d: {title_trimmed}"
        message = (insight_text or "")[:400] + f"\n\nBlocker had been open {age_days} days."
        triggers.append(("resolved-long-blocker", "info", subject, message))

# ---- Trigger 3: recovered — infra/session/job recovery ----
# Matches output language from recovery-gate, stale-scanner, crash recovery
# notices. Same keyword set used by Part A's recovery-notice display.
IF goal.title starts with "Recover:" OR any(
    k in (insight_text or "").lower() for k in [
        "crashed runner recovered",
        "stale_scanner killed",
        "stale scanner killed",
        "reaped",
        "infrastructure recovered",
        "recovery-gate fired",
    ]
):
    subject = f"Recovered: {goal.title[:60]}"
    message = (insight_text or goal.title)[:400]
    triggers.append(("recovered", "info", subject, message))

# ---- Future extensions (NOT shipped in this MVP — documented for next pass) ──
# Trigger 4: hypothesis-confirmed — requires /review-hypotheses integration
#            (read outcome + confidence from pipeline record resolved this
#            iteration). Would fire for correct high-conviction resolutions
#            AND for wrong-but-high-confidence (surprise) resolutions.
# Trigger 5: participant-user-goal-created — requires a goal-creation delta
#            (compact snapshot before/after this iteration) OR a WM counter
#            that aspirations-add-goal bumps when --participants includes user.
#            Collapses the original "something to say" flag into the push
#            classifier. MVP omits because the delta signal isn't wired.

# ── Short-circuit if nothing triggered ──
IF triggers is empty:
    SKIP silent

# ── Rate cap: max 3 immediate pushes per rolling hour ──
# SINGLE SOURCE OF TRUTH for send history: wm.notification_log (owned by
# /notify-user; written on successful send only). DO NOT introduce a
# parallel counter — dual stores drift. Entry shape: {subject, category,
# sent_at: <ISO 8601 string>}. LLM parses sent_at via datetime.fromisoformat
# before comparing.
Bash: wm-read.sh notification_log
recent_pushes = [e for e in (notification_log or [])
                 if datetime.fromisoformat(e.sent_at) >= now - timedelta(seconds=3600)]
push_slots_remaining = max(0, 3 - len(recent_pushes))

# ── Dispatch (preserve trigger order; first N under cap go out immediate) ──
# Over-cap pushes are DROPPED, not queued. Rationale: a user-notable moment
# has decay — a push 6h later stamps the wrong time (rb-464 stale-narrative).
# Suppression surfaces in the iteration Output line only; do NOT write a
# shadow journal entry (orphaned — no index via journal-add.sh).
FOR (trigger_id, category, subject, message) in triggers:
    IF push_slots_remaining <= 0:
        Output: f"▸ Step 8.77: user-push suppressed ({trigger_id}, rate cap) — {subject}"
        CONTINUE
    # Canonical prose form per .claude/rules/forged-skill-resolution.md so a
    # domain-registered notify forge-override can intercept. /notify-user owns
    # 30m same-subject dedup AND the notification_log append — this step
    # decides WHEN, /notify-user decides HOW.
    Notify the user about {trigger_id} with category={category}, subject={subject}, message={message}.
    push_slots_remaining -= 1
    Output: f"▸ Step 8.77: user-push fired ({trigger_id}) — {subject}"
```

```
# ── Step 8.78: Post-State-Update Fresh-Eyes Gate (guard-343 bash-enforced) ─
# Bash decides WHETHER (threshold gate), LLM decides WHAT (Skill dispatch).
# The gate fires only for deep outcomes with material core/ changes. See
# core/scripts/post-state-update-gate.sh for the threshold spec (core_files>=3,
# loc>=100, or new script in core/scripts). If the gate fires, the LLM
# dispatches /fresh-eyes-code on the returned file list. rb-393 + guard-343.

IF outcome_class == "deep":
    Bash: core/scripts/post-state-update-gate.sh deep
    gate_json = parse stdout as JSON
    IF gate_json.fired:
        Output: "▸ Step 8.78: fresh-eyes gate fired ({gate_json.reason})"
        Skill('fresh-eyes-code') with args: space-separated paths from gate_json.files
    # ELSE: silent pass — gate.reason already logged in gate_json for audit
# ELSE: gate is always false for routine — skip silently
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

```
# ── Step 8.12: Outcome-Observation Hook (Tranche C — rb-390) ──────
# Hook slot for domain-supplied outcome observation after state update. The
# process side (goals completed, productive_ratio) can inflate while nothing
# material moves; an outcome-observation convention PULLS evidence from the
# actual systems the work is supposed to affect, producing the process-vs-
# outcome divergence signal downstream consumers (agent-completion-report
# "Outcome Delta" section) report on.
#
# Pattern B hook slot (`outcome-observation`). See
# core/config/conventions/domain-hooks.md. Core names the slot, the world
# convention (if it exists) names what to run. Skipped for routine outcomes
# (the routine early-return above already returned). Fail-open — a missing
# or broken convention does NOT abort state-update.

IF outcome_class != "routine":
    Bash: paths=$(bash core/scripts/load-conventions.sh outcome-observation 2>/dev/null)
    IF paths is non-empty:
        Read the file at the returned path
    # Procedural convention — gate on file EXISTENCE, not load status.
    Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/outcome-observation.md" && echo "exists"
    IF exists:
        Follow each Step in the convention.
        Any step that fails SHOULD be logged and swallowed — never abort state-update.
    ELSE:
        # No domain outcome-observation convention exists (fresh agent).
        # Nothing to do — downstream Outcome Delta section will show
        # "no outcome signal configured" and consumers degrade gracefully.
```

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.

## Chaining

- **Called by**: `/aspirations` orchestrator (Phase 8)
- **Calls**: `aspirations-update-goal.sh --source {source}`, `aspirations-meta-update.sh --source {source}`, `aspirations-add-goal.sh --source {source}`, `wm-set.sh`, `wm-read.sh`, `wm-append.sh`, `skill-evaluate.sh`, `meta-impk.sh`, `meta-backpressure.sh`, `experience-update-field.sh`, `team-state-update.sh`, `board-post.sh`, `/notify-user` (Step 8.77 user-notable event push classifier)
- **Reads**: Goal object, execution result, `core/config/evolution-triggers.yaml`, `core/config/memory-pipeline.yaml`, `meta/encoding-strategy.yaml`
