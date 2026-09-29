# Rationale: Plan Per Goal

Referenced from `core/config/execute-protocol-digest.md` ("PLAN FROM AN EMPTY BOARD").
Why an executor clears its plan before laying out a new goal's steps.

## Why clear, rather than just start a new plan

A plan tool takes the whole plan on every call (Claude Code's TodoWrite, zakcode's
update_plan). zakcode adds one rule on top: when a resend leaves out done steps that its
working-memory render had folded out of view, it puts them back, on the reasoning that
what the model could not see it cannot have meant to drop. Inside one goal that is right.
Across goals it undoes the executor's fresh start.

Measured 2026-09-28 on four worker Bodies (session documents, counts only): the last 200
plan events held 28-43 `restored` events and 61-76 `dropped` events (open steps of earlier
goals), and the stored plans had 61-76 steps, 52-73 of them done. So the executors were
already starting new plans; the harness kept restoring the old ones.

An empty plan clears the board through the tool's own contract, and the harness keeps the
record of what was cleared. Nothing is restored into an empty board, because there is no
folded prior plan left to restore from.

## Why it matters on a slow backend

Measured 2026-09-28 12:55Z (g-375-65): responses that call the plan tool took 11-26% of
each Body's summed call latency, rising with plan size. One response hit the 8,192-token
output cap with 15,158 characters of plan arguments, so its update was never applied. The
engines decode at about 8 tokens a second and were busy 91-98% of wall time, so plan text
competes directly with the goal's own work.

## Why in the same response

A response that only updates the plan costs a whole model call (zakcode ADR-0237). The
clear rides in the goal's first response, beside a call the executor makes anyway (its new
plan, or its first action when the goal needs no plan): a cue to a small model holds when it
lands on an action the model already takes (rb-10927). The no-plan case matters too: zakcode
re-shows an unfinished plan on every request as the plan "for the current goal", so an
earlier goal's leftover steps would be presented as this goal's.

## Why in this digest

Both entry points read it at execution time: the worker loop (its Phase 3 follows Phase
3.9-4.5 of this digest) and the full loop. The worker-loop SKILL.md is size-ratcheted by
the hot-path budget; this digest is not, and the instruction belongs where execution starts.

## Cross-references

- g-375-65 — the goal; its outcome 2 re-measures plan size and the plan-call share after
  12 hours of adoption
- guard-399 — a prose instruction needs a detector. Here the detector is that re-measure;
  a hook on the plan tool is the fallback if the prose does not hold
- rb-10927 — a cue works when it lands on an action the model already takes
- rb-11769 — a worker Body takes a framework fix only when its current unit ends
