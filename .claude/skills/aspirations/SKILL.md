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
conventions: [aspirations, pipeline, goal-schemas, session-state, handoff-working-memory, infrastructure, reasoning-guardrails, experience, goal-selection, coordination]
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

# Phase -1.4: Graceful Stop Handler
# Detects stop-requested signal (set by /stop) and completes in-flight obligations
# before running the full stop sequence. This ensures no learning is lost.
Bash: `session-signal-exists.sh stop-requested`
IF exit 0 (signal exists):
    Output: "▸ GRACEFUL STOP: stop requested — checking for in-flight obligations..."

    # Check for iteration checkpoint (written during Phase 4, tracks obligation progress)
    IF <agent>/session/iteration-checkpoint.json EXISTS:
        Read <agent>/session/iteration-checkpoint.json
        # Staleness guard: if checkpoint > 1 hour old, treat as stale
        IF checkpoint.started_at is > 1 hour ago:
            Output: "▸ GRACEFUL STOP: stale checkpoint (>1h old) — reverting goal to pending"
            Bash: aspirations-update-goal.sh --source {checkpoint.source} {checkpoint.goal_id} status pending
            rm <agent>/session/iteration-checkpoint.json
        ELSE:
            # Reconstruct goal context from checkpoint
            Bash: aspirations-query.sh --goal-id {checkpoint.goal_id} --source {checkpoint.source}
            goal = parsed goal object
            outcome_class = checkpoint.outcome_class
            result = checkpoint.result_summary

            # Complete remaining obligations based on phase_completed
            IF checkpoint.phase_completed == "execute":
                Output: "  Completing verification..."
                Skill(aspirations-verify) with: goal, result, checkpoint.source
                Output: "  Completing state update..."
                Skill(aspirations-state-update) with: goal, result, session_count, outcome_class, checkpoint.source
            ELIF checkpoint.phase_completed == "verify":
                Output: "  Completing state update..."
                Skill(aspirations-state-update) with: goal, result, session_count, outcome_class, checkpoint.source
            ELIF checkpoint.phase_completed == "spark":
                Output: "  Completing state update..."
                Skill(aspirations-state-update) with: goal, result, session_count, outcome_class, checkpoint.source
            ELIF checkpoint.phase_completed == "state_update":
                Output: "  All critical obligations already complete"
            # (phase_completed == "complete" should not happen — checkpoint deleted at LOOP_CONTINUE)

            rm <agent>/session/iteration-checkpoint.json
            Output: "  Obligations complete."
    ELSE:
        # No checkpoint — check for orphaned in-progress goals
        Bash: aspirations-query.sh --goal-status in-progress
        FOR EACH returned goal:
            Bash: aspirations-update-goal.sh --source {goal.source} {goal.id} status pending
            Output: "  Reverted {goal.id} to pending (execution interrupted)"

    # ═══ DEFERRED STOP SEQUENCE ═══
    # This replaces the old /stop steps 1-7. Runs here because state is still
    # RUNNING (mode still autonomous), so consolidation can run with full permissions.
    Output: "▸ Running stop sequence..."
    # D1: Set IDLE
    Bash: session-state-set.sh IDLE
    # D2: Set stop-loop (allows the stop hook to pass on next fire)
    Bash: session-signal-set.sh stop-loop
    # D3: Clear stop-requested
    Bash: session-signal-clear.sh stop-requested
    # D4: Consolidation
    Bash: consolidation-precheck.sh
    IF verdict == "FULL": invoke /aspirations-consolidate with: stop_mode = true
    ELIF verdict == "FAST":
        Bash: load-consolidation-housekeeping.sh → IF path returned: Read it
        # Follow housekeeping steps with stop_mode = true
    ELSE: invoke /aspirations-consolidate with: stop_mode = true
    # D5: Clear loop_state
    echo 'null' | Bash: wm-set.sh loop_state
    # D6: Session cleanup
    Bash: rm -f <agent>/session/running-session-id <agent>/session/aspirations-compact.json <agent>/session/context-budget.json <agent>/session/context-reads.txt <agent>/session/background-jobs.yaml <agent>/session/compact-pending <agent>/session/iteration-checkpoint.json
    Bash: SID=$(cat <agent>/session/latest-session-id 2>/dev/null | tr -d '\r\n'); [ -n "$SID" ] && rm -f ".active-agent-$SID"
    # D7: Reader mode
    Bash: session-mode-set.sh reader
    # D8: Output
    Output: "Agent stopped. Session consolidated — encoding, journal, and handoff saved.
    Mode set to reader (read-only). You can now chat with me — I have full access to
    all accumulated knowledge. Ask me anything.
    Type `/start` to resume autonomous mode, or `/start --mode assistant` for user-directed learning."
    RETURN  # Do NOT continue to the iteration loop

# Phase -1: Initialize Working Memory (runs once, idempotent)
Bash: wm-read.sh --json
if all slots null:
    Read core/config/memory-pipeline.yaml
    Bash: wm-init.sh
    echo '{"session_id": "session-{session_count}", "session_start": "<today>"}' | Bash: wm-set.sh active_context
    Seed recent_violations, pending_resolutions from scripts
    IF handoff.yaml has known_blockers_active: seed known_blockers

# Phase -0.5: Session Marker + Loop State Restoration
Bash: `session-signal-set.sh loop-active`

# Restore loop state from WM (persists across self-reinvocations and compaction)
Bash: wm-read.sh loop_state --json
IF loop_state is not null (not "null" string, not empty):
    # Continuing from previous iteration — restore counters
    goals_completed_this_session = loop_state.goals_completed
    productive_goals_this_session = loop_state.productive_goals
    evolutions_this_session = loop_state.evolutions
    last_evolution_goal_count = loop_state.last_evolution_at
    goals_since_last_alignment_check = loop_state.alignment_check_at
    aspirations_touched_this_session = set(loop_state.touched)
    session_signals = loop_state.signals
ELSE:
    # First iteration of session — initialize + increment session_count
    # IMPORTANT: session_count increments HERE (not above) because LOOP_CONTINUE
    # re-invokes this skill every iteration. Only the first iteration should count.
    Bash: aspirations-meta-update.sh session_count <N+1>
    goals_completed_this_session = 0
    productive_goals_this_session = 0
    evolutions_this_session = 0
    last_evolution_goal_count = 0
    goals_since_last_alignment_check = 0
    aspirations_touched_this_session = set()
    session_signals = {
        routine_streak_global: 0,
        productive_streak: 0,
        routine_count_total: 0,            # total routine outcomes this session (never resets mid-session)
        goals_since_last_tree_update: 0,   # encoding drift counter (all outcome types)
        consecutive_goal_failures: 0,      # circuit breaker counter (same goal failing repeatedly)
        last_failed_goal_id: null,          # tracks which goal is looping
        consecutive_blocked_sleeps: 0       # exponential backoff counter for blocked-idle sleep
    }

# Phase -0.5a: Background Agent Result Collection
IF <agent>/session/pending-agents.yaml EXISTS:
    # list --json prunes stale agents (past timeout_minutes) before returning.
    # Stale agents are removed from the file and excluded from output.
    Bash: `pending-agents.sh list --json`
    FOR EACH non-stale agent: collect results, deregister. IF none pending: clear.

# Phase -0.5c: Compact Checkpoint Processing
# Full protocol: core/config/conventions/compact-recovery.md
IF <agent>/session/compact-checkpoint.yaml EXISTS:
    # Step 1: Restore ALL WM slots from checkpoint (not just 4 — includes dynamic slots like loop_state)
    Bash: `compact-restore-slots.sh`
    # Step 2: Process encoding queue (precision-first — queue items contain precision_manifests)
    Process encoding queue (budget: min(5, queue_length)).
    # Step 3: One-shot consumption
    Delete checkpoint.

# Phase -0.5e: Blocked-Sleep Recovery (compaction interrupted a B7 sleep)
Bash: wm-read.sh blocked_sleep_until
IF value is not "null" AND is a valid ISO timestamp:
    remaining_seconds = max(0, seconds_until(parse(value)))
    IF remaining_seconds > 15:
        Output: "▸ Resuming blocked-sleep: {remaining_seconds}s remaining"
        Bash: interruptible-sleep.sh {remaining_seconds}
    ELSE:
        Output: "▸ Blocked-sleep timer expired during compaction — proceeding"
    echo 'null' | Bash: wm-set.sh blocked_sleep_until

# Phase -0.5d: Identity Context Restoration
Read <agent>/self.md
Bash: world-cat.sh program.md  # skip if empty/missing
```

## ═══ PER-ITERATION OBLIGATIONS (MANDATORY — never skip) ═══

After every goal execution, these MUST complete before `LOOP_CONTINUE`.
Each line below is a **literal `Skill()` tool call** — not an inline approximation.
Writing a manual journal entry or WM update does NOT satisfy these obligations.

1. **VERIFY**: `Skill(aspirations-verify)` — did the goal succeed?
2. **STATE**: `Skill(aspirations-state-update)` — tree encoding, journal, capability
3. **MAINTAIN**: Working memory maintenance — sensory buffer, aging, prune
4. **LEARN**: `Skill(aspirations-learning-gate)` — learning gate, retrieval gate, reflection → calls LOOP_CONTINUE

Skip rules:
- Spark: SKIP if outcome_class == "routine"
- Completion review: SKIP if aspiration not fully complete
- Evolution: respects per-session cap (max_evolutions_per_session)

If ANY obligation is skipped without justification, log "OBLIGATION SKIPPED: {phase}".

## Loop Continuation Protocol (LOOP_CONTINUE)

Every `LOOP_CONTINUE` in this loop means: execute these two steps, in order:
1. `echo '<loop_state as JSON>' | Bash: wm-set.sh loop_state`
   — where `<loop_state>` is the current values of all iteration counters (see Phase -0.5)
2. `Skill('aspirations') with args='loop'`

This is NOT optional. The `Skill()` call is what keeps the loop alive.
NEVER produce text-only output at a `LOOP_CONTINUE` point.
NEVER skip the `Skill()` call. NEVER say "continuing..." without calling it.
NEVER substitute with inline code instead of the `Skill()` call.

```
# ═══ SINGLE ITERATION (self-reinvoking via LOOP_CONTINUE) ═══
    # ── PRE-SELECTION (Phases 0-1) ──
    invoke /aspirations-precheck
    # Returns: aspirations compact refreshed, blockers updated, recurring checked

    # ── GOAL SELECTION (Phases 2-2.9) ──
    invoke /aspirations-select
    # Returns: goal, effort_level, batch_mode, batch, ranked_goals, prefetch_goals, selection_context, selection_reason, source

    if goal is None AND selection_reason starts with "all_blocked":
        # ── ALL-BLOCKED PATH ("deep while blocked") ──
        # Goals exist but all are blocked on external dependencies.
        # Principle: "No Terminal State" — generate new work that avoids blocked resources.
        # Only sleep as absolute LAST RESORT after all generation attempts fail.
        Output: "▸ ALL BLOCKED — goals waiting on external dependencies"
        Output: blocked goal summaries from selection context
        blocked_idle_attempts = []

        # Step B0: Board scan for cross-agent actionable items
        # See coordination convention for full scan protocol
        Bash: board-read.sh --channel coordination --type escalation --since 12h --json
        FOR EACH escalation_msg NOT from this agent:
            Extract goal_id from msg.tags
            IF no existing investigation goal for this goal_id:
                Create investigation goal: "Investigate: why {goal_title} keeps failing (escalated by {msg.author})"
                blocked_idle_attempts.append("board-scan: created investigation for escalated {goal_id}")
        Bash: board-read.sh --channel coordination --type review-request --since 12h --json
        deep_review_count = 0
        FOR EACH review_msg NOT from this agent:
            Extract goal_id from msg.tags
            IF goal has review_requested but NOT review_completed:
                IF deep_review_count >= 3:
                    # Cap at 3 deep reviews per B0 scan to bound context cost
                    BREAK

                # ── Deep Code Review Protocol (5-phase hypothesis-driven review) ──
                # See coordination convention "Deep Review Protocol" for rationale.

                # R1 Context: Load full review context
                Bash: experience-read.sh --goal {goal_id}
                # Read the content .md file referenced in the experience trace
                Read the experience trace's content file (the article or diff output)
                Load goal description and verification outcomes from the goal record

                # R2 Architectural Assessment (3 questions):
                # Q1: Do ALL verification outcomes match the claimed result?
                #     Compare each verification check against the experience trace.
                #     Flag any mismatch between claimed outcome and actual evidence.
                # Q2: What downstream goals depend on the changed artifact?
                #     Bash: goal-selector.sh blocked
                #     Scan blocked goals for any whose blocked_by or description
                #     references the same artifact/file/module touched by this change.
                # Q3: Does this change invalidate existing tree knowledge or active hypotheses?
                #     Bash: tree-find-node.sh {artifact_or_topic}
                #     Bash: pipeline-read.sh --stage active
                #     Check if any tree nodes reference the changed artifact with
                #     facts that are now stale, or if active hypotheses assumed the
                #     pre-change state.

                # R3 Hypothesis Formation:
                # Form a testable prediction about the change's downstream impact.
                # Example: "Change to X will cause Y in the next N executions"
                # Apply calibration gate (same ceiling as spark Step 0.5):
                #   a. Read recent accuracy: Bash: pipeline-read.sh --stage resolved
                #      Count CONFIRMED vs CORRECTED for code_review category (or overall if <3)
                #      Compute recent_accuracy = confirmed / total
                #   b. Apply confidence ceiling:
                #      - If recent_accuracy < 0.40: cap at 0.55
                #      - If recent_accuracy >= 0.40 and < 0.60: cap at 0.65
                #      - If recent_accuracy >= 0.60 and < 0.80: cap at 0.80
                #      - If recent_accuracy >= 0.80: no cap
                # Add to pipeline:
                #   Bash: pipeline-add.sh --title "Review: {prediction_summary}" \
                #         --confidence {calibrated_confidence} --horizon short \
                #         --type code_review --tags "review,{goal_id}" \
                #         --context "{goal_id} review by {this_agent}"

                # R4 Post to Board:
                # Share review hypothesis on findings channel so both agents learn.
                #   Bash: board-post.sh --channel findings --type finding \
                #         --tags "code_review,{goal_id}" \
                #         --message "Review of {goal_id}: {hypothesis_summary}. Assessment: {architectural_notes}"

                # R5 Issue Handling (preserved from original protocol):
                IF issues_found during R2 assessment:
                    Create investigation goal: "Investigate: review issue in {goal_id} — {issue_summary}"

                Bash: aspirations-update-goal.sh --source world <goal-id> review_completed <today>
                deep_review_count += 1
                blocked_idle_attempts.append("board-scan: deep-reviewed {goal_id}")

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
            LOOP_CONTINUE
        blocked_idle_attempts.append("create-aspiration: no viable aspirations found")

        # Step B2.5: Idle Playbook Goal Generation
        # Walk items 1-6 of the agent's idle playbook (items 7-10 need user input).
        # Creates ONE goal per cycle in agent queue, then continues to let selector pick it up.
        # Dedup: skips items that already exist as pending goals in asp-001.
        IF "create-aspiration: no viable aspirations found" in blocked_idle_attempts:
            Output: "▸ Walking idle playbook for goal generation..."
            # Item 1 (codebase audit) is covered by g-001-12 recurring goal — not duplicated here
            idle_playbook = [
                {"title": "Production health deep-dive: infrastructure trends and error rates", "category": "infrastructure"},
                {"title": "Knowledge gap research: Low-confidence tree nodes", "category": "research"},
                {"title": "Competitor and best-practice research", "category": "research"},
                {"title": "Alpha's work review: Verify completed goals", "category": "code"},
                {"title": "Test coverage analysis: Find untested code", "category": "code"},
            ]
            # Filter: not blocked by resource AND not already pending in asp-001
            Bash: aspirations-read.sh --source agent --id asp-001 → existing_goals
            existing_titles = set(g["title"] for g in existing_goals.goals if g.status == "pending")
            viable = [item for item in idle_playbook
                      if item["category"] not in constraint_context.get("blocked_resources", [])
                      and item["title"] not in existing_titles]
            IF viable:
                chosen = viable[0]
                goal_json = {
                    title: chosen["title"],
                    status: "pending", priority: "MEDIUM",
                    participants: ["agent"], category: chosen["category"],
                    description: "Auto-generated from idle playbook. Execute this playbook item, create follow-up goals in world queue for any findings."
                }
                echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh --source agent asp-001
                blocked_idle_attempts.append("idle-playbook: created '" + chosen["title"] + "'")
                Output: "▸ Idle playbook generated: {chosen['title']}"
                LOOP_CONTINUE  # Re-enter loop to select the new goal
            blocked_idle_attempts.append("idle-playbook: all items blocked, duplicate, or overlapping")

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
                LOOP_CONTINUE
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
            LOOP_CONTINUE

        # Step B7: Exponential backoff sleep, then re-check
        BACKOFF_SCHEDULE = [300, 600, 1200, 1800]  # 5min, 10min, 20min, 30min cap
        sleep_index = min(session_signals.consecutive_blocked_sleeps, len(BACKOFF_SCHEDULE) - 1)
        sleep_seconds = BACKOFF_SCHEDULE[sleep_index]
        session_signals.consecutive_blocked_sleeps += 1
        wake_at = (now + sleep_seconds seconds) as ISO timestamp
        echo '"{wake_at}"' | Bash: wm-set.sh blocked_sleep_until
        Output: "▸ All generation attempts exhausted. Sleeping {sleep_seconds}s (backoff level {sleep_index})."
        Output: "  Attempts: " + "; ".join(blocked_idle_attempts)

        # Step B7.1: Proactive user notification — all self-remediation exhausted
        IF config.proactive_escalation.b7_notify:
            Bash: wm-read.sh proactive_escalation_log --json
            last_b7 = find entry where blocker_id == "_all_blocked"
            IF last_b7 is null OR hours_since(last_b7.sent_at) >= config.proactive_escalation.blocker_age_hours:
                # Build summary from known_blockers
                Bash: wm-read.sh known_blockers --json
                blocker_summary = ""
                FOR EACH blocker WHERE resolution is null:
                    age_hours = hours_since(blocker.detected_at)
                    blocker_summary += "• {blocker.reason} (blocked {age_hours:.0f}h, {len(blocker.affected_goals)} goals)\n"
                    blocker_summary += "  Unblock: {blocker.unblocking_goal}\n"
                IF blocker_summary is empty:
                    blocker_summary = "All goals blocked on dependencies (no infrastructure blockers active)."
                Notify the user:
                    category: blocker
                    subject: "All work blocked — agent waiting"
                    message: |
                        All self-remediation exhausted (board scan, aspiration generation, idle playbook,
                        evolution, research, reflection). Entering backoff sleep ({sleep_seconds}s).

                        Active blockers:
                        {blocker_summary}

                        The single highest-value action you can take:
                        {most_impactful_blocker_or_dependency_description}
                # Record escalation
                echo '{"blocker_id":"_all_blocked","sent_at":"{now}"}' | Bash: wm-append.sh proactive_escalation_log

        Bash: interruptible-sleep.sh {sleep_seconds}
        # Normal completion (no compaction interruption) — clear timer
        echo 'null' | Bash: wm-set.sh blocked_sleep_until
        LOOP_CONTINUE

    if goal is None:
        # No-goals path (genuinely no goals exist) — NOT a stop condition
        # ASSERT: goal-selector.sh returned [] — see core/config/conventions/goal-selection.md
        invoke /create-aspiration from-self --plan
        if new_aspirations_generated: LOOP_CONTINUE
        # ASAP fallback: search tree, reasoning bank, experience for work
        invoke /research-topic (explore broadly)
        invoke /reflect --full-cycle
        LOOP_CONTINUE

    # ── DECOMPOSE (Phase 3) ──
    if goal is compound (title has "and", vague verbs, multi-skill, multi-session):
        invoke /decompose goal.id
        if goal.status == "decomposed": LOOP_CONTINUE  # re-select from sub-goals

    # ── CLAIM + EXECUTE (Phase 4) ──
    # Claim world goals before execution (prevents duplicate work across agents)
    IF source == "world":
        Bash: aspirations-claim.sh <goal-id>
        IF exit code != 0:
            Output: "▸ CLAIM CONFLICT: {goal.id} already claimed — re-selecting"
            LOOP_CONTINUE  # Re-enter selection loop
    Bash: aspirations-update-goal.sh --source {source} <goal-id> status in-progress
    Bash: aspirations-update-goal.sh --source {source} <goal-id> started <today>
    echo "Claimed: ${goal.title} [${goal.id}]" | Bash: board-post.sh --channel coordination --type claim --tags ${goal.id},${aspiration_id}
    # Load the execute protocol DIGEST (~170 lines) — NOT the full 883-line skill.
    # The digest contains the complete execution protocol. Follow it inline.
    # For rare edge cases (CREATE_BLOCKER, Cognitive Primitives JSON):
    #   Read .claude/skills/aspirations-execute/SKILL.md directly.
    Bash: load-execute-protocol.sh → IF path returned: Read it
    # Follow the loaded protocol. Inputs: goal, effort_level, batch_mode, batch
    # Returns: result, outcome_class, infrastructure_failure

    IF infrastructure_failure:
        # Release claim so other agent can attempt (world goals only)
        IF source == "world": Bash: aspirations-release.sh <goal-id>
        LOOP_CONTINUE  # Skip Phases 5-9

    # ── ITERATION CHECKPOINT (survives compaction + /stop interruption) ──
    # Written ONCE after execution completes. Updated after each obligation.
    # Consumed by Phase -1.4 Graceful Stop Handler if /stop interrupts mid-iteration.
    # Deleted at LOOP_CONTINUE when the iteration is fully complete.
    echo '{"goal_id":"<goal.id>","aspiration_id":"<asp.id>","source":"<source>","outcome_class":"<outcome_class>","result_summary":"<one-line result summary>","phase_completed":"execute","started_at":"<ISO timestamp>"}' > <agent>/session/iteration-checkpoint.json

    # Routine streak tracking (per-goal anti-drift safeguard)
    IF outcome_class == "routine":
        routine_streaks[goal.id] += 1
        IF routine_streaks[goal.id] >= 5:
            outcome_class = "deep"  # force DEEP after 5 consecutive routine
            routine_streaks[goal.id] = 0
            Log: "PER-GOAL ANTI-DRIFT: forced deep after 5 consecutive routine for {goal.id}"
    ELIF outcome_class == "deep":
        routine_streaks[goal.id] = 0

    # Session signal tracking (streak counters for global anti-drift)
    IF outcome_class == "routine":
        session_signals.routine_streak_global += 1
        session_signals.routine_count_total += 1   # cumulative (never resets mid-session)
        session_signals.productive_streak = 0
    ELSE:
        session_signals.routine_streak_global = 0
        session_signals.productive_streak += 1

    # Global anti-drift safeguard (across ALL goals, not per-goal-id)
    IF session_signals.routine_streak_global >= 8:
        outcome_class = "deep"  # force DEEP pipeline
        session_signals.routine_streak_global = 0
        Log: "GLOBAL ANTI-DRIFT: forced deep after 8 consecutive routine outcomes"

    # Ratio-based anti-drift safeguard (catches interleaving pattern where different
    # recurring goals alternate routine outcomes, evading per-goal and consecutive counters)
    IF outcome_class == "routine" AND goals_completed_this_session >= 6:
        routine_ratio = session_signals.routine_count_total / goals_completed_this_session
        IF routine_ratio > 0.80:
            outcome_class = "deep"  # force DEEP pipeline
            Log: "RATIO ANTI-DRIFT: {routine_ratio:.0%} routine ({session_signals.routine_count_total}/{goals_completed_this_session}) — forced deep"

    # Count productive goals AFTER all reclassification
    IF outcome_class == "deep":
        productive_goals_this_session += 1

    # ── VERIFY (Phase 5) ── ← OBLIGATION (literal Skill() tool call — not inline)
    Skill(aspirations-verify) with: goal, result, source
    # Update iteration checkpoint (Phase -1.4 uses this for graceful stop recovery)
    echo '{"goal_id":"...","aspiration_id":"...","source":"...","outcome_class":"...","result_summary":"...","phase_completed":"verify","started_at":"..."}' > <agent>/session/iteration-checkpoint.json

    # ── ATTRIBUTION (Phase 5.3) ── Record which agent completed/failed (world goals only)
    IF source == "world" AND goal.status == "completed":
        Bash: aspirations-complete-by.sh <goal-id>
    ELIF source == "world" AND goal.status != "completed" AND goal.status != "in-progress":
        # Goal reverted (failed/blocked/skipped) — release claim for other agent
        Bash: aspirations-release.sh <goal-id>

    # ── CIRCUIT BREAKER (Phase 5.5) ── Escalate goals that fail repeatedly
    IF goal.status != "completed":
        IF session_signals.last_failed_goal_id == goal.id:
            session_signals.consecutive_goal_failures += 1
        ELSE:
            session_signals.consecutive_goal_failures = 1
            session_signals.last_failed_goal_id = goal.id
        IF session_signals.consecutive_goal_failures >= 3:
            echo "Escalation: ${goal.title} [${goal.id}] failed ${session_signals.consecutive_goal_failures} consecutive times — needs investigation or re-scoping" | Bash: board-post.sh --channel coordination --type escalation --tags ${goal.id},urgent
            # Proactive escalation: notify user about repeated failure
            IF config.proactive_escalation.circuit_breaker_notify:
                Notify the user:
                    category: blocker
                    subject: "Goal failing repeatedly: {goal.title}"
                    message: |
                        Goal {goal.id} ({goal.title}) has failed {session_signals.consecutive_goal_failures}
                        consecutive times. Circuit breaker activated — goal deferred pending investigation.

                        Last failure context: {brief_error_summary}
                        Aspiration: {aspiration.title}
                # No cooldown needed — circuit breaker resets counter after firing,
                # so it can only re-fire after 3+ more consecutive failures.
            Bash: aspirations-update-goal.sh --source {source} <goal-id> defer_reason "Circuit breaker: 3+ consecutive failures, escalated via board"
            Bash: aspirations-update-goal.sh --source {source} <goal-id> defer_reason_set_at "$(date +%Y-%m-%dT%H:%M:%S)"
            session_signals.consecutive_goal_failures = 0
            session_signals.last_failed_goal_id = null
    ELSE:
        session_signals.consecutive_goal_failures = 0
        session_signals.last_failed_goal_id = null
        session_signals.consecutive_blocked_sleeps = 0  # reset backoff on successful execution

    # ── REVIEW GATE (Phase 5.7) ── Request peer review for code-change world goals
    IF source == "world" AND goal.status == "completed":
        IF goal.skill is null OR goal.category in ("code", "pipeline", "infrastructure"):
            echo "Review requested: ${goal.title} [${goal.id}] — ${one-line-summary-of-changes}" | Bash: board-post.sh --channel coordination --type review-request --tags ${goal.id},code-change
            Bash: aspirations-update-goal.sh --source world <goal-id> review_requested <today>

    # ── SPARK (Phase 6) ── (literal Skill() tool call)
    # Deep: full spark treatment (all questions)
    # Routine: creative+hypothesis spark on EVERY routine outcome (no % 3 gate).
    # The routine_spark question set is limited (6 categories, self-selecting)
    # so cost is bounded. Principle: we are here to learn — never skip.
    IF outcome_class == "deep":
        Skill(aspirations-spark) with: goal, result, effort_level, outcome_class, source
        # ↑ Text output here kills the session — continue to next tool call immediately
    ELIF outcome_class == "routine":
        Skill(aspirations-spark) with: goal, result, effort_level, outcome_class="routine_spark", source
        # ↑ Text output here kills the session — continue to next tool call immediately
        Log: "ROUTINE SPARK: creative+hypothesis spark on routine #{session_signals.routine_count_total}"
    # Update iteration checkpoint after spark
    echo '{"...","phase_completed":"spark","..."}' > <agent>/session/iteration-checkpoint.json

    # ── COMPLETION REVIEW (Phase 7-7.6) ──
    asp = get_aspiration(goal)
    has_recurring = any(g.get("recurring", False) for g in asp.goals)
    if not has_recurring and aspiration_fully_complete(asp):
        invoke /aspirations-complete-review with: asp, goal, source
    # NOTE: aspirations with ANY recurring goals skip completion review — they are perpetual.
    # The data layer (aspirations-complete.sh) also blocks archival of such aspirations.

    # ── STATE UPDATE (Phase 8) ── ← OBLIGATION (literal Skill() tool call — not inline)
    Skill(aspirations-state-update) with: goal, result, session_count, outcome_class, source
    # Update iteration checkpoint after state update
    echo '{"...","phase_completed":"state_update","..."}' > <agent>/session/iteration-checkpoint.json

    # ── STOP-REQUESTED FAST CHECK (Phase 8-stop) ──
    # If stop-requested, skip non-obligation phases (evolution, WM maintenance, periodic tree)
    # and go directly to learning gate → deferred stop at next loop re-entry.
    Bash: session-signal-exists.sh stop-requested
    IF exit 0 (signal exists):
        Output: "▸ Stop requested — skipping evolution, proceeding to final obligations"
        # Delete checkpoint (state_update is the last critical obligation)
        rm <agent>/session/iteration-checkpoint.json
        # Save loop state and re-enter loop (Phase -1.4 will handle the stop sequence)
        goals_completed_this_session += 1
        LOOP_CONTINUE  # → Phase -1.4 detects stop-requested, runs deferred stop

    # ── Encoding drift tracking (Phase 8.0.5) ──
    # Track whether Step 8 ACTUALLY wrote to the tree.
    # step_8_tree_encoded is set by the deep branch that performs immediate tree writes.
    IF step_8_tree_encoded:
        session_signals.goals_since_last_tree_update = 0
    ELSE:
        session_signals.goals_since_last_tree_update += 1

    # ── Encoding drift safeguard (Phase 8.0.6) ─────────────────────────
    # Fires when 3+ goals pass without ANY tree update (regardless of outcome type).
    # Sets a WM flag that aspirations-state-update Step 8 reads to bypass
    # the subjective "new insight" gate on the NEXT iteration.
    IF session_signals.goals_since_last_tree_update >= 3:
        echo '"true"' | Bash: wm-set.sh force_tree_encoding
        session_signals.goals_since_last_tree_update = 0
        Log: "ENCODING ANTI-DRIFT: {N} goals without tree update — forcing encoding on next state update"
    # ── End encoding drift safeguard ───────────────────────────────────

    # Phase 8.1: Session touch tracking
    IF asp.id not in aspirations_touched_this_session:
        aspirations_touched_this_session.add(asp.id)
        Increment asp.sessions_active via aspirations-update.sh (use --source {source})

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
    # Part B: Performance triggers (deep only)
    IF outcome_class == "deep":
        performance_triggers = check_performance_triggers()
        if performance_triggers and evolutions_this_session < max_evolutions_per_session:
            invoke /aspirations-evolve with: performance_triggers
            evolutions_this_session += 1
            last_evolution_goal_count = goals_completed_this_session

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
    # Budget: 1 item per iteration to avoid overhead. Only fires when queue >= 2
    # (below that, session-end consolidation is sufficient).
    Bash: wm-read.sh encoding_queue --json
    IF encoding_queue is non-empty AND len(encoding_queue) >= 2:
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

    # ── Periodic tree maintenance (every 5 goals) ─────────────────
    # Ensures structural growth (DECOMPOSE, SPLIT, etc.) happens mid-session,
    # not just at session-end consolidation.
    IF goals_completed_this_session > 0 AND goals_completed_this_session % 5 == 0:
        Output: "▸ PERIODIC TREE MAINTENANCE: {goals_completed_this_session} goals completed"
        Invoke /tree maintain
        Log: "PERIODIC TREE MAINTENANCE: after {goals_completed_this_session} goals"
    # ── End periodic tree maintenance ─────────────────────────────

    Bash: wm-ages.sh --json → flag stale slots (> 30 min)
    Bash: wm-prune.sh

    # ── LEARNING GATE (Phase 12 — LAST PHASE) ── ← OBLIGATION
    # This is the LAST phase before LOOP_CONTINUE. The learning gate sub-skill
    # ends with LOOP_CONTINUE internally, which re-invokes this skill.
    # Phases 10-11 above ensure stop check and WM maintenance happen BEFORE
    # the learning gate, so they're never skipped.
    goals_completed_this_session += 1
    # Delete iteration checkpoint — all obligations complete, safe to continue
    rm -f <agent>/session/iteration-checkpoint.json
    Skill(aspirations-learning-gate) with: goal, outcome_class, goals_completed_this_session, productive_goals_this_session, batch_mode, prefetch_goals, session_signals.goals_since_last_tree_update, source
    # NOTE: Control does NOT return here — the learning gate calls LOOP_CONTINUE.
```

### Session-End Consolidation Pass

Run when the loop stops (Phase 10 BREAK). Hippocampal "sleep replay".

```
# Graduated consolidation: check encoding queues before loading full skill
Bash: consolidation-precheck.sh
# Returns JSON: {"verdict":"FAST"|"FULL", "total": N, ...}

IF verdict == "FULL" (total > 0):
    invoke /aspirations-consolidate with: session_count, goals_completed_this_session, evolutions_this_session
ELIF verdict == "FAST" (total == 0):
    # Load ~150-line housekeeping digest instead of full ~497-line skill
    Bash: load-consolidation-housekeeping.sh → IF path returned: Read it
    # Follow housekeeping steps. If encoding work appears mid-consolidation
    # (rare), invoke /aspirations-consolidate for the full pipeline.
ELSE:
    # Precheck error — safe fallback
    invoke /aspirations-consolidate with: session_count, goals_completed_this_session, evolutions_this_session

# Clear loop_state — clean slate for next session
echo 'null' | Bash: wm-set.sh loop_state
```

Consolidation MUST NOT call session-state-set.sh.
/stop invokes with stop_mode=true (skips tree rebalancing, reporting, user recap, restart).

### Auto-Session Continuation Protocol

**Within a session**: Stop hook (`.claude/settings.json`) blocks unconditionally when RUNNING.
No tiers, no counter, no safety valve. Just BLOCK + re-invoke `Skill('aspirations') with args='loop'`.

**Self-reinvocation**: Each iteration ends with LOOP_CONTINUE → saves state to WM → calls
`Skill('aspirations') with args='loop'`. The agent re-reads this SKILL.md fresh every iteration.
State persists via the `loop_state` WM slot.

**Across consolidation cycles**: Consolidation invokes `/boot` → detects handoff.yaml → continuation mode.

**Signal files**: `loop-active`, `stop-loop`, `handoff.yaml`

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
| `/aspirations-spark` | Phase 6: deep outcomes (all sparks) | New goals, guardrails |
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
| `/reflect-on-outcome` (Batch Micro mode) | Consolidation step 0 | Batch stats, promotions |
