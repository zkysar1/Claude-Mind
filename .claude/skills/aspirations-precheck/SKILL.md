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

## Phase 0.5.1: Pipeline Depth Check

Proactive starvation prevention — create work BEFORE the pipeline empties.
Uses compact data already loaded in Phase 0.5 (no extra I/O).

```
Read core/config/aspirations.yaml → pipeline_low_water_mark (default 3)

# Count executable goals from compact data
executable_count = 0
completed_ids = set()

# First pass: collect all completed goal IDs (for blocked_by resolution)
FOR EACH active aspiration in compact data:
    FOR EACH goal:
        IF status == "completed": completed_ids.add(goal.id)

# Second pass: count executable goals
FOR EACH active aspiration in compact data:
    FOR EACH goal:
        IF status == "pending"
           AND (deferred_until is null OR deferred_until <= now)
           AND (blocked_by is empty OR all blocked_by IDs are in completed_ids):
            executable_count += 1

IF executable_count < pipeline_low_water_mark:
    Output: "▸ Pipeline thin: {executable_count} executable goals (threshold: {pipeline_low_water_mark})"
    invoke /create-aspiration from-self
    log "Pipeline depth: below low-water-mark ({executable_count} < {pipeline_low_water_mark}), created new work"
```

## Phase 0.5.2: Hypothesis Pipeline Health Check

Proactive hypothesis starvation prevention — ensure the prediction pipeline stays
active. Hypotheses come from sq-009 sparks during goal execution, which only fire on
standard/deep outcomes. If outcomes are mostly routine, hypothesis generation stalls.
This gate detects starvation and creates a review goal that forces prediction formation.

```
Read core/config/aspirations.yaml → hypothesis_pipeline_low_water_mark (default 2)

# Count active pipeline hypotheses (discovered + evaluating + active stages)
Bash: pipeline-read.sh --counts
pipeline_counts = parse JSON
active_hypothesis_count = pipeline_counts.discovered + pipeline_counts.evaluating + pipeline_counts.active

IF active_hypothesis_count < hypothesis_pipeline_low_water_mark:
    Output: "▸ Hypothesis pipeline thin: {active_hypothesis_count} active hypotheses (threshold: {hypothesis_pipeline_low_water_mark})"

    # Dedup: skip if a matching goal is already pending or in-progress
    existing_review = false
    FOR EACH active aspiration in compact data:
        FOR EACH goal WHERE status in ("pending", "in-progress"):
            IF title starts with "Investigate: Prediction opportunities":
                existing_review = true
                BREAK

    IF NOT existing_review:
        target_asp = first active aspiration from compact data
        echo '{"title":"Investigate: Prediction opportunities in recent work","description":"Hypothesis pipeline is thin ({active_hypothesis_count} active). Review the last 3-5 completed goals for prediction opportunities. For each: what would we expect to see if we revisited this domain? What consequence of this work could we verify later? Form at least 1 testable prediction via sq-009 pattern.","priority":"HIGH","participants":["agent"]}' | Bash: aspirations-add-goal.sh --source {target_asp.source} {target_asp.id}
        Log: "HYPOTHESIS PIPELINE: below low-water-mark ({active_hypothesis_count} < {hypothesis_pipeline_low_water_mark}), created review goal"
```

## Phase 0.5.3: Accuracy Health Gate

Detects critically low hypothesis accuracy and creates diagnostic work. The calibration
gate in aspirations-spark caps confidence at low accuracy (symptom management), but does
not investigate WHY accuracy is low. This gate creates root-cause investigation work.

```
Read core/config/aspirations.yaml → accuracy_critical_threshold (default 0.40), accuracy_min_sample (default 5)

Bash: pipeline-read.sh --accuracy
accuracy_data = parse JSON
total_resolved = accuracy_data.total_resolved
accuracy_pct = accuracy_data.accuracy_pct

IF total_resolved >= accuracy_min_sample AND accuracy_pct < (accuracy_critical_threshold * 100):
    Output: "▸ Accuracy critically low: {accuracy_pct}% ({accuracy_data.confirmed}/{total_resolved} confirmed, threshold: {accuracy_critical_threshold * 100}%)"

    # Dedup check: don't create if an existing pending/in-progress goal already addresses this
    existing_investigation = false
    FOR EACH active aspiration in compact data:
        FOR EACH goal WHERE status in ("pending", "in-progress"):
            IF title starts with "Investigate: Low hypothesis accuracy" OR title starts with "Investigate: Why accuracy":
                existing_investigation = true
                BREAK

    IF NOT existing_investigation:
        # Identify worst-performing strategies for targeted investigation
        by_strategy = accuracy_data.by_strategy or {}
        worst_strategies = [name for name, stats in by_strategy.items() if stats.pct < 40 and stats.total >= 3]
        worst_detail = ", ".join(worst_strategies[:3]) if worst_strategies else "across all strategies"

        target_asp = first active aspiration from compact data
        echo '{"title":"Investigate: Low hypothesis accuracy ({accuracy_pct}%)","description":"Overall accuracy is {accuracy_pct}% ({accuracy_data.confirmed}/{total_resolved}), below critical threshold of {accuracy_critical_threshold * 100}%. Worst areas: {worst_detail}. Diagnose: (1) Are predictions too ambitious for current knowledge? (2) Is the domain changing faster than predictions can track? (3) Are pre-mortems being skipped or ineffective? (4) Is evidence quality poor at formation time? Produce at least one corrective action.","priority":"HIGH","participants":["agent"]}' | Bash: aspirations-add-goal.sh --source {target_asp.source} {target_asp.id}
        Log: "ACCURACY GATE: critically low ({accuracy_pct}% < {accuracy_critical_threshold * 100}%), created investigation goal"
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
        echo '{"title":"Investigate: Why are we cycling on {asp.title}?","description":"Unproductive cycle detected ({cycle_reason}). Recent goals: {recent_goal_titles}. Apply first-principles thinking: surface assumptions, reduce to verifiable ground truth, rebuild approach from fundamentals.","priority":"HIGH","category":"{category}","participants":["agent"]}' | Bash: aspirations-add-goal.sh --source {asp.source} {asp.id}
        Log: "CYCLE DETECTION: Created investigation goal for {asp.id} — {cycle_reason}"
```

## Phase 1: Recurring Goal Check

```
check_recurring_goals()
# Ensures recurring goals are properly tracked and due goals are flagged
```

## Chaining

- **Called by**: `/aspirations` orchestrator (every iteration, first phase)
- **Calls**: `aspirations-read.sh`, `aspirations-meta-update.sh`, `guardrail-check.sh`, `infra-health.sh`, `wm-read.sh`, `wm-set.sh`, `aspiration-trajectory.sh` (cycle detection), `aspirations-add-goal.sh` (cycle detection, hypothesis pipeline, accuracy gate), `pipeline-read.sh` (hypothesis pipeline + accuracy health), `/create-aspiration` (health + pipeline depth), CREATE_BLOCKER protocol
- **Reads**: Aspirations compact, working memory (blockers), guardrails, trajectory data (cycle detection), pipeline meta (hypothesis counts + accuracy), `core/config/aspirations.yaml` (pipeline_low_water_mark, hypothesis_pipeline_low_water_mark, accuracy_critical_threshold, accuracy_min_sample)
