---
name: aspirations-learning-gate
description: "Enforces learning obligations inside the aspirations loop: the learning gate, retrieval gate, meta-learning signal, periodic reflection cadence, conclusion audit, and batch reflection. Use whenever a goal completes and the loop must verify that encoding, retrieval, and reflection actually happened before moving to the next goal. Catches sessions that ship commits without producing tree encodings or hypothesis resolutions. Internal sub-skill — never invoke directly."
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
revision_id: "skill-bootstrap-aspirations-learning-gate-634fc1"
previous_revision_id: null
---

# /aspirations-learning-gate — Obligation Enforcement Gates

**CRITICAL**: This is the obligation enforcement sub-skill. It prevents the loop from
continuing without learning. If this sub-skill is skipped, knowledge debt accumulates
and the agent drifts into "busy but not learning" mode.

## Abbreviation Policy

Mandatory writes for this obligation: see `core/config/obligation-schema.yaml`
→ `obligations.learn`. Abbreviation is permitted only when
`context_budget.zone == tight`. The zone is distance-to-autocompact, not raw
usage — see `core/scripts/context-budget-status.py` `classify_zone` for the
source of truth. Before abbreviating, `Bash: bash
core/scripts/context-budget-banner.sh` and quote its output; the banner line
is the evidence that makes the "tight" claim verifiable. When abbreviating,
log TWO lines in the journal entry (in this order):
  `OBLIGATION ABBREVIATED: learn — {condition}`
  `CTX: raw N% | of-autocompact N% | zone tight | headroom N tokens | env ... | updated ...`
The banner line must be the actual output captured from the banner script in
this iteration. `core/scripts/context-citation-audit.sh` scans for the pair
and reports any tight-zone claim that lacks its banner line. Then satisfy
`minimum_inline` (sensory-buffer append OR reasoning-bank-add, then
`wm-set loop_state`). Otherwise invoke this Skill literally. Phase 9.5d
(below) audits the PREVIOUS iteration's abbreviation claims against the
context-budget state captured at that time; false claims accumulate in
`agents/<agent>/session/obligation-audit.jsonl` and 3+ in a session auto-file a
single `Investigate: false-abbreviation-claims` goal.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter.

## Inputs (from orchestrator)

- `goal`: The executed goal
- `outcome_class`: "routine" or "deep"
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

**For deep outcomes**: The loop MUST NOT continue without confirming learning occurred.

```
IF outcome_class == "routine":
    Log: "▸ Learning gate: PASS — routine outcome, no encoding needed"

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
    # Valid reasons: goal was blocked/skipped, goal produced <100 chars of output,
    #   or encoding was coordination-deferred (queued to prevent multi-agent overwrite).
    # Invalid: "findings already known", "nothing new" (for investigation goals).
    IF tree_was_NOT_updated:
        # Check if encoding was coordination-deferred (not a learning failure)
        Bash: wm-read.sh encoding_queue --json
        last_eq = encoding_queue[-1] if encoding_queue else null
        IF last_eq AND last_eq.source_goal == goal.id AND last_eq.source_type == "coordination_deferred":
            Log: "▸ Learning gate: PASS — encoding queued (coordination deferred to prevent overwrite)"
        ELSE:
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
                # T21 PostToolUse hook auto-bumps last_updated — no explicit
                # `tree-update.sh --set last_updated` call required.
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

**NOTE: `iteration-close.sh`'s `_repair_utilization_pending` handles the critical
path. It runs inside `do_state_update` immediately BEFORE `phase-4-26-gate.sh`,
and applies `--infer --confidence balanced` (falling back to `--all-unknown` on
schema<2) when Phase 4.26 left `utilization_pending=true`. This gate is a
secondary check for escalation quality and retroactive retrieval when retrieval
itself was skipped entirely; it calls the same helper again as a backstop, which
no-ops when state-update already cleared the flag.**

**The `utilization-gate.sh` PreToolUse[Skill] hook covers ONLY direct
`Skill(aspirations-state-update)` invocations — /boot, consolidation, and ad-hoc
callers that bypass `iteration-close.sh`. It does NOT and structurally CANNOT
cover the Bash hot path (measured 15 fires / 12,325 skill invocations across 5
agents; bravo 0 / 2,552). Any claim that the hook alone guarantees non-zero
utilization data on the loop path is false — g-115-3123.**

```
# Check session file first (primary source), fall back to WM manifest (legacy)
SESSION_FILE="agents/<agent>/session/retrieval-session.json"
IF session file exists:
    IF utilization_pending == true:
        # Hook should have caught this — run feedback as safety net
        Bash: utilization-feedback.sh --goal {goal.id} --all-unknown
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

# ── Memory-utility curation scan (advisory, cadenced — g-115-1468 / Phase 1d) ──
# HOT-PATH HOME: core/config/iteration-close-digest.md § LEARNING-GATE bullet 7.
# This SKILL.md is the full-protocol copy for /boot, consolidation, and edge
# cases — NOT the hot path; keep both in sync. The earn-the-keep KPI flip:
# weight "was this later retrieved AND useful?" over "how much did we write?".
IF goals_completed_this_session % 20 == 0:
    Bash: export MIND_AGENT=<agent>; source core/scripts/_paths.sh
    FOR store in (reasoning-bank, guardrails):
        Bash: py -3 core/scripts/retrieval_utility_report.py --store "$WORLD_DIR/{store}.jsonl"
    # From each report: zero_hit_high_exposure (retrieved >=5x, never helpful —
    # noise) + never_retrieved (dead weight). Write counts + a capped sample
    # (first 25 ids each, per store) to the memory_curation_candidates WM slot
    # (overwrite) for /reflect --curate-memory.
    echo '{"reasoning-bank": {...}, "guardrails": {...}, "scanned_at": "<iso>"}' | Bash: wm-set.sh memory_curation_candidates
    # ADVISORY ONLY — NEVER auto-retire (guard-707): low times_helpful is usually
    # under-attestation, not zero value; retirement is a /reflect-gated decision.

# If goal genuinely has no matching tree nodes: pass silently.
```

## Phase 9.5-exp: Experience Archival Gate (MANDATORY for deep outcomes)

Verify Phase 4.25 experience archival completed for non-routine outcomes.

```
IF outcome_class == "deep":
    # ASK THE STORE, NOT THE POINTER (g-115-4876). This gate used to read
    # active_context.experience_refs and pass on "not empty/missing/null".
    # That predicate is GOAL-AGNOSTIC — it never compared the stored refs
    # against THIS goal — so any non-empty leftover passed it forever.
    # Measured: cc-03 passed on a ref 3 days stale from an unrelated goal,
    # cc-05 on one 11 days stale. The WM pointer is a cache of the last
    # writer's intent; the experience STORE is the thing the gate is
    # actually asserting about, and it is keyed BY goal.
    Bash: experience-read.sh --goal {goal.id}
    # rc is NOT the discriminator — measured, BOTH a hit and a miss return
    # rc=0 (a miss returns the empty array `[]`). A gate keying on rc would
    # pass unconditionally, which is this same defect in a new costume.
    # Branch on the returned ARRAY being empty.
    IF the returned array is empty:
        Output: "▸ EXPERIENCE GATE CATCH: Phase 4.25 skipped for {goal.id} — writing recovery record"
        experience_id = "exp-{goal.id}-recovery"
        Write agents/<agent>/experience/{experience_id}.md with:
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
            content_path: "agents/<agent>/experience/{experience_id}.md"
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

## Phase 9.5d: Abbreviated-Obligation Audit (NON-BLOCKING)

Audit is performed by `abbreviated-obligation-audit.sh --apply`. The script
parses today's journal, regex-matches `OBLIGATION ABBREVIATED:` claims in
the current iteration's section, validates each against
`obligation-schema.yaml` + `iteration-checkpoint.json`, appends records to
`agents/<agent>/session/obligation-audit.jsonl`, and flags `threshold_breached`
when cumulative false claims reach the configured threshold.

```
Bash: bash core/scripts/abbreviated-obligation-audit.sh --apply
Read JSON result:
  flags = []:                              no false claims; proceed
  flags = ["false_claims"]:                false claim(s) this iter — log, continue (non-blocking)
  flags = ["false_claims", "threshold_breached"]:
                                           file ONE `Investigate: false-abbreviation-claims`
                                           goal if investigate_goal_already_exists is false
```

On `threshold_breached`, the LLM caller files the
`Investigate: false-abbreviation-claims` goal (script emits the flag; goal
creation stays with the LLM per the capability-before-user routing). Per-claim
detail is always logged to `agents/<agent>/session/obligation-audit.jsonl`
regardless of threshold state.

Why NOT read `agents/<agent>/session/context-budget.json` in the audit: it is
rewritten every statusLine fire. The banner line embedded in the journal
entry (captured AT claim time by `context-budget-banner.sh`) is the only
faithful claim-time capture. See rb-313, guard-301.

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
                # T21 PostToolUse hook auto-bumps last_updated.
                Output: "▸ ENCODING DRIFT RECOVERY: forced encoding to {node.key} (score {top.encoding_score:.2f})"
        ELSE:
            echo '{"node_key": "general", "reason": "encoding_drift_checkpoint", "source_goal": "periodic-5-goal", "priority": "HIGH", "created": "'"$(date +%Y-%m-%d)"'"}' | Bash: wm-append.sh knowledge_debt
            Output: "▸ ENCODING DRIFT WARNING: no encodable items found — logged HIGH-priority knowledge debt"

    # Q6: Tree growth check (are decompose candidates accumulating?)
    Bash: tree-read.sh --decompose-candidates
    IF candidates output is non-empty (at least 1 candidate):
        Output: "▸ TREE GROWTH CHECK: {len(candidates)} nodes ready for decomposition"
        Invoke /tree maintain
        Log: "TREE GROWTH: triggered /tree maintain from learning gate ({len(candidates)} candidates)"

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

## Loop Re-Entry (MANDATORY — last action of this skill)

After ALL gates and checks above are complete, this skill is the LAST phase of the
iteration. It MUST end with LOOP_CONTINUE to re-enter the aspirations loop.

```
# LOOP_CONTINUE: read-merge-write loop_state, then re-invoke the loop.
# This is the mechanical heartbeat — without this tool call, the session dies.
#
# READ-MERGE-WRITE, never a bare overwrite (g-115-1072). The bash gates
# (loop-state-bump-counters.py Phase 8; recurring-loop-state-mutate.py) are the
# SINGLE WRITERS for the counters + most signals — they wrote authoritative
# values to the WM FILE this iteration, invisible to the orchestrator's in-memory
# copy (synced at Phase -0.5). A bare `echo '<loop_state as JSON>' | wm-set.sh
# loop_state` from stale variables REVERTS them (goals_completed 47→46, dropped
# counted_goals). Slot-specific, so siblings like pending_phase_6_spark survive —
# the exact reversion signature. g-283 retired the per-iteration mirror but left
# THIS overwrite; this completes it. Same pattern as aspirations-all-blocked Step
# B3 and recurring-close.sh:166-170's documented contract.
Bash: wm-read.sh loop_state --json
merged = current_loop_state                  # fresh — carries bash-gate counters + signals
# g-115-1561: evolutions / last_evolution_at / alignment_check_at / touched are
# now BASH-OWNED (loop-state-bump-counters.py: --goal-id increments alignment +
# touched every close; --reset-alignment at aspirations-select; --evolution-fired
# at aspirations-evolve). They are NO LONGER overlaid here — overlaying the
# in-context copies would CLOBBER the bash writes (e.g. select fires
# --reset-alignment → disk=0 → iteration-close --goal-id → disk=1; a stale
# in-context overlay of 0 reverts it). The four were orphaned BECAUSE this
# LLM overlay was unreliable (zeta g-115-1557: touched=[] universally despite
# 68-76 goals); bash ownership is the fix. Only the circuit-breaker pair below
# stays LLM-owned (no bash writer yet — tracked separately).
# Mutate signals IN PLACE (preserve bash-owned subkeys — routine_*, productive_streak,
# *_cooldown_streak, *_tree_update; never replace merged.signals wholesale):
merged.signals.consecutive_goal_failures = session_signals.consecutive_goal_failures
merged.signals.last_failed_goal_id = session_signals.last_failed_goal_id
echo '<merged as JSON>' | Bash: wm-set.sh loop_state
Skill('aspirations') with args='loop'
```

NEVER end this skill with text output like "Returning to orchestrator."
NEVER skip the Skill() call. The orchestrator does NOT continue after this skill — 
control flows directly from here to the next iteration via LOOP_CONTINUE.

## Chaining

- **Called by**: `/aspirations` orchestrator (Phase 12, every iteration — last phase before loop re-entry)
- **Calls**: `wm-read.sh`, `wm-append.sh`, `meta-log-append.sh`, `pipeline-add.sh`, `pipeline-read.sh`, `load-execute-protocol.sh`, `experience-add.sh` (Phase 9.5-exp), `/review-hypotheses --learn` (Phase 9.5c)
- **Returns**: Does NOT return to orchestrator — calls LOOP_CONTINUE directly
- **Reads**: Working memory, retrieval manifest, conclusions

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
Structurally safe: the terminal action is LOOP_CONTINUE (Skill call to /aspirations
with args='loop'). Never add a text summary after LOOP_CONTINUE.
