---
name: reflect-curate-aspirations
description: "Aspiration grooming — detect stuck goals whose evidence has converged, close or re-scope them"
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-curate-aspirations"
  - "/reflect --curate-aspirations"
conventions: [aspirations, experience, goal-schemas]
minimum_mode: autonomous
---

# /reflect-curate-aspirations — Aspiration Grooming

This sub-skill implements backlog grooming for the aspiration system. It is invoked
by the parent `/reflect` router when `--curate-aspirations` is specified, or during
`--full-cycle` as step 1.75. It detects stuck goals whose evidence has already
converged and closes, re-scopes, or unblocks them.

**Why this exists**: Aspirations get stuck on operational blockers (infrastructure
sessions, external compute, user input) while evidence converges elsewhere — sibling
goals complete, hypotheses resolve, knowledge tree fills in. Without grooming, these
aspirations sit blocked indefinitely until manual intervention.

Triggered by: `--full-cycle` step 1.75, or direct invocation `--curate-aspirations`.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Gather Candidates

```
Bash: load-aspirations-compact.sh → IF path returned: Read it
(compact data has IDs, titles, statuses, priorities, categories, recurring, blocked_by, deferred, started — no descriptions/verification)
For each aspiration:
  For each goal where status in (pending, blocked):
    candidate = false

    # Skip recurring goals — they reset naturally via interval mechanism
    IF goal.recurring == true: SKIP

    # 1a: Stuck goals — started but never completed
    IF goal.started is set AND (goal.achievedCount is unset OR achievedCount == 0):
      candidate = true, reason = "started but never completed"

    # 1b: Stale blockers — all dependencies resolved but blocked_by not cleared
    IF goal.blocked_by is non-empty:
      Look up each dependency goal ID in the same aspiration
      resolved_deps = [dep for dep in blocked_by where dep.status in (completed, skipped)]
      IF len(resolved_deps) == len(blocked_by):
        candidate = true, reason = "all dependencies resolved but still marked blocked"

    # 1c: Expired deferral — deferred_until has passed, goal never picked up
    IF goal.deferred_until is set AND deferred_until < now AND (achievedCount is unset OR achievedCount == 0):
      candidate = true, reason = "deferral expired, never executed"

    # 1d: Mature aspiration, stale goal — aspiration is 50%+ done, this goal is lagging
    IF aspiration.progress.completed_goals >= aspiration.progress.total_goals * 0.5:
      IF goal.started is set AND (achievedCount is unset OR achievedCount == 0):
        candidate = true, reason = "aspiration mature (50%+ done), goal stale"

    # 1e: Orphaned deferral — defer_reason references infrastructure but
    #     no matching decisions_locked entry exists (decision was invalidated/expired)
    IF goal.defer_reason is set:
      IF defer_reason mentions blocked/unavailable/down/infrastructure:
        Check decisions_locked from current session context (passed from boot)
        IF no decisions_locked entry substantiates the defer_reason:
          candidate = true, reason = "deferral reason not backed by active decision"

    IF candidate: add {aspiration_id, goal, reason} to grooming_candidates
```

If no candidates found: log "No grooming candidates found", return empty result. STOP.

## Step 2: Evidence Cross-Reference (Agent Judgment)

For each candidate, evaluate whether existing evidence already covers the goal:

```
1. Read goal.verification.outcomes — what would "done" look like?

2. Cross-reference against accumulated evidence:

   a. Experience archive:
      Bash: experience-read.sh --category {goal.category}
      Do any experiences demonstrate the goal's expected outcomes?

   b. Knowledge tree:
      Bash: retrieve.sh --category {goal.category} --depth shallow
      Does the tree already contain the answers this goal would produce?

   c. Sibling goal outcomes:
      Review completed/skipped goals in the SAME aspiration.
      Did a sibling goal already produce this goal's expected outputs?
      (e.g., a live test completed that covers the same data as a benchmark goal)

   d. Pipeline hypotheses:
      Are there resolved hypotheses whose outcomes render this goal moot?
      (e.g., hypothesis confirmed that makes the goal's premise false)

3. Decision — one of:

   COMPLETE — evidence shows ALL verification.outcomes already satisfied.
             Cite specific evidence: experience IDs, tree node keys, sibling goal IDs.

   SKIP    — goal's thesis falsified or rendered moot by other outcomes.
             Cite the falsifying evidence.

   SCOPE-DOWN — partial evidence exists. Revise goal description to cover
                only the remaining gap. Cite what's already covered.

   UNBLOCK — all blocked_by dependencies are resolved. Clear blocked_by,
             leave goal as pending for normal execution.
             Also for orphaned deferrals (reason 1e): clear deferred_until
             and defer_reason (set to null). Log: "UNBLOCKED: {goal.id} —
             deferral reason orphaned, no supporting decision"

   KEEP    — still needed, no existing evidence covers the outcomes.
             No action taken.
```

**IMPORTANT**: Grooming is never automatic. The agent MUST reason about each
candidate and cite specific evidence for every COMPLETE or SKIP decision.
A goal that "feels done" is not done — the evidence must be traceable.

## Step 3: Execute Decisions

```
For each COMPLETE decision:
  Bash: aspirations-update-goal.sh --source {asp.source} {goal_id} status completed
  Bash: evolution-log-append.sh with:
    {"event": "aspiration_grooming", "action": "completed", "goal_id": "{id}",
     "reason": "{reason}", "evidence": ["{refs}"], "date": "{today}"}

For each SKIP decision:
  Bash: aspirations-update-goal.sh --source {asp.source} {goal_id} status skipped
  Bash: evolution-log-append.sh with:
    {"event": "aspiration_grooming", "action": "skipped", "goal_id": "{id}",
     "reason": "{reason}", "evidence": ["{refs}"], "date": "{today}"}

For each SCOPE-DOWN decision:
  Bash: aspirations-update-goal.sh --source {asp.source} {goal_id} description "{revised description}"
  Bash: evolution-log-append.sh with:
    {"event": "aspiration_grooming", "action": "scoped_down", "goal_id": "{id}",
     "reason": "{reason}", "date": "{today}"}

For each UNBLOCK decision:
  Bash: aspirations-update-goal.sh --source {asp.source} {goal_id} blocked_by "[]"
  Bash: evolution-log-append.sh with:
    {"event": "aspiration_grooming", "action": "unblocked", "goal_id": "{id}",
     "reason": "{reason}", "date": "{today}"}

# Post-decision sweep
After all decisions executed:

  # Auto-complete aspirations where all goals are now done
  For each aspiration touched:
    Bash: aspirations-read.sh --source {asp.source} --id {asp_id}
    IF any goal has recurring == true:
      SKIP — aspirations with recurring goals are perpetual (data layer blocks archival)
    ELIF all goals have status in (completed, skipped):
      Bash: aspirations-complete.sh --source {asp.source} {asp_id}

  # Knowledge reconciliation (M.11-12 pattern)
  For each COMPLETE or SKIP decision:
    Check knowledge tree nodes referenced by the goal's category
    IF any nodes contain "TBD" entries or stale status values:
      Edit the node to resolve TBDs and correct stale data
      Update last_update_trigger front matter
```

## Step 4: Journal + Return Result

Log grooming activity to journal via `journal-merge.sh` (append key_events).

Return:
```yaml
grooming_result:
  candidates_found: {N}
  completed: {N}
  skipped: {N}
  scoped_down: {N}
  unblocked: {N}
  kept: {N}
  aspirations_closed: [{asp_id, ...}]
  details:
    - goal_id: {id}
      decision: {COMPLETE|SKIP|SCOPE-DOWN|UNBLOCK|KEEP}
      reason: "{explanation}"
      evidence_refs: ["{experience_id}", "{tree_node_key}", "{sibling_goal_id}"]
```

---

## Chaining

- **Called by**: `/reflect` router (`--curate-aspirations` or `--full-cycle` step 1.75)
- **Calls**: `aspirations-read.sh`, `experience-read.sh`, `retrieve.sh`, `aspirations-update-goal.sh`, `aspirations-complete.sh`, `evolution-log-append.sh`, `journal-merge.sh` (all read-write via scripts)
- **Does NOT call**: `/aspirations`, `/reflect`, `/boot`, or any other skill
- **Does NOT modify**: agent-state, session signals, working memory
