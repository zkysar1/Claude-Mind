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
- `outcome_class`: "routine" or "productive"
- `goals_completed_this_session`: Running counter (all goals)
- `productive_goals_this_session`: Running counter (productive outcomes only)
- `batch_mode`: Boolean (was this a batched execution?)
- `prefetch_goals`: Any pre-fetched research results

## Outputs (to orchestrator)

- `all_gates_passed`: Boolean
- `meta_signals_captured`: Count of meta-learning signals

## Phase 9.5: Learning Gate

**For routine outcomes**: Explicit bypass — no tree encoding needed.

**For productive outcomes**: The loop MUST NOT continue without confirming learning occurred.

```
IF outcome_class == "routine":
    Log: "No tree encoding needed — routine outcome"
ELSE:
    # Check 1: Was knowledge tree updated? (State Update Step 8)
    #   If no: complete it NOW — read _tree.yaml, find matching node,
    #   compress goal insight, edit node .md, propagate up chain.
    # Check 2: Was journal entry written? (State Update Step 7)
    # Check 3: Was working memory updated with goal context?

    Bash: wm-read.sh active_context --json
    # Verify active_context reflects current goal execution.
    # If goal produced no new insight (blocked, duplicate): note explicitly.
    # "No tree encoding needed — {reason}."
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

```
Bash: wm-read.sh active_context.retrieval_manifest --json

IF retrieval_manifest missing AND goal.category maps to existing tree nodes:
    # RETRIEVAL WAS SKIPPED — perform it NOW
    Bash: load-execute-protocol.sh → IF path returned: Read it
    Follow retrieval steps, then run Phase 4.26 utilization feedback
    Output: "▸ RETRIEVAL GATE: forced retroactive retrieval for {goal.id}"

ELIF retrieval_manifest exists AND retrieval_manifest.utilization_pending == true:
    # Phase 4.26 was interrupted — run it NOW
    Run utilization feedback using retrieval_manifest
    Output: "▸ RETRIEVAL GATE: forced utilization feedback for {goal.id}"

# Escalation quality check (per retrieval-escalation convention)
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
    # 4-question inline pause:
    1. What patterns have I seen across the last 5 goals?
    2. Any recurring surprises or corrections? → pipeline-add.sh
    3. Any knowledge nodes growing stale? → wm-append.sh knowledge_debt
    4. Conclusion audit: scan conclusions slot for stale/weak judgments

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
- **Calls**: `wm-read.sh`, `wm-append.sh`, `meta-log-append.sh`, `pipeline-add.sh`, `pipeline-read.sh`, `load-execute-protocol.sh`, `/review-hypotheses --learn` (Phase 9.5c)
- **Reads**: Working memory, retrieval manifest, conclusions
