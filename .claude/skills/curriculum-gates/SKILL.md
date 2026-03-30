---
name: curriculum-gates
description: "Evaluate curriculum graduation gates and promote stage if ready"
user-invocable: false
triggers: []
conventions: [curriculum]
minimum_mode: autonomous
---

# /curriculum-gates — Curriculum Gate Evaluation & Promotion

Evaluates all graduation gates for the agent's current curriculum stage.
If all gates pass, promotes the agent to the next stage and logs the promotion.
Called by `/aspirations-consolidate` (session end) and `/aspirations-evolve` (evolution cycle).

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Evaluate Gates

```
Bash: curriculum-evaluate.sh → parse JSON output

IF configured == false:
    Output: "Curriculum: Not configured — skipping gate evaluation."
    RETURN

IF terminal_stage == true:
    Output: "Curriculum: At terminal stage — all capabilities unlocked."
    RETURN

Output: "Curriculum gate evaluation for stage: {stage_name}"
For each gate in gates:
    Output: "  {gate.id}: {PASS/FAIL} (current: {current_value}, required: {operator} {threshold})"
Output: "Gates passed: {gates_passed_count}/{gates_total}"
```

## Step 2: Promote If Ready

```
IF all_passed == true:
    Bash: curriculum-promote.sh → parse JSON output

    IF promoted == true:
        Output: "CURRICULUM PROMOTION: {from_name} → {to_name}"
        Output: "New unlocks:"
        For each unlock in unlocks where value changed:
            Output: "  {capability}: now {enabled/disabled}"

        # Log promotion to evolution log
        echo '{"date":"<today>","event":"curriculum_promotion","details":"Promoted from {from_stage} ({from_name}) to {to_stage} ({to_name})","trigger_reason":"curriculum-gates evaluation"}' | bash core/scripts/evolution-log-append.sh

    IF promoted == false:
        Output: "Curriculum: All gates passed but promotion blocked — {reason}"

ELSE:
    Output: "Curriculum: {gates_passed_count}/{gates_total} gates passed — promotion not yet available."
    Output: "Remaining gates:"
    For each gate where passed == false:
        Output: "  {gate.id}: needs {threshold}, currently at {current_value}"
```

## Chaining

- **Called by**: `/aspirations-consolidate` (Step 8.6), `/aspirations-evolve` (Step 10)
- **Calls**: `curriculum-evaluate.sh`, `curriculum-promote.sh`, `evolution-log-append.sh`
- **Reads**: `<agent>/curriculum.yaml` (via scripts)
- **Writes**: `<agent>/curriculum.yaml` (gate_status update), `<agent>/curriculum-promotions.jsonl` (on promotion)
