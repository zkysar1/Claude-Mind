---
name: aspirations-execute
description: "Phase 4: Goal execution with intelligent retrieval, delegation, fail-fast cascade, experience archival, context utilization, domain post-execution steps, knowledge reconciliation, and batch execution"
user-invocable: false
parent-skill: aspirations
triggers:
  - "Phase 4"
conventions: [aspirations, pipeline, experience, tree-retrieval, retrieval-escalation, goal-schemas, infrastructure, reasoning-guardrails, agent-spawning]
minimum_mode: autonomous
---

# Phase 4: Goal Execution

Invoked as Phase 4 of the aspirations loop after goal selection (Phase 2) and decomposition (Phase 3). Covers the full execution pipeline: precondition checking, intelligent LLM-driven retrieval, memory deliberation, agent delegation, primary execution, fail-fast cascade, experience archival, context utilization feedback, domain post-execution steps, knowledge reconciliation, and batch execution.

## Inputs (from orchestrator)

- `goal`: Selected goal object from Phase 2
- `aspiration_id`: Parent aspiration ID
- `source`: Queue origin (`"world"` or `"agent"`) — pass `--source {source}` to all `aspirations-*.sh` calls
- `batch_mode`: Boolean (from Phase 2)
- `outcome_class`: Set by Phase 4-post after execution

---

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Execution Autonomy Rule

During goal execution, the agent makes ALL decisions autonomously.
Do NOT output text asking the user to choose between options.
Do NOT ask "should I push?" — Phase 4.2 domain steps handle push/deploy if configured.
Do NOT ask "what next?" — the loop handles goal selection.

**Decision logging for significant choices:**
When a goal requires a judgment call (e.g., "push now vs. modify CI first",
"use approach A vs. B"), the agent:
1. Makes the call — pick the safer/simpler option when unsure
2. Logs the decision:
   - Write to `<agent>/session/pending-questions.yaml`:
     ```yaml
     - id: pq-NNN
       date: "{today}"
       context: "{goal-id}: {goal title}"
       question: "I decided to {action} because {reasoning}. Override if you disagree."
       default_action: "Already executed: {what was done}"
       status: pending
     ```
   - OR for bigger decisions: note it in the experience trace
3. Continues immediately — never waits for confirmation

The user reviews decisions retroactively via `/respond` or session-end recap.

---

## Cognitive Primitives (Always Available)

During ANY phase — goal execution, error handling, reflection, spark checks —
the agent can create goals from things it notices. Three types:

### Unblocking Goals — "Something is stuck"
Created exclusively by the CREATE_BLOCKER protocol (see Phase 4.0/4.1e).
Never create these manually — always go through CREATE_BLOCKER.

### Investigation Goals — "Something seems off"
```
goal = {
    "title": "Investigate: {observation (50 chars)}",
    "status": "pending",
    "priority": "MEDIUM",
    "skill": null,
    "participants": ["agent"],
    "category": "{relevant category}",
    "description": "Observed during {goal.id}: {observation}\n\nContext: {what prompted this}",
    "verification": {
        "outcomes": ["Root cause understood and documented"],
        "checks": []
    },
    "blocked_by": []
}
```

### Idea Goals — "What if we tried...?"
```
goal = {
    "title": "Idea: {creative insight (50 chars)}",
    "status": "pending",
    "priority": "MEDIUM",
    "skill": null,
    "participants": ["agent"],
    "category": "{relevant category}",
    "description": "Idea from {goal.id}: {full description}\n\nExpected benefit: {why this matters}",
    "verification": {
        "outcomes": ["Idea evaluated — implemented, formed hypothesis, or retired"],
        "checks": []
    },
    "blocked_by": []
}
```

### Cross-Agent Insight Goals — "This changes something for the other agent"
When execution reveals something that invalidates, constrains, or enables another
agent's work, post an insight trigger to the findings board. This is the reactive
influence channel — discoveries during execution reshape the other agent's strategy.

```
# Post insight trigger finding (see board.md Insight Trigger Payload)
echo "Description of what was discovered and why it matters" | \
  bash core/scripts/board-post.sh --channel findings --type finding \
    --tags "insight_trigger,severity:<invalidates|constrains|enables|informs>,affects:<goal-id>,requires_action_by:<agent>,action_type:<re-scope|re-prioritize|investigate>,<category>"
```

**Severity guide:**
- `invalidates`: An assumption the other agent relies on is provably wrong
- `constrains`: The other agent's approach needs modification (but isn't wrong)
- `enables`: Something unblocked or became possible that the other agent should know about
- `informs`: Interesting finding — no immediate action needed

For all types: `echo '<JSON>' | bash core/scripts/aspirations-add-goal.sh --source {source} <aspiration_id>`
Place in the RIGHT aspiration (read active aspirations, pick best fit).
Dedup: check for existing goals with similar title before creating.

These are NOT mutually exclusive. A single event can spawn all four:
  Unblock: "fix the crash" + Investigate: "why does it crash?" + Idea: "add crash prevention" + Cross-Agent Insight: "this crash affects bravo's testing goals"

---

## Phase 4 Preamble: Cost-Ordered Precondition Checking

Before expensive data retrieval (SSH, large files, APIs), check local/cheap
preconditions first: timestamps, git log, file existence, metadata.
See: guard-009

## Phase 3.9: Pre-Execution Domain Steps

```
# Load domain convention into context if not yet loaded (dedup).
Bash: paths=$(bash core/scripts/load-conventions.sh pre-execution 2>/dev/null)
IF paths is non-empty:
    Read the file at the returned path

# Follow domain pre-execution steps if convention exists.
# CRITICAL: Gate on file existence, NOT on load status. The convention is procedural —
# it must run every goal, not just the first time it's loaded into context.
Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/pre-execution.md" && echo "exists"
IF exists:
    Follow each Step in the convention, evaluating conditions against current goal context
    IF any pre-execution step returns SKIP:
        Skip this goal (mark as skipped with reason from pre-execution check)
        GOTO Phase 7 (next iteration)
ELSE:
    # No domain pre-execution convention exists. Nothing to do.
```

## Phase 4: Execute (with intelligent retrieval)

```
Bash: aspirations-update-goal.sh --source {source} <goal-id> status in-progress
Bash: aspirations-update-goal.sh --source {source} <goal-id> started <today>

# ── Intelligent Retrieval Protocol ──────────────────────────────────
#
# The LLM drives retrieval, not a hardcoded depth parameter.
# Step 1: Understand the knowledge landscape
# Step 2: Reason about what this goal needs
# Step 3: Read relevant tree nodes
# Step 4: Load supplementary stores
# Step 5: Evaluate and supplement

Output: "▸ Intelligent retrieval: scanning knowledge tree..."

# Step 1: Read the tree index (compact summary, convention-style cached)
Bash: load-tree-summary.sh
IF output is non-empty (path returned):
    Read the returned path  # hooks auto-track; gates future re-reads
# If empty: tree summary already in context (Phase 2.25 or prior iteration) — use cached version.
# Returns compact overview: node keys, file paths, summaries, depth, capability_level,
# confidence, children keys. Replaces reading the full _tree.yaml.
# For entity_index lookups, use tree-find-node.sh --text <query> instead.

# Step 2: Reason about what this goal needs
# Given the goal description, skill, category, and verification outcomes:
# - Which node keys/summaries from the tree summary are directly relevant?
# - Use tree-find-node.sh --text <query> to resolve concept → node mappings
# - Are there sibling/parent nodes that provide useful context?
# - What categories should the experience search target?
#
# Identify: primary_nodes (must read), secondary_nodes (might need),
#           experience_categories (for supplementary search)

# Step 3: Read relevant tree node .md files
FOR EACH node_key in primary_nodes:
    Read {node.file}   # from _tree.yaml's file field for that node
    Bash: tree-update.sh --increment {node_key} retrieval_count

IF primary_nodes is non-empty:
    Output: "▸ Tree nodes: {list primary_node keys} ({N} nodes loaded)"
ELSE:
    Output: "▸ No relevant tree nodes found — supplementary stores only."

# Step 4: Load supplementary stores (mechanical — script handles this)
# --goal and --tree-nodes flags auto-write retrieval-session.json for utilization tracking.
# Pass the tree node keys loaded in Step 3 so they're recorded in the session file.
Bash: retrieve.sh --supplementary-only --category {goal.category} --goal {goal.id} --tree-nodes "{comma-separated primary_node keys from Step 3}"
# Returns: reasoning_bank, guardrails, pattern_signatures, experiences,
#          beliefs, experiential_index
# Tree nodes are empty (skipped) — we already loaded them in Step 3.
# Side effect: writes <agent>/session/retrieval-session.json (utilization tracking)

Output: "▸ Supplementary: {N} reasoning, {N} guardrails, {N} patterns, {N} experiences"

# Memory Deliberation: scan supplementary items for relevance
FOR EACH item in reasoning_bank + guardrails + pattern_signatures:
    Assess relevance to current goal context
    Note: ACTIVE (will inform execution) or SKIPPED (loaded but not applicable)

# Step 5: Evaluate sufficiency (with escalation per retrieval-escalation convention)
# Review what was loaded. For secondary_nodes identified in Step 2:
IF context feels insufficient for the goal's complexity:
    Read additional secondary_nodes .md files
    Bash: tree-update.sh --increment {node_key} retrieval_count
    Output: "▸ Follow-up: loaded {N} additional nodes for context"
# Follow entity cross-references spotted in .md front matter if needed.

# Step 5a.1: Codebase escalation (Tier 2 — per retrieval-escalation convention)
# After tree retrieval, if context still feels insufficient AND goal relates to
# codebase work (implementation details, code structure, configuration, debugging):
IF context still insufficient AND goal involves codebase knowledge:
    # Read workspace path from <agent>/self.md if not already known
    # Use targeted Grep/Glob/Read on the primary workspace (2-3 specific queries)
    # Look for: function implementations, config values, error patterns, file structure
    Output: "▸ Tier 2 codebase exploration: {summary of what was searched and found}"

# Step 5a.2: Web search escalation (Tier 3 — per retrieval-escalation convention)
# If context still insufficient AND topic involves external knowledge (APIs, services,
# technologies not in tree/codebase):
IF context still insufficient AND topic involves external knowledge:
    # WebSearch: targeted query for the specific gap
    # WebFetch: authoritative sources if identified
    Output: "▸ Tier 3 web search: {summary of what was found}"

# Step 5b: Retrieval manifest (AUTO-GENERATED by retrieve.sh --goal)
# retrieve.sh auto-writes <agent>/session/retrieval-session.json with:
#   goal_id, timestamp, categories, tree_nodes_loaded, supplementary_items,
#   counts, utilization_pending: true
# This happens automatically in Step 4 — no manual JSON construction needed.
#
# Optional enrichment: pipe deliberation details to working memory for richer tracking:
# echo '<manifest_json>' | Bash: wm-set.sh active_context.retrieval_manifest
# Enrichment fields: deliberation.active_items, deliberation.skipped_items,
#   tiers_used, escalation_reasons, sufficient
#
# BACKSTOP: utilization-gate.sh hook guarantees feedback runs before state-update
# even if Phase 4.26 is skipped entirely. The system NEVER has zero utilization data.
Output: "▸ Retrieval session: {N} nodes, {M} supplementary items recorded"

# Step 5c: Articulate retrieval influence (MANDATORY)
# Forces the agent to connect knowledge to action. "Loaded but ignored" becomes
# impossible — the agent must say HOW knowledge changes its approach.
IF active_items is non-empty:
    Output: "▸ Retrieval influence: {1-2 sentences on how active items inform execution}"
    # Example: "guard-NNN requires post-execution check; rb-NNN suggests retry with backoff"
ELSE:
    Output: "▸ Retrieval influence: none — executing without retrieved context"

# ── End Intelligent Retrieval ───────────────────────────────────────

# Team-Based Research Delegation
#
# The host MAY dispatch team agents to gather information via TeamCreate.
# Never use bare sub-agents (Agent tool without team_name) — they start
# with zero context. Use build-agent-context.sh to inject primed context
# directly into the agent prompt. The host builds context BEFORE spawning;
# the sub-agent receives it as data, not as an instruction to execute.
# See core/config/conventions/agent-spawning.md for the full protocol.
#
# CRITICAL: Team agents do READ-ONLY research. They MUST NOT:
#   - Invoke skills (skills write to shared state)
#   - Write or edit ANY files (world/, agent dirs, external repos, anything)
#   - Call state-mutating scripts (pipeline-move.sh, experience-add.sh, etc.)
#   - Make git commits or pushes
#
# Team agents report structured findings via message. The host then
# executes goals with enriched context (research already done = faster).
#
# This is a tool, not a rule. The host is free to skip delegation entirely.

# Curriculum gate: check if parallel execution is permitted before dispatching
Bash: `curriculum-contract-check.sh --action allow_multi_goal_parallelism`
IF exit code 1:
    Log: "Parallel dispatch blocked by curriculum (stage: {stage_name from JSON})"
    SKIP team delegation — execute research synchronously instead

IF prefetch_goals:
    TeamCreate(team_name="research-{session}", description="Pre-fetch research for goals")
    FOR pg in prefetch_goals:
        research_task = extract_research_question(pg)  # What info does this goal need?
        # Build primed context for spawned agent (read-only, category-targeted)
        Bash: agent_context=$(build-agent-context.sh --category "{pg.category}" --role researcher)
        # Register agent BEFORE dispatch (crash-safe: staleness timeout cleans up if dispatch fails)
        Bash: `pending-agents.sh register --id "researcher-{pg.goal_id}" --team "research-{session}" --goal "{pg.goal_id}" --purpose "{research_task[:100]}" --timeout 10`
        Agent(team_name="research-{session}", name="researcher-{pg.goal_id}",
              prompt="{agent_context}

                      YOUR TASK (READ-ONLY research):
                      {research_task}

                      TIME LIMIT: You have a maximum of 10 minutes. Wrap up and
                      report your findings before then — do not start new work
                      after 8 minutes of elapsed time.

                      CONSTRAINTS:
                      - Do NOT write or edit ANY files
                      - Do NOT invoke skills or call state-mutating scripts
                      - Report your findings as a structured summary",
              run_in_background=true)

# Misroute guard: catch skill-creation goals that arrived with skill: null
IF goal.skill is null AND goal.title matches pattern (forge|create.*skill|make.*skill|skill.*creation):
    Log: "MISROUTE GUARD: {goal.id} '{goal.title}' describes skill creation but has skill: null — re-routing to /forge-skill list"
    goal.skill = "/forge-skill"
    goal.args = "list"

# Execute primary goal inline (host does ALL writing)
result = invoke goal.skill with goal.args
```

## Phase 4-post: Outcome Classification

After execution, classify the outcome for post-execution pipeline gating.
This does NOT affect execution itself — the skill always runs fully.
It controls whether the expensive cognitive phases (experience archival,
spark checks, tree encoding) fire afterward.

```
outcome_class = "deep"  # default: full pipeline with immediate tree encoding

# ── ROUTINE demotion (only recurring goals with zero findings) ──
IF goal.recurring AND goal_succeeded (no errors, no timeouts):
    # Assess the ACTUAL execution result:
    #   - Did the skill find items to process? (emails, alerts, stale data, issues)
    #   - Did the skill take any action beyond the check itself?
    #   - Did any interaction reveal new information?
    # If the answer to ALL is "no" → routine check with no findings.
    IF result produced no actionable items and no new information:
        outcome_class = "routine"

IF outcome_class == "routine":
    Log: "▸ Outcome: ROUTINE — recurring, no findings"
IF outcome_class == "deep":
    Log: "▸ Outcome: DEEP — full pipeline with immediate tree encoding"

# Binary classification: routine (recurring + no findings) or deep (everything else).
# SAFETY: Non-recurring goals ALWAYS remain "deep" (inherently novel)
# SAFETY: Failed goals ALWAYS remain "deep" (failures are learning events)
# SAFETY: Recurring goals WITH findings remain "deep" (learning is the mission)
# SAFETY: If uncertain, remain "deep" — bias toward full treatment
```

## Phase 4-chain: Episode Chain Protocol (MR-Search)

After Phase 4-post outcome classification, before proceeding to Phase 4.0/4.1,
check if this goal should be retried with accumulated reflection context.
Inspired by MR-Search (arXiv 2603.11327): chaining N attempts with structured
self-reflection between each episode enables the agent to learn *through*
failure within the same problem context.

```
# Read episode chaining config
Read core/config/aspirations.yaml → episode_chaining section

# Determine if chaining should activate
# GUARD: Never chain infrastructure failures — Phase 4.0 owns the blocker protocol.
chain_trigger = false
IF result is INFRASTRUCTURE_UNAVAILABLE or RESOURCE_BLOCKED:
    chain_trigger = false  # Let Phase 4.0 handle infrastructure failures
ELIF outcome_class == "deep" AND NOT goal_succeeded:
    IF "failed" in episode_chaining.chain_on_outcomes:
        chain_trigger = true

IF chain_trigger:
    # Check context budget zone for max episodes
    Read <agent>/session/working-memory.yaml → context_zone (fresh|normal|tight)
    max_episodes = episode_chaining.context_zone_override[context_zone]
        # Default: episode_chaining.max_episodes_per_goal

    # Check current episode count
    Bash: wm-read.sh episode_chain --json
    IF episode_chain exists AND episode_chain.goal_id == goal.id:
        current_episode = episode_chain.current_episode
    ELSE:
        current_episode = 0

    IF current_episode < max_episodes:
        # ── Archive this attempt as an episode ────────────────────────
        episode_entry = {
            episode: current_episode + 1,
            approach: "1-2 sentence summary of approach taken",
            outcome: result.outcome_summary or "failed",
            key_observations: [
                "Key observation 1 from execution",
                "Key observation 2 (what was unexpected)"
            ],
            reflection: null,   # Populated below
            timestamp: "$(date +%Y-%m-%dT%H:%M:%S)"
        }

        # ── Structured mini-reflection between episodes ───────────────
        # MR-Search's core insight: explicit reflection between attempts
        # enables cross-episode exploration improvement.
        # The agent asks itself four questions:
        #   1. What went wrong or was unexpected?
        #   2. What assumptions were violated?
        #   3. What should I try differently next time?
        #   4. For violated assumptions: are there deeper inherited assumptions in
        #      my framing of this goal? Strip to ground truth and rebuild approach.
        episode_entry.reflection = generate_mini_reflection(
            goal, result, episode_entry.key_observations
        )

        # ── Update episode chain in working memory ────────────────────
        IF episode_chain exists:
            Append episode_entry to episode_chain.episodes
            episode_chain.current_episode = current_episode + 1
        ELSE:
            episode_chain = {
                goal_id: goal.id,
                max_episodes: max_episodes,
                current_episode: 1,
                episodes: [episode_entry]
            }
        echo '<episode_chain_json>' | Bash: wm-set.sh episode_chain

        # ── Update goal with episode history ──────────────────────────
        Bash: aspirations-update-goal.sh --source {source} <goal-id> episode_history '<episodes_array_json>'

        # ── Re-execute with accumulated context ───────────────────────
        # The execution preamble for the next attempt includes ALL prior
        # episodes + reflections. This is MR-Search's context accumulation:
        #   goal → episode₀ → reflection₀ → episode₁ → reflection₁ → ...
        Output: "▸ EPISODE CHAIN: Attempt {current_episode + 1}/{max_episodes} — retrying with reflection context"
        Log: "Episode {current_episode + 1}: Reflection: {episode_entry.reflection}"

        # Reset goal status for re-execution
        Bash: aspirations-update-goal.sh --source {source} <goal-id> status in-progress

        # Re-invoke Phase 4 execution with episode chain as context preamble
        → re-execute goal.skill with episode_chain.episodes as execution context
        → return to Phase 4-post with new result (episode chain protocol re-evaluates)

    ELSE:
        # Max episodes reached — proceed normally with the final outcome
        Output: "▸ EPISODE CHAIN: Max attempts ({max_episodes}) reached — accepting final outcome"
        # Clear episode chain from working memory
        echo 'null' | Bash: wm-set.sh episode_chain

ELSE:
    # No chaining needed — clear any stale episode chain
    Bash: wm-read.sh episode_chain --json
    IF episode_chain exists AND episode_chain.goal_id == goal.id:
        echo 'null' | Bash: wm-set.sh episode_chain
```

## Phase 4.0: Structured SKIP Fast-Path (with Recovery Attempt)

Skills that SKIP at preflight return INFRASTRUCTURE_UNAVAILABLE or RESOURCE_BLOCKED.
Before blocking, attempt ONE recovery via infra-health.sh (which has auto-recovery
for registered components). Only block if recovery fails.

```
IF result is INFRASTRUCTURE_UNAVAILABLE or RESOURCE_BLOCKED:
    # RECOVERY ATTEMPT — runs once. Do not remove this guard.
    IF NOT retry_attempted:
        component = map goal.skill to infra component via <agent>/infra-health.yaml skill_mapping
        IF component:
            Bash: probe=$(bash core/scripts/infra-health.sh check {component} 2>/dev/null)
            Parse probe JSON → status
            IF status == "ok":
                Log: "RECOVERY SUCCESS: {component} available after probe — retrying goal"
                retry_attempted = true
                → re-execute the skill (invoke goal's skill again, NOT Phase 4.0)
                → return to Phase 4.0 with the new result
            ELIF status == "provisionable":
                # Component is down but can be provisioned via a skill
                provision_skill = probe.provision_skill
                Log: "PROVISIONING: {component} not running — invoking {provision_skill}"
                provision_result = invoke {provision_skill}
                IF provision_result succeeded (no INFRASTRUCTURE_UNAVAILABLE/RESOURCE_BLOCKED):
                    Log: "PROVISIONING SUCCESS: {component} started"
                    IF provision_skill == goal.skill:
                        # Provisioning IS the goal execution — do not re-execute.
                        # Re-executing would run the same skill twice (e.g., start two game sessions).
                        result = provision_result
                        → return to Phase 5 (verify completion) with this result
                    ELSE:
                        retry_attempted = true
                        → re-execute the ORIGINAL skill (not the provisioning skill)
                        → return to Phase 4.0 with the new result
                ELSE:
                    Log: "PROVISIONING FAILED: {provision_skill} could not start {component}"
                    # Fall through to CREATE_BLOCKER with provisioning failure in diagnostic
    # Recovery already tried, failed, no component mapping, or probe failed.
    # NEGATIVE CONCLUSION GATE (core/config/conventions/negative-conclusions.md):
    # Before creating a blocker, verify the failure meets the multi-signal requirement.
    evidence_signals = []
    # Did the original failure come from a silent-failure command (curl -sf, 2>/dev/null)?
    # If so, it's ZERO signals. If it produced a real error message, it's 1 signal.
    IF original failure produced a visible error (non-empty stderr, HTTP status code, connection refused):
        evidence_signals.append({signal: "original failure", weight: 1})
    # The recovery probe above (infra-health.sh) is an independent signal if it ran
    IF probe was attempted AND probe returned a real failure (not "no_probe"):
        evidence_signals.append({signal: "infra-health probe", weight: 1})
    # Phase 4.0 always creates a blocker → convention requires 2+ signals.
    required_signals = 2
    IF len(evidence_signals) < required_signals:
        # Insufficient evidence — try one more independent check before blocking
        # (e.g., check process list, try alternative endpoint, check service status)
        Log: "NEGATIVE CONCLUSION: {len(evidence_signals)}/{required_signals} signals — trying alternative"
        # Run one alternative verification attempt (different tool/endpoint than original)
        IF alternative check succeeds (infrastructure IS available) AND NOT retry_attempted:
            Log: "NEGATIVE CONCLUSION OVERRIDDEN by alternative check"
            retry_attempted = true
            → re-execute the skill
            → return to Phase 4.0 with the new result
        ELIF alternative check succeeds BUT retry_attempted:
            # Already retried once — infrastructure appears up but skill still fails.
            # This is a skill-level issue, not infrastructure. Do not block or retry.
            Log: "CONTRADICTION: infra appears available but skill fails after retry — proceeding without blocker"
            Bash: aspirations-update-goal.sh --source {source} <goal-id> status pending
            continue  # Skip blocker, retry next iteration
        ELSE:
            evidence_signals.append({signal: "alternative check", weight: 1})
    # Sufficient evidence — proceed to blocker creation
    → invoke CREATE_BLOCKER protocol (below)
    Bash: aspirations-update-goal.sh --source {source} <goal-id> status pending
    continue  # Next goal
```

## CREATE_BLOCKER Protocol

Single source of truth for blocker creation. Invoked by Phase 4.0 (fast-path SKIP),
Phase 4.1e (unfixable infrastructure failure), and Phase 0.5a (pre-selection sweep).

```
CREATE_BLOCKER(failure_skill, failure_reason, goal, aspiration_id, diagnostic_context):

  1. Bash: wm-read.sh known_blockers --json
  2. Check for existing blocker with same skill + null resolution
     IF existing:
       Append goal.id to existing.affected_goals
       Update diagnostic_context with new info
       (unblocking goal already exists — no new goal)
     ELSE:
       3. Create unblocking goal (born with the blocker)
          participants = [user] if agent cannot fix, else [agent]
          title = "Unblock: {failure_reason (50 chars)}"
          priority = HIGH, skill = null
          description includes: reason, diagnostic context, affected goals, what was tried
          → echo '<goal-json>' | bash core/scripts/aspirations-add-goal.sh --source {source} <aspiration_id>
          → capture new goal ID

       4. Create blocker entry
          blocker_id = "infra-{skill-slug}-{date}"
          type = infrastructure | resource | user_action
          unblocking_goal = <new goal ID>
          diagnostic_context = { error_alerts, cascade_chain, attempted_fix }
          resolution = null

  5. Cascade-block same-skill goals in queue
     Append to affected_goals
  6. Alert the user about the error:
     Message: what's blocked, why, unblocking goal ID, cascade count
     If unable to alert, create participants: [user] goal. Continue.
  7. echo '<updated_blockers_json>' | Bash: wm-set.sh known_blockers
  8. Write journal entry about blocker creation + cascade chain
```

## Phase 4.1: Post-Execution Guardrail Consultation + Error Response

After goal execution, consult learned guardrails and reasoning bank for
checks relevant to this goal's outcome. This is how the agent applies
lessons from experience — the specific checks are learned behaviors stored
in world/ (guardrails, reasoning bank), not hardcoded here.

For infrastructure goals, this enables learned behaviors to fire even when
the goal appeared to succeed. A "successful" goal can mask real infrastructure
errors that only guardrails know to check for.

Phase 4.1 does NOT fire guardrail checks on local/tooling errors: script
validation rejections, file not found in world/ or agent dir, build/compile errors during
code editing, or git failures.

```
goal_succeeded = (result achieved verification.outcomes AND no errors/timeouts)
involved_infrastructure = (goal.skill (without leading /) in <agent>/infra-health.yaml skill_mapping OR goal.category in <agent>/infra-health.yaml category_mapping)

# Step 4.1-pre: Guardrail consultation (ALL infrastructure goals)
# After ANY infrastructure interaction, run guardrail-check.sh to get a
# concrete list of guardrails that MUST fire. No manual matching needed —
# the script returns exactly which guardrails apply and what command to run.
guardrail_found_issues = false
IF involved_infrastructure:
    outcome_flag = "succeeded" if goal_succeeded else "failed"
    Bash: matched=$(bash core/scripts/guardrail-check.sh --context infrastructure --outcome $outcome_flag --phase post-execution 2>/dev/null)

    # matched.matched_count > 0 means guardrails REQUIRE execution.
    # For EACH matched guardrail: run the action_hint command.
    IF matched.matched_count > 0:
        FOR EACH guardrail in matched.matched:
            # action_hint contains a runnable command (from the learned guardrail)
            Bash: <run {guardrail.action_hint}>
            IF output reveals issues (non-empty error alerts, health check failures):
                guardrail_found_issues = true
                guardrail_issue_description = what was found

# Step 4.1-testing: Testing guardrail consultation
# Parallel to infrastructure consultation — fires for testing-related goals.
# This enables guard-028 (integration path verification) and similar testing
# guardrails to reach the agent during goal execution.
involved_testing = (goal.category contains "test" OR goal.title contains "test" or "verify")
IF involved_testing:
    outcome_flag = "succeeded" if goal_succeeded else "failed"
    Bash: testing_matched=$(bash core/scripts/guardrail-check.sh --context testing --outcome $outcome_flag --phase post-execution 2>/dev/null)

    IF testing_matched.matched_count > 0:
        FOR EACH guardrail in testing_matched.matched:
            IF action_hint: Bash: <run {guardrail.action_hint}>
            Evaluate guardrail rule against execution outcome
            IF rule requirements not met:
                guardrail_found_issues = true
                guardrail_issue_description = what the testing guardrail flagged

# Step 4.1a-e: Error Response Protocol (Blocker-Centric)
# Fires when: guardrail check revealed issues, OR goal failed + infrastructure.
# Does NOT fire on local/tooling errors.
# New model: diagnose → try fix → if can't fix → CREATE_BLOCKER
IF guardrail_found_issues OR (NOT goal_succeeded AND involved_infrastructure):
    failure_skill = goal.skill
    failure_reason = guardrail_issue_description if guardrail_found_issues
                     else result.error_summary or result.skip_reason or "Goal did not succeed"

    # Step 4.1a: SEEK ERROR ALERTS — widen window to catch full cascade
    # Error alerts reveal root causes the agent cannot see.
    # If guardrails already confirmed alerts exist, skip the wait.
    IF NOT guardrail_found_issues:
        sleep 45  # Wait for async error alert delivery (failure path only)
    # Read error check config from infra-health.yaml; skip if none configured
    error_check = mind-read.sh infra-health.yaml --field error_check
    IF error_check is not null:
        Bash: error_alerts=$(bash {error_check.script} {error_check.check_args} 2>/dev/null)
    ELSE:
        error_alerts = []  # No error check mechanism configured for this domain

    # Step 4.1b: CASCADE DETECTION — sort by time, identify root cause
    cascade_report = null
    IF error_alerts is non-empty array:
        Sort alerts by timestamp ascending (oldest first)

        # Read EVERY alert body — each may contain different diagnostic clues
        cascade_chain = []
        FOR EACH alert in sorted_alerts:
            Bash: alert_body=$(bash {error_check.script} {error_check.read_args} <alert.key> 2>/dev/null)
            Extract: identifier, error_type from body
            cascade_chain.append({
                timestamp: alert.timestamp,
                subject: alert.subject,
                error_type: extracted error type,
                identifier: extracted identifier,
                is_root_cause: (first alert in chain),
                body_summary: key diagnostic lines from body
            })

        # Cascade pattern: multiple alerts within 5 min = cascade chain
        # Earliest alert = root cause, later alerts = downstream effects
        # Agent's observed symptom is likely the LAST link
        cascade_report = {
            root_cause: cascade_chain[0],
            cascade_effects: cascade_chain[1:],
            agent_observed_symptom: failure_reason,
            chain_summary: "Root: {root.error_type} -> {effect1} -> ... -> Agent saw: {symptom}"
        }

    # Step 4.1c: DETERMINE SEVERITY
    IF error_alerts found (non-empty):
        severity = "confirmed_infrastructure"  # Error alerts confirm real problem
    ELIF result indicates infrastructure_failure OR skill returned skip_reason:
        severity = "explicit_failure"  # Structured markers present
    ELSE:
        severity = "soft_failure"  # No emails, no markers — may be logic error
        Log: "Goal {goal.id} did not succeed. No error alerts found. Continuing."

    IF severity in ("confirmed_infrastructure", "explicit_failure"):

        # Step 4.1d: TRY FIX INLINE (moved before blocker creation)
        # The agent is a manager — try to solve it before escalating.
        fixed_inline = false
        Search knowledge tree, reasoning bank, experience archive for solutions
        IF solution found:
            Attempt one fix (one attempt, no retry loops)
            IF fix succeeded:
                fixed_inline = true
                Log: "Resolved inline: {what was done}"
                # OPTIONAL: Create investigation/idea goals for what was noticed
                IF root cause warrants deeper investigation:
                    Create investigation goal (MEDIUM priority, via Cognitive Primitives above)
                IF prevention opportunity spotted:
                    Create idea goal (MEDIUM priority, via Cognitive Primitives above)

        # Step 4.1e: COULDN'T FIX → CREATE BLOCKER
        # Only fires when inline fix was not attempted or failed.
        # A successful inline fix skips this entirely — no blocker needed.
        IF NOT fixed_inline:
            diagnostic_context = {
                error_alerts: count of error_alerts,
                cascade_chain: cascade_report,
                attempted_fix: what was tried or "No solution found"
            }
            → invoke CREATE_BLOCKER protocol (defined above):
                CREATE_BLOCKER(failure_skill, failure_reason, goal, aspiration_id, diagnostic_context)

            # OPTIONAL: Create investigation/idea goals alongside the unblocking goal
            # Unblock: "fix the configuration error" (mandatory, from CREATE_BLOCKER)
            # Investigate: "why does this configuration break under these conditions?" (optional)
            # Idea: "add startup validation for this configuration" (optional)

        # CRITICAL: Two exit paths depending on goal outcome.
        # Do NOT merge these — they have different semantics.
        IF NOT goal_succeeded:
            # Goal did not succeed. Revert to pending (will retry after fix or unblock).
            Bash: aspirations-update-goal.sh --source {source} <goal-id> status pending
            continue  # Skip Phases 4.25-9 for this goal
        # ELSE: goal succeeded but guardrail found issues. Blocker or fix handled above.
        # Fall through to Phase 4.25+ so the ORIGINAL goal gets normal completion.

    # severity == "soft_failure" or guardrail-success path: fall through to Phase 4.25+

# SAFETY: Guardrail findings override routine classification.
# Phase 4-post classified before guardrails ran — if guardrails found
# real issues, this IS new information regardless of skill result.
IF guardrail_found_issues:
    outcome_class = "deep"  # guardrail issues → override to deep
```

## Phase 4.2: Post-Execution Domain Steps

```
# Load domain convention into context if not yet loaded (dedup).
Bash: paths=$(bash core/scripts/load-conventions.sh post-execution 2>/dev/null)
IF paths is non-empty:
    Read the file at the returned path

# Follow domain post-execution steps if convention exists.
# CRITICAL: Gate on file existence, NOT on load status. The convention is procedural —
# it must run every goal, not just the first time it's loaded into context.
Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/post-execution.md" && echo "exists"
IF exists:
    Follow each Step in the convention, evaluating conditions against current goal context
    Collect results (external_changes, behavioral_observations)
    Pass collected results to Phase 4.5 for knowledge reconciliation
ELSE:
    # No domain post-execution convention exists (fresh agent). Nothing to do.
    external_changes = null
    behavioral_observations = null
```

## Phase 4.25: Archive Goal Execution Trace

Store full-fidelity interaction trace for this goal execution.
This preserves evidence that tree node encoding compresses away.

SKIP: outcome_class == "routine" — nothing meaningful to archive.

```
IF outcome_class != "routine":
    experience_id = "exp-{goal.id}-{goal.skill_name_slug}"
    Write <agent>/experience/{experience_id}.md with:
        - Full reasoning trace from goal execution
        - Tool outputs and decisions made
        - Outcome and verification results
        - Any surprising findings or unexpected behavior
    echo '<experience-json>' | bash core/scripts/experience-add.sh
    Experience JSON:
        id: "{experience_id}"
        type: "goal_execution"
        created: "{ISO timestamp}"
        category: "{goal.category}"
        summary: "One-line summary of goal execution and outcome"
        goal_id: "{goal.id}"
        tree_nodes_related: [from retrieval_manifest.tree_nodes_loaded, or [] if no manifest]
        retrieval_audit:
            manifest_present: true/false  # Was retrieval manifest written in Step 5b?
            nodes_count: N               # Number of tree nodes loaded
            active_count: N              # Items marked ACTIVE in Memory Deliberation
            skipped_count: N             # Items marked SKIPPED
            utilization_fired: true/false # Did Phase 4.26 complete?
            influence: "{the Step 5c retrieval influence line}"
        verbatim_anchors:   # MANDATORY: capture ALL precise technical values discovered.
            # Ground truth the encoding pipeline draws from.
            # Each anchor: {key: "descriptive-name", content: "EXACT verbatim text"}
            # Always capture: error messages with codes, discovered limits/timeouts/quotas
            # with exact numbers, configuration values, equations/formulas/algorithms,
            # file paths + line numbers + commit hashes, latencies/sizes/counts/percentages,
            # exact API responses/status codes. Store the raw value, not a summary.
        content_path: "<agent>/experience/{experience_id}.md"
        # MR-Search temporal credit fields (Priority 4)
        enabled_by: []             # Populated below by causal enabler scan
        temporal_credit: 0.0       # Accumulated by Step 8.9 from downstream successes
    echo '{"experience_refs": ["{experience_id}"]}' | Bash: wm-set.sh active_context.experience_refs
```

## Phase 4.26: Context Utilization Feedback

After goal execution, update utilization counters on retrieved items.
This closes the feedback loop so the system learns which knowledge is helpful.

**PRIMARY PATH** (script-based — one command replaces the manual loop):
```
# Identify which items actually informed execution (structural helpfulness):
# - Tree node keys referenced in execution commands, decisions, or output
# - Guardrail/reasoning bank IDs matched in Phase 4.1 or cited in execution
helpful_items = [items that met structural helpfulness criteria]

IF outcome_class != "routine":
    Bash: utilization-feedback.sh --goal {goal.id} --helpful "{comma-separated helpful IDs}"
    # Reads <agent>/session/retrieval-session.json (auto-written by retrieve.sh in Step 4)
    # Increments times_helpful on named items, times_noise on everything else
    # Auto-recomputes utility_ratio on all affected tree nodes
    # Clears utilization_pending flag
    Output: "▸ Utilization feedback: {result from script}"
ELSE:
    # Routine outcome — clear the pending flag without feedback
    Bash: utilization-feedback.sh --goal {goal.id} --all-noise
```

**BACKSTOP**: The `utilization-gate.sh` PreToolUse hook auto-applies `--all-noise`
before `aspirations-state-update` if this phase is skipped entirely. The system
NEVER has zero utilization data, even if the LLM skips Phase 4.26.

**MR-Search reflection quality tracking** (Priority 2): If any helpful item has a
`source_reflection_id` field, record positive downstream signal in
`meta/reflection-strategy.yaml → reflection_quality_log`.

## Phase 4.27: Causal Enabler Scan (MR-Search Temporal Credit)

Runs after Phase 4.26 so helpfulness data is available.
When execution succeeds and retrieved items were structurally helpful,
identify prior experiences that causally enabled this success.

```
IF outcome_class != "routine" AND goal_succeeded:
    # Apply the same structural helpfulness criteria used by Phase 4.26
    FOR EACH item in retrieval_manifest.deliberation.active_items:
        IF item met structural helpfulness criteria (same test as Phase 4.26: referenced in influence text, matched guardrails, or cited in execution):
            # Find the experience that originally produced this item
            item_source_goal = item.source_goal or item.source  # field name varies by store
            IF item_source_goal:
                goals_between = count goals completed between item_source_goal and current goal
                enabler = {
                    experience_id: "exp-{item_source_goal}",
                    relationship: "provided_foundation",
                    temporal_distance: goals_between
                }
                Bash: experience-update-field.sh {experience_id} enabled_by '<append enabler>'
    IF any enablers recorded:
        Output: "▸ Causal enablers: {N} prior experiences credited"
```

## Phase 4.28: Skill Co-Invocation Logging

Log which skills were invoked together during this goal execution.
Feeds the skill relation graph discovery pipeline (used by consolidation Step 8).

```
IF goal.skill is set:
    # Collect all skills involved in this execution
    # Primary skill + any skills invoked during execution (decompose, tree, reflect, etc.)
    invoked_skills = [goal.skill stripped of "/" and params]
    IF decompose was invoked: append "decompose" to invoked_skills
    IF tree operations were used: append "tree" to invoked_skills
    IF research-topic was invoked: append "research-topic" to invoked_skills
    # Only log if 2+ skills were involved (single skill = no co-invocation)
    IF len(invoked_skills) >= 2:
        Bash: skill-relations.sh co-invoke --goal {goal.id} --skills {comma_separated_skills}
```

## Phase 4.5: Knowledge Reconciliation Check

After executing a goal, check if the knowledge that informed it needs updating.
This closes the loop: knowledge -> action -> knowledge update.

```
# Freshness prioritization: check most-retrieved nodes first
# High-retrieval nodes have more impact if they're wrong
Bash: experience-read.sh --most-retrieved 10
high_retrieval_nodes = extract tree_nodes_related from top experiences
Prioritize these nodes in the reconciliation scan below

IF external_changes:  # Set by Phase 4.2 domain steps (concrete detection, not assumed)
    tree_nodes_used = primary_nodes read during intelligent retrieval (from Phase 4)
    IF tree_nodes_used is non-empty:
        For each node_key in tree_nodes_used:
            Read the node's .md file (brief scan, not deep read)
            Ask: "Does this node still accurately reflect reality after what I just changed?"
            IF node is stale or contradicted:
                IF quick fix (< 3 sentences): update node now, set last_update_trigger:
                    {type: "reconciliation", source: goal.id, session: N}
                ELSE: echo '{"node_key": "<node_key>", "reason": "<reason>", "source_goal": "<goal.id>", "priority": "medium", "created": "<today>"}' | Bash: wm-append.sh knowledge_debt

ELIF goal resolved a hypothesis with outcome CORRECTED:
    # Corrections mean our knowledge was WRONG — high-priority reconciliation
    affected_nodes = nodes from retrieval context matching goal.category
    For each affected node:
        IF outcome contradicts node content → reconcile immediately or log HIGH debt
        IF outcome refines understanding → update confidence, add compressed insight
```

### Phase 4.6: Post Findings to Board

After goal execution and knowledge reconciliation, post notable findings:

```
IF goal produced actionable findings OR hypothesis was resolved:
    summary = one-line summary of what was learned or accomplished
    echo "${summary}" | Bash: board-post.sh --channel findings --type finding --tags <goal.category>
```

Skip for routine/maintenance goals that produce no new knowledge.

## Batched Execution

Batched execution (RARE — only when batch_mode is true).
Default is single-goal. Batch only fires for trivially small second goals.
Sequential batch execution (non-delegated path).
When using agent delegation, parallel dispatch is handled in Phase 2.6.
Each batched goal MUST complete full Phase 5-8 before the next starts.

```
IF batch_mode AND more goals in batch:
    Execute next goal in batch (reuse retrieval context, skip Phase 2)
    Classify outcome_class for batched goal (same Phase 4-post rules)
    MANDATORY per-goal phases (in order, gated by outcome_class):
    - Phase 5: Verify completion (always runs)
    - Phase 6: Spark check (SKIP if routine)
    - Phase 7: Aspiration-level check (always runs)
    - Phase 8: State Update Protocol — full steps with immediate tree encoding if deep,
      Steps 1-4 + abbreviated Step 7 if routine
    Complete ALL phases for this goal before starting the next batched goal.
```

## Chaining

- **Called by**: `/aspirations` orchestrator (Phase 4, every iteration)
- **Calls**: `aspirations-update-goal.sh --source`, `aspirations-add-goal.sh --source`, `load-conventions.sh`, `load-tree-summary.sh`, `retrieve.sh`, `tree-update.sh`, `guardrail-check.sh`, `infra-health.sh`, `experience-add.sh`, `wm-set.sh`, `wm-read.sh`, `board-post.sh`, `skill-relations.sh`, `build-agent-context.sh`, `curriculum-contract-check.sh`, `pending-agents.sh`
