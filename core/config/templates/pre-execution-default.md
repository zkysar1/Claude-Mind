# Domain Pre-Execution Convention (default template)

Steps to follow before each goal execution during Phase 3.9 of the
aspirations loop. Evaluate conditions against the current goal context —
skip steps that do not apply.

This file was seeded from `core/config/templates/pre-execution-default.md`
by `/start` Phase C0.5 on fresh-world setup. Edit freely; the convention
evolves through the feedback paths registered in
`core/config/conventions/domain-hooks.md` (Evolution → Mutation Sources)
with each change recorded in `world/conventions/convention-changes.jsonl`.

## Step 1: Curriculum Stage Check

IF the goal involves creating, editing, or deleting files in the codebase
or primary workspace:
  Bash: bash core/scripts/curriculum-status.sh
  IF current stage does not permit the planned work:
    Log: "Goal requires capabilities not unlocked at current curriculum stage"
    Return SKIP with reason: "Curriculum stage {stage} does not permit {action}"

## Step 2: Pull Latest

IF the goal involves code changes in any repo of the primary workspace:
  For each repo directory that this goal will touch:
    Bash: git -C <repo> pull --ff-only || true
    (Best-effort — `|| true` keeps a failed pull from aborting the phase;
     stderr surfaces the diagnostic. Do NOT add `2>/dev/null` here —
     guard-114/guard-438 forbid silencing stderr on external commands.)

## Step 3: Fix Scope Coverage Check

IF the goal forms a hypothesis that a code fix will FULLY eliminate a class
of errors:
  Enumerate every code path that produces the error — not just the patched
  paths. If the fix covers < 100% of paths, lower confidence proportionally
  or predict partial reduction instead of elimination.

## Step 4: Causal Isolation Before Diagnostic Hypothesis

IF the goal forms a hypothesis about the cause of a failure, crash, or
degradation:
  Before naming a cause, verify it can be isolated from alternatives:
  - Can the attributed cause be present WITHOUT the symptom?
  - Can the symptom occur WITHOUT the attributed cause?
  - Is there a more proximate cause (resource exhaustion, config error,
    schema drift) that was not checked?
  If any isolation check fails, lower confidence by 0.15 or reformulate the
  hypothesis.

## Step 5: Dependency Chain Verification for Predictions

IF the goal predicts an external behavioral improvement from a code change:
  Enumerate the intermediate runtime dependencies (service availability,
  integration linkage, downstream consumers) that must be functional for
  the change to reach the predicted behavior. If ANY dependency is
  unverified or known-broken, either lower confidence by 0.20 per
  unverified dependency or predict partial improvement only.

## Fail-Open Policy

A failed step logs and returns; it does not abort the caller. Phase 3.9
proceeds to Phase 4 unless a step explicitly returns SKIP — in which case
the goal is marked `skipped` with the returned reason and the loop moves
on to the next goal.

## Downstream Consumers

- `aspirations-execute/SKILL.md` Phase 3.9 — invokes this convention
- Phase 4 execution context — receives the pulled-latest state and the
  confidence-adjustment signal from Steps 3–5
