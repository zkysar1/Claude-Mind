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
---

# /aspirations — Perpetual Goal Loop Engine

The heartbeat of the continual learning agent. This skill drives ALL autonomous work by reading aspirations via `core/scripts/aspirations-read.sh`, selecting the highest-priority executable goal, running it via the appropriate skill, reflecting on results, and evolving strategy. Active aspirations live in `mind/aspirations.jsonl`, archived in `mind/aspirations-archive.jsonl`, metadata in `mind/aspirations-meta.json`. **This loop is designed to never reach a terminal state.** When all goals are done, it generates new ones. When aspirations complete, it spawns replacements. It runs forever.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Core Principle: No Terminal State

```
The system ALWAYS has work to do. If it doesn't, it creates work.
Completion of one thing seeds the next thing.
Nature abhors a vacuum — fill every gap with a new aspiration.
```

## Sub-Commands

### `status`

Display current aspiration state:
1. Bash: `aspirations-read.sh --active` (aspirations and goals)
   Bash: `aspirations-read.sh --meta` (readiness gates, session_count)
2. Show readiness gates (all must be true for meaningful hypotheses)
3. Show each aspiration: id, title, priority, status, progress (completed/total goals)
4. List all pending/in-progress goals with blocked status, achievement counters, and readiness gates
5. Show recurring goals with interval (hours), last achieved timestamp, and next due time
6. Read `mind/evolution-log.jsonl` (last 5 entries)
7. Show user actions queue
8. Show meta-memory summary (strengths, weaknesses, accuracy)

### `next`

Select and execute ONE goal, then return:
1. Run completion check runners (Step 0)
2. Run goal selection algorithm
3. If compound → invoke `/decompose` inline (no approval needed)
4. Execute goal via linked skill
5. Run spark check
6. Update state
7. Return result

## Aspiration Update Notification (MANDATORY)

Whenever goals are added to an existing aspiration — whether from spark questions,
forge detection, evolve, decompose, micro-batch discoveries, or any other source —
notify the user with a summary of what changed:

```
Reach out to the user about the aspiration update:
  Subject: "Aspiration Updated: <asp-title>"
  Message: "Aspiration updated: <asp-id>: <asp-title>. New goals added: <goal-titles-list>"
If unable to reach the user, create a participants: [user] goal to inform them. Do NOT block the update.
```

## Autonomous Solution Attempt Protocol (ASAP)

Before writing ANY entry to `mind/session/pending-questions.yaml`:

1. **Search** knowledge tree, reasoning bank, guardrails, experience archive for solutions
2. **If found**: ATTEMPT it (bounded standard effort)
3. **If attempt succeeds**: question unnecessary — do not write pq-XXX
4. **If attempt fails**: write pq-XXX with `attempted_solutions` documenting what was tried
5. **If nothing found**: write pq-XXX with concrete `default_action`

Enhanced pq-XXX schema adds:
- `attempted_solutions: [{source, action, result}]`
- `autonomous_search_done: true`

### `loop`

**The perpetual heartbeat.** Select and execute goals continuously until explicitly stopped:

```
Bash: aspirations-read.sh --meta → get session_count
Bash: load-aspirations-compact.sh → IF path returned: Read it
(compact aspirations data now in context — IDs, titles, statuses, priorities, categories, skills, recurring fields, participants, blocked_by, deferred fields, args, parent_goal, discovered_by, started — NO descriptions, NO verification blocks)

# Phase -1.5: Agent State Gate Check (must run before any state mutation)
Bash: `session-state-get.sh` → read output
IF output is NOT "RUNNING":
    Output: "Agent is not in RUNNING state. Cannot start loop."
    ABORT — do not enter the loop.

Bash: aspirations-meta-update.sh session_count <N+1>

# Phase -1: Initialize Working Memory (hippocampal session buffer)
Bash: wm-read.sh --json
if all working memory slots are null:
    Read core/config/memory-pipeline.yaml for slot definitions
    Bash: wm-init.sh
    # Seed individual slots:
    echo '{"session_id": "session-{session_count}", "session_start": "<today>"}' | Bash: wm-set.sh active_context
    recent_violations = read last 3 from mind/knowledge/patterns/ (if any exist)
    echo '<recent_violations_json>' | Bash: wm-set.sh recent_violations
    pending_resolutions = Bash: pipeline-read.sh --stage active, filter resolving_soon
    echo '<pending_resolutions_json>' | Bash: wm-set.sh pending_resolutions
    # session_goal set from first selected goal (updated in Phase 4)
    # micro_hypotheses = [] and known_blockers = [] already initialized by wm-init.sh
    IF mind/session/handoff.yaml exists AND handoff.known_blockers_active is non-empty:
        echo '<handoff_blockers_json>' | Bash: wm-set.sh known_blockers

# Phase -0.5: Session Marker + Recovery Counter Reset
# Note: Stop hook is defined globally in .claude/settings.json — no signal file needed.
# loop-active is informational only (indicates a loop ran this session).
Bash: `session-signal-set.sh loop-active`
Bash: `session-counter-clear.sh`
# Capture this session as the autonomous loop runner (stop hook uses this to
# allow other sessions to stop freely while only blocking the runner session)
Bash: `cp mind/session/latest-session-id mind/session/running-session-id 2>/dev/null || true`
goals_completed_this_session = 0  # counter for Phase 9.7 periodic reflection
evolutions_this_session = 0       # counter for Phase 9 evolution cap
goals_since_last_alignment_check = 0  # counter for Phase 2 Self-alignment check
aspirations_touched_this_session = set()  # track which aspirations had goals executed (for sessions_active)

# Phase -0.5a: Background Agent Result Collection
# If background agents were dispatched in a previous turn, their completion
# notifications may have re-engaged us. Collect results and deregister.
IF mind/session/pending-agents.yaml EXISTS:
    Bash: `pending-agents.sh list --json`
    pending = parse JSON output
    IF pending.agents is non-empty:
        FOR EACH agent in pending.agents:
            # Check if this agent's results are available in context
            # (Claude Code injects completion notifications into the turn)
            IF completion results available for agent.agent_id:
                Store results as prefetch research for agent.goal_id
                Bash: `pending-agents.sh deregister --id {agent.agent_id}`
                Log: "AGENT RESULT COLLECTED: {agent.agent_id} for {agent.goal_id}"
            ELSE:
                # Agent still running or no results yet — prune-stale handles timeout
                Log: "AGENT PENDING: {agent.agent_id} dispatched at {agent.dispatched_at}"
        # If all agents collected or pruned, clean up
        Bash: `pending-agents.sh has-pending` → check exit code
        IF exit 1 (no pending): Bash: `pending-agents.sh clear`

# Phase -0.5c: Compact Checkpoint Processing
# After autocompact, PreCompact hook saves encoding state to compact-checkpoint.yaml.
# Process the encoding queue NOW while context is freshest (just after compaction).
IF mind/session/compact-checkpoint.yaml EXISTS:
    Read mind/session/compact-checkpoint.yaml → checkpoint
    compact_count = checkpoint.compact_count

    # 1. Restore encoding queue if lost during compaction
    Bash: wm-read.sh encoding_queue --json
    IF encoding_queue is empty AND checkpoint.encoding_queue is non-empty:
        all_items = checkpoint.encoding_queue + checkpoint.prior_encoding_items
        Dedup by (source_goal + observation hash)
        echo '<all_items_json>' | Bash: wm-set.sh encoding_queue

    # 2. Process encoding queue NOW (freshest context after compaction)
    Bash: wm-read.sh encoding_queue --json
    IF encoding_queue is non-empty:
        budget = min(5, len(encoding_queue))
        Sort encoding_queue by encoding_score descending
        FOR EACH item in encoding_queue[:budget]:
            Read target leaf node from mind/knowledge/tree/_tree.yaml
            # PRECISION-FIRST ENCODING (see mind/conventions/precision-encoding.md)
            IF item has precision_manifest AND non-empty:
                precision_data = item.precision_manifest
            ELIF item has source_experience:
                Load experience record; extract precision from verbatim_anchors + content
            ELSE:
                Scan observation for exact values; build precision manifest
            IF precision_data non-empty:
                Append to node "## Verified Values" section (create if missing):
                  For each item: - **{label}**: `{value}` {unit} — {context}
            Append compressed narrative (1-3 sentences) to Key Takeaways section
            PRECISION AUDIT: verify all precision items appear in Verified Values
            Update last_updated + last_update_trigger: "compact_encoding"
            Invoke /reflect-tree-update for affected node (propagate upward)
        Remove processed items from encoding_queue
        echo '<updated_encoding_queue_json>' | Bash: wm-set.sh encoding_queue
        Output: "POST-COMPACT ENCODING: {budget} items processed (compaction #{compact_count})"

    # 2.5 Restore retrieval manifest if utilization feedback is pending
    # If autocompact hit between goal execution and Phase 4.26, the retrieval
    # manifest (which items were ACTIVE/SKIPPED) would be lost. Restore it so
    # the Learning Gate (Phase 9.5 check #4) can force Phase 4.26 to complete.
    IF checkpoint.retrieval_manifest exists AND checkpoint.retrieval_manifest.utilization_pending == true:
        echo '<checkpoint_retrieval_manifest_json>' | Bash: wm-set.sh active_context.retrieval_manifest
        Output: "POST-COMPACT RETRIEVAL: manifest restored for {checkpoint.retrieval_manifest.goal_id} — Phase 4.26 pending"

    # 3. Restore other state if lost during compaction
    Bash: wm-read.sh knowledge_debt --json
    IF knowledge_debt is empty AND checkpoint.knowledge_debt is non-empty:
        echo '<checkpoint_knowledge_debt_json>' | Bash: wm-set.sh knowledge_debt
    Bash: wm-read.sh micro_hypotheses --json
    IF micro_hypotheses is empty AND checkpoint.micro_hypotheses is non-empty:
        echo '<checkpoint_micro_hypotheses_json>' | Bash: wm-set.sh micro_hypotheses

    # 4. Consume checkpoint (one-shot — prevents reprocessing)
    Delete mind/session/compact-checkpoint.yaml

FOREVER:
    # Phase 0: Automated completion checks
    run_completion_checks()

    # Phase 0.5: Aspiration health check
    Bash: load-aspirations-compact.sh → IF path returned: Read it
    (compact aspirations now in context — use for health check)
    active_count = count of aspirations with status "active"
    if active_count < 2:
        invoke /create-aspiration from-self --plan
        log "Aspiration health: below minimum, created new aspirations via planning"

    # Phase 0.5a: Pre-Selection Guardrail Check
    # Before choosing the next goal, run guardrail-check.sh for pre-selection
    # guardrails. The script returns concrete matches — no manual filtering.
    Bash: matched=$(bash core/scripts/guardrail-check.sh --context any --phase pre-selection 2>/dev/null)
    IF matched.matched_count > 0:
        FOR EACH guardrail in matched.matched:
            # action_hint contains a runnable command (from the learned guardrail)
            Bash: <run {guardrail.action_hint}>
            IF output reveals issues (non-empty error alerts, health failures):
                # Invoke CREATE_BLOCKER protocol (same as aspirations-execute)
                # Unblocking goal created automatically — HIGH priority,
                # goal-selector will rank it above routine work.
                → invoke CREATE_BLOCKER(affected_skill, issue_description,
                    null, relevant_aspiration_id, {diagnostics: check_output})

    # Phase 0.5b: Blocker resolution check
    Bash: wm-read.sh known_blockers --json
    IF known_blockers is non-empty:
        FOR EACH blocker WHERE resolution is null:
            # PRIMARY CHECK: Did the unblocking goal complete?
            IF blocker.unblocking_goal:
                goal_status = check aspirations for goal {blocker.unblocking_goal}
                IF goal_status == "completed":
                    Set blocker.resolution = "Unblocking goal completed"
                    Log: "BLOCKER RESOLVED: {blocker.blocker_id} — unblocking goal done"
                    continue
            # Existing checks: user goal, expiry, infra-health success
            IF blocker.blocker_id matches a completed user goal (check aspirations):
                Set blocker.resolution = "User goal completed"
                Log: "BLOCKER RESOLVED: {blocker.blocker_id}"
            ELIF current_session - blocker.detected_session > 3:
                Set blocker.resolution = "Expired (3-session limit)"
                Log: "BLOCKER EXPIRED: {blocker.blocker_id}"
            # SUCCESS-BASED CLEARING: check infra-health.yaml
            ELIF component = map blocker.affected_skills to infra component:
                # Resolve skill → component via mind/infra-health.yaml skill_mapping + category_mapping
                last_success = Bash: mind-read.sh infra-health.yaml --field components.{component}.last_success
                IF last_success is not null AND last_success > blocker.detected_at:
                    Set blocker.resolution = "Infrastructure recovered (success at {last_success})"
                    Log: "BLOCKER RESOLVED: {blocker.blocker_id} — infrastructure healthy since {last_success}"
                # ACTIVE REPROBING: probe every iteration while blocker exists.
                # Probes are lightweight (curl localhost <1s, SSH echo ~5s).
                # Cost of staying blocked all session >> cost of repeated probes.
                # infra-health.sh has auto-recovery for registered components
                # (see infra-health.yaml), so probing IS recovery.
                # Previous design probed once per session — if infrastructure came up
                # 5 minutes later, the blocker persisted for the entire session.
                ELSE:
                    Bash: result=$(bash core/scripts/infra-health.sh check {component} 2>/dev/null)
                    Parse result JSON → status
                    IF status == "ok":
                        Set blocker.resolution = "Infrastructure recovered (probe succeeded)"
                        Log: "BLOCKER CLEARED BY PROBE: {blocker.blocker_id} — {component} reachable"
                    ELIF status == "provisionable":
                        # Component is down but can be provisioned
                        provision_skill = result.provision_skill
                        Log: "BLOCKER PROVISIONING: attempting {provision_skill} for {component}"
                        provision_result = invoke {provision_skill}
                        IF provision_result succeeded:
                            Set blocker.resolution = "Provisioned via {provision_skill}"
                            Log: "BLOCKER CLEARED BY PROVISIONING: {blocker.blocker_id}"
                        ELSE:
                            Log: "BLOCKER PROVISIONING FAILED: {blocker.blocker_id} — {provision_skill} could not start {component}"
                    ELSE:
                        Log: "BLOCKER PROBE FAILED: {blocker.blocker_id} — {component} still unreachable"
        echo '<updated_blockers_json>' | Bash: wm-set.sh known_blockers

    # Phase 0.7: (removed)

    # Phase 1: Check for recurring goals due
    check_recurring_goals()

    # Phase 2: Select next goal (scored via script, with exploration noise)
    batch_mode = false
    IF first_action is set (from handoff, first iteration only):
        Look up goal by first_action.goal_id via compact aspirations data (loaded at loop entry)
        effort_level = first_action.effort_level  # pre-computed default
        Clear first_action (consumed — subsequent iterations score normally)
        # Still run Phase 2.5 — apply focus context and locked decisions.
        # Use pre-computed effort_level as starting point (assessment may override).
    ELSE:
        # ASSERTION: goal-selector.sh MUST run every iteration. No exceptions.
        # Do NOT skip this step or substitute narrative assessment of goal availability.
        # After autocompact, memory of blockers is unreliable. The script reads live state.
        # Convention: core/config/conventions/goal-selection.md
        Bash: goal-selector.sh
        ranked_goals = parse JSON array output
        # Each entry: {goal_id, aspiration_id, title, skill, category, recurring, score, breakdown, raw}

        # Precondition gate: goal-selector.py cannot evaluate natural-language preconditions.
        # Check them here against session state (working memory, journal).
        for each goal in ranked_goals:
            goal_record = look up goal in active_aspirations by goal.goal_id
            if goal_record.verification.preconditions exist:
                Evaluate each precondition against current session state
                if any precondition not met: remove goal from ranked_goals

        # Context-aware batching: batch more aggressively when context is fresh,
        # conservatively when tight. Reads zone from mind/session/context-budget.json
        # (written by status line script). Default zone "normal" if file missing.
        Bash: cat mind/session/context-budget.json 2>/dev/null || echo '{"zone":"normal"}'
        zone = parsed zone field from output

        batch = [ranked_goals[0]] if ranked_goals else []
        batch_mode = False
        if len(ranked_goals) > 1:
            IF zone == "fresh":
                # Fresh context (<40% used) — batch up to 3 same-category goals.
                # Stop at first mismatch — never skip a higher-ranked goal.
                for g in ranked_goals[1:3]:
                    if g.category == batch[0].category:
                        batch.append(g)
                        batch_mode = True
                    else:
                        break
            ELIF zone == "normal":
                # Normal context (40-65%) — batch up to 2 same-category + same-aspiration
                g = ranked_goals[1]
                if (g.category == batch[0].category
                        AND g.aspiration_id == batch[0].aspiration_id):
                    batch.append(g)
                    batch_mode = True
            ELSE:
                # Tight context (>65%) — original strict criteria
                g = ranked_goals[1]
                same_context = (g.category == batch[0].category
                                AND g.aspiration_id == batch[0].aspiration_id
                                AND g.skill == batch[0].skill)
                if same_context AND g.effort_level == "minimal":
                    batch.append(g)
                    batch_mode = True

        goal = ranked_goals[0] if ranked_goals else None

        # Self-alignment check (replaces binary "creative boredom")
        # Runs periodically (every N goals) or when all goals are recurring.
        # Provides the LLM with alignment data to decide if planning is needed.
        # Guard: only when ranked_goals is non-empty — empty case handled by
        # the "if goal is None" no-goals path below (avoids double invocation).
        IF ranked_goals is non-empty:
            goals_since_last_alignment_check += 1
            all_recurring = every entry in ranked_goals has recurring == true

            IF all_recurring OR goals_since_last_alignment_check >= planning.check_interval_goals:
                goals_since_last_alignment_check = 0
                Bash: work-alignment.sh check --ranked-goals '<ranked_goals_json>'
                alignment = parse JSON output

                # The LLM interprets the alignment data. Reference thresholds from config
                # (novelty_drought_hours, maintenance_threshold) are guidelines. The LLM
                # considers the full picture: Does Self have uncovered priorities? Has
                # novelty dried up? Is work dominated by maintenance? Use judgment.
                IF alignment data suggests planning would be valuable OR all_recurring:
                    invoke /create-aspiration from-self --plan
                        with: alignment_data = alignment

                # Ambition check: are aspirations scoped ambitiously enough?
                # Count aspirations that are sprint-scope or legacy small (≤4 goals, no scope field).
                small_count = count active aspirations where:
                    scope == "sprint" OR (scope is null AND total_goals <= 4)
                IF small_count >= 3:
                    Output: "▸ AMBITION CHECK: {small_count} sprint-scope aspirations detected. Consider merging related ones into project-scope arcs."
                    # Log for evolve step 2 merge evaluation
                    echo '{"date":"<today>","event":"ambition_check","details":"{small_count} small aspirations — merge candidates flagged"}' | bash core/scripts/evolution-log-append.sh

                # Proceed — execute top goal this iteration regardless

    if goal is None:
        # No goals available — THIS IS NOT A STOP CONDITION
        # Generate new work via Self-driven aspiration creation
        # IMPORTANT: Do NOT use AskUserQuestion here. The loop must never block.
        invoke /create-aspiration from-self --plan
        if new_aspirations_generated:
            continue
        else:
            # Fallback: ASAP before escalating
            # Search for autonomous work before writing pq-XXX
            Search knowledge tree for under-explored nodes (low retrieval_count, low confidence)
            Search reasoning bank for applicable strategies
            Search experience archive for "what worked when stuck"
            IF autonomous work found:
                Execute the discovered work
                continue
            # Only after ASAP exhausted:
            Write to mind/session/pending-questions.yaml:
                question: "No executable goals remain. What should the agent focus on next?"
                default_action: "Exploring broadly based on current knowledge gaps"
                autonomous_search_done: true
                attempted_solutions:
                  - source: "knowledge tree scan"
                    action: "checked for under-explored nodes"
                    result: "none found with actionable work"
                status: pending
            invoke /research-topic (explore broadly based on current knowledge gaps)
            invoke /reflect --full-cycle
            continue

    # Phase 2.25: Selection Context Loading
    # Load tree summary for candidate goals (convention-style demand loading).
    # Uses cached _summary.json — same dedup pattern as convention files.
    # Informs Phase 2.5 familiarity/value assessment with domain grounding.
    IF ranked_goals is non-empty:
        Bash: load-tree-summary.sh
        IF output is non-empty (path returned):
            Read the returned path  # hooks auto-track; gates future re-reads
        # Parse tree summary JSON. For each candidate goal's category:
        # Extract: {key, summary, confidence, capability_level, depth, children}
        selection_context = match ranked_goals[:5] categories against tree summary nodes

    # Phase 2.5: Metacognitive Assessment (self-regulated effort)
    # Tree summary cached by Phase 2.25 — Phase 4 reuses it without reloading.
    Read mind/profile.yaml → focus (null or natural language string)
    Read decisions_locked from handoff context (if carried forward by boot):
      - Apply locked decisions that constrain this goal's domain/strategy
      - Ignore expired entries (current_session - made_session > 3)

    For the selected goal, assess:
      1. Familiarity: Have I done something like this before?
         - Check: category accuracy in experiential-index, similar past goals
         - Check selection_context for goal's category:
           capability_level MASTER/EXPLOIT → System 1 (high familiarity)
           capability_level CALIBRATE → moderate familiarity
           capability_level EXPLORE or missing → System 2 (low familiarity)
         - High familiarity → System 1 available (fast, pattern-matching)
         - Low familiarity → System 2 required (deliberate reasoning)

      2. Expected Value: What will I learn or accomplish?
         - High (novel insight, deadline, coding deliverable) → worth full effort
         - Medium (useful but not critical) → standard effort
         - Low (routine, already known, marginal) → standard or skip

      3. Cost Estimate: How many tokens will this likely consume?
         - Quick check/lookup or routine → standard
         - Deep exploration + multi-step → full

      4. Infrastructure Needs: Does this goal require infrastructure that might not be running?
         - Check selection_context (tree summary from Phase 2.25) for infrastructure cues:
           goal title/category references live data, game session, runtime verification,
           or test circuits that need a running server
         - Check knowledge nodes loaded for this category: do they mention infrastructure
           dependencies? (e.g., test circuits node lists required services)
         - IF infrastructure is needed:
             component = determine component name from knowledge context
             Bash: probe=$(bash core/scripts/infra-health.sh check {component} 2>/dev/null)
             Parse probe JSON → status
             IF status == "provisionable":
                 provision_skill = probe.provision_skill
                 # Same-skill guard: if provisioning IS the goal, skip — Phase 4 handles it.
                 IF provision_skill == goal.skill:
                     Log: "INFRA ASSESSMENT: {component} provisionable — skipping (goal skill will provision)"
                 ELSE:
                     Log: "INFRA ASSESSMENT: {component} needed — invoking {provision_skill}"
                     provision_result = invoke {provision_skill}
                     IF succeeded: Log: "INFRA PROVISIONED: {component} started for {goal.id}"
                     ELSE: Log: "INFRA PROVISIONING FAILED — proceeding anyway (fail open)"
             ELIF status == "ok":
                 Log: "INFRA ASSESSMENT: {component} already running"
             # Fail open: goal proceeds regardless of provisioning outcome

    If focus is set, apply focus context to value assessment:
      - focus contains coding/development/shipping keywords →
          coding goals get value boost, exploration goals get value penalty
      - focus contains exploration/learning/research keywords →
          exploration goals get value boost, routine goals get value penalty
      - focus contains efficiency/tokens/minimal keywords →
          all goals get higher skip threshold

    Determine effort_level:
      - full:     Thorough execution, full spark check + metacognitive Q
      - standard: Normal execution, normal sparks (default behavior)
      - skip:     Don't execute now — focus mismatch or zero expected value

    Note: Retrieval is always intelligent and full. effort_level controls execution
    thoroughness and spark depth, not retrieval depth.

    IMPORTANT: Token cost and wall-clock time are NOT reasons to defer or skip.
    The loop runs forever. If a goal requires a long-running process (e.g., running
    a batch job for hours, waiting for test results), start the process in the
    background (run_in_background or nohup), set deferred_until for the check-back
    time, and continue with other goals. "This is expensive" is never a valid skip
    reason. Valid skip reasons: focus mismatch, zero expected value, blocker gate.
    NOT valid: cost, time, "too uncertain", "too expensive."

    If skip: remove from candidates, reassess next-highest scoring goal
    If all goals are skipped: fall through to no-goals behavior (gap analysis)

    # Phase 2.5b: Blocker Gate (with verification probe)
    # Rule: verify-before-assuming.md — never trust a stale blocker without probing.
    Bash: wm-read.sh known_blockers --json
    IF known_blockers is non-empty:
        FOR EACH blocker WHERE resolution is null:
            IF goal.skill in blocker.affected_skills:
                # VERIFY: probe infrastructure before trusting stale blocker
                # Resolve skill → component via mind/infra-health.yaml skill_mapping + category_mapping
                component = map goal.skill to infra component
                IF component:
                    Bash: result=$(bash core/scripts/infra-health.sh check {component} 2>/dev/null)
                    Parse result JSON → status
                    IF status == "ok":
                        Set blocker.resolution = "Probe succeeded — infrastructure available"
                        Log: "BLOCKER CLEARED BY PROBE: {blocker.blocker_id}"
                        echo '<updated_blockers_json>' | Bash: wm-set.sh known_blockers
                        continue  # Blocker no longer applies, goal proceeds normally
                    ELIF status == "provisionable":
                        # Component is down but can be provisioned
                        provision_skill = result.provision_skill
                        Log: "BLOCKER GATE PROVISIONING: attempting {provision_skill} for {component}"
                        provision_result = invoke {provision_skill}
                        IF provision_result succeeded:
                            Set blocker.resolution = "Provisioned via {provision_skill}"
                            Log: "BLOCKER CLEARED BY PROVISIONING: {blocker.blocker_id}"
                            echo '<updated_blockers_json>' | Bash: wm-set.sh known_blockers
                            continue  # Blocker cleared, goal proceeds
                        ELSE:
                            Log: "BLOCKER GATE PROVISIONING FAILED: {provision_skill} could not start {component}"
                # Probe failed, provisioning failed, no component mapping, or no_credentials — blocker remains valid
                Log: "BLOCKER GATE: {goal.id} blocked by {blocker.blocker_id} (probe confirmed)"
                Append goal.id to blocker.affected_goals if not present
                echo '<updated_blockers_json>' | Bash: wm-set.sh known_blockers
                Set effort_level = skip
                # Re-evaluate: remove this goal, try next-highest scoring

    # Phase 2.6: Pre-Fetch Context for Upcoming Goals
    #
    # The host MAY dispatch team agents to gather information for upcoming goals.
    # This parallelizes the SLOW part (research, reading, analysis) while the host
    # executes the current goal. Team agents are research assistants, not executors.
    #
    # Team agents MUST NOT:
    #   - Invoke skills (skills write to shared state)
    #   - Write or edit ANY files
    #   - Call state-mutating scripts
    #
    # Team agents report findings via message. Host applies findings during execution.
    #
    # When to pre-fetch:
    #   - Next goal needs web research (team agent can search while host executes current goal)
    #   - Next goal needs code analysis (team agent can read and analyze while host works)
    #   - Multiple independent research tasks exist
    #
    # When NOT to pre-fetch:
    #   - Only one goal available
    #   - Next goal is write-only (no research phase)
    #   - Goals are interdependent (second depends on first's outcome)

    prefetch_goals = []
    IF host chooses to pre-fetch:
        FOR g in ranked_goals[1:]:
            IF len(prefetch_goals) >= max_concurrent_goals - 1: break
            IF g has a research/analysis phase that can run independently:
                prefetch_goals.append(g)

    # Phase 2.9: Load full goal detail for execution
    # Compact data (from load-aspirations-compact.sh) lacks description and verification.
    # Phase 3 (decomposition) and Phase 4-5 (execution, verification) need these fields.
    # ONE targeted read per iteration — do NOT remove this or execution runs blind.
    Bash: aspirations-read.sh --id {goal.aspiration_id}
    goal = find goal by goal.goal_id in the returned aspiration's goals array
    # Now goal has: description, verification, context_needed, plus all compact fields.

    # Phase 3: Compound goal decomposition
    # A goal needs decomposition if ANY of these are true (from /decompose Step 3):
    #   1. Title contains "and" connecting distinct actions
    #   2. Requires discovering information before acting on it
    #   3. Later steps depend on intermediate findings
    #   4. Uses vague verbs: "Establish", "Build", "Improve", "Set up"
    #   5. Would require invoking 2+ different skills
    #   6. Estimated effort > 1 session
    if goal is compound by ANY of the above criteria:
        invoke /decompose goal.id
        # /decompose marks parent "decomposed" + adds sub-goals if truly compound.
        # If its Step 2 primitiveness test says primitive, it returns unchanged.
        if goal.status == "decomposed":
            continue  # re-select from new sub-goals
        # else: heuristic false positive — goal is primitive, fall through to Phase 4

    # Phase 4 Preamble: Cost-Ordered Precondition Checking
    # Before expensive data retrieval, check local/cheap preconditions first.
    # See: guard-009

    # Phase 4: Execute (MANDATORY — read protocol file, DO NOT inline from memory)
    # CRITICAL: The intelligent retrieval protocol must be loaded from a file — never from
    # compressed memory. Without reading it, the agent skips retrieval and executes blind.
    Bash: aspirations-update-goal.sh <goal-id> status in-progress
    Bash: aspirations-update-goal.sh <goal-id> started <today>
    Bash: load-execute-protocol.sh
    IF output is non-empty (path returned): Read the returned path
    # The protocol digest is now in context. Follow its full protocol.
    # For rare edge cases (CREATE_BLOCKER detail, Cognitive Primitives JSON):
    #   Read .claude/skills/aspirations-execute/SKILL.md
    Inputs: goal, effort_level, batch_mode, batch, parallel_goals
    # Returns: result, external_changes, experience_id, infrastructure_failure flag, outcome_class

    IF infrastructure_failure:
        continue  # Skip Phases 5-9 for this goal

    # Routine streak tracking (anti-drift safeguard)
    # After 5 consecutive routine outcomes for the same recurring goal,
    # force one full productive pass to catch accumulated learning debt.
    IF outcome_class == "routine":
        routine_streaks[goal.id] = routine_streaks.get(goal.id, 0) + 1
        IF routine_streaks[goal.id] >= 5:
            outcome_class = "productive"  # override — force full pipeline
            routine_streaks[goal.id] = 0
    ELIF outcome_class == "productive":
        routine_streaks[goal.id] = 0  # reset on any productive outcome

    # Phase 5: Verify completion
    if goal.hypothesis_id:
        # Hypothesis goal — result comes from /review-hypotheses
        if result == "CONFIRMED" or result == "CORRECTED":
            if not goal.recurring:
                Bash: aspirations-update-goal.sh <goal-id> status completed
            Bash: aspirations-update-goal.sh <goal-id> completed_date <today>
            Bash: aspirations-update-goal.sh <goal-id> achievedCount <N+1>
            if goal.recurring:
                # Recurring goals NEVER set status to "completed" — they stay "pending".
                # Update streaks and timestamps only. Goal-selector time gate prevents
                # re-selection until interval_hours elapses.
                interval = goal.interval_hours (fallback: remind_days * 24, default: 24)
                elapsed = hours_since(goal.lastAchievedAt)
                Bash: aspirations-update-goal.sh <goal-id> lastAchievedAt "$(date +%Y-%m-%dT%H:%M:%S)"
                if elapsed is not None and elapsed > 2 * interval:
                    new_streak = 1
                else:
                    new_streak = currentStreak + 1
                Bash: aspirations-update-goal.sh <goal-id> currentStreak <new_streak>
                Bash: aspirations-update-goal.sh <goal-id> longestStreak <max(new_streak, longestStreak)>
            # aspiration progress is updated automatically by update-goal when status changes
            unblock dependent goals
        elif result == "EXPIRED":
            Bash: aspirations-update-goal.sh <goal-id> status expired
        else:
            # PENDING — hypothesis hasn't resolved yet
            Bash: aspirations-update-goal.sh <goal-id> status pending  # retry next cycle
    elif goal.verification or goal.completion_check:
        # Unified verification: check verification.checks (new) or completion_check (legacy)
        checks = goal.verification.checks if goal.verification else [goal.completion_check]
        # --- Phase 5 Verification Escalation (empty-checks protocol) ---
        # When checks are empty, the agent MUST answer three structured questions
        # before deciding completion. Replaces open-ended "judgment" with evidence
        # articulation. Cost: ~200 tokens per empty-checks goal.
        IF len(checks) == 0:
            # Q1 EVIDENCE: "What concrete artifact (file, output, state change, commit)
            #   proves this goal succeeded?" Must reference a checkable artifact.
            # Q2 NEGATIVE CHECK: "What would it look like if this APPEARED to succeed
            #   but actually failed? Did I check for that?" Must name a specific failure mode.
            # Q3 INTEGRATION SCOPE: "Did I verify at the integration level
            #   (caller -> target -> side effect), or only the unit level?"
            # Evaluate Q1:
            IF Q1 references a concrete artifact (file path, command output, git diff):
                Attempt to verify the artifact (Read file, check existence, inspect output)
                IF artifact verification fails:
                    all_passed = false  # artifact didn't check out
                    Bash: aspirations-update-goal.sh <goal-id> status pending
                    log "Phase 5 escalation: claimed artifact not verified — retrying"
                ELSE:
                    all_passed = true  # artifact confirmed
            ELSE:
                all_passed = false  # no concrete evidence — goal stays pending
            # Evaluate Q2 only when goal is passing — no point flagging already-failed goals.
            # HARD GATE: if you CAN name a failure mode but DIDN'T check, check now.
            IF all_passed:
                IF Q2 names a specific failure mode AND confirms it was checked → PASS
                ELIF Q2 names a specific failure mode but was NOT checked:
                    # You know what could go wrong — prove it didn't.
                    Run the check now (read file, run command, inspect state).
                    IF check reveals a problem:
                        all_passed = false
                        Bash: aspirations-update-goal.sh <goal-id> status pending
                        log "Phase 5 Q2 hard gate: named failure mode was NOT checked — check failed"
                    # If check passes: all_passed stays true — failure mode was disproven
                ELIF Q2 is vague ("I don't think it failed", empty, or too generic):
                    # Genuinely can't think of a failure mode — soft signal (unchanged)
                    echo '{"verification_gap": "negative_check_missing", "goal_id": "<goal-id>"}' | Bash: wm-append.sh sensory_buffer
        ELSE:
            all_passed = all(check_passes(c) for c in checks)
        if all_passed:
            if not goal.recurring:
                Bash: aspirations-update-goal.sh <goal-id> status completed
            Bash: aspirations-update-goal.sh <goal-id> completed_date <today>
            Bash: aspirations-update-goal.sh <goal-id> achievedCount <N+1>
            if goal.recurring:
                # Recurring goals NEVER set status to "completed" — they stay "pending".
                # Update streaks and timestamps only. Goal-selector time gate prevents
                # re-selection until interval_hours elapses.
                interval = goal.interval_hours (fallback: remind_days * 24, default: 24)
                elapsed = hours_since(goal.lastAchievedAt)
                Bash: aspirations-update-goal.sh <goal-id> lastAchievedAt "$(date +%Y-%m-%dT%H:%M:%S)"
                if elapsed is not None and elapsed > 2 * interval:
                    # Missed interval — reset streak
                    new_streak = 1
                else:
                    # On-time or first completion — increment streak
                    new_streak = currentStreak + 1
                Bash: aspirations-update-goal.sh <goal-id> currentStreak <new_streak>
                Bash: aspirations-update-goal.sh <goal-id> longestStreak <max(new_streak, longestStreak)>
            else:
                update goal streaks (currentStreak, longestStreak, lastAchievedAt)
            # aspiration progress is updated automatically by update-goal when status changes
            unblock dependent goals
        else:
            # Goal ran but didn't achieve desired end state
            Bash: aspirations-update-goal.sh <goal-id> status pending  # retry next cycle
            log "Goal executed but verification check failed"

    # Phase 6: Spark check (micro-evolution) + Phase 6.5: Immediate Learning
    # SKIP: outcome_class == "routine" — routine outcomes have zero spark potential.
    IF outcome_class == "productive":
        invoke /aspirations-spark with: goal, result, effort_level
        # Sub-skill handles: adaptive spark questions, all handlers (sq-009, sq-012,
        # sq-c05, sq-c03, sq-c04, sq-013, sq-007), immediate learning (reasoning bank,
        # guardrails, forge awareness), aspiration-level spark when entire aspiration completes.

    # Phase 7: Aspiration-level check
    # Re-read the goal's parent aspiration via compact loader
    # GUARD: Aspirations where ALL goals are recurring can never "complete" — they're perpetual.
    asp = get_aspiration(goal)  # from compact aspirations data
    all_recurring = all(g.get("recurring", False) for g in asp.goals)
    if not all_recurring and aspiration_fully_complete(asp):
        run_aspiration_spark(goal.aspiration)

        # ── Phase 7.5: Aspiration Completion Review ──────────────────────
        # Before archival, sweep ALL goal outcomes for outstanding work.
        # Step 8.5 catches per-goal findings during tree encoding;
        # this is the cross-goal safety net at aspiration close.
        #
        # Runs BEFORE archival so the aspiration is still in the live file.
        # If new goals are added to THIS aspiration, archival is deferred.

        Output: "▸ Completion Review: scanning {asp.id} goals for outstanding work..."
        goals_added_to_completing_asp = 0  # If >0 after review, aspiration is reopened (skip archival)

        # ── Step 7.5.1: Gather and scan goal data ────────────────────────
        outstanding_findings = []

        FOR EACH g in asp.goals:
            IF g.recurring:
                continue  # Recurring goals don't produce outstanding work

            # Skipped/expired = planned work that never happened
            IF g.status in ("skipped", "expired"):
                outstanding_findings.append({
                    type: "abandoned_goal",
                    goal_id: g.id,
                    title: g.title,
                    description: g.description,
                    match: g.title,
                    priority: g.priority or "HIGH",
                    category: g.category
                })
                continue

            # Load experience entry for this goal
            exp_result = Bash: experience-read.sh --goal {g.id}

            IF exp_result is empty:
                # No experience — check goal's verification.outcomes for partial signals
                IF g.verification and g.verification.outcomes:
                    outcomes_text = join(g.verification.outcomes)
                    IF outcomes_text matches (not yet|partial|remaining|deferred|TODO):
                        outstanding_findings.append({
                            type: "partial_completion",
                            goal_id: g.id,
                            title: g.title,
                            match: extracted_reference,
                            priority: "MEDIUM",
                            category: g.category
                        })
                continue

            # ── Step 7.5.2: Keyword scan with negative filters ───────────
            # Scan summary first (cheap). Only read content_path if summary
            # triggers a signal (expensive).
            FOR EACH exp in exp_result:
                scan_text = exp.summary

                # Signal detection — same families as Step 8.5, plus follow-up patterns.
                # Negative filters prevent false positives on resolved findings.
                signals = []

                IF scan_text matches (root cause|caused by|due to|because of|stems from)
                   AND NOT followed by (fixed|resolved|applied|addressed|patched):
                    signals.append({type: "unresolved_root_cause", match: extracted_reference})

                IF scan_text matches (bug|defect|mismatch|incorrect|wrong|broken)
                   AND NOT followed by (fixed|resolved|patched|corrected):
                    signals.append({type: "unfixed_bug", match: extracted_reference})

                IF scan_text matches (should be changed|needs to be|replace with|update to|fix by|TODO|needs fixing)
                   AND NOT followed by (done|completed|applied|implemented):
                    signals.append({type: "proposed_change", match: extracted_reference})

                IF scan_text matches (follow-up|next step|remaining|outstanding|not yet|deferred|future work|blocked on):
                    signals.append({type: "explicit_followup", match: extracted_reference})

                IF scan_text matches (could also|might benefit|worth exploring|opportunity|improvement):
                    signals.append({type: "unacted_idea", match: extracted_reference})

                IF scan_text matches (needs|requires|must|should) + (to be|updating|fixing|adding|removing)
                   AND NOT followed by (done|completed|applied):
                    signals.append({type: "unimplemented_action", match: extracted_reference})

                # If summary had signals and content_path exists, read content for richer match
                IF len(signals) > 0 AND exp.content_path exists:
                    content = Read exp.content_path
                    # Re-scan on full content for better match extraction
                    # (signals already detected — this just enriches the match text)

                FOR EACH signal in signals:
                    outstanding_findings.append({
                        type: signal.type,
                        goal_id: g.id,
                        title: g.title,
                        match: signal.match,
                        source_experience: exp.id,
                        priority: determine_priority(signal.type),
                        category: g.category
                    })

        # ── Step 7.5.2b: Motivation Fulfillment Check ─────────────────
        # Even aspirations with no outstanding findings may have unfulfilled
        # motivations. The audit pattern produced follow-on
        # work (asp-264) because the motivation was broader than the goals.
        #
        # Read asp.motivation. Given the completed goals and their outcomes,
        # is this aspiration's stated motivation genuinely fulfilled?
        #
        # Fulfillment criteria:
        #   FULFILLED: Every claim in the motivation has been addressed by
        #     a completed goal, AND no natural next steps remain.
        #   NOT FULFILLED: The motivation describes ongoing work, the goals
        #     only covered part of it, OR goal outcomes revealed the
        #     motivation has more depth than initially planned.
        #
        # IF motivation is NOT fulfilled AND aspiration had < 10 completed goals:
        #     Generate 1-3 follow-up goals that advance the motivation
        #     For project+ scope aspirations (when scope field exists):
        #       consult knowledge tree for goal category before generating
        #     Add via: echo '<goal_json>' | aspirations-add-goal.sh <asp.id>
        #     goals_added_to_completing_asp += count
        #     Output: "▸ Motivation check: not yet fulfilled — added {count} goal(s)"
        # ELSE:
        #     Output: "▸ Motivation check: fulfilled"
        #
        # The < 10 completed goals guard prevents infinite growth. Aspirations
        # that have already completed 10+ goals have had ample opportunity to
        # deepen — let them archive.
        #
        # NOTE: This check is complementary to the scope-based maturity gate
        # in Phase 7 (if present). Maturity asks "did this live long enough?"
        # This asks "is the purpose actually done?" Both can fire.

        # ── Step 7.5.3: Early exit for clean aspirations ────────────────
        # Also check goals_added_to_completing_asp — Step 7.5.2b (motivation
        # check) may have added goals even when structural findings are empty.
        # Without this, we'd output "clean completion" right after "not yet
        # fulfilled" and skip journal/notification (Steps 7.5.6-7.5.8).
        IF len(outstanding_findings) == 0 AND goals_added_to_completing_asp == 0:
            Output: "▸ Completion Review: no outstanding work — clean completion"
            # Fall through to archival

        ELSE:
            Output: "▸ Completion Review: {len(outstanding_findings)} finding(s) — routing..."

            # ── Step 7.5.4: Dedup against ALL active goals ──────────────
            Bash: load-aspirations-compact.sh → IF path returned: Read it
            (compact aspirations now in context for dedup)
            pending_titles = extract all goal titles with status pending/in-progress from all_active
            # Completed goals may have already addressed the finding — include them in dedup
            completed_titles = extract all goal titles with status completed from all_active
            existing_titles = pending_titles + completed_titles

            deduplicated = []
            FOR EACH finding in outstanding_findings:
                candidate_title = build_title(finding)
                IF similar title already exists in existing_titles:
                    Output: "▸ Completion Review: {finding.type} from {finding.goal_id} — already tracked"
                    continue
                deduplicated.append(finding)

            IF len(deduplicated) == 0:
                Output: "▸ Completion Review: all findings already tracked — clean completion"
                # Fall through to archival

            ELSE:
                # ── Step 7.5.5: Route findings ──────────────────────────
                # Three tiers (same pattern as sq-013):
                # A) Fits completing aspiration → add goals here, defer archival
                # B) Fits another active aspiration → add goals there
                # C) New body of work → /create-aspiration with context

                goals_added_elsewhere = 0
                new_asp_context = []

                FOR EACH finding in deduplicated:
                    # Priority mapping:
                    # abandoned_goal, unresolved_root_cause, unfixed_bug → HIGH
                    # proposed_change, explicit_followup, unimplemented_action,
                    # unacted_idea, partial_completion → MEDIUM

                    # Title mapping:
                    # abandoned_goal → original title preserved
                    # unresolved_root_cause, unfixed_bug → "Unblock: Fix {match (50 chars)}"
                    # all others → "Idea: {match (50 chars)}"
                    # partial_completion → "Idea: Complete {title (40 chars)}"

                    # Routing: check if finding relates to asp's domain
                    IF finding directly relates to asp's motivation/scope:
                        target_asp = asp.id
                        goals_added_to_completing_asp += 1
                    ELIF another active aspiration covers this work:
                        target_asp = that aspiration's id
                        goals_added_elsewhere += 1
                    ELSE:
                        new_asp_context.append(finding)
                        continue

                    goal_json = {
                        title: build_title(finding),
                        status: "pending",
                        priority: finding.priority,
                        skill: null,
                        participants: ["agent"],
                        category: finding.category,
                        description: "Found during completion review of {finding.goal_id}: {finding.match}\n\nSource: {finding.source_experience or 'goal object'}\n\nDiscovered by: Phase 7.5 Aspiration Completion Review",
                        verification: {
                            outcomes: ["Finding addressed — fix applied or determined not actionable with reasoning"],
                            checks: []
                        },
                        discovered_by: finding.goal_id,
                        discovery_type: finding.type
                    }
                    echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh <target_asp>
                    Output: "▸ COMPLETION REVIEW: Created {goal_json.title} in {target_asp}"

                # Create new aspiration for unroutable findings
                IF new_asp_context:
                    invoke /create-aspiration from-self with: follow_up_context = new_asp_context,
                        source_aspiration = asp.id

                # ── Step 7.5.6: Notify user ─────────────────────────────
                total_new = goals_added_to_completing_asp + goals_added_elsewhere
                IF total_new > 0 OR new_asp_context:
                    notify user with:
                        type: "info"
                        subject: "Aspiration Completion Review: {asp.title}"
                        message: "{asp.id} completed. Review found {len(deduplicated)} outstanding item(s). {total_new} goal(s) created, {len(new_asp_context)} routed to new aspiration."

                # ── Step 7.5.7: Journal entry ───────────────────────────
                Append to journal:
                    "## Completion Review: {asp.id}\nFindings: {len(outstanding_findings)} raw, {len(deduplicated)} after dedup\nGoals added to {asp.id}: {goals_added_to_completing_asp}\nGoals added elsewhere: {goals_added_elsewhere}\nNew aspiration contexts: {len(new_asp_context)}"

                # ── Step 7.5.8: Archival gate ───────────────────────────
                IF goals_added_to_completing_asp > 0:
                    Output: "▸ Completion Review: {asp.id} reopened with {goals_added_to_completing_asp} new goal(s)"
                # ── End Phase 7.5 ───────────────────────────────────────

        # ── Phase 7.6: Maturity Check ────────────────────────────────
        # Before archival, check if aspiration has been active long enough for its scope.
        # Read core/config/aspirations.yaml → aspiration_scopes for definitions.
        scope = asp.scope or "project"  # default for legacy aspirations
        min_sessions_map = {"sprint": 1, "project": 2, "initiative": 4}
        min_sessions = min_sessions_map.get(scope, 2)
        sessions_active = asp.sessions_active or 0

        IF goals_added_to_completing_asp == 0 AND sessions_active < min_sessions AND scope != "sprint":
            # Aspiration completing too quickly for its scope — add depth
            Output: "▸ Maturity: {asp.id} completing after {sessions_active} session(s) but scope={scope} expects {min_sessions}. Deepening."
            # Generate 3-5 deeper follow-up goals:
            # - Brief web search on the aspiration's domain for deeper angles
            # - Tree consult: retrieve.sh --category {asp_primary_category} --depth shallow
            # - Add research, testing, or integration goals that deepen the work
            # Route findings back into this aspiration via aspirations-add-goal.sh
            goals_added_to_completing_asp += (number of goals added)
            Output: "▸ Maturity: {asp.id} reopened with {N} deeper goal(s)"
        # ── End Phase 7.6 ─────────────────────────────────────────────

        # Archive unless review or maturity check reopened the aspiration
        IF goals_added_to_completing_asp == 0:
            Bash: aspirations-complete.sh <asp-id>  # marks completed + moves to archive
            invoke /create-aspiration from-self --plan

    # Phase 8: State Update Protocol
    # For productive outcomes: all steps including Step 8.5 Actionable Findings Gate.
    # For routine outcomes: Steps 1-4 + abbreviated Step 7 only (bookkeeping + journal).
    # CRITICAL: Step 8 encodes goal insight to tree node — skipped for routine because
    # there IS no insight to encode. This is not bypassing learning; there is nothing to learn.
    invoke /aspirations-state-update with: goal, result, session_count, outcome_class
    # Sub-skill handles: goal status, aspiration progress, metadata, session count,
    # evolution triggers, readiness gates, journal write, TREE ENCODING, capability propagation.

    # Phase 8.1: Session touch tracking (for aspiration maturity check in Phase 7.6)
    # aspirations-update.sh requires full JSON on stdin — no field-level update API.
    asp_id = goal.aspiration_id
    IF asp_id not in aspirations_touched_this_session:
        aspirations_touched_this_session.add(asp_id)
        # Read-modify-pipe: increment sessions_active (once per session per aspiration)
        asp_json = Bash: aspirations-read.sh --id {asp_id}
        current = asp_json.sessions_active or 0
        asp_json.sessions_active = current + 1
        echo '<modified_asp_json>' | Bash: aspirations-update.sh {asp_id}

    # Phase 9: Evolution Triggers
    # Read core/config/evolution-triggers.yaml (definitions) and mind/evolution-triggers.yaml (state).
    #
    # Part A: Cadence/lifecycle triggers — checked EVERY iteration regardless of outcome_class.
    # These depend on system-level state, not individual goal execution quality.
    #   - evolution_cadence: Evolution hasn't run in N sessions → force periodic strategic review.
    #     Compare current session count (from aspirations-meta.json) vs trigger's last_fired session.
    #     If gap >= sessions_without_evolution (default 7) → FIRE.
    #   - capability_unlock: Any category just crossed a capability level threshold.
    #     This checks tree state, not execution output.
    #
    # If ANY cadence/lifecycle trigger fires (respecting cooldown):
    #   - Update trigger's last_fired and times_fired
    #   - Invoke /aspirations evolve with trigger context
    #   - Respect global.max_evolutions_per_session cap (default 2)
    cadence_triggers = check_cadence_triggers()  # evolution_cadence, capability_unlock
    if cadence_triggers and evolutions_this_session < max_evolutions_per_session:
        invoke /aspirations-evolve with: cadence_triggers, aspiration state
        evolutions_this_session += 1

    # Part B: Performance-based triggers — only for productive outcomes.
    # These analyze individual goal execution quality and need outcome data.
    # SKIP: outcome_class == "routine" — no insight to trigger evolution from.
    IF outcome_class == "productive":
        # 1. accuracy_drop: Compare last 10 hypotheses' accuracy vs overall. Drop > threshold → evolve.
        # 2. consecutive_losses: Check if last N hypotheses in any category are all corrected.
        # 3. pattern_divergence: For each pattern signature, compare actual hit_rate vs claimed. Divergence > threshold → evolve.
        # 4. stale_strategy: Check if category touched 3+ sessions without accuracy improvement.
        # 5. context_retrieval_ineffectiveness: Check if memory retrieval is failing to help.
        #    Scan resolved records with context_quality populated (requires min_sample=10):
        #    a. Gap recurrence: if any gap type in context_gaps_identified appears in >= 40% of records → FIRE
        #    b. Usefulness rate: if helpful_count / total < 50% → FIRE
        #    c. When fired: create research goal to investigate the weak memory layer
        #    Fail open: skip if fewer than min_sample records have context_quality rated.
        #
        # If ANY trigger fires (respecting cooldown):
        #   - Update trigger's last_fired and times_fired
        #   - Invoke /aspirations evolve with trigger context
        #   - Respect global.max_evolutions_per_session cap (default 2)
        #
        # When `stale_strategy` evolution trigger fires:
        #   - Invoke `/reflect --curate-memory` scoped to the trigger's category context
        #   - Log trigger activation in evolution log
        performance_triggers = check_performance_triggers()  # accuracy_drop, consecutive_losses, pattern_divergence, stale_strategy, context_retrieval_ineffectiveness
        if performance_triggers and evolutions_this_session < max_evolutions_per_session:
            invoke /aspirations-evolve with: performance_triggers, aspiration state
            # Reset cadence counter — evolution ran, so cadence should not fire redundantly.
            # Without this, cadence tracks only its OWN firings and would ignore performance-triggered evolutions.
            update evolution_cadence.last_fired in mind/evolution-triggers.yaml
            if "stale_strategy" in performance_triggers:
                invoke /reflect --curate-memory scoped to performance_triggers["stale_strategy"].category
                echo '{"event":"stale_strategy_curation","details":"curate-memory invoked for {category}","date":"<today>"}' | bash core/scripts/evolution-log-append.sh
            evolutions_this_session += 1

    # Phase 9.5: Learning Gate
    # For routine outcomes: explicit bypass — no tree encoding needed.
    # For productive outcomes: verify learning occurred before continuing.
    IF outcome_class == "routine":
        # No tree encoding needed — routine outcome, no new insight.
    ELSE:
        # The loop MUST NOT continue without confirming learning occurred.
        # Check:
        #   1. Was knowledge tree updated? (State Update Protocol Step 8)
        #      If no: complete it NOW — read _tree.yaml, find matching node,
        #      compress goal insight, edit node .md, propagate up chain.
        #   2. Was journal entry written? (State Update Protocol Step 7)
        #   3. Was working memory updated with goal context?
        Bash: wm-read.sh active_context --json
        # Verify active_context reflects current goal execution.
        # If goal produced no new insight (e.g., blocked goal, duplicate), note
        # explicitly: "No tree encoding needed — {reason}."
        # This gate prevents the "rush through goals without learning" failure mode.
        verify_learning_gate()

    # Phase 9.5b: Retrieval Gate (MANDATORY — runs for ALL outcomes)
    # Verify retrieval happened and utilization feedback completed.
    # This catches two failure modes:
    #   a) Retrieval was skipped entirely (no manifest — agent executed from compressed context)
    #   b) Retrieval happened but Phase 4.26 was interrupted by autocompact (utilization_pending: true)
    Bash: wm-read.sh active_context.retrieval_manifest --json
    IF retrieval_manifest missing AND goal.category maps to existing tree nodes:
        # RETRIEVAL WAS SKIPPED — perform it NOW.
        # Bash: load-execute-protocol.sh → IF path returned: Read it.
        # Follow Steps 1-5 of intelligent retrieval from the loaded protocol,
        # then run Phase 4.26 utilization feedback using the freshly-written manifest.
        Output: "▸ RETRIEVAL GATE: forced retroactive retrieval for {goal.id}"
    ELIF retrieval_manifest exists AND retrieval_manifest.utilization_pending == true:
        # Phase 4.26 was interrupted — run it NOW using the restored manifest.
        # (Manifest was restored by Phase -0.5c step 2.5 after compaction)
        Run Phase 4.26 utilization feedback using retrieval_manifest
        Output: "▸ RETRIEVAL GATE: forced utilization feedback for {goal.id}"
    # If goal genuinely has no matching tree nodes: pass silently.

    # Post-Batch Reflection (MANDATORY when batch_mode, runs once after all batch goals)
    IF batch_mode AND all batch goals complete:
        # Quick structured pause — NOT a full /reflect, just 3 questions:
        #   1. What patterns or connections emerged across the batch goals?
        #   2. Any surprises or corrections that should become hypotheses?
        #      If yes: add to working memory encoding_queue
        #   3. Any knowledge tree nodes that need reconciliation across batch findings?
        # Write observations to working memory sensory_buffer:
        # echo '<observation_json>' | Bash: wm-append.sh sensory_buffer
        # If any surprise > 7: form session-level hypothesis via pipeline-add.sh.
        # Time budget: 1-2 minutes, not a deep reflection.

    # Phase 9.7: Periodic Reflection Checkpoint
    # Every 5 completed goals, pause for a structured mini-reflection.
    # This is NOT a /reflect invocation — it's an inline 4-question pause:
    #   1. What patterns have I seen across the last 5 goals?
    #   2. Any recurring surprises or corrections? → form hypothesis via pipeline-add.sh
    #   3. Any knowledge nodes growing stale? → echo '<debt_json>' | Bash: wm-append.sh knowledge_debt
    #   4. Conclusion audit: scan conclusions slot for stale/weak judgments
    # Write observations: echo '<observation_json>' | Bash: wm-append.sh sensory_buffer
    # This catches patterns the per-goal spark checks miss.
    goals_completed_this_session += 1  # initialized to 0 at loop entry (Phase -0.5)
    IF goals_completed_this_session % 5 == 0:
        Run inline reflection checkpoint (questions 1-3 above).
        # Q4: Conclusion audit (judgment quality feedback loop)
        Bash: wm-read.sh conclusions --json
        FOR EACH conclusion in conclusions:
            # Re-verify stale blocking conclusions
            IF conclusion.re_verify_at is not null AND now >= conclusion.re_verify_at AND conclusion.outcome is null:
                IF conclusion.blocks_goals is non-empty:
                    # Re-probe: try to disprove the conclusion
                    Log: "CONCLUSION AUDIT: re-verifying '{conclusion.conclusion}' (blocking {N} goals)"
                    Run an independent check (different from original evidence)
                    IF check contradicts conclusion:
                        Update conclusion.outcome = "wrong"
                        Update conclusion.outcome_source = "Phase 9.7 re-verification"
                        Clear any blockers based on this conclusion
                        Log: "CONCLUSION OVERTURNED: '{conclusion.conclusion}'"
                    ELSE:
                        # Still valid — extend re-verify timer
                        Update conclusion.re_verify_at = now + 30 minutes
            # Flag low-evidence conclusions
            real_signals = count(e for e in conclusion.evidence if e.weight > 0)
            IF real_signals < 2 AND conclusion.blocks_goals is non-empty AND conclusion.outcome is null:
                Log: "WEAK CONCLUSION: '{conclusion.conclusion}' has {real_signals} signal(s) but blocks {N} goals"

    # Apply Pre-Fetched Research to Upcoming Goals
    # Team agents have gathered information for upcoming goals.
    # The host now executes each goal with enriched context.
    # The research is INPUT to the goal, not the goal's OUTPUT.
    IF prefetch_goals had agents dispatched:
        FOR EACH completed team agent research report:
            Set goal = the prefetch goal
            Store agent's findings as pre-gathered context for this goal
            Execute goal with enriched context (invoke goal.skill with args + findings)
            Run Phase 4.1 through Phase 9 for this goal (full cycle)
            # Each goal completes ALL phases before the next starts
        Shutdown team (SendMessage shutdown_request to all workers)
        # Deregister all agents from this team's tracking
        Bash: `pending-agents.sh deregister-team --team "research-{session}"`

    # Phase 10: Check stop conditions
    # 10a: Agent state check (authoritative)
    Bash: `session-state-get.sh` → read output
    IF output is NOT "RUNNING":
        Log "Agent state changed to non-RUNNING. Exiting loop gracefully."
        BREAK — run session-end consolidation, then exit.

    # Phase 10b: NEVER STOP, NEVER ASK
    # Once the aspirations loop has begun (you are in a running state):
    #   1. Do NOT pause to ask the human ANYTHING — not "should I continue?",
    #      not "should I push?", not "what do you prefer?", not "what next?"
    #   2. Do NOT output text that presents options and waits for a response
    #   3. Do NOT stop at the end of a goal to ask what to focus on next
    #   4. Do NOT ask for confirmation before pushing, deploying, or committing
    #      (Phase 4.2 domain steps handle this autonomously — just do it)
    # The human might be asleep, or gone from a computer and expects you to
    # continue working indefinitely. You are autonomous. Make the best decision
    # you can and keep going. If you're unsure, pick the safer/simpler option
    # and continue — don't stop to ask.
    #
    # If you run out of ideas, think harder — read papers referenced in the code,
    # re-read the in-scope files for new angles, try combining previous
    # near-misses, try more radical architectural changes. Run reflect, or
    # replay, and close out hypotheses, or make more.
    #
    # While team agents gather research, the host SHOULD use that time productively:
    # scan for new patterns, review knowledge gaps, check hypothesis pipeline,
    # or generate new aspirations. The host is an orchestrator, not idle.
    #
    # The ONLY stop conditions are:
    #   1. agent-state is no longer RUNNING (checked in 10a above)
    #   2. Critical unrecoverable error (file system corruption, etc.)
    # Context filling up is NOT a stop condition — autocompact handles it.
    # Needing user input is NOT a stop condition — make a decision and continue.

    # Phase 11: Working Memory Maintenance
    Read core/config/memory-pipeline.yaml
    Bash: wm-read.sh sensory_buffer --json
    If sensory_buffer.length > max_items (20):
        For each overflow item:
            Calculate encoding_score using encoding_gate formula
            If score >= encode_threshold (0.40):
                echo '<item_json>' | Bash: wm-append.sh encoding_queue
            If score < skip_threshold (0.15) → discard
            Else → flag for end-of-session review
        echo '<trimmed_sensory_buffer_json>' | Bash: wm-set.sh sensory_buffer
    Bash: wm-ages.sh --json
    For each slot in ages output:
        If slot age is stale (> 30 min) → flag for refresh
    Bash: wm-prune.sh

    # Re-score and loop
    continue
```

### Session-End Consolidation Pass

Run when the loop stops (any stop condition). This is the hippocampal "sleep replay"
that compresses session observations into long-term memory.

invoke /aspirations-consolidate with: session_count, goals_completed_this_session, evolutions_this_session
# Sub-skill handles: micro-sweep, encoding queue, dynamic budget, overflow management,
# encoding competition, tree encoding, knowledge debt sweep, snapshot invalidation,
# experience archive maintenance, journal, tree rebalancing, skill health report,
# aspiration archive sweep, user goal recap, continuation handoff, restart via /boot.

Note: Consolidation MUST NOT call session-state-set.sh.
Only /start and /stop may change agent-state.
Note: /stop also invokes this skill with stop_mode=true, which skips
Steps 6 (tree rebalancing), 7-8 (reporting), 8.7 (user recap), and 10 (restart).

### Auto-Session Continuation Protocol

The system keeps the aspirations loop running using two mechanisms:

**Within a session (global Stop hook with 4-tier recovery):**
A Stop hook defined in `.claude/settings.json` prevents Claude from stopping
between loop iterations. The hook (`core/scripts/stop-hook.sh`) uses escalating tiers:

- **Tier 1-3**: Tells Claude to invoke `/aspirations loop` (re-enter the loop).
  Handles transient context loss — the most common failure mode.
- **Tier 4**: Tells Claude to invoke `/recover` (diagnose + report to user).
  Creates `stop-loop` so the next stop attempt succeeds.
- **Tier 5+**: Safety valve — allows stop unconditionally. Prevents infinite loops.

The counter (via `session-counter-*.sh`) resets on successful loop entry
(Phase -0.5) and on boot (stale cleanup).

**Across consolidation cycles (inline restart):**
At the end of session-end consolidation, the loop invokes `/boot` directly.
`/boot` detects `mind/session/handoff.yaml` and runs in continuation mode (abbreviated
report, fast handoff to new loop cycle). Claude Code's context compression keeps this
going through multiple cycles.

**Signal files (all in `mind/session/`):**

| File | Purpose |
|------|---------|
| `loop-active` | Informational: indicates the loop ran this session |
| `stop-loop` | Create to stop the loop gracefully (Stop hook respects this) |
| `stop-block-count` | Recovery tier counter (reset on loop entry, boot, and safety valve) |
| `handoff.yaml` | Cross-cycle state written at consolidation, read by `/boot` |

**Stopping the system:**

- **/stop**: Sets agent-state to IDLE + creates stop-loop → hook allows stop → loop exits
- **Recovery**: If loop loses context, stop hook escalates through 3 re-entry attempts,
  then `/recover` (status report + clean exit), then safety valve
- **Ctrl+C**: Interrupt also stops the loop
- **Resume**: User runs `/start`

### `evolve`

Trigger evolution check — the system evaluates its own strategy and generates new aspirations.
Full procedure in /aspirations-evolve.

invoke /aspirations-evolve with: fired_triggers (if called from Phase 9), aspiration state
# Sub-skill handles: developmental stage assessment (Step 0), config parameter tuning (Step 0.5),
# state reading (Step 1), evolve-first aspiration review (Step 2), constraint-aware rebalancing (Step 2.5),
# gap analysis (Step 3), novelty filter (Step 4), cap enforcement (Step 5), logging (Step 6),
# profile/meta update (Steps 7-8), forge check (Step 9), pattern signature calibration, strategy archive.

### `complete <goal-id> [--permanent]`

Mark a goal as completed:
1. If NOT recurring: Bash: `aspirations-update-goal.sh <goal-id> status completed`
   (Recurring goals NEVER set status to completed — they stay pending.)
   Bash: `aspirations-update-goal.sh <goal-id> completed_date <today>`
2. Bash: `aspirations-update-goal.sh <goal-id> achievedCount <N+1>`
   Update streaks: if recurring, check hours_since(lastAchievedAt) > 2 * interval_hours first.
   If overdue: new_streak = 1. Otherwise: new_streak = currentStreak + 1.
   ALWAYS update both: currentStreak = new_streak, longestStreak = max(new_streak, longestStreak).
   Bash: `aspirations-update-goal.sh <goal-id> lastAchievedAt "$(date +%Y-%m-%dT%H:%M:%S)"` (local system time)
3. Aspiration progress is updated automatically by update-goal when status changes
4. Unblock any goals listing this in their `blocked_by`
5. Verify actual state change (run `verification.checks` or legacy `completion_check` if defined)
6. If recurring AND NOT `--permanent`: status is NEVER set to `completed`. Only streaks,
   lastAchievedAt, achievedCount, and completed_date are updated. Status stays `pending`.
   The goal-selector time gate prevents re-selection until `interval_hours` elapses.
   If `--permanent`: Bash: `aspirations-update-goal.sh <goal-id> recurring false` — permanently stops the goal.
7. Run spark check
8. Update readiness gates if relevant

### `add <title>`

Add a new aspiration (scope-aware):
1. Run gap analysis first — Bash: `load-aspirations-compact.sh` → IF path returned: Read it (compact data for overlap check)
2. If overlap detected → suggest merging with existing instead of creating new
3. Generate next `asp-NNN` id
4. **Scope classification**: Apply Step 1.5 logic from `/create-aspiration`:
   - Quick tactical fix? → `sprint` (2-5 goals via /decompose)
   - Multi-session work? → `project` (default — invoke `/create-aspiration from-self --plan` with title context)
   - Cross-cutting strategic arc? → `initiative`
5. Create aspiration with title, `status: active`, `priority: MEDIUM`, `scope: {scope}`, `sessions_active: 0`
6. **Sprint scope**: Auto-generate 2-5 initial goals using `/decompose` logic
   **Project+ scope**: Invoke `/create-aspiration from-self --plan` with title as context —
   gets full research, lifecycle planning (Step 4a.5), and scope-appropriate goal counts (5-15)
7. Each goal gets: skill assignment, `verification` field (outcomes + checks + preconditions), achievement counters
8. Enforce aspiration cap (archive lowest if over limit)
9. Log via: `echo '{"date":"...","event":"aspiration_added","details":"...","scope":"{scope}"}' | bash core/scripts/evolution-log-append.sh`
10. Pipe aspiration JSON to: `echo '<aspiration-json>' | bash core/scripts/aspirations-add.sh`

---

## Phase 0: Completion Check Runners

Run BEFORE goal selection to auto-detect completed goals.
Checks `verification.checks` (new unified field) or `completion_check` (legacy). Both formats supported.

### File Existence Checks
```
For each goal with verification.checks or completion_check containing type "file_check"/"file_exists:":
    if goal.recurring: skip  # Recurring goals are never auto-completed
    path = extract path from check
    if file exists at path:
        mark goal completed
        log "Auto-completed {goal.id}: file {path} exists"
```

### Pipeline Count Checks
```
For each goal with verification.checks or completion_check referencing pipeline counts:
    if goal.recurring: skip  # Recurring goals are never auto-completed
    Bash: pipeline-read.sh --counts
    if count meets threshold:
        mark goal completed
        log "Auto-completed {goal.id}: pipeline count threshold met"
```

### Config State Checks
```
For each goal with completion_check referencing config fields:
    if goal.recurring: skip  # Recurring goals are never auto-completed
    read relevant config file
    if field matches expected value:
        mark goal completed
        log "Auto-completed {goal.id}: config state matches"
```

### Readiness Gate Checks
```
Check each readiness gate (from aspirations-read.sh --meta output):
    Gates are domain-specific and added dynamically as the agent learns.
    Example gates: knowledge_base_seeded, first_hypothesis_formed, first_outcome_tracked
Bash: aspirations-meta-update.sh readiness_gates '<JSON>'
```

### Recurring Goal Checks (Safety Net)
```
# Primary mechanism: Phase 5 resets recurring goals to pending immediately after completion.
# This check is a SAFETY NET for goals that got stuck at "completed" due to interrupted sessions.
For each goal with recurring: true AND status: completed:
    # Always reset — if status is still "completed", something went wrong (interrupted session)
    reset goal status to "pending"
    log "Recurring goal {goal.id} reset to pending (was stuck at completed)"
```

### Hypothesis Expiration Checks
```
For each goal with hypothesis_id AND (status: pending OR status: in-progress):
    if now > goal.resolves_by:
        mark goal status = "expired"
        move pipeline file to archived/ with reason: "expired_past_deadline"
        update archived/_index.yaml
        log "Hypothesis-goal {goal.id} expired (past resolves_by: {goal.resolves_by})"
```

---

## Goal Selection Algorithm

**Implemented by**: `core/scripts/goal-selector.py` (invoked via `bash core/scripts/goal-selector.sh`).
The script handles all arithmetic scoring, including exploration noise scaled by the
developmental stage's epsilon. This eliminates 200-400 tokens of LLM computation per
iteration and prevents scoring drift across sessions. The LLM reads the ranked JSON
output and applies Phase 2.5 metacognitive assessment, which may override the script's
ranking.

```
1. FILTER: active aspirations where:
   - status == "active"
   - days_since(last_worked) >= cooldown_days (or last_worked is null)

2. COLLECT: all unblocked goals where:
   - status == "pending"
   - all IDs in blocked_by have status "completed" or "decomposed"
   - if recurring: hours_since(lastAchievedAt) >= interval_hours (fallback: remind_days * 24, default: 24)
   - if hypothesis_id exists: now >= resolves_no_earlier_than (skip if time-gate not reached)
   - if deferred_until is set and now < deferred_until: skip (deferred time not reached)
   - if participants == [user]: skip (agent cannot execute user-only goals)
   - if verification.preconditions exist: all preconditions must be satisfied
   - Check prior attempts: Bash: experience-read.sh --category {goal.category}
     If prior goal_execution experiences exist for this category, note outcomes for metacognitive assessment

3. SCORE each goal (multi-criteria weighted scoring):

   priority_score:     HIGH=3, MEDIUM=2, LOW=1           (weight: 1.0)
   deadline_urgency:   +3 if deadline within 1 day        (weight: 1.0)
                       +2 if deadline within 3 days
                       +1 if deadline within 7 days
   agent_executable:   +2 if agent in participants         (weight: 0.8)
                       +0 if participants == [user]
   variety_bonus:      +1.5 if aspiration != last_session.aspiration_touched  (weight: 0.7)
   streak_momentum:    +0.5 if same aspiration had a goal completed this session  (weight: 0.5)
   novelty_bonus:      +1.0 if goal.achievedCount == 0 (never done before)  (weight: 0.6)
   recurring_urgency:  min(2.0 + (hours_overdue - interval_hours) / interval_hours, 5.0)  (weight: 0.8)
                       2.0 base = "goal is due" signal. Grows linearly with overdue ratio.
                       Cap at 5.0 (max weighted: 4.0) prevents starvation of domain work.
                       At due time: 2.0. At 1 interval late: 3.0. At 3+ intervals: 5.0.
                       Never-completed recurring goals (lastAchievedAt null) treated as due.
   reward_history:     +1.0 if previous goals in this aspiration had high success  (weight: 0.5)
   evidence_backing:   resolved hypothesis support score   (weight: 0.7)
                       For each resolved hypothesis relevant to this goal's aspiration:
                         earned_confirmed: +2.0, unlucky_corrected: +1.0
                         lucky_confirmed: +0.5, deserved_corrected: -1.0
                       Normalize by count. 0 if no relevant hypotheses.
   deferred_readiness: +1.5 if deferred_until is set and now >= deferred_until  (weight: 0.6)
                       Goal was specifically deferred to a future time — boost when due.
                       Not reset after execution (stays as historical metadata).
   context_coherence:  +2.0 if goal category == last_goal_category AND zone != "tight"  (weight: 1.0)
                       +1.0 if goal category == last_goal_category AND zone == "tight"
                       0 otherwise.
                       Reads context budget zone from mind/session/context-budget.json
                       (written by status line). Clusters same-category goals to amortize
                       retrieval context before compaction. last_goal_category tracked in
                       working memory, written by aspirations-state-update Step 3.
   exploration_noise:  random(0, 1)                         (weight: epsilon × noise_scale)
                       Scaled by developmental epsilon from mind/developmental-stage.yaml.
                       noise_scale from core/config/developmental-stage.yaml (default 3.0).
                       At exploring (~0.85 epsilon): max noise ~2.55 (can reorder rankings).
                       At mastering (~0.19 epsilon): max noise ~0.57 (tiebreaking only).

   TOTAL = sum(score × weight) for each static criterion + exploration_noise × epsilon × noise_scale

4. SELECT: highest total score
   Exploration noise provides effective randomized tiebreaking.
   Fallback tiebreak: lower aspiration number first, then lower goal number.
```

---

## Spark Check (Micro-Evolution)

Full spark check procedure — including all sq-XXX handlers, immediate learning,
and aspiration-level spark — is defined in /aspirations-spark.

---

## Stop Conditions

The loop ONLY stops for these reasons:
1. **Agent state changed** — agent-state file is no longer "RUNNING" (user ran /stop)
2. **Critical error** — unrecoverable file corruption, authentication permanently revoked

These are NOT stop conditions (the loop MUST continue through them):
- Context filling up → autocompact handles it, keep going
- No agent-executable goals → run gap analysis → generate new goals
- All aspirations complete → evolve → create new aspirations
- All goals blocked → check blockers → try to resolve or skip
- API rate limit → cooldown 60 seconds → retry
- "Is this a good stopping point?" → NO. Never ask. Keep going.
- Running out of ideas → think harder, reflect, replay, research
- Wanting to ask a question → write to pending-questions.yaml, execute default_action, continue
- Unsure whether to push/deploy → Phase 4.2 domain steps handle it. Just do it.
- "What should I focus on next?" → the loop selects the next goal. Never ask.

---

## Skip Conditions

| Condition | Action |
|---|---|
| API rate limit hit | Log, wait 60s, skip to next goal |
| Duplicate work detected | Mark goal completed, log reason |
| External dependency not met | Mark goal blocked, create prep task |
| Cooldown period active | Skip aspiration, try next one |
| Goal already in-progress | Resume or skip based on context |
| Completion check passes already | Auto-complete, no execution needed |

---

## State Update Protocol

Full state update procedure (Steps 1-8 + Step 8.5 Actionable Findings Gate) is defined in /aspirations-state-update.
Invoked in Phase 8 after every goal execution.

---

## Output Format

Read `core/config/status-output.md` for status line formats used during RUNNING state output.

When running `next` or `loop`, output progress as:

```
## Aspiration Loop — Session {N}

### Goal 1: {goal.id} — {goal.title}
Score: {total} (priority={p}, urgency={u}, variety={v}, novelty={n})
Skill: {skill} {args}
Result: {outcome}
Completion check: {PASS/FAIL}
Spark: {spark result or "no spark"}

### Goal 2: {goal.id} — {goal.title}
...

### Session Summary
Goals executed: {N}
Goals completed: {N}
Aspirations touched: {list}
Sparks fired: {N}
New goals created: {N}
Evolution triggered: {yes/no}
Next session focus: {recommendation}

### Session Accuracy
Hypotheses resolved this session: {N}
Confirmed: {N} ({pct}%)
All-time accuracy: {confirmed}/{total} ({pct}%)

### Micro-Hypothesis Accuracy (if any this session)
Micro-predictions: {confirmed}/{total} ({pct}%)
Promoted to encoding: {N} surprises
Self-model insights: {any overconfidence/underconfidence notes}
```

---

## Chaining Map

This skill chains to/from every other skill:

| Skill | How it's called | What feeds back |
|---|---|---|
| `/research-topic` | Execute research goals | Tree node updates, new questions |
| `/review-hypotheses` | Execute review goals | Accuracy data, resolved hypotheses |
| `/reflect` | After every goal (spark), after reviews | Patterns, strategies, violations |
| `/replay` | During session-end consolidation, via /reflect --full-cycle | Reconsolidation updates, domain transfers |
| `/decompose` | When compound goal detected | Sub-goals inserted into aspirations |
| `/boot` | At session start (optional) | Status overview, alerts |
| `/forge-skill` | During evolve (forge check), spark Q6 | Forged skills, gap status, tree health |
| `/tree maintain` | Session-end consolidation step 6 | Tree structural changes (DECOMPOSE/SPLIT/SPROUT/MERGE/PRUNE) |
| `/aspirations-execute` | Phase 4: goal execution | Result, experience_id, infrastructure_failure |
| `/aspirations-spark` | Phase 6: spark check + immediate learning | Evolution events, new goals, guardrails |
| `/aspirations-state-update` | Phase 8: post-goal state update | Tree encoding, journal, capability propagation |
| `/aspirations-consolidate` | Session-end consolidation, /stop (stop_mode) | Long-term memory encoding, handoff |
| `/aspirations-evolve` | Phase 9 / evolve sub-command | New aspirations, parameter tuning, stage assessment |
