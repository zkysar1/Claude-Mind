---
name: curriculum-gates
description: "Evaluates all graduation gates for the agent's current curriculum stage and promotes to the next stage if every gate passes. Gates include metric_threshold, count_check, log_scan, and command_check types. Use whenever /aspirations-consolidate hits session end, /aspirations-evolve runs a mandatory evolution pass, or the agent completes enough goals to plausibly graduate. Promotion unlocks new capabilities (self-edits, forge-skill, parallelism) per curriculum.yaml."
user-invocable: false
triggers: [curriculum, graduation, stage-promotion, gate-evaluation, developmental-stage, cur-stage]
conventions: [curriculum]
minimum_mode: autonomous
revision_id: "skill-bootstrap-curriculum-gates-871349"
previous_revision_id: null
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
    Bash: echo "Curriculum: Not configured — skipping gate evaluation."
    RETURN

IF terminal_stage == true:
    Bash: echo "Curriculum: At terminal stage — all capabilities unlocked."
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

        # E14: Curriculum-stage transition encoding. Routes the promotion through
        # sensory_buffer so the standard encoding pipeline (consolidation Phase 2
        # / state-update Phase 8) selects/creates the right tree node. We do NOT
        # hard-code a target path: promotion is rare (multi-week cadence), and
        # the encoding gate's category_class_multiplier + curator gate handle
        # routing.
        #
        # `curriculum-promote.sh` JSON returns: {promoted, from_stage, to_stage,
        # unlocks (NEW stage's full unlocks map)}. It does NOT return a diff or
        # a stage description. To get the destination stage's description, read
        # `agents/<agent>/curriculum.yaml` and look up the stage where id == to_stage.
        # The `unlocks` already-printed list above ("New unlocks: ...") drives
        # the observation text — use those same keys, don't recompute.

        Read agents/<agent>/curriculum.yaml → find stage where id == to_stage
        to_stage_description = that stage's `description` field (one-line)
        unlocked_capability_keys = [k for k, v in unlocks.items() if v == true]

        IF unlocked_capability_keys is non-empty:
            observation_text = "Promoted from {from_stage} ({from_name}) to {to_stage} ({to_name}) on <today>. Capabilities now active: " + ", ".join(unlocked_capability_keys) + ". " + to_stage_description
            echo '{
              "source_goal": "curriculum-gates-promotion",
              "observation": "<observation_text>",
              "encoding_score": 0.0,
              "scores": {
                "novelty": 0.8,
                "outcome_impact": 0.7,
                "surprise": 0.2,
                "goal_relevance": 0.8,
                "repetition_strength": 0.1
              },
              "target_article": null,
              "replay_priority": "goal_completions"
            }' | bash core/scripts/wm-append.sh sensory_buffer
            # Fail-open: append errors log to execution-diary but never block
            # the promotion. The evolution-log entry above is the durable record;
            # tree encoding is the value-add.

    IF promoted == false:
        Output: "Curriculum: All gates passed but promotion blocked — {reason}"

ELSE:
    Output: "Curriculum: {gates_passed_count}/{gates_total} gates passed — promotion not yet available."
    Output: "Remaining gates:"
    For each gate where passed == false:
        Bash: echo "  {gate.id}: needs {threshold}, currently at {current_value}"
```

## Chaining

- **Called by**: `/aspirations-consolidate` (Step 8.6), `/aspirations-evolve` (Step 10)
- **Calls**: `curriculum-evaluate.sh`, `curriculum-promote.sh`, `evolution-log-append.sh`
- **Reads**: `agents/<agent>/curriculum.yaml` (via scripts)
- **Writes**: `agents/<agent>/curriculum.yaml` (gate_status update), `agents/<agent>/curriculum-promotions.jsonl` (on promotion)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is `curriculum-evaluate.sh`, `curriculum-promote.sh`, or
`evolution-log-append.sh`. Never end with a text summary of gate status.
