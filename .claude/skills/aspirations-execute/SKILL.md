---
name: aspirations-execute
description: "Phase 4: Goal execution with intelligent retrieval, delegation, fail-fast cascade, experience archival, context utilization, domain post-execution steps, knowledge reconciliation, and batch execution"
user-invocable: false
parent-skill: aspirations
triggers:
  - "Phase 4"
conventions: [aspirations, pipeline, experience, tree-retrieval, goal-schemas, infrastructure, reasoning-guardrails]
---

# Phase 4: Goal Execution

Invoked as Phase 4 of the aspirations loop after goal selection (Phase 2) and decomposition (Phase 3). Covers the full execution pipeline: precondition checking, intelligent LLM-driven retrieval, memory deliberation, agent delegation, primary execution, fail-fast cascade, experience archival, context utilization feedback, domain post-execution steps, knowledge reconciliation, and batch execution.

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
   - Write to `mind/session/pending-questions.yaml`:
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

For all types: `echo '<JSON>' | bash core/scripts/aspirations-add-goal.sh <aspiration_id>`
Place in the RIGHT aspiration (read active aspirations, pick best fit).
Dedup: check for existing goals with similar title before creating.

These are NOT mutually exclusive. A single event can spawn all three:
  Unblock: "fix the crash" + Investigate: "why does it crash?" + Idea: "add crash prevention"

---

## Phase 4 Preamble: Cost-Ordered Precondition Checking

Before expensive data retrieval (SSH, large files, APIs), check local/cheap
preconditions first: timestamps, git log, file existence, metadata.
See: guard-009

## Phase 4: Execute (with intelligent retrieval)

```
Bash: aspirations-update-goal.sh <goal-id> status in-progress
Bash: aspirations-update-goal.sh <goal-id> started <today>

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
Bash: retrieve.sh --supplementary-only --category {goal.category}
# Returns: reasoning_bank, guardrails, pattern_signatures, experiences,
#          beliefs, experiential_index
# Tree nodes are empty (skipped) — we already loaded them in Step 3.

Output: "▸ Supplementary: {N} reasoning, {N} guardrails, {N} patterns, {N} experiences"

# Memory Deliberation: scan supplementary items for relevance
FOR EACH item in reasoning_bank + guardrails + pattern_signatures:
    Assess relevance to current goal context
    Note: ACTIVE (will inform execution) or SKIPPED (loaded but not applicable)

# Step 5: Evaluate sufficiency
# Review what was loaded. For secondary_nodes identified in Step 2:
IF context feels insufficient for the goal's complexity:
    Read additional secondary_nodes .md files
    Bash: tree-update.sh --increment {node_key} retrieval_count
    Output: "▸ Follow-up: loaded {N} additional nodes for context"
# Follow entity cross-references spotted in .md front matter if needed.

# Step 5b: Write retrieval manifest (MANDATORY — survives autocompact via hooks)
# The manifest makes Memory Deliberation results durable. Phase 4.26 reads it
# instead of relying on transient LLM state. PreCompact hook saves it to checkpoint.
echo '<retrieval_manifest_json>' | Bash: wm-set.sh active_context.retrieval_manifest
# JSON payload:
#   goal_id: "{goal.id}"
#   goal_title: "{goal.title}"
#   timestamp: "{ISO timestamp}"
#   tree_nodes_loaded: [list of node keys from Steps 3 + 5]
#   supplementary_counts: {reasoning_bank: N, guardrails: N, patterns: N, experiences: N}
#   deliberation:
#       active_items: [{id, type} for each item marked ACTIVE]
#       skipped_items: [{id, type} for each item marked SKIPPED]
#   utilization_pending: true  # Phase 4.26 has not yet fired
Output: "▸ Retrieval manifest: {N} nodes, {A} active, {S} skipped items written"

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
# with zero context. Team agents call /prime as their first action,
# which primes them with the full knowledge tree, guardrails, reasoning
# bank, and domain context.
#
# CRITICAL: Team agents do READ-ONLY research. They MUST NOT:
#   - Invoke skills (skills write to shared state)
#   - Write or edit ANY files (mind/, external repos, anything)
#   - Call state-mutating scripts (pipeline-move.sh, experience-add.sh, etc.)
#   - Make git commits or pushes
#
# Team agents report structured findings via message. The host then
# executes goals with enriched context (research already done = faster).
#
# This is a tool, not a rule. The host is free to skip delegation entirely.

IF prefetch_goals:
    TeamCreate(team_name="research-{session}", description="Pre-fetch research for goals")
    FOR pg in prefetch_goals:
        research_task = extract_research_question(pg)  # What info does this goal need?
        # Register agent BEFORE dispatch (crash-safe: staleness timeout cleans up if dispatch fails)
        Bash: `pending-agents.sh register --id "researcher-{pg.goal_id}" --team "research-{session}" --goal "{pg.goal_id}" --purpose "{research_task[:100]}" --timeout 30`
        Agent(team_name="research-{session}", name="researcher-{pg.goal_id}",
              prompt="First, invoke /prime. Then do READ-ONLY research:
                      {research_task}. Do NOT write files or invoke skills.
                      Report your findings as a structured summary.",
              run_in_background=true)

# Execute primary goal inline (host does ALL writing)
result = invoke goal.skill with goal.args
```

## Phase 4-post: Outcome Classification

After execution, classify the outcome for post-execution pipeline gating.
This does NOT affect execution itself — the skill always runs fully.
It controls whether the expensive cognitive phases (experience archival,
spark checks, tree encoding) fire afterward.

```
outcome_class = "productive"  # default: full pipeline

IF goal.recurring AND goal_succeeded (no errors, no timeouts):
    # Assess the ACTUAL execution result:
    #   - Did the skill find items to process? (emails, alerts, stale data, issues)
    #   - Did the skill take any action beyond the check itself?
    #   - Did any interaction reveal new information?
    # If the answer to ALL is "no" → routine check with no findings.
    IF result produced no actionable items and no new information:
        outcome_class = "routine"

# SAFETY: Non-recurring goals ALWAYS remain "productive"
# SAFETY: Failed goals ALWAYS remain "productive" (failures are learning events)
# SAFETY: If uncertain, remain "productive"
```

## Phase 4.0: Structured SKIP Fast-Path (with Recovery Attempt)

Skills that SKIP at preflight return INFRASTRUCTURE_UNAVAILABLE or RESOURCE_BLOCKED.
Before blocking, attempt ONE recovery via infra-health.sh (which has auto-recovery
for registered components). Only block if recovery fails.

```
IF result is INFRASTRUCTURE_UNAVAILABLE or RESOURCE_BLOCKED:
    # RECOVERY ATTEMPT — runs once. Do not remove this guard.
    IF NOT retry_attempted:
        component = map goal.skill to infra component via mind/infra-health.yaml skill_mapping
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
            Bash: aspirations-update-goal.sh <goal-id> status pending
            continue  # Skip blocker, retry next iteration
        ELSE:
            evidence_signals.append({signal: "alternative check", weight: 1})
    # Sufficient evidence — proceed to blocker creation
    → invoke CREATE_BLOCKER protocol (below)
    Bash: aspirations-update-goal.sh <goal-id> status pending
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
          → echo '<goal-json>' | bash core/scripts/aspirations-add-goal.sh <aspiration_id>
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
in mind/, not hardcoded here.

For infrastructure goals, this enables learned behaviors to fire even when
the goal appeared to succeed. A "successful" goal can mask real infrastructure
errors that only guardrails know to check for.

Phase 4.1 does NOT fire guardrail checks on local/tooling errors: script
validation rejections, file not found in mind/, build/compile errors during
code editing, or git failures.

```
goal_succeeded = (result achieved verification.outcomes AND no errors/timeouts)
involved_infrastructure = (goal.skill (without leading /) in mind/infra-health.yaml skill_mapping OR goal.category in mind/infra-health.yaml category_mapping)

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
            Bash: aspirations-update-goal.sh <goal-id> status pending
            continue  # Skip Phases 4.25-9 for this goal
        # ELSE: goal succeeded but guardrail found issues. Blocker or fix handled above.
        # Fall through to Phase 4.25+ so the ORIGINAL goal gets normal completion.

    # severity == "soft_failure" or guardrail-success path: fall through to Phase 4.25+

# SAFETY: Guardrail findings override routine classification.
# Phase 4-post classified before guardrails ran — if guardrails found
# real issues, this IS new information regardless of skill result.
IF guardrail_found_issues:
    outcome_class = "productive"
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
Bash: test -f mind/conventions/post-execution.md && echo "exists"
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
IF outcome_class == "productive":
    experience_id = "exp-{goal.id}-{goal.skill_name_slug}"
    Write mind/experience/{experience_id}.md with:
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
        content_path: "mind/experience/{experience_id}.md"
    echo '{"experience_refs": ["{experience_id}"]}' | Bash: wm-set.sh active_context.experience_refs
```

## Phase 4.26: Context Utilization Feedback

After goal execution, update utilization counters on retrieved items.
This closes the feedback loop so the system learns which knowledge is helpful.
Reads the durable retrieval manifest (written in Step 5b) instead of relying
on transient LLM state. This is critical — without the manifest, utilization
feedback silently doesn't fire and the system never learns what helps.

```
# Read the durable manifest — not transient LLM state
Bash: wm-read.sh active_context.retrieval_manifest --json

IF outcome_class == "productive" AND retrieval_manifest exists AND retrieval_manifest.goal_id == current goal.id:
    FOR EACH item in retrieval_manifest.deliberation.active_items:
        # STRUCTURAL HELPFULNESS: item is helpful if referenced in execution trace.
        # Replaces vague "helped" judgment — the LLM never evaluated this (0/32 helpful).
        IF item.id appears in retrieval_manifest.influence text
           OR (item.type == "guardrail" AND item.id in matched_guardrails from Phase 4.1)
           OR item.id explicitly referenced in execution commands, decisions, or output:
            Bash: {reasoning-bank|guardrails}-increment.sh {item.id} utilization.times_helpful
        ELSE:
            Bash: {reasoning-bank|guardrails}-increment.sh {item.id} utilization.times_noise
    FOR EACH item in retrieval_manifest.deliberation.skipped_items:
        Bash: {reasoning-bank|guardrails}-increment.sh {item.id} utilization.times_skipped
    Output: "▸ Utilization feedback: {H} helpful, {N} noise, {S} skipped"

    # Tree node utilization feedback (closes the tree utility loop)
    # Same structural helpfulness pattern as guardrails/reasoning bank above.
    # Neutral (not clearly either) = no action — only strong signals get recorded.
    FOR EACH node_key in retrieval_manifest.tree_nodes_loaded:
        IF node_key referenced in retrieval_manifest.influence text
           OR node_key explicitly cited in execution commands, decisions, or output:
            Bash: tree-update.sh --increment {node_key} times_helpful
        ELSE:
            Bash: tree-update.sh --increment {node_key} times_noise
    Output: "▸ Tree utilization: {TH} helpful, {TN} noise out of {T} nodes"

ELIF outcome_class == "productive":
    Output: "▸ Utilization feedback: SKIPPED — no retrieval manifest (retrieval was skipped!)"
# else: routine outcome — no feedback needed, just clear the flag below

# Always clear utilization_pending — prevents false alarms in Learning Gate (Phase 9.5b).
# Step 5b sets this to true before outcome_class is known. For routine outcomes,
# Phase 4.26 correctly skips feedback but must still clear the flag.
IF retrieval_manifest exists AND retrieval_manifest.goal_id == current goal.id:
    echo 'false' | Bash: wm-set.sh active_context.retrieval_manifest.utilization_pending
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
    - Phase 8: State Update Protocol — full 9 steps if productive,
      Steps 1-4 + abbreviated Step 7 if routine
    Complete ALL phases for this goal before starting the next batched goal.
```
