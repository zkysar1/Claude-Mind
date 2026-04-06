---
name: open-questions
description: "Show open questions, user-assigned goals, and blocked goals — user dashboard"
user-invocable: true
triggers:
  - "/open-questions"
conventions: [aspirations]
minimum_mode: reader
---

# /open-questions — User Dashboard

Shows what needs the user's attention: pending questions the agent logged,
goals assigned to the user, and blocked goals grouped by reason. Primes context
first so follow-up discussion is informed by domain knowledge.

**USER-ONLY COMMAND.** Claude MUST NEVER invoke this skill autonomously.
Valid from ANY state (RUNNING, IDLE, UNINITIALIZED).

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase 1: Prime Context

```
1. Invoke /prime
   — Loads Self, guardrails, reasoning bank, category-specific knowledge
   — If UNINITIALIZED and /prime outputs "Nothing to prime": SKIP (continue to Phase 2)
```

## Phase 2: Scan Pending Questions

```
1. Read <agent>/session/pending-questions.yaml
   IF file missing: pending_questions = []
   ELSE: filter questions where status == "pending"
   Store as pending_questions list
```

## Phase 3: Scan User-Participant Goals

```
1. Bash: `aspirations-query.sh --goal-field participants user`
   → Filter returned goals where status NOT in ("completed", "skipped", "expired")
   Store as user_goals list

2. Bash: aspirations-read.sh --archive
   → Same filter (catch goals in archived aspirations that are still open)
   Append to user_goals list
```

## Phase 3.5: Scan Blocked Goals

```
1. Bash: goal-selector.sh blocked
   → Parse JSON output → store as blocked_data
   → blocked_goals = blocked_data.blocked_goals
   → by_reason = blocked_data.by_reason
   → summary = blocked_data.summary
```

## Phase 4: Output Summary

```
═══ OPEN ITEMS ════════════════════════════════

IF pending_questions is non-empty:
  ## Pending Questions
  | ID | Date | Question | Default Action |
  |---|---|---|---|
  {for each: id, date, question (truncated to ~100 chars), default_action}

IF user_goals is non-empty:
  ## User Goals
  | Goal | Aspiration | Title | Priority | Status |
  |---|---|---|---|---|
  {for each: goal_id, aspiration_id, title, priority, status}

IF blocked_goals is non-empty:
  ## Blocked Goals ({summary.total_blocked} of {summary.total_active_goals} active goals)

  IF by_reason.infrastructure.count > 0:
    ### Infrastructure ({count})
    | Goal | Aspiration | Title | Blocked By |
    |---|---|---|---|
    {for each infrastructure goal: goal_id, aspiration_id, title (truncated ~50 chars), block_detail}

  IF by_reason.dependency.count > 0:
    ### Dependency Chain ({head_count} heads, {downstream_count} downstream)
    | Goal | Aspiration | Title | Waiting On |
    |---|---|---|---|
    {for each HEAD goal only: goal_id, aspiration_id, title (truncated ~50 chars), unmet dep IDs}
    IF downstream_count > 0:
      "... plus {downstream_count} downstream goals in dependency chains"

  IF by_reason.deferred.count > 0:
    ### Deferred ({count})
    | Goal | Aspiration | Title | Until | Reason |
    |---|---|---|---|---|
    {for each: goal_id, aspiration_id, title (truncated ~50 chars), deferred_until, defer_reason}

  IF by_reason.hypothesis_gate.count > 0:
    ### Hypothesis Gate ({count})
    | Goal | Aspiration | Title | Not Before |
    |---|---|---|---|
    {for each: goal_id, aspiration_id, title (truncated ~50 chars), block_detail}

  IF by_reason.explicit_status.count > 0:
    ### Explicitly Blocked ({count})
    | Goal | Aspiration | Title | Reason |
    |---|---|---|---|
    {for each: goal_id, aspiration_id, title (truncated ~50 chars), block_detail}

IF all three empty (pending_questions, user_goals, blocked_goals):
  Nothing requires your attention. All questions answered, no user goals open, no blocked goals.

───────────────────────────────────────────────
Summary: {N} pending questions, {M} user goals, {B} blocked goals
═══════════════════════════════════════════════
```

## Chaining

- **Called by**: User only. NEVER by Claude.
- **Calls**: `/prime` (read-only context loading)
- **Modifies**: Nothing. This skill is entirely read-only.
