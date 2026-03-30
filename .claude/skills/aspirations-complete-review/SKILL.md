---
name: aspirations-complete-review
description: "Aspiration completion review — cross-goal sweep for outstanding work, motivation fulfillment check, maturity gate, archival decision, replacement creation"
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

## Outputs (to orchestrator)

- `goals_added_to_completing_asp`: Count of new goals added (if > 0, aspiration is reopened)
- `should_archive`: Boolean (true if aspiration should be archived)

## Phase 7: Aspiration-Level Check

Re-read the goal's parent aspiration via compact loader.
GUARD: Aspirations where ALL goals are recurring can never "complete" — they're perpetual.

```
asp = get_aspiration(goal)
all_recurring = all(g.get("recurring", False) for g in asp.goals)
if all_recurring: RETURN (should_archive = false, goals_added = 0)  # perpetual
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
    Add via: aspirations-add-goal.sh <asp.id>
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

        # Route: A) fits completing asp → add here, B) fits another → add there
        #         C) new work → /create-aspiration with context
        echo '<goal_json>' | aspirations-add-goal.sh <target_asp>
```

### Steps 7.5.6-7.5.8: Notify, Journal, Archival Gate

```
IF new goals created:
    notify user: "Aspiration Completion Review: {asp.title}" with finding counts
    Journal: findings count, dedup count, goals added per aspiration

IF goals_added_to_completing_asp > 0:
    Output: "▸ Completion Review: {asp.id} reopened with {N} new goal(s)"
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
    Bash: aspirations-complete.sh <asp-id>
    invoke /create-aspiration from-self --plan
    RETURN (should_archive = true, goals_added = 0)
ELSE:
    RETURN (should_archive = false, goals_added = goals_added_to_completing_asp)
```

## Chaining

- **Called by**: `/aspirations` orchestrator (when aspiration fully completes)
- **Calls**: `experience-read.sh`, `aspirations-add-goal.sh`, `aspirations-complete.sh`, `aspiration-trajectory.sh`, `/create-aspiration`, user notification
- **Reads**: Aspiration data (compact), experience entries, knowledge tree (for motivation check), trajectory data (for maturity gate)
