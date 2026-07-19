---
name: aspirations-complete-review
description: "Reviews aspirations approaching completion: sweeps remaining goals for outstanding work, checks motivation fulfillment, runs the maturity gate (Phase 7.4 intent satisfaction), decides archival or continuation, and creates replacement aspirations. Use whenever an aspiration reaches high completion_ratio, the zombie scan (aspirations-precheck Phase 0.5.0a) surfaces high-completion-stale-blocked aspirations, or an aspiration's last executable goal finishes."
user-invocable: false
parent-skill: aspirations
conventions: [aspirations, goal-schemas, experience]
minimum_mode: autonomous
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-aspirations-complete-review-2cea10"
previous_revision_id: null
---

# /aspirations-complete-review — Aspiration Completion Review

Invoked by the aspirations orchestrator when an aspiration has ALL non-recurring goals completed.
Sweeps goal outcomes for outstanding work, checks motivation fulfillment, and decides whether
to archive or reopen the aspiration with new goals.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Inputs (from orchestrator)

- `asp`: The completing aspiration object (from compact loader)
- `goal`: The goal that triggered completion (last completed goal)
- `goals_completed_this_session`: Counter for maturity check
- `source`: Queue origin (`"world"` or `"agent"`) — pass `--source {source}` to all `aspirations-*.sh` calls

## Outputs (to orchestrator)

- `goals_added_to_completing_asp`: Count of new goals added (if > 0, aspiration is reopened)
- `should_archive`: Boolean (true if aspiration should be archived)

## Phase 7: Aspiration-Level Check

Re-read the goal's parent aspiration via compact loader.
GUARD: Aspirations containing ANY recurring goals cannot be archived — the data layer
(`aspirations-complete.sh`) will block it. Check here to avoid wasted work.

```
asp = get_aspiration(goal)
has_recurring = any(g.get("recurring", False) for g in asp.goals)

# ── Functionally Complete Path ──
# An aspiration with recurring goals where ALL non-recurring goals are done.
# The aspiration's original PURPOSE has been achieved — only monitoring remains.
# We do NOT archive (data layer blocks it), but we DO generate replacement work.
if has_recurring and functionally_complete:
    # Guard: only fire once per aspiration (prevent repeated triggering)
    IF asp.functionally_complete_at is not null:
        RETURN (should_archive = false, goals_added = 0)  # already processed
    # ASPIRATION-FIELD setter, NOT aspirations-meta-update.sh (g-115-2592):
    # meta-update takes exactly <key> <value> — passing {asp.id} as a positional
    # made it write a spurious meta key `{asp.id}: functionally_complete_at` and
    # silently DROP the timestamp, so the field was never set and precheck-eval
    # re-flagged the aspiration as an all_terminal zombie every iteration.
    Bash: aspirations-update.sh --source {source} {asp.id} functionally_complete_at "$(date +%Y-%m-%dT%H:%M:%S)"

    Output: "▸ Functionally complete: {asp.id} '{asp.title}' — all non-recurring goals done, {recurring_count} recurring continue"
    run_aspiration_spark(goal.aspiration)
    invoke /create-aspiration from-self --plan with:
        replacement_context: "Aspiration '{asp.title}' is functionally complete — all non-recurring goals done. Recurring goals continue as monitoring. Generate a NEW aspiration that advances the agent's purpose in a fresh direction, informed by the completed aspiration's outcomes."
    RETURN (should_archive = false, goals_added = 0)  # keep alive for recurring, but replacement was triggered

# ── Standard recurring guard ──
if has_recurring: RETURN (should_archive = false, goals_added = 0)  # not yet functionally complete
# Note: the `aspiration_fully_complete(asp)` gate now lives AFTER Phase 7.4 so
# zombie aspirations (mostly complete, trailing blocked goals) can reach it.
```

## Phase 7.4: Intent-Satisfaction Pre-Gate

Rescues zombie aspirations — those where `motivation` was achieved by completed goals but
trailing goals are stuck on external blockers and will likely never close. Runs BEFORE the
standard `aspiration_fully_complete` gate so zombies can reach it. Exits quickly for every
non-zombie case.

```
terminal_statuses = {"completed", "skipped", "expired", "decomposed", "superseded"}
unfinished = [g for g in asp.goals if not g.recurring and g.status not in terminal_statuses]

# Fast path: no unfinished work — Phase 7.5 handles fully-complete aspirations
IF not unfinished:
    SKIP Phase 7.4  # fall through to Phase 7's fully-complete gate

# Trigger gate 1: all unfinished must be blocked (not merely pending/in-progress)
IF any(g.status != "blocked" for g in unfinished):
    RETURN (should_archive = false, goals_added = 0)  # standard work remains — not a zombie

# Trigger gate 2: blockers must be stale. Config is single source of truth — fail loud if missing.
Bash: python3 -c "import yaml; cfg=yaml.safe_load(open('core/config/aspirations.yaml')); print(cfg['intent_satisfaction']['phase_7_4_min_blocked_hours'])"
min_blocked_hours = parsed int from stdout  # non-zero exit aborts the skill
now = datetime.now()
IF any(
    g.blocked_since is null OR
    (now - parse_iso(g.blocked_since)).total_seconds() / 3600 < min_blocked_hours
    for g in unfinished
):
    RETURN (should_archive = false, goals_added = 0)  # blockers too fresh — let them try to resolve

# At this point: aspiration has only blocked-and-stale trailing goals.
# LLM judgment: was motivation met by completed goals?

Read asp.motivation.
completed_goals = [g for g in asp.goals if g.status == "completed"]

# LLM evaluates:
#   - Does the set of completed goal verification.outcomes collectively fulfill motivation?
#   - If yes: identify which completed goals are the EVIDENCE (>=3 typically, scope-dependent).
#   - If no: skip; not an intent-satisfied zombie.

IF motivation_NOT_met_by_completed:
    Output: "▸ Phase 7.4: {asp.id} has {N} blocked goals but motivation not yet met by completed work — keeping active"
    RETURN (should_archive = false, goals_added = 0)

# Build intent_satisfaction block
evidence_goal_ids = [LLM-selected subset of completed_goals, with scope-aware minimum]
superseded_goal_ids = [g.id for g in unfinished]  # all blocked-and-stale goals

# Rationale: MUST quote at least one token from motivation, MUST be >=40 chars,
# MUST explain which completed-goal outcomes map to motivation and why trailing goals are moot.
rationale = "{motivation-quoting explanation of how evidence goals fulfilled the motivation
              and why the {len(superseded)} trailing blocked goals are no longer necessary}"

intent_block = {
    "evidence_goal_ids": evidence_goal_ids,
    "rationale": rationale,
    "superseded_goal_ids": superseded_goal_ids,
}

# Attempt the intent-satisfied closure. Script validates evidence cardinality, quality,
# rationale length, token overlap, and post-supersession closure.
Bash: echo '{intent_block_as_json}' | aspirations-complete-intent.sh --source {source} {asp.id}

IF exit 0:
    Output: "▸ Phase 7.4: {asp.id} closed via intent satisfaction ({len(evidence)} evidence, {len(superseded)} superseded)"
    PROCEED to Phase 7.5.9 (learning emission)
ELSE:
    # Validation failed. Evidence insufficient, rationale didn't quote motivation, etc.
    # Do NOT force. Surface to user via open-questions for human review.
    Read agents/<agent>/session/pending-questions.yaml
    Append a question: "Phase 7.4 wanted to intent-close {asp.id} but validation failed: {stderr}.
                       Review evidence and either tighten rationale, complete trailing goals,
                       or confirm retirement."
    Output: "▸ Phase 7.4: {asp.id} rejected by evidence gate — escalated to user"
    RETURN (should_archive = false, goals_added = 0)
```

### Phase 7 (continued): Standard fully-complete gate

```
if not aspiration_fully_complete(asp): RETURN (should_archive = false, goals_added = 0)

run_aspiration_spark(goal.aspiration)
```

## Phase 7.5: Aspiration Completion Review

Before archival, sweep ALL goal outcomes for outstanding work.

Output: "▸ Completion Review: scanning {asp.id} goals for outstanding work..."
goals_added_to_completing_asp = 0

### Step 7.5.1: Gather and Scan Goal Data

```
outstanding_findings = []

FOR EACH g in asp.goals:
    IF g.recurring: continue

    # Skipped/expired = planned work that never happened
    IF g.status in ("skipped", "expired"):
        outstanding_findings.append({
            type: "abandoned_goal", goal_id: g.id, title: g.title,
            description: g.description, match: g.title,
            priority: g.priority or "HIGH", category: g.category
        })
        continue

    # Load experience entry for this goal
    exp_result = Bash: experience-read.sh --goal {g.id}
    IF exp_result is empty:
        IF g.verification and g.verification.outcomes:
            outcomes_text = join(g.verification.outcomes)
            IF outcomes_text matches (not yet|partial|remaining|deferred|TODO):
                outstanding_findings.append({
                    type: "partial_completion", goal_id: g.id, title: g.title,
                    match: extracted_reference, priority: "MEDIUM", category: g.category
                })
        continue
```

### Step 7.5.2: Keyword Scan with Negative Filters

Scan experience summaries for outstanding work signals. Negative filters prevent false positives.

```
FOR EACH exp in exp_result:
    scan_text = exp.summary
    signals = []

    # Signal families (must NOT be followed by resolution keywords):
    # unresolved_root_cause: (root cause|caused by|due to) NOT (fixed|resolved|applied)
    # unfixed_bug: (bug|defect|mismatch|incorrect) NOT (fixed|resolved|patched)
    # proposed_change: (should be changed|needs to be|replace with|TODO) NOT (done|completed)
    # explicit_followup: (follow-up|next step|remaining|outstanding|deferred)
    # unacted_idea: (could also|might benefit|worth exploring|opportunity)
    # unimplemented_action: (needs|requires|must) + (to be|updating|fixing) NOT (done|completed)

    IF signals found AND exp.content_path exists:
        Read content for richer match extraction

    FOR EACH signal: append to outstanding_findings with source_experience
```

### Step 7.5.2b: Motivation Fulfillment Check

```
Read asp.motivation. Given completed goals and outcomes:
  FULFILLED: Every claim addressed, no natural next steps remain.
  NOT FULFILLED: Motivation broader than goals, or depth remains.

IF NOT fulfilled AND aspiration had < 10 completed goals:
    Generate 1-3 follow-up goals advancing the motivation
    Each goal MUST set origin_signal: "parent_aspiration:{asp.id}" (motivation-driven follow-up)
    Add via: aspirations-add-goal.sh --source {source} <asp.id>
    goals_added_to_completing_asp += count
    Output: "▸ Motivation check: not yet fulfilled — added {count} goal(s)"
ELSE:
    Output: "▸ Motivation check: fulfilled"
```

### Step 7.5.3: Early Exit

```
IF len(outstanding_findings) == 0 AND goals_added_to_completing_asp == 0:
    Output: "▸ Completion Review: no outstanding work — clean completion"
    # Fall through to maturity check
```

### Steps 7.5.4-7.5.5: Dedup and Route Findings

```
Bash: load-aspirations-compact.sh → Read
existing_titles = all pending/in-progress/completed goal titles

deduplicated = [f for f in outstanding_findings if not similar_title_exists(f)]

IF deduplicated:
    FOR EACH finding:
        # Priority: abandoned_goal/root_cause/bug → HIGH; others → MEDIUM
        # Title: abandoned → original; root_cause/bug → "Unblock: Fix {match}"
        #        others → "Idea: {match}"; partial → "Idea: Complete {title}"
        # origin_signal: title-prefix determines prefix —
        #   "Unblock:" → "unblock:completion-review-{asp.id}"
        #   "Idea:"   → "idea:completion-review-{asp.id}"
        #   abandoned (no prefix) → "parent_aspiration:{asp.id}"
        # goal_json MUST set origin_signal per mapping above.

        # Route: A) fits completing asp → add here, B) fits another → add there
        #         C) new work → /create-aspiration with context
        echo '<goal_json>' | aspirations-add-goal.sh --source {source} <target_asp>
```

### Steps 7.5.6-7.5.8: Notify, Journal, Archival Gate

```
IF new goals created:
    Notify the user about the completion review.
    (Check world/forged-skills.yaml for a skill whose triggers match
    "notify the user" and invoke it with:
      subject: "Aspiration Completion Review: {asp.title}"
      message: finding counts + goals added per aspiration + asp.id
    If no matching skill is registered, fall back to a `participants: [agent, user]`
    goal via aspirations-add-goal.sh. Never block completion review on
    notification failure.)
    Journal: findings count, dedup count, goals added per aspiration

IF goals_added_to_completing_asp > 0:
    Output: "▸ Completion Review: {asp.id} reopened with {N} new goal(s)"
```

### Step 7.5.9: Learning Emission (Intent-Satisfied Closures Only)

Runs ONLY when Phase 7.4 successfully closed the aspiration via `aspirations-complete-intent.sh`.
Captures the pattern for later retrieval — over time, the reasoning-bank accumulates procedural
priors like "outcome-metric motivations need ~40% of planned goals to fulfill."

```
# Gather the intent_satisfaction block written by cmd_complete
archived = Bash: aspirations-read.sh --archive --id {asp.id}  # find in archive
intent = archived.intent_satisfaction  # {claimed_at, evidence_goal_ids, rationale, superseded_goal_ids}

evidence_goals = [g for g in archived.goals if g.id in intent.evidence_goal_ids]
superseded_goals = [g for g in archived.goals if g.id in intent.superseded_goal_ids]

evidence_categories = Counter(g.category for g in evidence_goals if g.category)
superseded_categories = Counter(g.category for g in superseded_goals if g.category)

content = f"""
Aspiration '{asp.title}' ({asp.scope}) closed via intent satisfaction.
Motivation: {asp.motivation}

Evidence: {len(evidence_goals)} completed goals ({len(evidence_goals) / len(archived.goals) * 100:.0f}% of planned)
  Categories: {dict(evidence_categories)}
Superseded: {len(superseded_goals)} goals mooted by aspiration outcome
  Categories: {dict(superseded_categories)}

Effective/planned scope ratio: {len(evidence_goals)} / {len(archived.goals)}

Rationale: {intent.rationale}
"""

Bash: echo '<reasoning-bank JSON>' | reasoning-bank-add.sh
  # type: success
  # title: "Intent satisfaction pattern: {first 40 chars of asp.motivation}"
  # category: "meta-strategy"
  # content: <content above>
  # when_to_use: "When an aspiration with similar motivation shape has trailing blocked goals"
  # applies_to: any  # REQUIRED. Intent-satisfaction is a methodological pattern that
  #                  # transfers across domains — same shape applies regardless of
  #                  # what the aspiration was about.
  # tags: ["intent-satisfaction", "meta-strategy", asp.scope, ...evidence_categories]

Output: "▸ Learning emitted: reasoning-bank entry captures evidence/superseded split"
```

## Phase 7.6: Maturity Check (with Trajectory Analysis)

```
scope = asp.scope or "project"
min_sessions_map = {"sprint": 1, "project": 2, "initiative": 4}
min_sessions = min_sessions_map.get(scope, 2)
sessions_active = asp.sessions_active or 0

# AVO-inspired trajectory convergence check: use learning velocity to inform
# maturity decision. An aspiration with high velocity shouldn't be archived
# just because it met a session count. An aspiration with zero velocity
# shouldn't be deepened just because it hasn't met a session count.
Bash: aspiration-trajectory.sh {asp.id}
trajectory = parse JSON output

IF goals_added_to_completing_asp == 0:
    IF trajectory.current_velocity > 0.5 AND scope != "sprint":
        # Still producing significant learning — deepen regardless of session count
        Output: "▸ Maturity: {asp.id} still producing learning (velocity {trajectory.current_velocity:.2f}) — deepening."
        # Generate 3-5 deeper follow-up goals informed by trajectory gaps
        # Use trajectory.inflection_points to identify what directions worked best
        goals_added_to_completing_asp += N
    ELIF sessions_active < min_sessions AND scope != "sprint":
        Output: "▸ Maturity: {asp.id} completing after {sessions_active} session(s) — deepening."
        # Generate 3-5 deeper follow-up goals (web search + tree consult)
        goals_added_to_completing_asp += N
    ELIF trajectory.plateau_detected:
        Output: "▸ Maturity: {asp.id} — learning plateaued, clean archival."
        # Plateau confirms this aspiration has run its course
```

## Archival Decision

```
IF goals_added_to_completing_asp == 0:
    Bash: aspirations-complete.sh --source {source} <asp-id>
    invoke /create-aspiration from-self --plan
    RETURN (should_archive = true, goals_added = 0)
ELSE:
    RETURN (should_archive = false, goals_added = goals_added_to_completing_asp)
```

## Chaining

- **Called by**: `/aspirations` orchestrator (when aspiration fully completes)
- **Calls**: `experience-read.sh`, `aspirations-add-goal.sh --source`, `aspirations-complete.sh --source`, `aspiration-trajectory.sh`, `/create-aspiration`, user notification
- **Reads**: Aspiration data (compact), experience entries, knowledge tree (for motivation check), trajectory data (for maturity gate)
- **Source routing**: All `aspirations-*.sh` calls receive `--source {source}` from the orchestrator input

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the last `aspirations-complete.sh` or `aspirations-add-goal.sh`
invocation. Never end with a text summary of the review.
