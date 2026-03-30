# Completion Check Runners (Phase 0)

Run BEFORE goal selection to auto-detect completed goals.
Checks `verification.checks` (new) or `completion_check` (legacy).

## File Existence Checks
```
For each goal with verification.checks containing type "file_check"/"file_exists":
    if goal.recurring: skip
    path = extract path from check
    if file exists at path:
        mark goal completed
        log "Auto-completed {goal.id}: file {path} exists"
```

## Pipeline Count Checks
```
For each goal with verification.checks referencing pipeline counts:
    if goal.recurring: skip
    Bash: pipeline-read.sh --counts
    if count meets threshold:
        mark goal completed
        log "Auto-completed {goal.id}: pipeline count threshold met"
```

## Config State Checks
```
For each goal with completion_check referencing config fields:
    if goal.recurring: skip
    read relevant config file
    if field matches expected value:
        mark goal completed
```

## Readiness Gate Checks
```
Check each readiness gate (from aspirations-read.sh --meta):
    Gates are domain-specific and added dynamically.
    Example: knowledge_base_seeded, first_hypothesis_formed
Bash: aspirations-meta-update.sh readiness_gates '<JSON>'
```

## Recurring Goal Safety Net
```
# Primary: Phase 5 resets recurring goals to pending after completion.
# Safety net: if status stuck at "completed" (interrupted session), reset.
For each goal with recurring: true AND status: completed:
    reset status to "pending"
    log "Recurring goal {goal.id} reset to pending (was stuck at completed)"
```

## Hypothesis Expiration Checks
```
For each goal with hypothesis_id AND (status: pending OR in-progress):
    if now > goal.resolves_by:
        mark status = "expired"
        move pipeline file to archived/ with reason: "expired_past_deadline"
        update archived/_index.yaml
```
