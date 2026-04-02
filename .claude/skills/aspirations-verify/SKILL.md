---
name: aspirations-verify
description: "Phase 5: Verification — hypothesis outcomes, unified checks, Q1/Q2/Q3 escalation, streak tracking, dependent unblocking"
user-invocable: false
parent-skill: aspirations
conventions: [aspirations, goal-schemas]
minimum_mode: autonomous
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
---

# /aspirations-verify — Goal Completion Verification

Verifies whether a goal achieved its desired end state after execution.
Handles hypothesis goals, unified verification checks, and the Q1/Q2/Q3
structured escalation protocol for goals with empty checks.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter.

## Inputs (from orchestrator)

- `goal`: The executed goal object (with verification field)
- `result`: Execution result (from Phase 4)
- `source`: Queue origin (`"world"` or `"agent"`) — pass `--source {source}` to all `aspirations-*.sh` calls

## Outputs (to orchestrator)

- `goal_completed`: Boolean — did the goal pass verification?
- `aspiration_complete`: Boolean — is the parent aspiration now fully complete?

## Hypothesis Goal Verification

```
if goal.hypothesis_id:
    if result == "CONFIRMED" or result == "CORRECTED":
        if not goal.recurring:
            Bash: aspirations-update-goal.sh --source {source} <goal-id> status completed
        Bash: aspirations-update-goal.sh --source {source} <goal-id> completed_date <today>
        Bash: aspirations-update-goal.sh --source {source} <goal-id> achievedCount <N+1>
        Update recurring streaks if applicable (see Recurring Streak Logic below)
        Unblock dependent goals
    elif result == "EXPIRED":
        Bash: aspirations-update-goal.sh --source {source} <goal-id> status expired
    else:
        # PENDING — hypothesis hasn't resolved yet
        Bash: aspirations-update-goal.sh --source {source} <goal-id> status pending
```

## Unified Verification Checks

```
checks = goal.verification.checks if goal.verification else [goal.completion_check]
```

### Empty-Checks Escalation Protocol (Q1/Q2/Q3)

When `len(checks) == 0`, the agent MUST answer three structured questions:

**Q1 EVIDENCE**: "What concrete artifact (file, output, state change, commit) proves
this goal succeeded?" Must reference a checkable artifact.
- If references concrete artifact: attempt to verify (Read file, check existence)
- If artifact verification fails: `all_passed = false`, status → pending
- If no concrete reference: `all_passed = false`

**Q2 NEGATIVE CHECK** (only when Q1 passes): "What would it look like if this
APPEARED to succeed but actually failed? Did I check for that?"
- HARD GATE: if you CAN name a failure mode but DIDN'T check → check NOW
- If check reveals problem: `all_passed = false`, status → pending
- If vague/empty: soft signal → append `verification_gap` to sensory_buffer

**Q3 INTEGRATION SCOPE**: "Did I verify at the integration level
(caller → target → side effect), or only the unit level?"

### Standard Checks

```
IF len(checks) > 0:
    all_passed = all(check_passes(c) for c in checks)
```

### On Pass

```
if all_passed:
    if not goal.recurring:
        Bash: aspirations-update-goal.sh --source {source} <goal-id> status completed
    Bash: aspirations-update-goal.sh --source {source} <goal-id> completed_date <today>
    Bash: aspirations-update-goal.sh --source {source} <goal-id> achievedCount <N+1>
    Update recurring streaks if applicable
    Unblock dependent goals
```

### On Fail

```
Bash: aspirations-update-goal.sh --source {source} <goal-id> status pending  # retry next cycle
log "Goal executed but verification check failed"
```

## Recurring Streak Logic

Recurring goals NEVER set status to "completed" — they stay "pending".
Goal-selector time gate prevents re-selection until `interval_hours` elapses.

```
interval = goal.interval_hours (fallback: remind_days * 24, default: 24)
elapsed = hours_since(goal.lastAchievedAt)
Bash: aspirations-update-goal.sh --source {source} <goal-id> lastAchievedAt "$(date +%Y-%m-%dT%H:%M:%S)"

if elapsed is not None and elapsed > 2 * interval:
    new_streak = 1  # Missed interval — reset
else:
    new_streak = currentStreak + 1
Bash: aspirations-update-goal.sh --source {source} <goal-id> currentStreak <new_streak>
Bash: aspirations-update-goal.sh --source {source} <goal-id> longestStreak <max(new_streak, longestStreak)>
```

## Chaining

- **Called by**: `/aspirations` orchestrator (Phase 5)
- **Calls**: `aspirations-update-goal.sh --source {source}`
- **Reads**: Goal verification field, hypothesis result
