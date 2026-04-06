---
name: aspirations-select
description: "Goal selection — scoring, metacognitive assessment, batching, blocker gate, pre-fetch, and full goal detail loading"
user-invocable: false
parent-skill: aspirations
conventions: [aspirations, goal-selection, goal-schemas, infrastructure, reasoning-guardrails]
minimum_mode: autonomous
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
---

# /aspirations-select — Goal Selection + Metacognitive Assessment

Selects the highest-value goal for execution using algorithmic scoring (via script)
plus metacognitive assessment (model judgment on familiarity, value, cost, infrastructure).

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter.

## Inputs (from orchestrator)

- `first_action`: Pre-scored goal from handoff (first iteration only, or None)
- `decisions_locked`: Carried forward decisions from previous session

## Outputs (to orchestrator)

- `goal`: Selected goal object (or None if no candidates)
- `effort_level`: "full", "standard", or "skip"
- `batch_mode`: Boolean
- `batch`: Array of batched goals (if batch_mode)
- `ranked_goals`: Full ranked list from selector
- `prefetch_goals`: Goals for pre-fetch research agents
- `selection_context`: Raw parsed output from goal-selector.sh (includes `by_reason`, `blocked_goals`, `blocked_count` when all_blocked)
- `selection_reason`: Why no goal was returned (`"all_blocked"`, `"all_blocked_by_gate"`, or absent when goal selected)
- `source`: Queue origin of the selected goal (`"world"` or `"agent"`) — from goal-selector output. Pass to all downstream `aspirations-*.sh` calls via `--source {source}`.

## Phase 2: Select Next Goal

### First-Action Override (first iteration only)
```
IF first_action is set (from handoff):
    Look up goal by first_action.goal_id
    effort_level = first_action.effort_level
    Clear first_action (consumed)
    # Still run Phase 2.5 for focus context
```

### Algorithmic Scoring
```
# ASSERTION: goal-selector.sh MUST run every iteration. No exceptions.
# After autocompact, memory of blockers is unreliable. The script reads live state.
Bash: goal-selector.sh
parsed_output = parse JSON output

# Blocked-goals detection: script returns object with "all_blocked" when
# goals exist but none are executable (all deferred/blocked/gated)
IF parsed_output is a JSON object with "all_blocked": true:
    Output: "▸ ALL GOALS BLOCKED: {blocked_count} goals — {by_reason summary}"
    FOR EACH goal in blocked_goals: Output: "  {goal_id}: {detail}"
    # parsed_output contains blocked_goals, blocked_count, by_reason — orchestrator needs these
    RETURN (goal = None, selection_reason = "all_blocked", selection_context = parsed_output)

ranked_goals = parsed_output  # JSON array of scored candidates
# Each entry: {goal_id, aspiration_id, title, skill, category, recurring, score, breakdown, raw}
```

### Phase 2.05: Meta-Strategy Adjustment
```
Bash: meta-read.sh goal-selection-strategy.yaml
IF selection_heuristics is non-empty: apply post-score adjustments, re-sort
IF custom_criteria is non-empty: evaluate + add weighted score
```

### Phase 2.07: Directive & Insight Trigger Scan

Scan for cross-agent directives and insight triggers before applying precondition gates.
Directives influence scoring (handled mechanically by `goal-selector.py` `directive_boost`
criterion). The LLM handles acknowledgment and insight trigger processing.

```
# Directive acknowledgment
Bash: board-read.sh --channel coordination --type directive --since 24h --json
FOR EACH directive WHERE no acknowledgment reply from this agent exists:
    Output: "▸ DIRECTIVE: {directive.text} (from {directive.author}, weight: {parsed weight})"
    echo "Acknowledged directive {directive.id}" | \
      Bash: board-post.sh --channel coordination --type status \
        --reply-to {directive.id} --tags "acknowledged,{AGENT_NAME}"

# Insight trigger scan (cross-agent findings that affect our goals)
Bash: board-read.sh --channel findings --type finding --since 24h --json
FOR EACH finding WITH "insight_trigger" in tags:
    Parse severity from tags (severity:invalidates|constrains|enables|informs)
    Parse affected goals from tags (affects:<goal-id>)
    Parse required action (requires_action_by:<agent>, action_type:<type>)

    IF requires_action_by does not match this agent: SKIP
    IF already processed (check for reply from this agent): SKIP

    IF severity == "invalidates":
        Output: "▸ INSIGHT TRIGGER (INVALIDATES): {finding.text}"
        Create investigation goal: "Investigate: {affected goal title} — assumption invalidated"
        Acknowledge: echo "Processed insight trigger" | board-post.sh --reply-to {finding.id}
    ELIF severity == "constrains":
        Output: "▸ INSIGHT TRIGGER (CONSTRAINS): {finding.text}"
        Append constraint note to affected goal description via aspirations-update-goal.sh
        Acknowledge reply
    ELIF severity == "enables":
        Output: "▸ INSIGHT TRIGGER (ENABLES): {finding.text}"
        # Enabling insights are informational — the directive_boost handles scoring
        Acknowledge reply
    ELIF severity == "informs":
        Output: "▸ INSIGHT TRIGGER (INFORMS): {finding.text}"
        Acknowledge reply
```

### Precondition Gate
```
For each goal in ranked_goals:
    if goal.verification.preconditions exist:
        Evaluate each against current session state
        if any not met: remove from ranked_goals
```

### Context-Aware Batching
```
Bash: cat <agent>/session/context-budget.json 2>/dev/null || echo '{"zone":"normal"}'
zone = parsed zone field

batch = [ranked_goals[0]] if ranked_goals else []
batch_mode = False

IF zone == "fresh" (<40%): batch up to 3 same-category goals
ELIF zone == "normal" (40-65%): batch up to 2 same-category + same-aspiration
ELSE zone == "tight" (>65%): batch only if same-category + same-aspiration + same-skill + minimal effort
```

### Self-Alignment Check
```
goals_since_last_alignment_check += 1
all_recurring = every entry in ranked_goals has recurring == true

IF all_recurring OR goals_since_last_alignment_check >= check_interval_goals:
    goals_since_last_alignment_check = 0
    Bash: work-alignment.sh check --ranked-goals '<ranked_goals_json>'
    IF alignment data suggests planning valuable OR all_recurring:
        invoke /create-aspiration from-self --plan with: alignment_data

    # Ambition check: sprint-scope proliferation
    small_count = count active aspirations where scope == "sprint" or (null and ≤4 goals)
    IF small_count >= 3:
        Output: "▸ AMBITION CHECK: {small_count} sprint-scope aspirations"
```

### No-Goals Path
```
if goal is None: RETURN (goal = None)
# Orchestrator owns the fallback logic (create-aspiration, ASAP, research, reflect)
```

## Phase 2.25: Selection Context Loading
```
Bash: load-tree-summary.sh
IF output non-empty: Read the returned path
# Match candidate goals' categories against tree summary nodes
selection_context = match ranked_goals[:5] categories
```

## Phase 2.5: Metacognitive Assessment

```
Read <agent>/profile.yaml → focus
Read decisions_locked from handoff context

For selected goal, assess:

1. FAMILIARITY: Check experiential-index, selection_context capability_level
   MASTER/EXPLOIT → System 1 (fast), EXPLORE/missing → System 2 (deliberate)

2. EXPECTED VALUE: Novel insight/deadline/code deliverable → full
   Useful but not critical → standard. Routine/marginal → standard or skip

3. COST ESTIMATE: Quick check → standard. Deep exploration → full

4. INFRASTRUCTURE NEEDS: Check if goal needs running services
   IF needed: Bash: infra-health.sh check {component}
   IF provisionable: invoke provision_skill (unless goal IS the provision skill)

5. CONSOLIDATION: Check consolidation_health from working memory.
   IF near_complete aspirations exist (consolidation_health.near_complete > 0):
       Bias toward goals in those aspirations — completion pull is strongest near the finish.
   IF selected goal is from a stalled aspiration (consolidation_health.stalled > 0):
       Consider effort_level = "full" (invest deeply to unstall, not skim).

Apply focus context to value assessment.
```

### MR-Search Exploration Mode
```
IF capability_level < auto_designate_below_capability threshold:
    IF session exploration fraction < max_exploration_fraction:
        Bash: aspirations-update-goal.sh --source {goal.source} <goal-id> execution_mode exploration
        Output: "▸ EXPLORATION MODE: {goal.category} shielded"
```

### Phase 2.55: Self-Abstention Check
```
# Can I add genuine value to this goal given my capabilities?
# Not about effort — about capability match. (arXiv 2603.28990: 8.6% voluntary
# abstention in top model improves overall system quality.)
IF goal requires capabilities outside <agent>/self.md "What I Do" section:
    IF goal.abstained_by is set AND goal.abstained_by != AGENT_NAME:
        # Both agents can't do this goal — defer with timestamp for expiry
        Bash: aspirations-update-goal.sh --source {source} <goal-id> defer_reason "Both agents abstained — needs user attention or capability expansion"
        Bash: aspirations-update-goal.sh --source {source} <goal-id> defer_reason_set_at "$(date +%Y-%m-%dT%H:%M:%S)"
        Log: "DOUBLE-ABSTENTION: ${goal.id} — deferring (${goal.abstained_by} also abstained)"
    ELSE:
        Bash: aspirations-update-goal.sh --source {source} <goal-id> abstained_by <AGENT_NAME>
        Bash: aspirations-update-goal.sh --source {source} <goal-id> abstained_at "$(date +%Y-%m-%dT%H:%M:%S)"
    echo "Abstaining: ${goal.id} — capability mismatch: {specific_gap}" | Bash: board-post.sh --channel coordination --type status --tags abstain,${goal.id},${goal.category}
    Log: "SELF-ABSTENTION: ${goal.id} — {reason}"
    continue to next ranked goal

# Self-abstention expires after abstention_timeout_hours (default 72h).
# goal-selector.py checks abstained_at timestamp — expired abstentions fall through.
# If the original reason still holds, the agent will re-abstain with a fresh timestamp.
# If both agents abstain, the goal is deferred to prevent ping-pong.
```

### Determine effort_level
```
full:     Thorough execution, full spark check + metacognitive Q
standard: Normal execution, normal sparks (default)
skip:     Focus mismatch or zero expected value

Token cost and wall-clock time are NOT valid skip reasons.
Valid: focus mismatch, zero expected value, blocker gate, self-abstention.
```

## Phase 2.5b: Blocker Gate (with verification probe)
```
Bash: wm-read.sh known_blockers --json
FOR goal in ranked_goals (iterate if current goal is blocked):
    IF goal.skill in blocker.affected_skills:
        # VERIFY: probe infrastructure before trusting stale blocker
        component = map goal.skill to infra component
        IF component:
            Bash: infra-health.sh check {component}
            IF ok: clear blocker, proceed with this goal
            ELIF provisionable: attempt provisioning
            IF still blocked: effort_level = skip, try next goal in ranked_goals
    ELIF goal.skill is null AND goal.category in [cat for b in known_blockers for cat in b.get("affected_categories", []) if b.get("resolution") is None]:
        # Category-based block (fallback for skill=null goals): probe before trusting
        component = map goal.category to infra component
        IF component:
            Bash: infra-health.sh check {component}
            IF ok: proceed (category block may be stale)
            IF still blocked: effort_level = skip, try next goal in ranked_goals

# After FOR loop: if every ranked goal was skipped by the blocker gate
IF all ranked_goals exhausted by blocker gate:
    Output: "▸ ALL CANDIDATES BLOCKED BY GATE"
    # No goal-selector-level blocked_goals data — gate rejections are skill-level infrastructure blocks
    RETURN (goal = None, selection_reason = "all_blocked_by_gate", selection_context = {blocked_goals: [], blocked_count: 0, by_reason: {}})
```

## Phase 2.6: Pre-Fetch Context
```
IF host chooses to pre-fetch:
    FOR g in ranked_goals[1:] (up to max_concurrent_goals - 1):
        IF g has independent research phase: prefetch_goals.append(g)
```

## Phase 2.9: Load Full Goal Detail
```
# Compact data lacks description and verification. Load full goal for execution.
# do NOT remove this step — without it, execution has no description or verification criteria
Bash: aspirations-read.sh --source {goal.source} --id {goal.aspiration_id}
goal = find by goal_id in returned aspiration's goals array
```

## Chaining

- **Called by**: `/aspirations` orchestrator (Phase 2, every iteration)
- **Calls**: `goal-selector.sh`, `load-tree-summary.sh`, `work-alignment.sh`, `infra-health.sh`, `aspirations-read.sh --source`, `aspirations-update-goal.sh --source`, `/create-aspiration` (no-goals + alignment)
- **Reads**: meta/goal-selection-strategy.yaml, profile.yaml (focus), working memory (blockers), context-budget.json, tree summary, handoff decisions
