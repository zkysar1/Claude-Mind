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
- `deviation_code`: Scorer Sovereignty Layer B (g-115-2812) — the sanctioned-deviation enum code when the selected goal is NOT the scorer's top pick (`ranked_goals[0]`), else `""`. Phase 4's world-goal claim forwards it via `aspirations-claim.sh {goal.id} --deviation {deviation_code}`. Computed in Phase 2.94.

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
    # g-357-94: an idle agent must still HEAR directives. Phase 2.07 sits BELOW this return,
    # so while all-blocked no directive was acked, marked or passed on for as long as the
    # agent stayed idle (measured 2026-09-03: a user scope directive unheard for 4h). ACK
    # here exactly as 2.07 (dedup read + moot short-circuit); the honor set is empty with
    # zero candidates. Hand the ACTIVE set to the all-blocked handler, which reads it as
    # generation SCOPE (B0/B1/B2) — a directive never creates work by itself (guard-732).
    Bash: new_directives = board-read.sh --channel coordination --type directive --since 24h --unread-only --mark-read --json
    FOR EACH directive in new_directives: ack per Phase 2.07 (skip moot targets; else
        echo "Acknowledged directive {directive.id}" | board-post.sh --channel coordination --type status --reply-to {directive.id} --tags "acknowledged,{AGENT_NAME}")
    Bash: parsed_output.active_directives = board-read.sh --channel coordination --type directive --since 24h --json
    RETURN (goal = None, selection_reason = "all_blocked", selection_context = parsed_output)

ranked_goals = parsed_output  # JSON array of scored candidates
# Each entry: {goal_id, aspiration_id, title, skill, category, recurring, score, breakdown, raw, cross_world_origin, intended_agent, routed_to_me}
# Foreign-goal display hint (g-336-12): cross_world_origin is non-null
# "<identity>@<origin-world>" ONLY on goals injected by a cross-world INFLUENCE
# grant; null for native goals. Whenever a candidate is rendered to a
# human-visible line — the ALL-BLOCKED list above, the Program-alignment probe,
# any ▸ Output that names a goal by title — append a " [foreign: {cross_world_origin}]"
# badge when the field is non-null, so the agent KNOWS it is executing another
# world's intent and applies appropriate scrutiny (guardrails, review gate).
# A native goal (null) renders unchanged — no badge, no false positive.
# Routed-to-me display hint (g-115-2940): routed_to_me is true ONLY on a
# source='cross-agent:<owner>' candidate, which — by collect_cross_agent_candidates'
# strict-match contract (intended_agent==agent_name) — is BY CONSTRUCTION routed to
# THIS agent. When routed_to_me is true, render the goal as
# " [routed to YOU (owner: {source.split(':',1)[1]})]" and treat it as YOUR work,
# NOT another agent's: a 'cross-agent'/not-my-lane abstention on it is ALWAYS wrong
# (Phase 2.55 CROSS-AGENT-ROUTED EXEMPTION). Dropping intended_agent made bravo
# abstain 13x from its own HIGH-routed g-001-339.

# Partner-claim filter: drop any goal the partner is already in_flight on.
# This is the live claim-conflict HINT — it avoids wasting decomposition and
# context-fetch effort on a goal we would lose at the Phase 4 claim-conflict
# gate anyway. The authoritative gate still runs in the orchestrator (digest
# Phase 4) immediately before aspirations-claim.sh, because partner state can
# flip between this filter and the claim attempt.
#
# BOTH SHAPES ARE REQUIRED, AND READING ONLY `in_flight` OPENS THIS FILTER
# COMPLETELY (g-306-276). `in_flight` is REDUCER-OWNED: team-state-in-flight.sh
# stamps it only when this box's running-session-id exists AND equals MIND_SID,
# and SKIPs for every other Body — writing `in_flight_bodies.<sid>` instead.
# So a partner running as a WORKER Body is invisible in `in_flight` (measured
# 2026-08-10, alpha cc-07: null for ALL four live agents while three held live
# body-row claims). Same defect g-306-160 repaired in
# goal-pickup-coordination-check.py ("silently opened"); this is the second of
# three readers, the digest Phase 4 hard gate the third.
#
# Staleness is the REAPER's job, not this filter's: body_row_reaper (via
# stranded-claim-sweep) deletes rows whose carrier is stale for
# DEFAULT_REAP_STALE_MINUTES (180). Do NOT add a second freshness heuristic
# here — it would drift and put two policies on one store. The ~3h bound
# holds ONLY because every row is born with a carrier (g-306-349); a
# carrier-less row is unreapable at ANY age.
Bash: team-state-read.sh --field agent_status.<partner>.in_flight.goal_id --json
Bash: team-state-read.sh --field agent_status.<partner>.in_flight_bodies --json
    # `--field` returns the whole nested map, so no new endpoint/helper is needed.
partner_held_ids = ({first call's value} if it is a non-null string else {})
                 | {b.goal_id for b in second call's values if b is a dict and b.goal_id}
IF partner_held_ids is non-empty:
    ranked_goals = [g for g in ranked_goals if g.goal_id not in partner_held_ids]
    IF len(ranked_goals) == 0:
        Output: "▸ Partner holds the only candidate goal(s) ({partner_held_ids}) — yielding"
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
# Directive acknowledgment + HONOR (g-115-2797 / guard-1310). TWO reads with DIFFERENT scopes
# (g-115-2990): the ACK read must DEDUP — --unread-only returns only directives THIS agent has
# not seen and --mark-read records the receipt so the next iteration filters them out (the old
# single read had --mark-read without --unread-only: receipts written, never consumed, every
# directive re-acked every iteration, 5x-spam) — while the HONOR read needs ALL active
# directives (no --unread-only, no --mark-read) so a directive acked yesterday whose target is
# in TODAY's ranked_goals is still honored.
Bash: all_directives = board-read.sh --channel coordination --type directive --since 24h --json
Bash: new_directives = board-read.sh --channel coordination --type directive --since 24h --unread-only --mark-read --json
directive_targeted_goals = {}   # goal_id -> directive_id, ONLY for directives directed at THIS agent
FOR EACH directive in new_directives:   # ONLY unseen directives — dedup by construction
    # Terminal-target short-circuit (mirror the select-path stale-trigger detection
    # g-115-2969 / guard-1310): a directive whose target:{goal-id} tags are ALL terminal
    # (completed/skipped/expired) is MOOT — the work it tasked is already done, so acking it is
    # noise. Do NOT ack (the --mark-read above already recorded the receipt, so it will not
    # re-surface next iteration). Read each target's current status: Bash: aspirations-read.sh
    # --source <world|agent> --id asp-<NNN>, then find the goal by id (the aspiration prefix is
    # g-<NNN>-*); or aspirations-read.sh --source <world|agent> --id <asp-id> to read
    # a specific aspiration. NOTE: aspirations-query.sh has NO --goal-id flag (it takes
    # --goal-status / --title-contains / --goal-field / --full and errors without one).
    targets = [t.split(":",1)[1] for t in (directive.tags or []) if t.startswith("target:")]
    IF targets is non-empty AND every target goal's status in (completed, skipped, expired):
        Output: "▸ DIRECTIVE (moot — targets {targets} already terminal): {directive.id}, no ack (g-115-2990)"
        continue
    Output: "▸ DIRECTIVE: {directive.text} (from {directive.author}, weight: {parsed weight})"
    echo "Acknowledged directive {directive.id}" | \
      Bash: board-post.sh --channel coordination --type status \
        --reply-to {directive.id} --tags "acknowledged,{AGENT_NAME}"
# Build the honor set from ALL active directives in all_directives (acked or not). A directive is
# DIRECTED AT THIS AGENT by exactly one of these, in order:
#   1. a requires_action_by:{AGENT_NAME} tag;
#   2. ONLY when NO requires_action_by: tag is present at all, a bare {AGENT_NAME} tag;
#   3. ONLY when NO explicit routing tag is present at all, its text names this agent (g-115-2870).
# Steps 1-2: AN EXPLICIT ADDRESSEE IS THE WHOLE ADDRESSEE SET — board-post.sh stamps the AUTHOR's
# own name into every post, so a bare-name match on an already-addressed directive makes an agent
# honor work assigned to someone else (g-115-8827). Either tag form may be bare or
# @ENVIRONMENT_ID-qualified (g-115-4188); compare component-wise, never as a prefix (guard-2860).
# Canonical implementation — import it, do not re-derive it: peer_surface.routing_tag_targets_agent,
# which goal-selector.py emit_directive_honor_banner calls, so both enforcement points agree.
# Each of a directed directive's target:{goal-id} tags names a goal this agent must prioritize.
# Rationale (WHY the addressee set is exclusive, the @-qualified table, the precedence example):
# core/config/rationale/directive-honor-addressee-set.md
FOR EACH directive in all_directives WHERE it is directed at this agent (steps 1-3 above)
        AND still active:
    FOR EACH "target:{goal-id}" tag: directive_targeted_goals[goal-id] = directive.id

# ── DIRECTIVE-HONOR HARD RULE (guard-1310 — the residual LLM-path gap) ──
# The selector's directive_boost (+3.0, criterion 13b) already ranks a directed goal near
# the top, but that boost is a SILENT scoring nudge — the LLM selection path (lane
# discipline / self-abstention / focus judgment in Phase 2.5–2.55) can still pass over it.
# On 2026-07-20 a USER DIRECTIVE targeting zeta (g-315-390, +2.0) was lane-skipped as
# "Echo's ARC lane" across 5+ selections over 8h; directive_boost pushed it to #1/#2 every
# time and the LLM skipped it anyway (0 acks, 0 read-receipts) — then re-committed the
# identical skip live DURING the g-115-2797 investigation. THEREFORE:
IF directive_targeted_goals is non-empty AND any of its goal-ids is in ranked_goals:
    honored = highest-scored ranked_goals entry whose id is in directive_targeted_goals
    Output: "▸ ⚠ DIRECTIVE-HONOR REQUIRED: {honored.id} is targeted by directive
      {directive_targeted_goals[honored.id]} directed at YOU (score {honored.score}). A
      directive-targeted goal MUST NOT be passed over by lane / focus / consolidate /
      self-abstention judgment — a directive IS the tasking (self.md 'unless tasked' is
      satisfied). SELECT {honored.id} now, OR post a justified-deferral ack (--reply-to the
      directive) naming a HARD blocker (infra-blocked / genuine capability gap — NOT
      lane/focus). A silent lane-skip is FORBIDDEN (guard-1310)."
    # Overrides the lane-discipline / consolidate-before-expand preference for THIS pick
    # only. It does NOT override a genuine blocker or real capability gap — those get a
    # justified-deferral ack, not a silent skip. Unless such an ack is posted, set
    # selected_goal = honored and skip the remaining lane/focus reasoning for this pick.

# Insight trigger scan (cross-agent findings that affect our goals)
Bash: board-read.sh --channel findings --type finding --since 24h --json
FOR EACH finding WITH "insight_trigger" in tags:
    Parse severity from tags (severity:invalidates|constrains|enables|informs)
    Parse affected goals from tags (affects:<goal-id>)
    Parse required action (requires_action_by:<agent>, action_type:<type>)

    IF requires_action_by does not match this agent: SKIP
    IF already processed (check for reply from this agent): SKIP

    # Stale-trigger guard (g-115-2969 / rb-4860): a trigger whose affects:<goal-id>
    # target has ALREADY gone terminal is moot — acting on it decides about
    # finished work AND can clobber the completer's outcome_note (incident
    # g-335-94: a stale zeta trigger for g-335-94, which completed ~1 min after
    # the post, drove a moot select decision + overwrote bravo's outcome_note).
    # Mirrors insight-trigger-sweep audit_stale (rb-1150) into the LLM
    # select-path. For each affected goal-id, read its current status
    # (Bash: aspirations-read.sh --source <world|agent> --id asp-<NNN>, then find
    # the goal by id — the aspiration is the g-<NNN>-* prefix). Filter to
    # NON-terminal affected goals; a goal whose status is completed/skipped/
    # expired is terminal.
    affected_live = [gid for gid in affected_goals if status(gid) not in ("completed","skipped","expired")]
    IF affected_live is empty:
        Output: "▸ INSIGHT TRIGGER (STALE): all affected goal(s) {affected_goals} already terminal — ack-as-stale, no action (g-115-2969)"
        Acknowledge: echo "Stale insight trigger — affected goal(s) {affected_goals} already terminal at scan time; no action taken (g-115-2969)" | board-post.sh --reply-to {finding.id}
        SKIP this finding
    # else: act only on affected_live below (moot terminal goals are excluded from
    # the investigate/constrain actions).

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
# --goal is REQUIRED here and is NOT optional decoration (g-115-3466).
# retrieve.sh records the consult into agents/<agent>/session/retrieval-session.json
# only when it can name a goal. Absent --goal it falls back to
# retrieve.py::_infer_in_flight_goal_id(), which reads team-state in_flight — and
# THIS phase runs during SELECTION, strictly BEFORE the Phase 4 claim that sets
# in_flight. So the infer can only ever resolve to null (no write at all) or to the
# PREVIOUS goal (a write credited to the wrong goal). Either way the consult that
# did happen is invisible for the goal it was performed for, and
# pre-apply-consult-drift-gate.py — which keys on `retrieval-summary: performed=false`
# for framework-deep closes — advances its streak on a COMPLIANT close. An
# enforcement layer that cannot see the compliance it demands punishes the compliant
# and trains the agent to distrust the sentinel, which is the same drift-to-miss the
# gate exists to stop.
# MEASURED 2026-07-27 (mtime discriminator, alpha/cc-04): in_flight populated + no
# --goal DOES write (so the infer leg itself is sound — the defect is this call
# site's ordering, not the fallback); in_flight null + no --goal does NOT write;
# --read-only suppresses the write in all cases. The sibling call site in
# code-review-protocol.md step 4 needs NO change: it runs inside Phase 4, after the
# claim, where infer resolves correctly.
# Caveat: on a sanctioned deviation the executed goal may differ from
# ranked_goals[0], so this pre-write can name a candidate that is not ultimately
# claimed. That is strictly better than naming the previous goal or nothing, and the
# Phase 4 goal-execution retrieval (which passes --goal explicitly) corrects it.
Bash: retrieve.sh --category "{top_goal.category} {top_goal.title[:60]}" --depth shallow --goal {top_goal.goal_id}

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
       Consolidation is handled by the scorer — the scorer ranking IS the consolidation
       assessment (completion_pressure + tail_bonus + depth_bonus + streak_momentum +
       context_coherence already meter completion pull, ~4.3 weight-points). Do NOT apply
       additional consolidation pressure as a per-pick veto over the ranking; trust
       ranked_goals[0]. (The guard-1310 DIRECTIVE-HONOR hard rule in Phase 2.07 still
       governs directive-targeted picks — that is a directive obligation, not consolidation.)
   IF selected goal is from a stalled aspiration (consolidation_health.stalled > 0):
       Consider effort_level = "full" (invest deeply to unstall, not skim) — this governs
       HOW to execute, not WHICH goal to pick.

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
# DIRECTIVE EXEMPTION (guard-1310, g-115-2797): a goal in directive_targeted_goals
# (Phase 2.07 — directed at THIS agent) is NOT abstainable on lane/focus grounds. Only a
# GENUINE capability gap justifies deferral, and it must be a justified-deferral ack
# (--reply-to the directive naming the hard blocker), NEVER a silent abstain. "Focus
# mismatch" / "not my lane" is satisfied by the directive itself — do not abstain on it.
# CROSS-AGENT-ROUTED EXEMPTION (g-115-2940): a candidate with routed_to_me==true
# (source='cross-agent:<owner>') is BY CONSTRUCTION routed to THIS agent —
# collect_cross_agent_candidates' strict-match contract only pulls goals where
# intended_agent==agent_name. So it IS your work, not "someone else's goal": a
# 'cross-agent'/not-my-lane/'focus mismatch' abstention on it is ALWAYS wrong (the
# exact bug this goal fixes — bravo abstained 13x from its own HIGH-routed g-001-339,
# reading source='cross-agent:alpha' as "alpha's goal"). Do NOT set abstained_by or
# deviation=cross-agent on a routed_to_me candidate. Only a GENUINE capability gap
# (the goal needs a skill outside your "What I Do") justifies deferral — and then via
# defer_reason naming the hard gap, never a lane abstention. The intended_agent
# stamp IS the tasking.
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

# THEN READ, IN THIS ORDER, BEFORE ANY SCOPE REASONING:
#   goal.outcome_note · goal.outcome_notes (PLURAL — a real second field, guard-3512) · goal.progress_note
# Live population, asp-335 (bravo, hostname cc-05, uname -r 6.8.0-137-generic,
# 2026-08-13): outcome_note on 520 of 1212 goals, progress_note on 13,
# outcome_notes on 2. This is not a rare field — it is present on 43% of the
# aspiration, so a reader who skips it is skipping the handoff by default.
#
# WHY THIS LIST AND NOT THE ONE TWO LINES ABOVE. The comment above names
# "description and verification" twice, and that framing IS the defect: it tells
# the reader what matters, and the field recording what has ALREADY BEEN BUILT
# is not in it. `status: pending` does NOT mean unstarted — under the Mind/Body
# split a worker finishes a goal and hands it back pending, because verify is a
# reducer-only phase (guard-2803).
#
# THE COST LANDS HERE, ONE PHASE BEFORE guard-2803's OWN TRIGGER. That guardrail
# fires after aspirations-claim.sh returns, which is correct and still too late:
# selection is where "this goal is bigger than it says" gets decided, and that
# decision is made from the description while the answer sits unread in the same
# record. Three occurrences of exactly that, escalating, and guard-2803 was
# already written and active (times_active 763) for all three:
#   2026-08-05 g-335-818  (bravo) caught AT claim — worked as designed
#   2026-08-13 g-335-1173 (alpha) ~15 min re-deriving scope already written down
#   2026-08-13 g-335-1201 (bravo) FULL duplicate implementation of a partner's
#                                 open PR (#193 vs #194), merged before discovery
# A guardrail cannot outvote the instrument it guards (guard-1984), which is why
# the fix is these lines and not a fourth guardrail.
#
# TELL, and it is counter-intuitive: a re-derived conclusion arriving CORRECT is
# not reassurance — it is the signature. It matched because it was already
# recorded. In g-335-1201 two independent implementations converged on
# byte-compatible wire formats, which read as strong validation of the design and
# was ALSO the proof that one of them never needed writing.
```

## Phase 2.94: Scorer-Divergence Deviation Code (Scorer Sovereignty Layer B, g-115-2812)

The claim chokepoint (`scorer-verdict-gate.py`, invoked inside
`aspirations-claim.sh`) REFUSES a world-goal claim that diverges from the
scorer's fresh top pick unless a `--deviation <code>` names the sanctioned
reason. `goal-selector.py` writes the verdict sidecar (`top_goal_id` =
`ranked_goals[0]`); compute `deviation_code` here so Phase 4 can forward it.
This is a single-point computation (compare the finalized selection to the
scorer top) — NOT a variable threaded through the divergence phases above — so
a sanctioned divergence can never silently reach the claim without a code.

```
IF goal is None:
    deviation_code = ""          # no claim will happen
ELIF ranked_goals is empty OR goal.goal_id == ranked_goals[0].goal_id:
    deviation_code = ""          # HAPPY PATH: claiming the scorer's top pick — no flag needed
ELSE:
    # Selection diverged from the scorer top via a sanctioned phase above. Set
    # the enum code matching the phase that caused THIS divergence (the gate
    # ALLOWS any valid code — the code is for Layer C audit granularity, not
    # correctness, so an approximately-right code still passes; picking NO code
    # or an invalid one is the only failure):
    #   first-action     — First-Action Override (first-iteration handoff)
    #   partner-claim    — partner-claim filter dropped the scorer top
    #   guardrail-forbids — Phase 2.27 guardrail forbade the top goal now
    #   self-abstention  — Phase 2.55 abstained past the top goal
    #   blocker-gate     — Phase 2.5b blocker/infra probe skipped the top goal
    #   precondition-fail — Precondition Gate removed the top goal
    #   meta-tiebreaker  — Phase 2.05/2.07 meta re-sort chose a different top
    #   cross-agent      — deliberately claiming a cross-lane / foreign-world goal
    #   no-goals-rebound — the scorer top is gone from the live queue; rebounded
    #   force-override   — explicit last-resort escape hatch (audited)
    deviation_code = <the enum code matching the sanctioned path that diverged>
    Output: "▸ SCORER-DIVERGENCE: claiming {goal.goal_id} over scorer top {ranked_goals[0].goal_id} — deviation={deviation_code}"
```

Emit `deviation_code` as a Phase output (see Outputs). Only world-goal claims
consult it; agent-queue goals are single-agent and never claim.

## Phase 2.95: Anchor Selection for Autocompact Resilience

Write an iteration checkpoint with the selected goal id. Survives autocompact
so `postcompact-restore.py` can tell the model exactly which goal was picked —
prevents post-compact goal-substitution drift (bug traced 2026-04-22 alpha
session-56: pre-compact selected g-115-22, post-compact resumed as /decompose
g-250-13 because the compact summary reconstructed the wrong in-flight goal).

Cleared in `/aspirations-execute` Phase 8 on goal completion and in
`/start --recover` / `aspirations-graceful-stop` D6.

**This phase is NO LONGER the only anchor for EITHER source (g-115-3590; the
`source == agent` half was still true when that was written and stopped being
true at g-306-249 — see the DECIDED block below, which is the current answer).**
`aspirations-claim.sh`
`_post_claim_effects` now anchors the checkpoint itself on every rc=0 claim, so
a loop that selects by calling `goal-selector.sh` directly instead of invoking
this skill still gets a world-goal anchor — that drift was the measured cause of
101 `update_against_missing_checkpoint` rows on one box. The claim chokepoint
uses ENSURE semantics (writes only when no checkpoint exists or it names a
DIFFERENT goal), so when this phase has already run its RICHER anchor
(`selector_score`, `skill`, `cross_agent_owner`) survives untouched.

**DECIDED, do not re-derive (g-306-249): this phase STAYS, and the claim
chokepoint is the NET — they do not half-own the invariant.** The reason above
has changed and the conclusion has not, which is exactly the case worth writing
down. Agent-queue goals used to be unable to reach the chokepoint at all: the
daemon refused them `400 agent_queue_goal` "by design" and the loop digest
guarded the claim with `IF source==world`. Both are now false — g-306-238 landed
`&source=agent` (LIVE-probed from cc-02 2026-08-07: `id=<absent>&source=agent`
answers "not found in AGENT queue", `source=bogus` answers 400 `invalid_source`)
and g-306-249 dropped the digest guard. So the chokepoint DOES anchor agent-queue
goals now, and this phase is no longer the only init site for `source == agent`.

It stays because it is the SOLE WRITER of two fields the chokepoint cannot know:
`cross_agent_owner` and `selector_score`/`skill`. `cross_agent_owner` is derived
HERE, from `goal.source.startswith('cross-agent:')`, and the source is translated
to plain `'agent'` before any claim runs — so by the time `_post_claim_effects`
sees it, the owner is unrecoverable. Phase 4's claim block READS that field back
to env-prefix the owner's writes. A cross-agent goal whose anchor lost
`cross_agent_owner` writes its state into the WRONG agent's tree, silently.

The split is therefore: **this phase owns the anchor's CONTENT; the chokepoint
owns its EXISTENCE** (ENSURE semantics — writes only when absent or naming a
different goal, so this phase's richer anchor survives). Retiring this phase
requires moving `cross_agent_owner` into the claim first, not merely observing
that the claim now anchors.

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
      # otherwise emit the base shape. loop-state-save.py validates known
      # keys, WARNS+drops unknown ones; the shape stays checked.
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
