---
name: agent-completion-report
description: "Produces an agent completion report showing what changed since the last status marker: completed goals, new tree encodings, emitted findings, resolved hypotheses, and in-flight work. Use whenever the user asks \"what have you done\", \"what's the status\", \"give me a recap\", or requests a dashboard; also use when the agent needs to summarize progress before a handoff, stop, or consolidation checkpoint. Writes to {agent}/reports/ and {agent}/COMPLETION-REPORT.md."
user-invocable: true
triggers:
  - "/agent-completion-report"
tools_used: [Bash, Read, Write]
conventions: [aspirations, pipeline, tree-retrieval, reasoning-guardrails, board]
minimum_mode: reader
revision_id: "skill-bootstrap-agent-completion-report-ca45dd"
previous_revision_id: null
---

# Agent Completion Report

Displays a delta summary of what changed since the last status report.

Valid from ANY state. User-invocable AND agent-callable. Writes report files to `agents/<agent>/reports/` and `agents/<agent>/COMPLETION-REPORT.md`.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase 1: Determine Report Window

```
1. Determine "since" timestamp (first match wins):
   a. If --since argument provided: use that timestamp
   b. Read agents/<agent>/session/last-report-timestamp → use if present
   c. Read agents/<agent>/session/handoff.yaml → use session_start if present
   d. If none found:
      since = null → show lifetime totals only, skip deltas
      Label report as "Lifetime" instead of delta window

1.5. Positive-state audit (verify-before-assuming.md Positive File-State Claims):
   Any narrative reference in the generated report to a specific framework file
   (`handoff.yaml`, `aspirations.jsonl`, `self.md`, `program.md`, etc.) that
   describes its current state or contents MUST be preceded by an in-turn Read
   of that file. Do not narrate from prior-session memory or from the
   aspirations-compact index — read the actual file before stating what it says.
   Recommended probe at report-generation time, for each referenced file:
   ```bash
   py core/scripts/positive-state-gate.py --claim "<narrative sentence>" --evidence "<concatenated Read outputs from this turn>"
   ```
   Exit 1 = re-read the file and re-state the claim from fresh evidence.
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

3c. Knowledge-debt closures in the report window
   From the same completed-goal set as 3b, collect goals where
   `closes_knowledge_debt` is a non-empty list. For each, record
   `{goal_id, title, closes_knowledge_debt}`. Aggregate:
   - `debt_closure_events` = count of such goals
   - `debt_closure_node_keys` = unique union of node_keys closed
   - `debt_closure_sample` = up to 3 {goal_id, title, node_keys} entries
   This surfaces the semantic override firings from aspirations-execute
   Phase 4-post ("DEBT-CLOSURE OVERRIDE") without requiring log scanning.

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
   Read: agents/<agent>/session/pending-questions.yaml → filter status == "pending"
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

    # 11e. Knowledge debt items (aggregate + per-entry detail)
    Bash: bash core/scripts/wm-read.sh knowledge_debt --json
    → knowledge_debt_count = count of items (0 if empty/null)
    → knowledge_debt_high = count where priority == "HIGH"
    → knowledge_debt_oldest_age_days = max(now - item.created) across items,
      or 0 if empty
    → knowledge_debt_entries = up to 5 items sorted by sessions_deferred
      DESC then age DESC, each as
      {node_key, priority, source_goal, age_days, sessions_deferred}

    # 11f. Hypothesis pipeline flow
    # Uses pipeline counts already gathered in step 4/5
    Bash: bash core/scripts/pipeline-read.sh --stage active
    → time_gated = count hypotheses where formed_date + horizon window > now
    → flowing = total active - time_gated

12. Outcome Delta (Tranche C — rb-390)
    # Reads the outcome-metrics snapshot populated by the outcome-observation
    # hook in aspirations-state-update Step 8.12 (Pattern B hook slot — see
    # core/config/conventions/domain-hooks.md). Fail-open: missing snapshot
    # means outcome_delta_available = false and the section shows
    # "no outcome signal configured" — never an error.
    goals_completed_count = len(goals_completed)  # from step 1 (list of ids)
    Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/outcome-metrics.yaml" && echo "exists"
    IF exists:
        Read "$WORLD_DIR/outcome-metrics.yaml" → outcome_now = parsed YAML
        outcome_delta_available = true
        # Prior snapshot: saved alongside the report-timestamp marker so the
        # delta is computed against the last completion report, not the live
        # file (which the outcome-observation hook mutates every goal).
        IF agents/<agent>/reports/last-outcome-snapshot.yaml exists:
            Read it → outcome_prior = parsed YAML
        ELSE:
            outcome_prior = {}  # first report — deltas appear as "initial"
        # Compute per-source deltas. Each source contributes exactly these
        # three keys (consumer contract — do not omit any):
        #   available: bool     — was the source present in outcome_now?
        #   moved:     bool     — did any observable field change value vs prior?
        #   delta_summary: str  — one-line human description for the report
        # Never error if a key is missing from outcome_prior or outcome_now —
        # the source shape is domain-specific and may change.
        git_delta       = compute_source_delta(outcome_prior.get("git", {}),      outcome_now.get("git", {}))
        ci_delta        = compute_source_delta(outcome_prior.get("ci", {}),       outcome_now.get("ci", {}))
        operator_delta  = compute_source_delta(outcome_prior.get("operator", {}), outcome_now.get("operator", {}))
        # Process-vs-outcome divergence flag:
        # IF goals_completed_count >= 5 AND no source moved → divergence.
        # This is the exact signal that caught "77 goals done and nothing
        # material moved." Magic number 5 is the minimum window under which
        # divergence is noise not signal.
        any_outcome_moved = any(
            d.get("moved") for d in [git_delta, ci_delta, operator_delta]
        )
        divergence = (
            goals_completed_count >= 5
            and not any_outcome_moved
        )
    ELSE:
        outcome_delta_available = false
        divergence = false
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

## Knowledge Debt
  Outstanding: {knowledge_debt_count} items ({knowledge_debt_high} HIGH, oldest {knowledge_debt_oldest_age_days}d)
  {For each entry in knowledge_debt_entries (max 5):}
    - {node_key} [{priority}] — from {source_goal}, deferred {sessions_deferred}× ({age_days}d old)

  Closures this window: {debt_closure_events} goal(s)
  {IF debt_closure_events > 0:}
    {For each entry in debt_closure_sample (max 3):}
    - {goal_id}: {title (60 chars)} → closes [{comma-join node_keys}]
    {IF debt_closure_events > 3:} + {remaining} more

  If knowledge_debt_count == 0 AND debt_closure_events == 0: omit entire section.

## Outcome Delta
  {IF outcome_delta_available is false:}
    No outcome signal configured.
    (To enable: create world/conventions/outcome-observation.md per
    core/config/conventions/domain-hooks.md → Pattern B hook slots.)
  {ELSE:}
    Git:      {git_delta.delta_summary}
    CI:       {ci_delta.delta_summary}
    Service:  {operator_delta.delta_summary}
    {IF divergence is true:}
      ⚠ Process-vs-outcome divergence: {goals_completed_count} goals
      completed this window but no observed outcome signal moved. Either
      the work did not move a measurable outcome, or the
      outcome-observation hook is not reading the right sources.
      Investigate before declaring progress.

  If outcome_delta_available is false AND this is a fresh world: omit
  the divergence warning but keep the "no outcome signal configured"
  hint.

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

Full report saved to: agents/<agent>/COMPLETION-REPORT.md
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
   Bash: mkdir -p agents/<agent>/reports/

3. Write timestamped report file (archive):
   Write: agents/<agent>/reports/completion-report-{YYYY-MM-DDTHH-MM-SS}.md

4. Write latest report pointer (overwrite):
   Write: agents/<agent>/COMPLETION-REPORT.md

5. Save outcome-metrics snapshot for next report's delta baseline
   # Tranche C — rb-390. If the current outcome-metrics.yaml exists, copy
   # it to agents/<agent>/reports/last-outcome-snapshot.yaml so the NEXT completion
   # report computes "delta since last report" against this snapshot rather
   # than against the live file (which mutates every goal). Fail-open: if
   # the live file is missing, skip silently.
   Bash: source core/scripts/_paths.sh && \
         [ -f "$WORLD_DIR/outcome-metrics.yaml" ] && \
         mkdir -p "agents/<agent>/reports" && \
         cp "$WORLD_DIR/outcome-metrics.yaml" \
            "agents/<agent>/reports/last-outcome-snapshot.yaml" || true
```

Note: `agents/<agent>/reports/` is append-only history. Never add pruning, rotation, or retention caps — every completion report is kept permanently. `last-outcome-snapshot.yaml` is the single exception — it is overwritten each report (delta baseline only).

## Phase 5: Save Report Timestamp

```
1. Write current timestamp to agents/<agent>/session/last-report-timestamp:
   Bash: echo "$(date +%Y-%m-%dT%H:%M:%S)" > agents/<agent>/session/last-report-timestamp
```

## Phase 5.5: Notify the User

The completion report is a primary user-visibility event — the user should
receive it, not just find it on disk.

Notify the user about the completion report.
(Check `world/forged-skills.yaml` for a skill whose triggers match "notify
the user" and invoke it with:
- category: `completion`
- subject: build a stats-summary subject from the report data, e.g.,
  `"Completion Report ({since_label}, {N} goals, {N_deep} deep)"`
  (real example: `"Completion Report (31h, 6 goals, 1 deep)"`)
- message-file: the timestamped archive file written in Phase 4 Step 3:
  `agents/<agent>/reports/completion-report-{YYYY-MM-DDTHH-MM-SS}.md`

The notify skill MUST consume the file via `--message-file` (not via a
re-constructed prose summary). The 2026-05-20 incident — completion
emails arriving with only Title + UTC + reply-footer because the LLM
hand-constructed an empty Body — was caused by re-summarizing the
already-rich report file into a prose blurb that got dropped. The file
on disk IS the deliverable; pass its path through.

The `notify-build-payload.py` helper (called by /notify-user Step 2)
will refuse the send with rc=2 if the message body is too short, so a
missing or trivial report file fails loud instead of producing a blank
email.

If no matching skill is registered, fall back to a `participants: [agent, user]`
goal via `aspirations-add-goal.sh` with title
`"User Notice: Completion Report available"` and
`origin_signal: "idea:completion-report-available"`. Never block completion-report
generation on notification failure — the report is already on disk.)

The skill ends here. Goal status management (if any) is the caller's responsibility.

## Chaining

- **Called by**: User directly, OR by other skills (e.g., status report wrappers)
- **Calls**: Notification forged skill (resolved via `world/forged-skills.yaml`) in Phase 5.5
- **Modifies**: `agents/<agent>/session/last-report-timestamp`, `agents/<agent>/reports/*.md`, `agents/<agent>/COMPLETION-REPORT.md`

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is Phase 5.5's notification invocation (or its
`aspirations-add-goal.sh` fallback). The report files on disk are the
deliverable; do not append a text summary.
