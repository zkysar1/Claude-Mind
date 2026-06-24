---
name: agent-completion-report
description: "Produces an agent completion report showing what changed since the last status marker: completed goals, new tree encodings, emitted findings, resolved hypotheses, and in-flight work. Use whenever the user asks \"what have you done\", \"what's the status\", \"give me a recap\", or requests a dashboard; also use when the agent needs to summarize progress before a handoff, stop, or consolidation checkpoint. Writes {agent}/COMPLETION-REPORT.md (its git history is the permanent archive; the reports/ directory is abolished)."
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

Valid from ANY state. User-invocable AND agent-callable. Writes the single latest-pointer file `agents/<agent>/COMPLETION-REPORT.md` (its git history is the permanent archive — there is no timestamped `reports/` archive; that directory was abolished by the file-model normalization).

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
        IF agents/<agent>/session/last-outcome-snapshot.yaml exists:
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

13. Recurring-goal lifetime substantive-hit data (FW-5/R1 -- g-317-16)
    # Feeds the "Contribution" recognition section (Phase 3), which frames
    # recurring/sweep goals that returned routine THIS window. g-317-02 shipped a
    # lifetime substantive-hit tally WRITTEN by recurring-close.sh onto each
    # recurring goal record:
    #   substantive_runs    = denominator, advances on EVERY close
    #   substantive_hits    = numerator, GENUINE-deep closes only (a forced
    #                         anti-drift flip is NOT a real catch, so it is excluded)
    #   last_substantive_at = date of the most recent genuine catch
    # The slim aspirations-compact index (step 8) does NOT carry these fields, so
    # gather them from the goal records for the recurring goals completed this
    # window (the set the report already lists).
    substantive_data = {}   # goal_id -> {hits, runs, last, rate}
    FOR EACH goal_id in goals_completed (from step 1) that is recurring:
        # Resolve aspiration_id via step 3b's goal_id -> aspiration mapping.
        Bash: bash core/scripts/aspirations-read.sh --source {source} --id {aspiration_id}
        → find the goal by goal_id; read substantive_hits / substantive_runs /
          last_substantive_at. Default 0 / 0 / null when the fields are ABSENT
          (legacy goals, or goals never closed via recurring-close.sh since the
          g-317-02 writer shipped -- the expected early-data case).
        runs = int(substantive_runs or 0); hits = int(substantive_hits or 0)
        rate = round(hits / runs, 3) if runs > 0 else None
        substantive_data[goal_id] = {"hits": hits, "runs": runs,
                                     "last": last_substantive_at, "rate": rate}
    # ALTERNATIVE aggregate/chronic view (ALL recurring goals, not just this
    # window): `bash core/scripts/cargo-cult-detector.py --audit-all --dry-run`
    # computes the same per-goal lifetime_hit_rate across world+agent queues and
    # flags chronic-low-rate rows. Use it when the report wants the full upkeep
    # track record rather than only this-window goals; parse its markdown table
    # (--dry-run suppresses the Idea-goal filing side effect).
    # Fail-open: a read error for any goal defaults that goal to runs=0 (graceful
    # omit in Phase 3) -- never error the report on missing substantive data.
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

## Contribution — what your upkeep protected
  {Recognition section (FW-5 / reward layer). The loop optimizes hard and rarely
   reflects value back; this is where it does. Reuses data already gathered in
   Phase 2 — do NOT recompute. Frame honestly: name real wins, never spin flat
   outcomes as movement (communication-clarity rule 6).}

  Goals delivered: {goals_completed_count} this window{, of which ~{routine_count}
   were upkeep (sweeps, cadence, reconciliation) — derive routine_count from
   routine_ratio × goals_completed_count when loop_state is present, else omit the clause}.
    → Upkeep is the connective tissue the whole team relies on. A sweep that returns
      0 today is the reason a regression didn't ship. Held cadence and clean sweeps
      are wins, not overhead (learning-philosophy.md "Recognition").
    {Lifetime upkeep track record (FW-5/R1, g-317-16) -- from substantive_data
     (Phase 2 step 13). For up to 5 recurring goals completed THIS window WHERE
     substantive_data[gid].runs > 0, replace the generic "returns 0 today"
     platitude with concrete proof the sweep HAS caught real things over its life.
     Order by runs DESC:}
      - {goal_id}: lifetime {hits} catch(es) / {runs} runs{", last catch " + last[:10]
        when last is not null} -- a non-zero lifetime catch count is evidence the
        cadence is load-bearing, not busywork.
    {GRACEFUL OMIT (the early-data default): IF NO recurring goal completed this
     window has runs > 0 -- the writer has not accumulated yet, or this window held
     only non-recurring goals -- show NONE of the per-goal lines above; keep only the
     generic recognition framing. NEVER print a "0/0" tally. Spinning an empty tally
     as a win violates communication-clarity rule 6.}
    → The journal's per-goal `Value:` line is the canonical one-line framing for each
      recorded outcome (DERIVED from outcome_class + work_class by
      `core/scripts/_value_framing.py`, value-framing-mapping.yaml). This
      session-level Recognition section is the aggregate of those same per-goal
      affirmations — keep the two surfaces consistent when either is edited (FW-5/R2,
      g-317-15).
  Learning banked: {confirmed} hypotheses confirmed + {corrected} corrected.
    → A corrected prediction is a belief fixed before it cost the team — count it
      equal to a confirmation, not as a miss.
  {IF debt_closure_events > 0:}
  Debt retired: {debt_closure_events} knowledge-debt item(s) closed across
   {len(debt_closure_node_keys)} node(s) — every future retrieval is cleaner for it.
  {IF outcome_delta_available AND any_outcome_moved:}
  Outcomes moved: {one-line summary from the moved source delta(s)} — the work
   reached the product.
  {ELIF outcome_delta_available AND divergence:}
  Outcomes flat this window — and that is stated honestly in Outcome Delta above.
   The counterweight: maintenance that prevents decay is value even when the needle
   doesn't move. The foundation held because of this window's upkeep.

  {IF nothing above produced a line (no goals, no hypotheses, no debt): omit the
   whole section — recognition must be earned to mean anything.}
  These wins are real. Carry them into the next session.

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

2. Write the report (overwrite the latest-pointer file):
   Write: agents/<agent>/COMPLETION-REPORT.md
   # This is the ONLY report file written. Its git history (COMPLETION-REPORT.md
   # is committed every iteration) IS the permanent archive — there is no
   # separate timestamped archive under reports/. The reports/ directory was
   # abolished by the file-model normalization (see
   # core/config/conventions/temp-store.md); writing there is denied by the
   # L1 allowlist gate.

3. Save outcome-metrics snapshot for next report's delta baseline
   # Tranche C — rb-390. If the current outcome-metrics.yaml exists, copy
   # it to agents/<agent>/session/last-outcome-snapshot.yaml so the NEXT completion
   # report computes "delta since last report" against this snapshot rather
   # than against the live file (which mutates every goal). Fail-open: if
   # the live file is missing, skip silently. session/ is machine-local and the
   # baseline is regenerable — losing it on a machine move just makes the next
   # report show "initial" deltas (benign).
   Bash: source core/scripts/_paths.sh && \
         [ -f "$WORLD_DIR/outcome-metrics.yaml" ] && \
         cp "$WORLD_DIR/outcome-metrics.yaml" \
            "agents/<agent>/session/last-outcome-snapshot.yaml" || true
```

Note: `agents/<agent>/COMPLETION-REPORT.md` is the single latest-pointer report, overwritten each cycle. Its git history (committed every iteration) is the permanent archive — there is no separate timestamped archive directory. Do NOT recreate a `reports/` directory; the file-model normalization abolished it and the L1 allowlist gate denies writes there (see `core/config/conventions/temp-store.md`). `last-outcome-snapshot.yaml` lives under `session/` and is overwritten each report (delta baseline only, machine-local, regenerable).

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
- message-file: `agents/<agent>/COMPLETION-REPORT.md`
  This is the latest-pointer file overwritten in Phase 4 Step 2 — it
  always reflects the just-finished report — it is the single report file
  written (its git history is the archive). This stable path eliminates the
  timestamp-threading drift between Phase 4 and Phase 5.5 that an earlier
  timestamped-archive design suffered. (N2 fresh-eyes finding, 2026-05-20.)

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
- **Modifies**: `agents/<agent>/session/last-report-timestamp`, `agents/<agent>/COMPLETION-REPORT.md`, `agents/<agent>/session/last-outcome-snapshot.yaml`

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is Phase 5.5's notification invocation (or its
`aspirations-add-goal.sh` fallback). The report files on disk are the
deliverable; do not append a text summary.
