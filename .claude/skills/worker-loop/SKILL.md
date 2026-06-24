---
name: worker-loop
description: >-
  The simplified per-Body execution loop a forked WORKER Body runs (Mind/Body
  convergence Phase 2, asp-306). select -> claim -> execute, then STOP. SKIPS the
  reducer-only phases (verify / encode / reflect / state-update / learning-gate);
  the single reducer applies those to all Bodies' merged state at generalize-down.
user-invocable: false
minimum_mode: autonomous
companion_scripts:
  - core/scripts/worker_execute.py
  - core/scripts/goal-selector.sh
  - core/scripts/aspirations-claim.sh
conventions:
  - session-state
  - goal-selection
---

# /worker-loop — Worker-Body Simplified Execution Loop (Phase 2A)

A **worker Body** is a forked instance of a Mind (keyed by `unitKey` = its
session SID) that is NOT the reducer. It runs a deliberately thin loop —
**select -> claim -> execute** — and then stops, leaving its divergent
working-memory for the single reducer to merge later. It does NOT verify,
encode, reflect, update state, run the learning gate, evolve, or do completion
review. Those are **reducer-only**: the one Body holding `running-session-id`
(the reducer) applies them to the MERGED state of every Body at generalize-down
(Phase 1C `body-merge.py`, run from `aspirations-consolidate` Step -1). Running
encode/reflect per-worker would create N reducers — the defect the convergence
forbids.

**Activation status (Phase 2A):** this loop is the worker entry point; an actual
2nd Body is forked + a reducer merges its output only once Phase 2C wires
fork-activation + the parity harness. Until then a single runner is the reducer
and never enters this loop. Design SSOT: the `mind-engine-identity-bridge` tree
node (Phase 2).

## The phase split (authoritative: `worker_execute.py`)

The phase contract is owned by `core/scripts/worker_execute.py`, NOT duplicated
here, so the worker and its tests agree on one source of truth:

```
Bash: py -3 core/scripts/worker_execute.py phases               # -> select claim execute
Bash: py -3 core/scripts/worker_execute.py reducer-only-phases  # the phases this loop SKIPS
Bash: py -3 core/scripts/worker_execute.py should-run-phase <p> # exit 0 = run, exit 1 = skip
```

A worker runs ONLY `select`, `claim`, `execute`. Every reducer-only phase
(`verify`, `spark`, `complete-review`, `state-update`, `evolution`,
`learning-gate`, `productivity-check`) returns `skip`.

## The loop

```
# Phase -0: confirm this Body is a worker, not the reducer.
# A worker has a forked body-WM-file (sessions/<unitKey>/working-memory.yaml);
# the reducer does not (it stays on the agent-wide WM). If this Body has no
# forked WM file it is the reducer/observer -> do NOT run the worker loop; the
# full /aspirations loop is the reducer's path.

# Phase 1 — SELECT (reuse the existing scorer; a worker selects like the reducer)
Bash: goal-selector.sh
Pick the top eligible unclaimed goal (drop any goal a partner is in_flight on).
IF no goal: this is a GENUINE close — the worker has exhausted its work. Write the
  body-closing sentinel so the stop-hook (Phase-2B producer) marks this Body
  closed-pending-merge for the reducer to merge at generalize-down, then STOP:
    Bash: touch "agents/$MIND_AGENT/sessions/$MIND_SID/body-closing"
  (The reducer generates work, not the worker — a worker never fabricates goals.)

# Phase 2 — CLAIM (claimed_by stays the mindKey/agent-name — same claim contract)
Bash: aspirations-claim.sh <goal-id> <agent>
IF claim conflict: abort this goal, loop to SELECT.

# Phase 3 — EXECUTE (the existing execute protocol; the worker DOES the work)
Bash: load-execute-protocol.sh -> Read -> follow Phase 3.9 .. 4.5 ONLY.
# The worker writes ONLY its own forked Body WM. wm-*.sh already route to the
# Body WM when BODY_WM_PATH is injected (Phase 1A); worker_execute.worker_wm_path
# is the matching CLI resolver. Do NOT touch the agent-wide WM.

# Phase 4 — STOP HERE. Do NOT run verify / spark / state-update / learning-gate /
# productivity-check. The worker's divergent WM + the in-progress/completed goal
# record are the hand-off; the reducer merges them at generalize-down and runs
# the encode/reflect/consolidate phases over the merged result.
# Do NOT write the body-closing sentinel here — finishing ONE work-unit is NOT a
# genuine close; the driver may re-invoke this loop for more work, and a sentinel
# left here would make the NEXT (between-turns) turn-end mark the Body closed
# prematurely, losing later divergence (g-306-70). The sentinel is written ONLY
# when SELECT finds no work (Phase 1) — the unambiguous genuine close. A worker
# that ends abruptly without reaching Phase 1 (crash, terminal closed) leaves no
# sentinel; cleanup-stale-bindings then stages its WM via the stale-binding path,
# so no divergence is lost either way.
```

## What a worker MUST NOT do

- Run any reducer-only phase (the gate above returns `skip` for all of them).
- Write the agent-wide working memory (`session/working-memory.yaml`) — a worker
  writes ONLY `sessions/<unitKey>/working-memory.yaml`.
- Set/clear `running-session-id` or claim the reducer role.
- Generate new aspirations/goals or run consolidation — that is the reducer's job.

## Return Protocol

See `.claude/rules/return-protocol.md` — the last action of any turn MUST be a
tool call, not a text summary. A worker Body that has finished an execute step
terminates with a Bash call handing control back to its driver (it does NOT call
`Skill(aspirations)` — that is the reducer's full-loop re-entry). When this skill
is consulted for the phase split only, end with the `worker_execute.py` Bash call
whose output answered the question.
