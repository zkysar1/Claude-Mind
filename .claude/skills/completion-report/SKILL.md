---
name: completion-report
description: "Show what changed since last status report — user dashboard"
user-invocable: true
triggers:
  - "/completion-report"
tools_used: [Bash, Read]
conventions: [aspirations, pipeline, tree-retrieval, reasoning-guardrails]
---

# Completion Report

Displays a delta summary of what changed since the last status report.

Valid from ANY state. User-invocable AND agent-callable (safe: read-only + timestamp update).

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase 1: Determine Report Window

```
1. Determine "since" timestamp (first match wins):
   a. If --since argument provided: use that timestamp
   b. Read mind/session/last-report-timestamp → use if present
   c. Read mind/session/handoff.yaml → use session_start if present
   d. If none found:
      since = null → show lifetime totals only, skip deltas
      Label report as "Lifetime" instead of delta window
```

## Phase 2: Gather Delta Data

All data comes from framework scripts — no direct JSONL reads.

```
1. Journal entries since last report
   Bash: bash core/scripts/journal-read.sh --recent 10
   → Filter entries where date >= since date
   → Extract goals_completed, goals_attempted, key_events

2. Aspirations completed since last report
   Bash: bash core/scripts/aspirations-read.sh --archive
   → Filter where completed_at >= since date
   → Count and list titles

3. Active aspirations progress
   Bash: bash core/scripts/aspirations-read.sh --summary

3b. Goal details for completed goals
   From the compact aspirations data (step 8) and archive data (step 2),
   resolve each goal ID from goals_completed into {id, title, aspiration_id, aspiration_title}.
   Group by aspiration. This provides the detailed goal listing for Phase 3.

4. Hypotheses resolved since last report
   Bash: bash core/scripts/pipeline-read.sh --stage resolved
   → Filter where outcome_date >= since date
   → Count confirmed vs corrected

5. Overall pipeline accuracy
   Bash: bash core/scripts/pipeline-read.sh --accuracy

6. Knowledge tree stats
   Bash: bash core/scripts/tree-read.sh --stats

7. Guardrails / reasoning bank / pattern signatures counts
   Bash: bash core/scripts/guardrails-read.sh --summary
   Bash: bash core/scripts/reasoning-bank-read.sh --summary
   Bash: bash core/scripts/pattern-signatures-read.sh --summary
   → Count lines from each

8. Pending questions + user goals
   Read: mind/session/pending-questions.yaml → filter status == "pending"
   Bash: bash core/scripts/load-aspirations-compact.sh → IF path returned: Read it
   (compact data has IDs, titles, statuses, participants — no descriptions/verification)
   Filter goals with participants containing "user"

9. Blocked goals analysis
   Bash: bash core/scripts/goal-selector.sh blocked
   → Parse JSON → store as blocked_data
   → blocked_data.bottlenecks = root bottlenecks with downstream counts
   → blocked_data.summary.total_blocked, blocked_data.summary.bottleneck_count
```

## Phase 3: Display Console Summary

```
Output the following format:

═══ COMPLETION REPORT ═════════════════════════
Since: {since_timestamp} ({hours}h {min}m ago)

## Completed ({N} goals across {M} aspirations)
  Aspirations completed: {list titles, or "none"}

  {For each aspiration that had goals completed, grouped:}
  **{asp_id}: {asp_title}** ({count} goals)
    {goal_id}: {goal_title}
    {goal_id}: {goal_title}
    ...

## Hypotheses
  - {N} resolved since last report ({X} confirmed, {Y} corrected)
  - Overall accuracy: {Z}% ({total} lifetime)

## Knowledge
  - {N} tree nodes ({interior} interior, {leaf} leaf)
  - {N} guardrails, {N} reasoning entries, {N} pattern signatures

## Active Work
  {aspiration summary lines with progress fractions}

## Blocked ({blocked_data.summary.total_blocked} goals, {blocked_data.summary.bottleneck_count} bottlenecks)
  {For each blocked_data.bottlenecks, max 8:}
  - {b.goal_id}: {b.title (50 chars)} → {b.downstream_count} downstream [{b.cause}]
  {If > 8:} + {remaining} more bottlenecks
  Largest: {bottlenecks[0].goal_id} — {bottlenecks[0].downstream_count} goals across {bottlenecks[0].affected_aspirations}

  If total_blocked == 0: omit entire section.

## Needs Attention
  {pending questions count, user goals count — or "None"}
═══════════════════════════════════════════════
```

If `since` is null, replace the "Since:" line with "Lifetime totals (no prior report found)".

## Phase 4: Save Report Timestamp

```
1. Write current timestamp to mind/session/last-report-timestamp:
   Bash: echo "$(date +%Y-%m-%dT%H:%M:%S)" > mind/session/last-report-timestamp
```

The skill ends here. Goal status management (if any) is the caller's responsibility.

## Chaining

- **Called by**: User directly, OR by other skills (e.g., status report wrappers)
- **Calls**: No other skills — only framework scripts
- **Modifies**: `mind/session/last-report-timestamp` only
