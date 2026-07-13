---
name: aspirations-select
description: "Selects the next goal for execution: runs the mandatory goal-selector.sh scoring pass, metacognitive assessment, batch candidacy check, blocker gate, context pre-fetch, and full goal-detail loading. Use whenever the aspirations loop needs the highest-priority unclaimed unblocked goal for the next iteration. Always called immediately after /aspirations-precheck; never invoked directly by the user. Output determines whether execution, all-blocked handling, or evolution fires next."
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
revision_id: "skill-bootstrap-aspirations-select-d5f0ce"
previous_revision_id: null
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
# Each entry: {goal_id, aspiration_id, title, skill, category, recurring, score, breakdown, raw, cross_world_origin}
# Foreign-goal display hint (g-336-12): cross_world_origin is non-null
# "<identity>@<origin-world>" ONLY on goals injected by a cross-world INFLUENCE
# grant; null for native goals. Whenever a candidate is rendered to a
# human-visible line — the ALL-BLOCKED list above, the Program-alignment probe,
# any ▸ Output that names a goal by title — append a " [foreign: {cross_world_origin}]"
# badge when the field is non-null, so the agent KNOWS it is executing another
# world's intent and applies appropriate scrutiny (guardrails, review gate).
# A native goal (null) renders unchanged — no badge, no false positive.

# Partner-claim filter: drop any goal the partner is already in_flight on.
# This is the live claim-conflict HINT — it avoids wasting decomposition and
# context-fetch effort on a goal we would lose at the Phase 4 claim-conflict
# gate anyway. The authoritative gate still runs in the orchestrator (digest
# Phase 4) immediately before aspirations-claim.sh, because partner state can
# flip between this filter and the claim attempt.
Bash: team-state-read.sh --field agent_status.<partner>.in_flight.goal_id --json
IF returned value is a non-null string:
    partner_in_flight_id = returned value
    ranked_goals = [g for g in ranked_goals if g.goal_id != partner_in_flight_id]
    IF len(ranked_goals) == 0:
        Output: "▸ Partner holds the only candidate goal ({partner_in_flight_id}) — yielding"
        RETURN (goal = None, selection_reason = "all_blocked", selection_context = {by_reason: {partner_held: 1}, blocked_count: 1})
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

### Precondition Gate (strings only — structured preconditions filtered in COLLECT)
```
For each goal in ranked_goals:
    string_pcs = [p for p in goal.verification.preconditions if isinstance(p, str)]
    # Structured dict preconditions are already filtered by goal-selector.py
    # COLLECT via predicate.evaluate_all (see conventions/preconditions.md).
    if string_pcs:
        Evaluate each against current session state (LLM judgment)
        if any not met:
            # SHAPE-RECURRING TRAP — string-precondition twin (g-241-04 / rb-441).
            # When a recurring goal's string precondition fails AFTER the time
            # gate elapsed, advancing lastAchievedAt prevents overdue_ratio
            # runaway. Without this branch, a string precondition that
            # consistently returns "not met" leaves the goal pinned at
            # increasing urgency every cycle, mirroring the structured-
            # precondition trap that recurring-precondition-sweep.py
            # already handles. The structural fix lives in the sweep
            # script; this branch handles the LLM-evaluated path that
            # the sweep cannot reach.
            #
            # MUST NOT increment consecutive_routine — the goal was never
            # closed, only shelved by the precondition filter. Cargo-cult-
            # detector reads consecutive_routine as the "this goal keeps
            # getting closed cheaply" signal; bumping it on a not-run
            # would corrupt the calibration logic.
            #
            # The time-gate check matches recurring-precondition-sweep.py's
            # _iter_recurring_past_gate predicate: lastAchievedAt is not
            # null AND elapsed_hours >= interval_hours. Goals that are
            # recurring but have never run (no lastAchievedAt) skip the
            # advance — they have no urgency-runaway problem yet.
            if goal.recurring and goal.lastAchievedAt is not null:
                elapsed_h = hours_since(goal.lastAchievedAt)
                interval_h = goal.interval_hours OR (goal.remind_days * 24) OR 24
                if elapsed_h >= interval_h:
                    Bash: aspirations-update-goal.sh --source {goal.source} {goal.id} lastAchievedAt "<now-iso>"
                    Output: "▸ STRING-PC-FAIL recurring shelved: {goal.id} ({elapsed_h:.1f}h ≥ {interval_h}h, lastAchievedAt advanced)"
            remove from ranked_goals
```

### Context-Aware Batching
```
# Status line writes context-budget.json every prompt; a missing file is real
# infrastructure breakage, not a condition to paper over with a default. Read
# without a fallback — fail loud (guard-160, g-243-01, rb-215 single-source-of-truth).
# Zones are distance-to-autocompact (pct_to_autocompact), NOT raw usage — see
# core/scripts/context-budget-status.py classify_zone for the source of truth.
Bash: bash core/scripts/context-budget-banner.sh   # required — quote this line in your response
Bash: cat agents/<agent>/session/context-budget.json
zone = parsed zone field

batch = [ranked_goals[0]] if ranked_goals else []
batch_mode = False

IF zone == "fresh" (pct_to_autocompact < 50): batch up to 3 same-category goals
ELIF zone == "normal" (50-85 pct_to_autocompact): batch up to 2 same-category + same-aspiration
ELSE zone == "tight" (pct_to_autocompact >= 85): batch only if same-category + same-aspiration + same-skill + minimal effort
```

### Self-Alignment Check
```
# goals_since_last_alignment_check is RESTORED from loop_state.alignment_check_at
# (orchestrator Phase -0.5) and INCREMENTED bash-side by iteration-close.sh
# (loop-state-bump-counters.py --goal-id, fires every goal close, both outcomes).
# Do NOT mutate it in-context here — bash owns it (g-283 single-writer). Before
# g-115-1561 the in-context `+= 1` / `= 0` were discarded at LOOP_CONTINUE (the
# field had no bash writer), so it stayed frozen at 0 and the goals-count branch
# below NEVER fired — only all_recurring / recurring_heavy ever triggered the check.
all_recurring = every entry in ranked_goals has recurring == true
recurring_heavy = len(ranked_goals) >= 5 and (sum(1 for g in ranked_goals if g.recurring) / len(ranked_goals)) > 0.90

IF all_recurring OR recurring_heavy OR goals_since_last_alignment_check >= check_interval_goals:
    # Reset the bash-owned cadence counter (persists across iterations, unlike the
    # retired in-context `= 0`). loop-state-bump-counters.py is the single writer.
    Bash: py -3 core/scripts/loop-state-bump-counters.py --reset-alignment
    Bash: work-alignment.sh check --ranked-goals '<ranked_goals_json>'
    IF alignment data suggests planning valuable OR all_recurring:
        invoke /create-aspiration from-self --plan with: alignment_data

    # Program-alignment probe (turns world/program.md from passive context into
    # an active per-alignment query — counters tactical-drift by forcing the
    # agent to justify the top goal against the shared Program every N goals).
    # The Program describes WHY this world exists; Self describes WHO this
    # agent is — work-alignment above covers the Self-side; this step covers
    # the Program-side.
    Bash: world-cat.sh program.md
    Ask (LLM, in-turn reflection): "Does the top-ranked goal ({ranked_goals[0].id} —
      {ranked_goals[0].title}) materially serve The Program's stated purpose? If no,
      what goal would better serve the Program right now?"
    Log the answer via a board post (journal-add.sh requires stdin JSON and
    was silently failing on the argv form; board-post is cross-agent visible
    and tagged for later retrieval):
      Bash: echo "goal=<top-id>; aligned=<true|false>; note=<brief justification or dissent>" | board-post.sh --channel findings --type finding --tags "program-alignment"
    # Persist a misalignment streak so 3 consecutive misalignments auto-boost
    # aspiration_generation sparks on the next spark cycle. wm.py has no
    # built-in increment — read, add, write.
    # wm-read.sh prints "null" + exit 0 when the key is missing (see wm.py
    # cmd_read). Treat "null" as 0; DO NOT add `|| echo 0` — it never fires
    # (exit was 0) and masks real read errors.
    IF answer indicates misalignment:
        raw = Bash: wm-read.sh program_misalignment_streak
        current = 0 if raw.strip() == "null" else int(raw)
        next_val = current + 1
        Bash: echo "$next_val" | wm-set.sh program_misalignment_streak
        IF next_val >= 3:
            Bash: echo 'true' | wm-set.sh boost_generative_sparks
            Bash: echo '0' | wm-set.sh program_misalignment_streak
    ELSE:
        Bash: echo '0' | wm-set.sh program_misalignment_streak

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

## Phase 2.27: Cross-Cutting Guardrail Probe (G1 / R8)

Tree summary (Phase 2.25) is shallow — it lists capability levels and node
summaries, not the reasoning-bank or guardrail entries that might constrain
WHICH goal in this category to pick now. Per
`.claude/rules/retrieve-before-deciding.md` decision point 1 ("picking the
next goal"), the selector should retrieve cross-cutting RB/G against the
top-ranked goal's category before metacognitive assessment commits to it.

```
top_goal = ranked_goals[0]
Bash: retrieve.sh --category "{top_goal.category} {top_goal.title[:60]}" --depth shallow

From the returned JSON, surface to Phase 2.5:
  - guardrails[] whose rule constrains work in this category right now
    (e.g., "do not run goal X while blocker Y exists", recently-failed
    aspiration patterns)
  - reasoning_bank[] entries describing prior attempts at this goal class
    that should adjust effort_level or expected value
  - beliefs[] that the goal's outcome would reinforce or contradict

Decision overrides this phase can apply:
  - If a guardrail explicitly forbids this goal now: skip to ranked_goals[1]
    and re-run Phase 2.27 for the next candidate
  - If a recent RB entry shows the same goal failed 2+ times this week:
    log "RECENT FAILURE DETECTED — pre-flight check required" and pass the
    RB IDs into Phase 2.5 effort_level decision (escalate to "full")
  - Otherwise: carry the loaded entries forward as
    selection_context.cross_cutting_constraints

Fail-open: if retrieve.sh errors, log and proceed to Phase 2.5 with empty
cross_cutting_constraints. Goal selection must not block on retrieval.
```

## Phase 2.5: Metacognitive Assessment

```
Read agents/<agent>/profile.yaml → focus
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
IF goal requires capabilities outside agents/<agent>/self.md "What I Do" section:
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

## Phase 2.95: Anchor Selection for Autocompact Resilience

Write an iteration checkpoint with the selected goal id. Survives autocompact
so `postcompact-restore.py` can tell the model exactly which goal was picked —
prevents post-compact goal-substitution drift (bug traced 2026-04-22 alpha
session-56: pre-compact selected g-115-22, post-compact resumed as /decompose
g-250-13 because the compact summary reconstructed the wrong in-flight goal).

Cleared in `/aspirations-execute` Phase 8 on goal completion and in
`/start --recover` / `aspirations-graceful-stop` D6.

```
# Cross-agent source translation (g-115-978 Option 3). collect_cross_agent_candidates
# emits source='cross-agent:<sib>' for goals pulled from a sibling agent's queue
# (g-115-946 stranding fix). Daemon validators reject that value and
# aspirations-*.sh wrappers expect strict 'world' or 'agent'. Translate at the
# orchestrator boundary: split the prefix into (effective_source='agent',
# cross_agent_owner='<sib>'); leave non-cross-agent sources unchanged. Phase 4's
# claim block reads cross_agent_owner from the checkpoint and env-prefixes
# downstream subprocess calls with MIND_AGENT=<owner> so writes route to the
# sibling's directory tree.
IF goal.source.startswith('cross-agent:'):
    cross_agent_owner = goal.source.split(':', 1)[1]
    effective_source  = 'agent'
ELSE:
    cross_agent_owner = None
    effective_source  = goal.source   # 'world' or 'agent'

Bash: NOW="$(date +%Y-%m-%dT%H:%M:%S)";
      # When cross_agent_owner is set, splice the extra field into the JSON;
      # otherwise emit the base shape. loop-state-save.py rejects unknown keys,
      # so the conditional shape stays validated either way.
      if [[ -n "{cross_agent_owner}" ]]; then
        printf '{"goal_id":"%s","aspiration_id":"%s","source":"%s","phase":"selected","selected_at":"%s","selector_score":%s,"skill":"%s","cross_agent_owner":"%s"}' \
          "{goal.goal_id}" "{goal.aspiration_id}" "{effective_source}" "$NOW" "{goal.score}" "{goal.skill or ''}" "{cross_agent_owner}"
      else
        printf '{"goal_id":"%s","aspiration_id":"%s","source":"%s","phase":"selected","selected_at":"%s","selector_score":%s,"skill":"%s"}' \
          "{goal.goal_id}" "{goal.aspiration_id}" "{effective_source}" "$NOW" "{goal.score}" "{goal.skill or ''}"
      fi | bash core/scripts/loop-state-save.sh init
# Single-writer wrapper (g-248-36): typed-key validation, atomic tempfile+rename.
# Replaces the prior inline `py -3 -c` write — same semantics, validated schema.
```

## Chaining

- **Called by**: `/aspirations` orchestrator (Phase 2, every iteration)
- **Calls**: `goal-selector.sh`, `load-tree-summary.sh`, `work-alignment.sh`, `infra-health.sh`, `aspirations-read.sh --source`, `aspirations-update-goal.sh --source`, `/create-aspiration` (no-goals + alignment)
- **Reads**: meta/goal-selection-strategy.yaml, profile.yaml (focus), working memory (blockers), context-budget.json, tree summary, handoff decisions

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is `goal-selector.sh` or an `aspirations-update-goal.sh` claim.
Never end with a text summary of the selected goal.
