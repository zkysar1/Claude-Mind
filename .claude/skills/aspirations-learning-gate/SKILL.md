---
name: aspirations-learning-gate
description: "Obligation enforcement — learning gate, retrieval gate, meta-learning signal, periodic reflection, conclusion audit, batch reflection"
user-invocable: false
parent-skill: aspirations
conventions: [aspirations, tree-retrieval, retrieval-escalation, goal-schemas, working-memory]
minimum_mode: autonomous
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
---

# /aspirations-learning-gate — Obligation Enforcement Gates

**CRITICAL**: This is the obligation enforcement sub-skill. It prevents the loop from
continuing without learning. If this sub-skill is skipped, knowledge debt accumulates
and the agent drifts into "busy but not learning" mode.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter.

## Inputs (from orchestrator)

- `goal`: The executed goal
- `outcome_class`: "routine", "standard", or "deep"
- `goals_completed_this_session`: Running counter (all goals)
- `productive_goals_this_session`: Running counter (productive outcomes only)
- `batch_mode`: Boolean (was this a batched execution?)
- `prefetch_goals`: Any pre-fetched research results
- `goals_since_last_tree_update`: From session_signals — encoding drift counter

## Outputs (to orchestrator)

- `all_gates_passed`: Boolean
- `meta_signals_captured`: Count of meta-learning signals

## Phase 9.5: Learning Gate

**For routine outcomes**: Explicit bypass — no tree encoding needed.

**For standard/deep outcomes**: The loop MUST NOT continue without confirming learning occurred.

```
IF outcome_class == "routine":
    Log: "▸ Learning gate: PASS — routine outcome, no encoding needed"

ELIF outcome_class == "standard":
    # Standard tier: tree write is DEFERRED. Verify encoding_queue was populated.
    # Check 1: Was encoding_queue item added for this goal? (State Update Step 8 deferred path)
    Bash: wm-read.sh encoding_queue --json
    last_eq_item = encoding_queue[-1] if encoding_queue else null
    IF last_eq_item AND last_eq_item.source_goal == goal.id:
        Log: "▸ Learning gate: PASS — encoding queued for consolidation (standard tier)"
    ELIF goal produced <100 chars of output OR goal.status in (blocked, skipped):
        Log: "▸ Learning gate: PASS — no encoding needed (insufficient output or blocked goal)"
    ELSE:
        # ENCODING WAS MISSED — check sensory buffer before falling back
        Bash: wm-read.sh sensory_buffer --json
        related_items = [item for item in sensory_buffer
                         if item.encoding_score >= 0.40
                         AND (item.source_goal == goal.id OR item.category == goal.category)]
        IF len(related_items) > 0:
            Output: "▸ LEARNING GATE CATCH: found {len(related_items)} high-value buffer items — forcing inline encoding"
            top = max(related_items, key=encoding_score)
            node=$(bash core/scripts/tree-find-node.sh --text "{top.target_article or goal.category}" --leaf-only --top 1)
            Read node.file → compress top.observation into "Key Insights" → Edit
            Log: "▸ Learning gate: RECOVERED via buffer — inline encoding to {node.key}"
        ELSE:
            # Force inline recovery from execution context directly
            Output: "▸ LEARNING GATE CATCH: encoding not queued for {goal.id} — forcing inline"
            node=$(bash core/scripts/tree-find-node.sh --text "{goal.category}" --leaf-only --top 1)
            # ... perform inline tree encoding as recovery ...
            Log: "▸ Learning gate: RECOVERED — inline encoding completed for {goal.id}"

    # Check 2: Was journal entry written? (State Update Step 7)
    # Check 3: Was working memory updated with goal context?
    Bash: wm-read.sh active_context --json
    # Verify active_context reflects current goal execution.

ELIF outcome_class == "deep":
    # Deep tier: tree write should have happened inline. Verify it did.
    # Check 1: Was knowledge tree updated? (State Update Step 8)
    #   If no: complete it NOW — read _tree.yaml, find matching node,
    #   compress goal insight, edit node .md, propagate up chain.
    # Check 2: Was journal entry written? (State Update Step 7)
    # Check 3: Was working memory updated with goal context?

    Bash: wm-read.sh active_context --json
    # Verify active_context reflects current goal execution.

    # ── HARDENED ESCAPE HATCH ─────────────────────────────────────────
    # "No tree encoding needed" requires STRUCTURAL justification, not self-assessment.
    # Valid reasons: goal was blocked/skipped, goal produced <100 chars of output.
    # Invalid: "findings already known", "nothing new" (for investigation goals).
    IF tree_was_NOT_updated:
        Bash: wm-read.sh sensory_buffer --json
        related_items = [item for item in sensory_buffer
                         if item.encoding_score >= 0.40
                         AND (item.source_goal == goal.id OR item.category == goal.category)]
        IF len(related_items) > 0:
            Output: "▸ LEARNING GATE: rejected 'no insight' claim — {len(related_items)} high-value buffer items found for {goal.category}"
            top = max(related_items, key=encoding_score)
            node=$(bash core/scripts/tree-find-node.sh --text "{top.target_article or goal.category}" --leaf-only --top 1)
            Read node.file
            Compress top.observation into "Key Insights" section (1-3 sentences)
            Edit node.file with updates
            bash core/scripts/tree-update.sh --set <node.key> last_updated $(date +%Y-%m-%d)
            Output: "▸ LEARNING GATE: forced encoding to {node.key}"
        ELIF goal produced <100 chars of output OR goal.status in (blocked, skipped):
            Log: "▸ Learning gate: PASS — no encoding needed (insufficient output or blocked goal)"
        ELSE:
            Log: "▸ LEARNING GATE WARNING: no tree update despite substantive output — flagging as knowledge debt"
            echo '{"node_key": "general", "reason": "deep_outcome_without_encoding", "source_goal": "'"${goal.id}"'", "priority": "HIGH", "created": "'"$(date +%Y-%m-%d)"'"}' | Bash: wm-append.sh knowledge_debt
    # ── End hardened escape hatch ─────────────────────────────────────
```

## Phase 9.5a: Meta-Learning Signal Capture

One question: "Did the way I learned from this goal suggest a better procedure?"

```
IF meta_insight_detected:
    echo '{"date":"<today>","event":"meta_signal","goal_id":"<goal.id>",
           "insight":"<insight>"}' | bash core/scripts/meta-log-append.sh
```

## Phase 9.5b: Retrieval Gate (MANDATORY — runs for ALL outcomes)

Verify retrieval happened and utilization feedback completed.

**NOTE: The `utilization-gate.sh` PreToolUse hook (Layer 3 programmatic enforcement)
handles the critical path — it auto-applies all-noise feedback before state-update
if Phase 4.26 was skipped. This gate is now a secondary check for escalation quality
and retroactive retrieval when retrieval itself was skipped entirely.**

```
# Check session file first (primary source), fall back to WM manifest (legacy)
SESSION_FILE="<agent>/session/retrieval-session.json"
IF session file exists:
    IF utilization_pending == true:
        # Hook should have caught this — run feedback as safety net
        Bash: utilization-feedback.sh --goal {goal.id} --all-noise
        Output: "▸ RETRIEVAL GATE: forced utilization feedback for {goal.id}"
    # else: already processed by Phase 4.26 or hook — pass

ELIF goal.category maps to existing tree nodes:
    # RETRIEVAL WAS SKIPPED ENTIRELY — no session file written
    Bash: load-execute-protocol.sh → IF path returned: Read it
    Follow retrieval steps, then run Phase 4.26 utilization feedback
    Output: "▸ RETRIEVAL GATE: forced retroactive retrieval for {goal.id}"

# Escalation quality check (per retrieval-escalation convention)
# Read WM manifest for enrichment data if available
Bash: wm-read.sh active_context.retrieval_manifest --json
IF retrieval_manifest exists AND retrieval_manifest.sufficient == false:
    max_tier = max(retrieval_manifest.tiers_used or [1])
    IF max_tier == 1 AND goal relates to codebase work:
        Output: "▸ ESCALATION GAP: Tier 1 insufficient but Tier 2 (codebase) not attempted for {goal.id}"
    IF max_tier <= 2 AND goal involves external knowledge:
        Bash: session-mode-get.sh
        IF mode != reader:
            Output: "▸ ESCALATION GAP: Tiers 1-2 insufficient but Tier 3 (web) not attempted for {goal.id}"
# Escalation gaps are logged as learning signals for reflection, not hard blockers.

# If goal genuinely has no matching tree nodes: pass silently.
```

## Phase 9.5-exp: Experience Archival Gate (MANDATORY for standard/deep outcomes)

Verify Phase 4.25 experience archival completed for non-routine outcomes.

```
IF outcome_class in ("standard", "deep"):
    Bash: wm-read.sh active_context.experience_refs --json
    IF experience_refs is empty, missing, or null:
        Output: "▸ EXPERIENCE GATE CATCH: Phase 4.25 skipped for {goal.id} — writing recovery record"
        experience_id = "exp-{goal.id}-recovery"
        Write <agent>/experience/{experience_id}.md with:
            - Goal: {goal.title}
            - Outcome: {outcome_summary}
            - Note: Recovery record — Phase 4.25 was skipped during execution.
              Full reasoning trace not available.
        echo '<experience-json>' | bash core/scripts/experience-add.sh
        Experience JSON:
            id: "{experience_id}"
            type: "goal_execution"
            created: "{ISO timestamp}"
            category: "{goal.category}"
            summary: "Recovery: {goal.title} — {outcome_summary}"
            goal_id: "{goal.id}"
            tree_nodes_related: []
            verbatim_anchors: []
            content_path: "<agent>/experience/{experience_id}.md"
        Output: "▸ EXPERIENCE GATE: recovery record written"
    ELSE:
        Log: "▸ Experience gate: PASS"
```

## Phase 9.5c: Unreflected Hypothesis Check (MANDATORY for all outcomes)

```
Bash: pipeline-read.sh --unreflected 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d))"
IF unreflected_count > 0:
    Output: "▸ UNREFLECTED HYPOTHESES: {unreflected_count} resolved but unlearned"
    invoke /review-hypotheses --learn
```

## Post-Batch Reflection (when batch_mode is true)

Quick structured pause after all batch goals complete:

```
1. What patterns or connections emerged across the batch goals?
2. Any surprises or corrections that should become hypotheses?
   → If yes: add to working memory encoding_queue
3. Any knowledge tree nodes that need reconciliation across batch findings?

echo '<observation_json>' | Bash: wm-append.sh sensory_buffer
If any surprise > 7: form session-level hypothesis via pipeline-add.sh
Time budget: 1-2 minutes, not a deep reflection.
```

## Phase 9.7: Periodic Reflection Checkpoint (every 5 goals)

```
IF goals_completed_this_session % 5 == 0:
    # 5-question inline pause:
    1. What patterns have I seen across the last 5 goals?
    2. Any recurring surprises or corrections? → pipeline-add.sh
    3. Any knowledge nodes growing stale? → wm-append.sh knowledge_debt
    4. Conclusion audit: scan conclusions slot for stale/weak judgments
    5. Encoding frequency check: how many goals since last tree update?

    # Q5 Encoding Drift Detector:
    # Uses the orchestrator's goals_since_last_tree_update counter (passed via inputs).
    # Also checks encoding_queue and sensory_buffer for accumulated unencoded items.
    IF goals_since_last_tree_update >= 3:
        Output: "▸ ENCODING DRIFT DETECTED: {goals_since_last_tree_update} goals without tree update"
        Bash: wm-read.sh sensory_buffer --json
        Bash: wm-read.sh encoding_queue --json
        high_value_items = [item for item in (sensory_buffer + encoding_queue)
                            if item.encoding_score >= 0.40]
        IF len(high_value_items) > 0:
            top = max(high_value_items, key=encoding_score)
            node=$(bash core/scripts/tree-find-node.sh --text "{top.target_article or top.category}" --leaf-only --top 1)
            IF node found:
                Read node.file
                Compress top.observation into "Key Insights" section (1-3 sentences)
                Edit node.file with updates
                bash core/scripts/tree-update.sh --set <node.key> last_updated $(date +%Y-%m-%d)
                Output: "▸ ENCODING DRIFT RECOVERY: forced encoding to {node.key} (score {top.encoding_score:.2f})"
        ELSE:
            echo '{"node_key": "general", "reason": "encoding_drift_checkpoint", "source_goal": "periodic-5-goal", "priority": "HIGH", "created": "'"$(date +%Y-%m-%d)"'"}' | Bash: wm-append.sh knowledge_debt
            Output: "▸ ENCODING DRIFT WARNING: no encodable items found — logged HIGH-priority knowledge debt"

    # Q4 Conclusion Audit Detail:
    Bash: wm-read.sh conclusions --json
    FOR EACH conclusion:
        # Re-verify stale blocking conclusions
        IF conclusion.re_verify_at AND now >= re_verify_at AND outcome is null:
            IF conclusion.blocks_goals is non-empty:
                Run independent re-probe
                IF contradicts: update outcome = "wrong", clear blockers
                ELSE: extend re_verify_at += 30 minutes

        # Flag low-evidence conclusions
        real_signals = count(e for e in evidence if e.weight > 0)
        IF real_signals < 2 AND blocks_goals non-empty AND outcome is null:
            Log: "WEAK CONCLUSION: '{conclusion}' — {real_signals} signal(s) but blocks {N} goals"

    echo '<observation_json>' | Bash: wm-append.sh sensory_buffer
```

## Phase 9.8: Full-Cycle Reflection Obligation (every 15 productive goals)

Mandatory deep learning checkpoint that fires based on goal count, not goal selection.
This prevents full-cycle reflection from being perpetually deferred by higher-scoring
productive work in the goal selector.

```
# Threshold from meta/reflection-strategy.yaml → mode_preferences.full_cycle_cadence_goals (default 15)
IF productive_goals_this_session > 0 AND productive_goals_this_session % full_cycle_cadence_goals == 0:
    # Team-aware deferral: if an orchestrator agent is active, defer to it
    Bash: board-read.sh --channel coordination --since 1h --json 2>/dev/null
    IF output contains messages from a non-self agent (e.g., "bravo"):
        Log: "DEFERRED: full-cycle reflection — orchestrator agent active"
    ELSE:
        Output: "▸ REFLECTION OBLIGATION: 15 productive goals reached — running full-cycle"
        invoke /review-hypotheses --learn   (catch up on unreflected)
        invoke /reflect --full-cycle        (patterns, calibration, strategy)
        Log: "OBLIGATION: full-cycle reflection after {productive_goals_this_session} productive goals"
```

## Pre-Fetched Research Application

```
IF prefetch_goals had agents dispatched:
    FOR EACH completed team agent research report:
        Store findings as pre-gathered context for the goal
        Execute goal with enriched context
        Run full Phase 4-9 cycle for each prefetched goal
    Shutdown team agents
    Bash: pending-agents.sh deregister-team --team "research-{session}"
```

## Chaining

- **Called by**: `/aspirations` orchestrator (Phase 9.5-9.8, every iteration)
- **Calls**: `wm-read.sh`, `wm-append.sh`, `meta-log-append.sh`, `pipeline-add.sh`, `pipeline-read.sh`, `load-execute-protocol.sh`, `experience-add.sh` (Phase 9.5-exp), `/review-hypotheses --learn` (Phase 9.5c)
- **Reads**: Working memory, retrieval manifest, conclusions
