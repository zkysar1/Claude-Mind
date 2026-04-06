---
name: agent-completion-report
description: "Show what changed since last status report — agent dashboard"
user-invocable: true
triggers:
  - "/agent-completion-report"
tools_used: [Bash, Read, Write]
conventions: [aspirations, pipeline, tree-retrieval, reasoning-guardrails, board]
minimum_mode: reader
---

# Agent Completion Report

Displays a delta summary of what changed since the last status report.

Valid from ANY state. User-invocable AND agent-callable. Writes report files to `<agent>/reports/` and `<agent>/COMPLETION-REPORT.md`.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase 1: Determine Report Window

```
1. Determine "since" timestamp (first match wins):
   a. If --since argument provided: use that timestamp
   b. Read <agent>/session/last-report-timestamp → use if present
   c. Read <agent>/session/handoff.yaml → use session_start if present
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
   Read: <agent>/session/pending-questions.yaml → filter status == "pending"
   Bash: bash core/scripts/load-aspirations-compact.sh → IF path returned: Read it
   (compact data has IDs, titles, statuses, participants — no descriptions/verification)
   Filter goals with participants containing "user"

9. Blocked goals analysis
   Bash: bash core/scripts/goal-selector.sh blocked
   → Parse JSON → store as blocked_data
   → blocked_data.bottlenecks = root bottlenecks with downstream counts
   → blocked_data.summary.total_blocked, blocked_data.summary.bottleneck_count

10. Message board activity since last report
    For each channel in [general, findings, coordination, decisions]:
      IF since is not null:
        Calculate hours = ceil((now_epoch - since_epoch) / 3600)
        Bash: bash core/scripts/board-read.sh --channel <channel> --since {hours}h --json
      ELSE (lifetime):
        Bash: bash core/scripts/board-read.sh --channel <channel> --json
      → Parse JSON output → store messages per channel
      → Skip channels that output "is empty or does not exist"
    Cap: max 10 most recent messages per channel.
    If more exist, note: "... and {N} earlier messages"
    Store as board_messages = {channel: [messages], ...}
    (Any channel with zero messages is omitted from output)

11. System Health Metrics
    # Structural health indicators surfaced for meta-awareness

    # 11a. Decompose candidates (tree nodes exceeding growth threshold)
    Bash: bash core/scripts/tree-read.sh --decompose-candidates
    → Parse output as JSON → decompose_candidate_count = len(result)

    # 11b. Encoding drift (from session signals if available)
    Bash: bash core/scripts/wm-read.sh loop_state --json
    → Extract goals_since_last_tree_update from loop_state.signals (if exists)
    → If WM has no loop_state (between sessions): encoding_drift = "N/A (between sessions)"

    # 11c. Reflection ROI (from meta/reflection-strategy.yaml)
    Bash: meta-read.sh reflection-strategy.yaml
    → If file exists: extract roi_history (last 5 entries)
    → If file missing: reflection_roi = "not initialized"

    # 11d. Routine-to-productive ratio (from loop_state if available)
    → If loop_state exists: routine_ratio = loop_state.signals.routine_count_total / loop_state.goals_completed
    → Else: routine_ratio = "N/A"

    # 11e. Knowledge debt items
    Bash: bash core/scripts/wm-read.sh knowledge_debt --json
    → knowledge_debt_count = count of items (0 if empty/null)
    → knowledge_debt_high = count where priority == "HIGH"

    # 11f. Hypothesis pipeline flow
    # Uses pipeline counts already gathered in step 4/5
    Bash: bash core/scripts/pipeline-read.sh --stage active
    → time_gated = count hypotheses where formed_date + horizon window > now
    → flowing = total active - time_gated
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

## Message Board
  {For each channel that has messages, in order: general, findings, coordination, decisions:}
  **{channel}** ({N} messages)
    [{timestamp}] {author}: {text (truncate to 80 chars)}
    [{timestamp}] {author}: {text (truncate to 80 chars)}
    ...
    {If > 10 messages in channel:} ... and {remaining} earlier messages

  If all channels empty or board not initialized: omit entire section.

## Active Work
  {aspiration summary lines with progress fractions}

## Blocked ({blocked_data.summary.total_blocked} goals, {blocked_data.summary.bottleneck_count} bottlenecks)
  {For each blocked_data.bottlenecks, max 8:}
  - {b.goal_id}: {b.title (50 chars)} → {b.downstream_count} downstream [{b.cause}]
  {If > 8:} + {remaining} more bottlenecks
  Largest: {bottlenecks[0].goal_id} — {bottlenecks[0].downstream_count} goals across {bottlenecks[0].affected_aspirations}

  If total_blocked == 0: omit entire section.

## System Health
  Decompose candidates: {decompose_candidate_count} nodes over threshold
  Encoding drift: {encoding_drift} goals since last tree update{" ⚠" if >= 3 else ""}
  Reflection ROI: {last 3-5 roi_history entries as "session N: ROI X.XX" lines, or "not initialized"}
  Routine ratio: {routine_ratio formatted as percentage}{" ⚠ high" if > 0.70 else ""}
  Knowledge debt: {knowledge_debt_count} items ({knowledge_debt_high} HIGH priority)
  Pipeline flow: {flowing} flowing / {time_gated} time-gated

  {IF decompose_candidate_count > 50 OR encoding_drift >= 3 OR routine_ratio > 0.70 OR knowledge_debt_high > 0:}
    Overall: ATTENTION NEEDED — {list specific concerns}
  {ELSE:}
    Overall: HEALTHY

## Needs Attention
  {pending questions count, user goals count — or "None"}

Full report saved to: <agent>/COMPLETION-REPORT.md
═══════════════════════════════════════════════
```

If `since` is null, replace the "Since:" line with "Lifetime totals (no prior report found)".

## Phase 4: Save Report to File

```
1. Build the full report as a markdown document:
   - Header: "# Agent Completion Report" + "Generated: {timestamp}" + "Since: {since}"
   - Include all sections from Phase 3 as markdown (same content, formatted for file)
   - Include the "## System Health" section (decompose candidates, encoding drift, reflection ROI,
     routine ratio, knowledge debt, pipeline flow, and overall verdict)

2. Ensure reports directory exists:
   Bash: mkdir -p <agent>/reports/

3. Write timestamped report file (archive):
   Write: <agent>/reports/completion-report-{YYYY-MM-DDTHH-MM-SS}.md

4. Write latest report pointer (overwrite):
   Write: <agent>/COMPLETION-REPORT.md

5. Prune old reports: keep the 30 most recent files in <agent>/reports/.
   Bash: ls -t <agent>/reports/completion-report-*.md | tail -n +31 | xargs rm -f
   (If fewer than 31 files exist, nothing is deleted.)
```

## Phase 5: Save Report Timestamp

```
1. Write current timestamp to <agent>/session/last-report-timestamp:
   Bash: echo "$(date +%Y-%m-%dT%H:%M:%S)" > <agent>/session/last-report-timestamp
```

The skill ends here. Goal status management (if any) is the caller's responsibility.

## Chaining

- **Called by**: User directly, OR by other skills (e.g., status report wrappers)
- **Calls**: No other skills — only framework scripts
- **Modifies**: `<agent>/session/last-report-timestamp`, `<agent>/reports/*.md`, `<agent>/COMPLETION-REPORT.md`
