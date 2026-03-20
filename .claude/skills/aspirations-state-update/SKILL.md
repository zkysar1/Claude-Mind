---
name: aspirations-state-update
description: "State Update Protocol — 9 steps + Step 8.5 Actionable Findings Gate + Step 8.75 Execution Reflection after every goal execution (routine outcomes: Steps 1-4 + abbreviated journal only)"
user-invocable: false
parent-skill: aspirations
triggers:
  - "run_state_update()"
conventions: [aspirations, tree-retrieval, goal-schemas]
---

# State Update Protocol

Invoked after EVERY goal execution as Phase 8 of the aspirations loop. Accepts `outcome_class` (default: `"productive"`).

For **productive** outcomes: all steps run (1-8, 8.5, 8.75). Step 8 (REFRESH MEMORY TREE) is the single most critical learning step — without it, the agent learns NOTHING from goal execution. Step 8.5 (ACTIONABLE FINDINGS GATE) ensures encoded findings get tracked as goals. Step 8.75 (EXECUTION REFLECTION) cross-references outcomes against institutional knowledge.

For **routine** outcomes (recurring goal, no findings): Steps 1-4 run (bookkeeping), then an abbreviated Step 7 (journal), then RETURN. Steps 5-8.75 are skipped because there is genuinely no insight to encode, no triggers to check, and no capability to propagate.

The loop MUST NOT continue to Phase 9 until state update is complete.

---

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## State Update Protocol

After EVERY goal execution (Steps 1-8, plus Steps 8.5 and 8.75 for productive outcomes):

```
1. UPDATE goal status via Bash: `aspirations-update-goal.sh <goal-id> status <status>`
   - `aspirations-update-goal.sh <goal-id> completed_date <today>` if completed
   - `aspirations-update-goal.sh <goal-id> achievedCount <N+1>`
   - Update streak counters via additional update-goal calls
   - If recurring: compute elapsed = hours_since(lastAchievedAt) BEFORE updating.
     Bash: `aspirations-update-goal.sh <goal-id> lastAchievedAt "$(date +%Y-%m-%dT%H:%M:%S)"`
     If elapsed > 2 * interval_hours: new_streak = 1.
     Otherwise: new_streak = currentStreak + 1.
     ALWAYS update both: currentStreak = new_streak, longestStreak = max(new_streak, longestStreak).

2. Aspiration progress is updated automatically by update-goal when status changes

3. UPDATE last_session via Bash: `aspirations-meta-update.sh last_updated <today>`
   - Append goal.id to goals_completed (tracked in session state)
   - Set aspiration_touched = goal's aspiration id (tracked in session state)
   - Set last_goal_category = goal's resolved category (from goal-selector.py output)
     Bash: echo '"<category>"' | wm-set.sh last_goal_category

4. INCREMENT session_count via Bash: `aspirations-meta-update.sh session_count <N>`
   (Note: session_count increments once per /aspirations loop invocation,
    NOT once per goal. Goals within the same loop share a session.)

# ── Routine outcome early return ──────────────────────────────────
IF outcome_class == "routine":
    # Abbreviated journal entry (Step 7 only of remaining steps)
    7r. WRITE abbreviated journal entry
       - Append to mind/journal/YYYY/MM/YYYY-MM-DD.md:
         "## {timestamp} — Routine: {goal.title}\nNo new items. Streak: {currentStreak}."
       - Update journal index via scripts (same merge/add pattern as full journal):
         - If session entry exists: pipe update JSON to `bash core/scripts/journal-merge.sh <session-num>`
         - If session entry does not exist: pipe new entry JSON to `bash core/scripts/journal-add.sh`
    RETURN  # Skip Steps 5-8.75 — no insight to encode, no triggers to check
# ── End routine early return ──────────────────────────────────────

5. CHECK evolution triggers — Read `core/config/evolution-triggers.yaml` (definitions) and `mind/evolution-triggers.yaml` (state) and check performance-based triggers (see Phase 9 for full protocol). Fixed session-count trigger is DEPRECATED.

6. UPDATE readiness gates via Bash: `aspirations-meta-update.sh readiness_gates '<JSON>'`
   - Re-run gate checks after any state change

7. WRITE journal entry
   - Append to mind/journal/YYYY/MM/YYYY-MM-DD.md
   - Format: "## {timestamp} — Goal: {goal.title}\nSkill: {skill}\nResult: {outcome}\nSpark: {spark_result}"
   - Update journal index via scripts (AUTHORITATIVE OWNER — only exception: /stop step 3.e for emergency cleanup):
     - If session entry exists: pipe update JSON to `bash core/scripts/journal-merge.sh <session-num>`:
       `echo '{"goals_completed":["g-001-01"],"key_events":["..."],"tags":["..."]}' | bash core/scripts/journal-merge.sh <session-num>`
     - If session entry does not exist: pipe new entry JSON to `bash core/scripts/journal-add.sh`:
       `echo '{"session":<N>,"date":"YYYY-MM-DD","journal_file":"mind/journal/YYYY/MM/YYYY-MM-DD.md"}' | bash core/scripts/journal-add.sh`
     - /boot Step 11 writes journal .md content only — it does NOT touch the journal index

8. REFRESH memory tree (dynamic lookup)
   - Find target node:
     node=$(bash core/scripts/tree-find-node.sh --text "{goal.category}" --leaf-only --top 1)
     # Returns: {key, score, file, depth, summary, node_type}
   - Navigate to node.file (use `Edit` to update — never `Write` on existing node files)
   - If goal produced new insight:
     a. EXTRACT PRECISION: Scan execution context for exact values. Build precision manifest —
        each item: {type, label, value (VERBATIM), unit, context}. Types: threshold, formula,
        constant, reference, measurement, config_value. Include ALL numbers, code refs, error codes,
        thresholds, formulas, config values, commit hashes, line numbers. When in doubt, INCLUDE.
        See mind/conventions/precision-encoding.md for schema and extraction heuristics.
     b. WRITE PRECISION BLOCK: IF precision manifest non-empty, append to or create
        "## Verified Values" section in node. Format: - **{label}**: `{value}` {unit} — {context}
     c. WRITE NARRATIVE: Compress qualitative insight into "Key Insights" section. Brief is OK
        because Verified Values carries the exact data.
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
     d. Read mind/developmental-stage.yaml
     e. If new level > highest_capability → update highest_capability

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
            echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh <target_asp>
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
