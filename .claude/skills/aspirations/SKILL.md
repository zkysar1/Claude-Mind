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
    productive_streak: 0,
    goals_since_last_tree_update: 0    # encoding drift counter (all outcome types)
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

After every goal execution, these MUST complete before `continue`.
Each line below is a **literal `Skill()` tool call** — not an inline approximation.
Writing a manual journal entry or WM update does NOT satisfy these obligations.

1. **VERIFY**: `Skill(aspirations-verify)` — did the goal succeed?
2. **STATE**: `Skill(aspirations-state-update)` — tree encoding, journal, capability
3. **LEARN**: `Skill(aspirations-learning-gate)` — learning gate, retrieval gate, reflection
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
    # Returns: goal, effort_level, batch_mode, batch, ranked_goals, prefetch_goals, selection_context, selection_reason

    if goal is None AND selection_reason starts with "all_blocked":
        # ── ALL-BLOCKED PATH ("deep while blocked") ──
        # Goals exist but all are blocked on external dependencies.
        # Principle: "No Terminal State" — generate new work that avoids blocked resources.
        # Only sleep as absolute LAST RESORT after all generation attempts fail.
        Output: "▸ ALL BLOCKED — goals waiting on external dependencies"
        Output: blocked goal summaries from selection context
        blocked_idle_attempts = []

        # Step B1: Extract constraint context from selection data
        blocked_skills = set()
        blocked_resources = set()
        FOR EACH bg in selection_context.blocked_goals:
            IF bg.reason == "infrastructure":
                blocked_skills.add(extract_skill_from(bg.detail))
                blocked_resources.add(extract_reason_from(bg.detail))
            ELIF bg.reason == "dependency" OR bg.reason == "explicit_status":
                blocked_resources.add(bg.detail)
        Bash: wm-read.sh known_blockers --json
        FOR EACH blocker in known_blockers:
            FOR EACH skill in blocker.affected_skills:
                blocked_skills.add(skill)
        constraint_context = {
            blocked_resources: list(blocked_resources),
            avoid_skills: list(blocked_skills),
            trigger: "all_blocked",
            blocked_count: selection_context.blocked_count,
            by_reason: selection_context.by_reason
        }

        # Step B2: Constraint-aware aspiration generation
        Output: "▸ Attempting constraint-aware aspiration generation..."
        invoke /create-aspiration from-self --plan with: constraint_context
        if new_aspirations_generated:
            blocked_idle_attempts.append("create-aspiration: SUCCESS")
            Output: "▸ Generated new aspirations avoiding blocked resources"
            continue
        blocked_idle_attempts.append("create-aspiration: no viable aspirations found")

        # Step B3: Evolution gap analysis (even outside normal triggers)
        IF evolutions_this_session < max_evolutions_per_session:
            Output: "▸ Attempting idle evolution gap analysis..."
            invoke /aspirations-evolve with: ["idle_blocked"]
            evolutions_this_session += 1
            last_evolution_goal_count = goals_completed_this_session
            # Check if evolution created new executable goals
            Bash: goal-selector.sh
            IF parsed_output is a JSON array with length > 0:
                blocked_idle_attempts.append("evolve: SUCCESS — new executable goals")
                Output: "▸ Evolution created new executable goals"
                continue
            blocked_idle_attempts.append("evolve: completed but no new executable goals")
        ELSE:
            blocked_idle_attempts.append("evolve: skipped (session cap reached)")

        # Step B4: Exploratory research
        Output: "▸ Attempting exploratory research..."
        invoke /research-topic (explore broadly based on Self's purpose, avoiding blocked domains)
        blocked_idle_attempts.append("research: completed")

        # Step B5: Full-cycle reflection
        Output: "▸ Running full-cycle reflection..."
        invoke /reflect --full-cycle
        blocked_idle_attempts.append("reflect: completed")

        # Step B6: Check if B4/B5 produced new goals (via spark/findings)
        Bash: goal-selector.sh
        IF parsed_output is a JSON array with length > 0:
            Output: "▸ Research/reflection produced new executable goals"
            continue

        # Step B7: Last resort — short sleep, then re-check
        Output: "▸ All generation attempts exhausted. Waiting 5 minutes."
        Output: "  Attempts: " + "; ".join(blocked_idle_attempts)
        Bash (timeout 300000): sleep 300
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
    # Load the execute protocol DIGEST (~170 lines) — NOT the full 883-line skill.
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
            outcome_class = "deep"  # force DEEP after 5 consecutive routine
            routine_streaks[goal.id] = 0
            Log: "PER-GOAL ANTI-DRIFT: forced deep after 5 consecutive routine for {goal.id}"
    ELIF outcome_class in ("standard", "deep"):
        routine_streaks[goal.id] = 0

    # Session signal tracking (streak counters for global anti-drift)
    IF outcome_class == "routine":
        session_signals.routine_streak_global += 1
        session_signals.productive_streak = 0
    ELSE:
        session_signals.routine_streak_global = 0
        session_signals.productive_streak += 1

    # Global anti-drift safeguard (across ALL goals, not per-goal-id)
    IF session_signals.routine_streak_global >= 8:
        outcome_class = "deep"  # force DEEP pipeline
        session_signals.routine_streak_global = 0
        Log: "GLOBAL ANTI-DRIFT: forced deep after 8 consecutive routine outcomes"

    # Count productive goals AFTER all reclassification (standard + deep both count)
    IF outcome_class in ("standard", "deep"):
        productive_goals_this_session += 1

    # ── VERIFY (Phase 5) ── ← OBLIGATION (literal Skill() tool call — not inline)
    Skill(aspirations-verify) with: goal, result

    # ── SPARK (Phase 6) ── (literal Skill() tool call for non-routine outcomes)
    IF outcome_class in ("standard", "deep"):
        Skill(aspirations-spark) with: goal, result, effort_level, outcome_class

    # ── COMPLETION REVIEW (Phase 7-7.6) ──
    asp = get_aspiration(goal)
    has_recurring = any(g.get("recurring", False) for g in asp.goals)
    if not has_recurring and aspiration_fully_complete(asp):
        invoke /aspirations-complete-review with: asp, goal
    # NOTE: aspirations with ANY recurring goals skip completion review — they are perpetual.
    # The data layer (aspirations-complete.sh) also blocks archival of such aspirations.

    # ── STATE UPDATE (Phase 8) ── ← OBLIGATION (literal Skill() tool call — not inline)
    Skill(aspirations-state-update) with: goal, result, session_count, outcome_class

    # ── Encoding drift tracking (Phase 8.0.5) ──
    # Track whether Step 8 wrote to the tree. step_8_wrote_insight is set by
    # aspirations-state-update when it successfully encodes to a tree node.
    IF step_8_wrote_insight:
        session_signals.goals_since_last_tree_update = 0
    ELSE:
        session_signals.goals_since_last_tree_update += 1

    # ── Encoding drift safeguard (Phase 8.0.6) ─────────────────────────
    # Fires when 4+ goals pass without ANY tree update (regardless of outcome type).
    # Sets a WM flag that aspirations-state-update Step 8 reads to bypass
    # the subjective "new insight" gate on the NEXT iteration.
    IF session_signals.goals_since_last_tree_update >= 4:
        echo '"true"' | Bash: wm-set.sh force_tree_encoding
        session_signals.goals_since_last_tree_update = 0
        Log: "ENCODING ANTI-DRIFT: {N} goals without tree update — forcing encoding on next state update"
    # ── End encoding drift safeguard ───────────────────────────────────

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
    # Part B: Performance triggers (standard + deep only)
    IF outcome_class in ("standard", "deep"):
        performance_triggers = check_performance_triggers()
        if performance_triggers and evolutions_this_session < max_evolutions_per_session:
            invoke /aspirations-evolve with: performance_triggers
            evolutions_this_session += 1
            last_evolution_goal_count = goals_completed_this_session

    # ── LEARNING GATE (Phase 9.5-9.8) ── ← OBLIGATION (literal Skill() tool call — not inline)
    goals_completed_this_session += 1
    Skill(aspirations-learning-gate) with: goal, outcome_class, goals_completed_this_session, productive_goals_this_session, batch_mode, prefetch_goals, session_signals.goals_since_last_tree_update

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

    # ── Mid-session encoding queue drain ──────────────────────────────
    # Prevents encoding loss from interrupted sessions by draining incrementally.
    # Budget: 1 item per iteration to avoid overhead. Only fires when queue >= 3
    # (below that, session-end consolidation is sufficient).
    Bash: wm-read.sh encoding_queue --json
    IF encoding_queue is non-empty AND len(encoding_queue) >= 3:
        top_item = max(encoding_queue, key=encoding_score)
        node=$(bash core/scripts/tree-find-node.sh --text "{top_item.target_article or top_item.category}" --leaf-only --top 1)
        IF node found:
            Read node.file
            IF top_item has precision_manifest AND precision_manifest non-empty:
                Append precision items to "## Verified Values" section
            Compress top_item.observation into "Key Insights" section (1-3 sentences)
            Edit node.file with updates
            bash core/scripts/tree-update.sh --set <node.key> last_updated $(date +%Y-%m-%d)
            Remove encoded item from encoding_queue
            session_signals.goals_since_last_tree_update = 0
            Output: "▸ MID-SESSION DRAIN: encoded 1 item to {node.key} (score {top_item.encoding_score:.2f}, {len(encoding_queue)-1} remaining)"
    # ── End mid-session drain ─────────────────────────────────────────

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
| `/aspirations-spark` | Phase 6: standard+deep outcomes (all sparks for both) | New goals, guardrails |
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
