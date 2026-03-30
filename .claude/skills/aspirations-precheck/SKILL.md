---
name: aspirations-precheck
description: "Pre-selection checks — completion runners, aspiration health, guardrail checks, blocker resolution, recurring goals"
user-invocable: false
parent-skill: aspirations
conventions: [aspirations, infrastructure, goal-schemas]
minimum_mode: autonomous
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
---

# /aspirations-precheck — Pre-Selection Checks

Runs all checks that must happen BEFORE goal selection each iteration.
Ensures completion runners fire, aspiration health is maintained,
guardrails are checked, blockers are resolved, and recurring goals are tracked.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter.

## Inputs (from orchestrator)

- Aspirations compact data (loaded at loop entry or refreshed here)

## Outputs (to orchestrator)

- Compact aspirations refreshed in context
- Blockers updated in working memory
- Any auto-completed goals logged

## Phase 0: Automated Completion Checks

Run completion check runners to auto-detect completed goals.

### File Existence Checks
For each goal with `verification.checks` containing `type: "file_check"`:
- If `goal.recurring`: skip
- If file exists at path: mark goal completed, log

### Pipeline Count Checks
For each goal referencing pipeline counts:
- If `goal.recurring`: skip
- `Bash: pipeline-read.sh --counts` — if threshold met: mark completed

### Config State Checks
For each goal referencing config fields:
- If `goal.recurring`: skip
- Read config file, check field value — if matches: mark completed

### Readiness Gate Checks
Check each readiness gate from `aspirations-read.sh --meta`.
`Bash: aspirations-meta-update.sh readiness_gates '<JSON>'`

### Recurring Goal Safety Net
```
For each goal with recurring: true AND status: completed:
    reset status to "pending"
    log "Recurring goal {goal.id} reset to pending (was stuck at completed)"
```

### Hypothesis Expiration Checks
```
For each goal with hypothesis_id AND status pending/in-progress:
    if now > goal.resolves_by:
        mark status = "expired"
        move pipeline file to archived/
```

## Phase 0.5: Aspiration Health Check

```
Bash: load-aspirations-compact.sh → IF path returned: Read it
active_count = count of aspirations with status "active"
if active_count < 2:
    invoke /create-aspiration from-self --plan
    log "Aspiration health: below minimum, created new aspirations"
```

## Phase 0.5a: Pre-Selection Guardrail Check

```
Bash: matched=$(bash core/scripts/guardrail-check.sh --context any --phase pre-selection 2>/dev/null)
IF matched.matched_count > 0:
    FOR EACH guardrail in matched.matched:
        Bash: <run {guardrail.action_hint}>
        IF output reveals issues:
            → invoke CREATE_BLOCKER(affected_skill, issue_description, ...)
```

## Phase 0.5b: Blocker Resolution Check

```
Bash: wm-read.sh known_blockers --json
IF known_blockers is non-empty:
    FOR EACH blocker WHERE resolution is null:
        # PRIMARY: Did unblocking goal complete?
        IF blocker.unblocking_goal completed: resolve

        # SECONDARY: User goal, expiry (3-session), infra-health success
        # ACTIVE REPROBING: probe every iteration
        Bash: result=$(bash core/scripts/infra-health.sh check {component})
        IF status == "ok": resolve
        ELIF status == "provisionable": attempt provisioning
        ELSE: log probe failed

    echo '<updated_blockers_json>' | Bash: wm-set.sh known_blockers
```

## Phase 0.5c: Unproductive Cycle Detection (AVO-inspired)

Inspired by NVIDIA AVO (arXiv:2603.24517) — detects when the agent cycles through
similar failing approaches across different goals within the same aspiration,
without stepping back to question root assumptions.

```
Read core/config/aspirations.yaml → cycle_detection config
lookback_window = cycle_detection.lookback_window (default 3)

Bash: load-aspirations-compact.sh → IF path returned: Read it
FOR EACH active aspiration with >= lookback_window completed/skipped goals:
    recent_goals = last {lookback_window} completed/skipped goals (chronological)
    cycle_detected = false
    cycle_reason = null

    # Check 1: Shared failure pattern — goals failing/skipped repeatedly
    skipped_goals = [g for g in recent_goals if g.status == "skipped"]
    IF len(skipped_goals) >= lookback_window - 1:
        cycle_detected = true
        cycle_reason = "repeated_failure"

    # Check 2: Same-category goals with no capability advancement
    IF NOT cycle_detected:
        categories = [g.category for g in recent_goals]
        IF len(set(categories)) == 1:  # All same category
            Bash: aspiration-trajectory.sh {asp.id}
            trajectory = parse JSON
            IF trajectory.current_velocity == 0:
                cycle_detected = true
                cycle_reason = "zero_learning_velocity"

    IF cycle_detected:
        Output: "▸ CYCLE DETECTED: {asp.id} — {cycle_reason} over last {lookback_window} goals"
        category = trajectory.primary_category if trajectory else recent_goals[0].category
        echo '{"title":"Investigate: Why are we cycling on {asp.title}?","description":"Unproductive cycle detected ({cycle_reason}). Recent goals: {recent_goal_titles}. Apply first-principles thinking: surface assumptions, reduce to verifiable ground truth, rebuild approach from fundamentals.","priority":"HIGH","category":"{category}","participants":["agent"]}' | Bash: aspirations-add-goal.sh {asp.id}
        Log: "CYCLE DETECTION: Created investigation goal for {asp.id} — {cycle_reason}"
```

## Phase 1: Recurring Goal Check

```
check_recurring_goals()
# Ensures recurring goals are properly tracked and due goals are flagged
```

## Chaining

- **Called by**: `/aspirations` orchestrator (every iteration, first phase)
- **Calls**: `aspirations-read.sh`, `aspirations-meta-update.sh`, `guardrail-check.sh`, `infra-health.sh`, `wm-read.sh`, `wm-set.sh`, `aspiration-trajectory.sh` (cycle detection), `aspirations-add-goal.sh` (cycle detection), `/create-aspiration` (health), CREATE_BLOCKER protocol
- **Reads**: Aspirations compact, working memory (blockers), guardrails, trajectory data (cycle detection)
