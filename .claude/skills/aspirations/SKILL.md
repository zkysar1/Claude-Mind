---
name: aspirations
description: "Autonomous goal loop engine — the perpetual heartbeat that selects, executes, evolves, and generates goals forever"
user-invocable: false
triggers:
  - "/aspirations"
sub_commands:
  - status
  - next
  - loop
  - evolve
  - "complete <goal-id>"
  - "add <title>"
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [aspirations, pipeline, goal-schemas, session-state, handoff-working-memory, infrastructure, reasoning-guardrails, experience, goal-selection]
minimum_mode: autonomous
---

# /aspirations — Perpetual Goal Loop Engine (Slim Orchestrator)

The heartbeat of the continual learning agent. Delegates ALL phase work to sub-skills
that load on-demand and fall out of context at compaction. This orchestrator is ~350 lines
to minimize re-entry cost after autocompact (down from 1,633 lines).

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter.

## Core Principle: No Terminal State

The system ALWAYS has work to do. If it doesn't, it creates work.
Completion of one thing seeds the next thing.

## Sub-Commands

### `status`

Display current aspiration state:
1. `aspirations-read.sh --active` + `--meta` (readiness gates, session_count)
2. Show readiness gates, aspirations, goals, recurring status, evolution log, user actions, meta-memory

### `next`

Select and execute ONE goal, then return:
1. invoke /aspirations-precheck → invoke /aspirations-select → execute → verify → state update
2. Return result

## Aspiration Update Notification (MANDATORY)

Whenever goals are added to an existing aspiration, notify the user with a summary.
If unable to reach user, create a `participants: [user]` goal. Do NOT block.

## Autonomous Solution Attempt Protocol (ASAP)

Before writing ANY entry to `<agent>/session/pending-questions.yaml`:
1. Search knowledge tree, reasoning bank, guardrails, experience archive
2. If found: ATTEMPT it. If succeeds: skip pq-XXX
3. If fails: write pq-XXX with `attempted_solutions` + `autonomous_search_done: true`

### `loop`

**The perpetual heartbeat.** Select and execute goals continuously until explicitly stopped:

```
Bash: aspirations-read.sh --meta → get session_count
Bash: load-aspirations-compact.sh → IF path returned: Read it

# Phase -1.5: Agent State Gate Check
Bash: `session-state-get.sh` → IF NOT "RUNNING": ABORT

Bash: aspirations-meta-update.sh session_count <N+1>

# Phase -1: Initialize Working Memory (runs once, idempotent)
Bash: wm-read.sh --json
if all slots null:
    Read core/config/memory-pipeline.yaml
    Bash: wm-init.sh
    echo '{"session_id": "session-{session_count}", "session_start": "<today>"}' | Bash: wm-set.sh active_context
    Seed recent_violations, pending_resolutions from scripts
    IF handoff.yaml has known_blockers_active: seed known_blockers

# Phase -0.5: Session Marker + Recovery Counter Reset
Bash: `session-signal-set.sh loop-active`
Bash: `session-counter-clear.sh`
Bash: `cp <agent>/session/latest-session-id <agent>/session/running-session-id 2>/dev/null || true`
goals_completed_this_session = 0
productive_goals_this_session = 0
evolutions_this_session = 0
last_evolution_goal_count = 0
goals_since_last_alignment_check = 0
aspirations_touched_this_session = set()
session_signals = {
    routine_streak_global: 0,
    productive_streak: 0
}

# Phase -0.5a: Background Agent Result Collection
IF <agent>/session/pending-agents.yaml EXISTS:
    # list --json prunes stale agents (past timeout_minutes) before returning.
    # Stale agents are removed from the file and excluded from output.
    Bash: `pending-agents.sh list --json`
    FOR EACH non-stale agent: collect results, deregister. IF none pending: clear.

# Phase -0.5c: Compact Checkpoint Processing
IF <agent>/session/compact-checkpoint.yaml EXISTS:
    Read checkpoint. Restore encoding_queue, retrieval_manifest,
    knowledge_debt, micro_hypotheses from checkpoint.
    # PRECISION-FIRST ENCODING: queue items contain precision_manifests — process before resuming
    Process encoding queue (budget: min(5, queue_length)).
    Delete checkpoint (one-shot).

# Phase -0.5d: Identity Context Restoration
Read <agent>/self.md
Read world/program.md (skip if empty/missing)
```

## ═══ PER-ITERATION OBLIGATIONS (MANDATORY — never skip) ═══

After every goal execution, these MUST complete before `continue`:
1. **VERIFY**: `invoke /aspirations-verify` — did the goal succeed?
2. **STATE**: `invoke /aspirations-state-update` — tree encoding, journal, capability
3. **LEARN**: `invoke /aspirations-learning-gate` — learning gate, retrieval gate, reflection
4. **MAINTAIN**: Working memory maintenance — sensory buffer, aging, prune

Skip rules:
- Spark: SKIP if outcome_class == "routine"
- Completion review: SKIP if aspiration not fully complete
- Evolution: respects per-session cap (max_evolutions_per_session)

If ANY obligation is skipped without justification, log "OBLIGATION SKIPPED: {phase}".

```
FOREVER:
    # ── PRE-SELECTION (Phases 0-1) ──
    invoke /aspirations-precheck
    # Returns: aspirations compact refreshed, blockers updated, recurring checked

    # ── GOAL SELECTION (Phases 2-2.9) ──
    invoke /aspirations-select
    # Returns: goal, effort_level, batch_mode, batch, ranked_goals, prefetch_goals, selection_reason

    if goal is None AND selection_reason starts with "all_blocked":
        # ── ALL-BLOCKED PATH ("idle with dignity") ──
        # Goals exist but all are blocked on external dependencies.
        # Principle: "real advancement over apparent activity"
        # Don't manufacture busywork. Wait, then check again.
        Output: "▸ ALL BLOCKED — goals waiting on external dependencies"
        Output: blocked goal summaries from selection context
        Output: "▸ Waiting 10 minutes before next check"
        Bash (timeout 600000): sleep 600
        continue

    if goal is None:
        # No-goals path (genuinely no goals exist) — NOT a stop condition
        # ASSERT: goal-selector.sh returned [] — see core/config/conventions/goal-selection.md
        invoke /create-aspiration from-self --plan
        if new_aspirations_generated: continue
        # ASAP fallback: search tree, reasoning bank, experience for work
        invoke /research-topic (explore broadly)
        invoke /reflect --full-cycle
        continue

    # ── DECOMPOSE (Phase 3) ──
    if goal is compound (title has "and", vague verbs, multi-skill, multi-session):
        invoke /decompose goal.id
        if goal.status == "decomposed": continue  # re-select from sub-goals

    # ── EXECUTE (Phase 4) ──
    Bash: aspirations-update-goal.sh <goal-id> status in-progress
    Bash: aspirations-update-goal.sh <goal-id> started <today>
    echo "Working on: ${goal.title}" | Bash: board-post.sh --channel coordination
    # Load the execute protocol DIGEST (144 lines) — NOT the full 883-line skill.
    # The digest contains the complete execution protocol. Follow it inline.
    # For rare edge cases (CREATE_BLOCKER, Cognitive Primitives JSON):
    #   Read .claude/skills/aspirations-execute/SKILL.md directly.
    Bash: load-execute-protocol.sh → IF path returned: Read it
    # Follow the loaded protocol. Inputs: goal, effort_level, batch_mode, batch
    # Returns: result, outcome_class, infrastructure_failure

    IF infrastructure_failure: continue  # Skip Phases 5-9

    # Routine streak tracking (per-goal anti-drift safeguard)
    IF outcome_class == "routine":
        routine_streaks[goal.id] += 1
        IF routine_streaks[goal.id] >= 5:
            outcome_class = "productive"  # force full pipeline
            routine_streaks[goal.id] = 0
    ELIF outcome_class == "productive":
        routine_streaks[goal.id] = 0

    # Session signal tracking (streak counters for global anti-drift)
    IF outcome_class == "routine":
        session_signals.routine_streak_global += 1
        session_signals.productive_streak = 0
    ELSE:
        session_signals.routine_streak_global = 0
        session_signals.productive_streak += 1

    # Global anti-drift safeguard (across ALL goals, not per-goal-id)
    # Threshold 8 is higher than per-goal threshold (5) because cycling through
    # different recurring goals is less concerning than one goal going stale.
    IF session_signals.routine_streak_global >= 8:
        outcome_class = "productive"  # force full pipeline
        session_signals.routine_streak_global = 0
        Log: "GLOBAL ANTI-DRIFT: forced productive after 8 consecutive routine outcomes"

    # Count productive goals AFTER all reclassification (per-goal + global anti-drift)
    IF outcome_class == "productive":
        productive_goals_this_session += 1

    # ── VERIFY (Phase 5) ── ← OBLIGATION
    invoke /aspirations-verify with: goal, result

    # ── SPARK (Phase 6) ──
    IF outcome_class == "productive":
        invoke /aspirations-spark with: goal, result, effort_level

    # ── COMPLETION REVIEW (Phase 7-7.6) ──
    asp = get_aspiration(goal)
    all_recurring = all(g.get("recurring", False) for g in asp.goals)
    if not all_recurring and aspiration_fully_complete(asp):
        invoke /aspirations-complete-review with: asp, goal

    # ── STATE UPDATE (Phase 8) ── ← OBLIGATION
    invoke /aspirations-state-update with: goal, result, session_count, outcome_class

    # Phase 8.1: Session touch tracking
    IF asp.id not in aspirations_touched_this_session:
        aspirations_touched_this_session.add(asp.id)
        Increment asp.sessions_active via aspirations-update.sh

    # ── EVOLUTION (Phase 9) ──
    # Part A: Cadence/lifecycle triggers (every iteration)
    cadence_triggers = check_cadence_triggers()
    # Part A.1: Goal-based cadence trigger (mid-session evolution)
    goals_since_last_evolution = goals_completed_this_session - last_evolution_goal_count
    IF goals_since_last_evolution >= evolution_goal_cadence.goals_without_evolution:
        cadence_triggers.append("evolution_goal_cadence")
    if cadence_triggers and evolutions_this_session < max_evolutions_per_session:
        invoke /aspirations-evolve with: cadence_triggers
        evolutions_this_session += 1
        last_evolution_goal_count = goals_completed_this_session
    # Part B: Performance triggers (productive only)
    IF outcome_class == "productive":
        performance_triggers = check_performance_triggers()
        if performance_triggers and evolutions_this_session < max_evolutions_per_session:
            invoke /aspirations-evolve with: performance_triggers
            evolutions_this_session += 1
            last_evolution_goal_count = goals_completed_this_session

    # ── LEARNING GATE (Phase 9.5-9.8) ── ← OBLIGATION
    goals_completed_this_session += 1
    invoke /aspirations-learning-gate with: goal, outcome_class, goals_completed_this_session, productive_goals_this_session, batch_mode, prefetch_goals

    # ── STOP CHECK (Phase 10) ──
    Bash: `session-state-get.sh`
    IF NOT "RUNNING": BREAK → run session-end consolidation
    # NEVER STOP, NEVER ASK. The only stop conditions:
    #   1. agent-state no longer RUNNING
    #   2. Critical unrecoverable error
    # See core/config/stop-skip-conditions.md for full list.

    # ── WORKING MEMORY MAINTENANCE (Phase 11) ── ← OBLIGATION
    Bash: wm-read.sh sensory_buffer --json
    If sensory_buffer.length > 20: encode overflow (score ≥ 0.40 → encoding_queue, < 0.15 → discard)
    Bash: wm-ages.sh --json → flag stale slots (> 30 min)
    Bash: wm-prune.sh

    continue
```

### Session-End Consolidation Pass

Run when the loop stops. Hippocampal "sleep replay".

```
invoke /aspirations-consolidate with: session_count, goals_completed_this_session, evolutions_this_session
```

Consolidation MUST NOT call session-state-set.sh.
/stop invokes with stop_mode=true (skips tree rebalancing, reporting, user recap, restart).

### Auto-Session Continuation Protocol

**Within a session**: Stop hook (`.claude/settings.json`) with 4-tier recovery.
Tier 1-3: "invoke /aspirations loop". Tier 4: /recover. Tier 5+: safety valve.
Counter resets on loop entry (Phase -0.5) and boot.

**Across consolidation cycles**: Consolidation invokes `/boot` → detects handoff.yaml → continuation mode.

**Signal files**: `loop-active`, `stop-loop`, `stop-block-count`, `handoff.yaml`

**Stopping**: /stop → IDLE + stop-loop → hook allows → loop exits. Ctrl+C also works.

### `evolve`

invoke /aspirations-evolve with: fired_triggers, aspiration state

### `complete <goal-id> [--permanent]`

1. If NOT recurring: set status completed. Update completed_date, achievedCount, streaks.
2. Recurring: NEVER set completed — update streaks/timestamps only. `--permanent`: set recurring false.
3. Unblock dependent goals. Run spark check. Update readiness gates.

### `add <title>`

1. Gap analysis (overlap check). 2. Scope classification (sprint/project/initiative).
3. Sprint: auto-generate 2-5 goals via /decompose. Project+: invoke /create-aspiration --plan.
4. Enforce cap. Log. Create via aspirations-add.sh.

---

## Reference Docs (loaded on-demand)

- **Completion check runners**: `Bash: load-completion-runners.sh` → `core/config/completion-check-runners.md`
- **Goal selection algorithm**: `core/config/goal-selection-algorithm.md`
- **Stop/skip conditions**: `Bash: load-stop-skip-conditions.sh` → `core/config/stop-skip-conditions.md`
- **Execute protocol**: `Bash: load-execute-protocol.sh` → `core/config/execute-protocol-digest.md`
- **Output format**: `core/config/status-output.md`
- **State update protocol**: Defined in /aspirations-state-update

---

## Chaining Map

| Skill | Called When | Returns |
|---|---|---|
| `/aspirations-precheck` | Every iteration (Phases 0-1) | Updated blockers, auto-completions |
| `/aspirations-select` | Every iteration (Phases 2-2.9) | goal, effort_level, batch |
| `/aspirations-execute` | Phase 4: via digest (load-execute-protocol.sh), full SKILL.md only for edge cases | result, outcome_class, infrastructure_failure |
| `/aspirations-verify` | Phase 5: verification | goal_completed, aspiration_complete |
| `/aspirations-spark` | Phase 6: productive outcomes only | New goals, guardrails |
| `/aspirations-complete-review` | Phase 7: aspiration completion | goals_added, should_archive |
| `/aspirations-state-update` | Phase 8: every iteration | Tree encoding, journal |
| `/aspirations-evolve` | Phase 9: triggered evolution | New aspirations, parameter tuning |
| `/aspirations-learning-gate` | Phase 9.5-9.8: every iteration | Learning verified |
| `/aspirations-consolidate` | Session-end | Handoff, encoding, restart |
| `/research-topic` | Execute research goals | Tree node updates |
| `/review-hypotheses` | Execute review goals | Accuracy data |
| `/reflect` | Via spark, full-cycle | Patterns, strategies |
| `/decompose` | Compound goal detected | Sub-goals |
| `/boot` | Session start, consolidation restart | Status, handoff |
| `/create-aspiration` | Health, alignment, completion | New aspirations |
| `/forge-skill` | Evolve forge check, spark Q6 | Forged skills |
| `/tree maintain` | Consolidation step 6 | Tree structure changes |
| `/curriculum-gates` | Consolidation step 8.6, evolve post-forge | Stage promotion |
| `/reflect-batch-micro` | Consolidation step 0 | Batch stats, promotions |
