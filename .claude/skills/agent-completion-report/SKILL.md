---
name: agent-completion-report
description: "Produces an agent completion report showing what changed since the last status marker: completed goals, new tree encodings, emitted findings, resolved hypotheses, and in-flight work. Use whenever the user asks \"what have you done\", \"what's the status\", \"give me a recap\", or requests a dashboard; also use when the agent needs to summarize progress before a handoff, stop, or consolidation checkpoint. Writes {agent}/COMPLETION-REPORT.md (its git history is the permanent archive; the reports/ directory is abolished)."
user-invocable: true
triggers:
  - "/agent-completion-report"
tools_used: [Bash, Read, Write]
companion_scripts: [core/scripts/completion-digest.sh, core/scripts/notify-user.sh]
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
   # BOTH stages are REQUIRED — a record can be archived within the report
   # window, so a resolved-only fetch UNDERCOUNTS any multi-day report while
   # staying exact for a same-session one. That split is why this site is the
   # least visible of the four and survived longest. Measured 2026-08-04T03:33
   # (bravo, hostname cc-05, uname -r 6.8.0-136-generic): of the scoreable
   # records in each trailing window, `--stage resolved` alone sees 100% at 2d
   # but only 75.9% at 7d and 29.8% at 30d — i.e. a week-long report silently
   # drops a quarter of its own subject matter. (g-115-4866.)
   #
   # ⚠ THOSE PERCENTAGES HAVE DECAYED — RE-MEASURE, DO NOT QUOTE THEM. Two
   # independent measurements on 2026-08-15, hours apart on different boxes,
   # each carrying its denominator (guard-3542 — a coverage rate over a moving n
   # is meaningless without it):
   #   bravo, cc-05, 6.8.0-137-generic, 216.1h window: 47 of 199 = 23.6% at 9d.
   #     Store split at that instant: 50 resolved vs 1,127 archived.
   #   alpha, cc-08, 6.8.0-137-generic,  48.5h window: 22 of  35 = 62.9% at 2d.
   #     Store split at that instant: 34 resolved vs 1,148 archived.
   # THE SECOND ONE IS THE LOAD-BEARING DATAPOINT, and it breaks the model the
   # rest of this comment implies. The 2026-08-04 row reads coverage as a
   # function of WINDOW WIDTH (100% at 2d, degrading to 29.8% at 30d), which
   # invites the reading that a short report is safe. It is not: the 2-DAY
   # figure itself fell 100% -> 62.9% in eleven days. A same-session report is
   # the ONLY case still near-exact, and even that is not guaranteed to hold.
   # So the resolved stage is not a time window with a fixed width; its
   # effective lookback is set by ARCHIVAL CADENCE, and that cadence keeps
   # tightening (2026-08-04: 86 resolved over ~4.5d; 2026-08-15: 50 then 34,
   # both over ~2.3d or less, at a HIGHER resolution rate).
   #
   # RE-MEASURED 2026-08-18 (echo, cc-03, 6.8.0-137-generic, 15.35h report
   # window). Store split at that instant: 49 resolved vs 1,204 archived.
   #   same-day (2026-08-18):    6 of   6 = 100%
   #   spanning 08-17 (~39h):   27 of  38 = 71.1%
   #   lifetime:                47 of 811 = 5.8%
   # THE 2-DAY FIGURE WENT BACK UP — 62.9% (08-15) -> ~71% (08-18). So the
   # decay narrated above is NOT monotonic, and reading it as a trend line is
   # the wrong model: it oscillates with archival cadence. Do not "correct" the
   # 08-15 row toward this one; both are true readings of a quantity that moves
   # in both directions, which is exactly why the instruction is to re-measure
   # rather than to quote.
   # AND DISCOUNT THE 100% (guard-2303 — check the instrument's resolution
   # before reporting a change-detection result): `outcome_date` is DATE-ONLY,
   # so a same-DAY window and the field's granularity are the same size. A
   # same-day 100% is partly the instrument saying "I cannot resolve inside
   # this window", not purely the store being complete. The 71.1% row spans two
   # calendar days and is the more informative one.
   #
   # SERIES — each entry (date, window, resolved-stage coverage, denominator).
   # APPEND YOUR READING HERE; do not add another dated paragraph. Five had
   # accumulated by 2026-08-21 and the sixth would have cost more to read than
   # the number is worth (learning-philosophy.md rule 5):
   #   08-04 2d 100% (n≈?) · 08-15 2d 62.9% (n=35) · 08-18 39h 71.1% (n=38)
   #   08-19 79.6h 73.5% (n=68) · 08-21 20.2h 81.1% (n=37, split 44/1261)
   #   08-21 9.7h 65.0% (n=20, split 48/1262)  <- SECOND 08-21 reading, ~8h later
   #   08-21 9.5h 75.0% (n=28, split 56/1262)  <- THIRD 08-21 reading, ~2h later
   #   08-22 25h(date-floored) 100% (n=22, split 60/1262)
   #   08-22 15.2h(date-floored 16.2h) 100% (n=14, split 62/1278)
   #   08-24 32.5h(date-floored 48.8h) 86.2% (n=29, split 73/1279)  <- 3 CALENDAR DAYS
   #   08-24 34.8h(date-floored 48.8h) 87.5% (n=32, split 76/1279)  <- SAME DAY, ~13min later, DIFFERENT BOX
   #   08-24 63.3h(date-floored 83.9h) 83.3% (n=66, split 78/1279)  <- WIDEST WINDOW IN THE SERIES
   #   08-25 13.3h(date-floored 25.3h) 63.0% (n=27, split 40/1335)  <- BACKLOG DRAINED 78->40, COVERAGE FELL WITH IT
   # THE THIRD 08-24 ROW REFINES THE BACKLOG MODEL THE OTHER TWO PROPOSED, and it is the
   # only row that can: it carries the HIGHEST backlog yet recorded (78 resolved) and still
   # scored the LOWEST of the three, because its window is ~1.7x wider and reaches back past
   # the archival horizon into days already swept. So backlog is not the sole driver either.
   # Reconcile them this way: WITHIN a fixed window, coverage tracks the backlog (the 13-min
   # pair proves that); ACROSS window widths, a window wide enough to cross the horizon pulls
   # in archived rows and drives coverage DOWN regardless of backlog. Both effects are real at
   # different scales, and neither licenses quoting a row — which is what the series already says.
   # THE TWO 08-24 ROWS CONFIRM THE DRIVER DIRECTLY, and they are the cleanest pair in
   # the series for it: 13 minutes apart, DIFFERENT BOXES (cc-0x, then foxtrot on
   # LAPTOP-3IOFCNEO / WSL2 6.18.33.2), near-identical window width, and coverage ROSE
   # 86.2% -> 87.5% while the resolved stage grew 73 -> 76 against an unchanged 1279
   # archived. Coverage tracked the BACKLOG, not the window and not the box. If window
   # width or box identity set coverage, these two would not move together like that.
   # THE FIRST 08-24 ROW IS THE HIGHEST NON-100% READING BEFORE IT AND IT IS NOT A
   # DATE-FLOOR ARTIFACT: it spans three calendar days, so guard-2303 does not
   # explain it. It is high because the RESOLVED STAGE WAS BACKED UP -- 73 resolved
   # against 44-62 in every prior row -- i.e. coverage rose because ARCHIVAL fell
   # behind, not because the instrument improved. Same driver, opposite direction
   # from the 08-15 low. Nothing here predicts your run; measure your own.
   # THE TWO 08-22 ROWS ARE BOTH 100% AND NEITHER MEANS THE INSTRUMENT IMPROVED: both
   # windows are SAME-DAY, and outcome_date is date-only, so the floor swallows the
   # whole window and the field cannot resolve inside it (guard-2303). The second row
   # scored 100% on a window 10h NARROWER than the first — read together with the
   # 08-21 trio, that is a fourth demonstration that width is not the driver.
   # THE THREE 08-21 ROWS ARE THE CHEAPEST PROOF OF THE INSTRUCTION BELOW: same day,
   # same box. 81.1% (20.2h) -> 65.0% (9.7h) -> 75.0% (9.5h). If window width set
   # coverage, the two NARROW rows would agree — they are 0.2h apart and they differ
   # by TEN POINTS, while the WIDEST row scored highest of all three. The store
   # barely moved across the whole day (44/1261 -> 48/1262 -> 56/1262). Archival
   # cadence is the only driver, so no row predicts any other.
   # READ THE SERIES, NEVER A ROW. It is not monotonic and not a function of
   # window width — a 3.3-DAY window (73.5%) beat a 2-DAY one (62.9%), and a
   # 20.2h window beat both. Only archival cadence sets it, so a NARROW window
   # buys you nothing and no row here predicts your run.
   #
   # The instruction is unchanged and is now stronger: read BOTH stages. What
   # changes is that no fixed percentage here can be trusted as guidance — the
   # number is a snapshot of a moving quantity (guard-390: an artifact
   # describing external state must cite what keeps it current; nothing does
   # here). COMPUTE the coverage in your own run — you already hold both
   # populations — and correct this comment in passing when it has moved again.
   Bash: bash core/scripts/pipeline-read.sh --stage resolved
   Bash: bash core/scripts/pipeline-read.sh --stage archived
   → Filter the UNION where outcome_date >= since date
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
   Bash: bash core/scripts/pending-questions-read.sh --status pending
   → JSON array of pending entries. This reader is shape-tolerant (flattens the
     dict-wrapper / list-with-wrapper / bare / mixed on-disk shapes via the same
     _load_questions logic the sweep sibling uses, rb-1786). Do NOT hand-roll a
     naive top-level `status == "pending"` scan of the raw YAML — it silently
     SKIPS entries nested inside a `{questions: [...]}` wrapper (g-115-3039).
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
      → The output is JSONL — one object per LINE, NOT a JSON array. A
        whole-stream json.load raises JSONDecodeError("Extra data: line 2
        column 1"). Parse line-by-line:
          [json.loads(l) for l in out.splitlines() if l.strip().startswith("{")]
      → A bare `except` around that parse is FORBIDDEN. It launders the raise
        into 0, and the report then states the board was silent — a claim about
        fleet coordination health. Measured 2026-08-03 (foxtrot): general 23 /
        findings 248 / coordination 436 / decisions 13 = 720 messages reported
        as 0. The 2026-08-02 report hit the identical zero and correctly
        diagnosed it in its NARRATIVE, and the next run reproduced it unchanged
        — because the narrative is not what the next agent executes; this file
        is. That is why the fix belongs here and not in another report.
      → POSITIVE-CONTROL a zero before believing it (guard-2421). This defect is
        self-concealing: an empty board is a plausible reading, so nothing
        prompts a second look. If a channel returns 0, re-read it at a wide
        window (--since 720h) or check for a known post-id. Cheapest check
        first: if you already hold a prior measurement that contradicts the
        empty, believe the prior one and re-read.
      → store messages per channel
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
            # BASELINE-CORRESPONDENCE ASSERTION (g-001-04, 2026-08-24). The delta
            # window is defined by `last-report-timestamp`; the baseline it diffs
            # against is this file. NOTHING guarantees they correspond, and when
            # they do not the report states a delta over the WRONG interval while
            # looking completely normal. Two independent ways they decorrelate:
            #   (1) a prior run wrote the timestamp and skipped this snapshot —
            #       Phase 4 step 3's `|| true` makes a skip, a failed cp and a
            #       success byte-identical after the fact;
            #   (2) sync-tier split: `last-report-timestamp` is sync_tier
            #       `continuity` (crosses machines) while this file is
            #       `machine_local` (does not) — session-manifest.yaml:397/896.
            #       A multi-box agent reporting from box B diffs against box B's
            #       last LOCAL snapshot, not against its own last report.
            # MEASURED 2026-08-24 (foxtrot, LAPTOP-3IOFCNEO): baseline
            # updated_at 2026-08-21T03:16:14 / mtime Aug 21 03:53 against a
            # last-report-timestamp of 2026-08-22T16:53:06 — a 3-DAY baseline
            # presented as a 35-hour delta. Cause was (1), proven by three
            # same-directory Phase-5/5.5 siblings (fleet-digest.md/.html,
            # digest-notes.md) all written 2026-08-22 16:55:08-09 on this box
            # while this file was not touched.
            baseline_age_note = None
            # GATE ON THE SNAPSHOT'S MTIME, NOT ITS updated_at — corrected
            # 2026-08-24 (echo, cc-03) on this assertion's FIRST live run, which
            # is what exposed it. `updated_at` records when the COLLECTOR last
            # wrote world/outcome-metrics.yaml; the snapshot is COPIED from that
            # file at report time, so updated_at is necessarily EARLIER than the
            # report timestamp on every healthy run and `updated_at < since`
            # fires ALWAYS. Measured here: updated_at 2026-08-21T20:00:07 against
            # since 2026-08-21T20:23:35 — a 23m28s collector interval rendered as
            # a window violation worth 0.6% of a 63.3h window. An assertion that
            # fires on every healthy run trains its reader to skip it, which is
            # precisely how the real 3-day case below would get missed.
            # MTIME is the right signal because it records when the snapshot was
            # TAKEN. It matched `since` to the second here (20:23:35.714 vs
            # 20:23:35), and it still catches the foxtrot failure case
            # decisively (mtime Aug 21 03:53 vs since 2026-08-22T16:53 = 37h).
            # Writers enumerated before trusting an mtime (guard-1504, rb-190):
            # session-manifest.yaml:898 names exactly ONE writer
            # (agent-completion-report Phase 4) at sync_tier machine_local, so no
            # sync layer and no background writer can move it. If that ever gains
            # a second writer, this gate is void — re-enumerate before trusting it.
            snapshot_mtime = mtime of agents/<agent>/session/last-outcome-snapshot.yaml
            IF since is not null AND snapshot_mtime is more than 60s BEFORE since:
                # The snapshot was not taken at the last report: a prior run wrote
                # the timestamp and skipped the copy. The delta spans MORE than the
                # report window, so every "moved" is over-stated.
                baseline_age_note = ("baseline snapshot was TAKEN {t} but the report "
                    "window starts {s} — a prior run wrote the timestamp and skipped "
                    "the snapshot, so this delta spans {d} extra hours and "
                    "OVER-STATES movement").format(...)
            # `updated_at` remains useful as SECONDARY colour (how stale the metrics
            # themselves were at snapshot time). Report it; never gate on it.
                # Do NOT suppress the section and do NOT silently widen the window:
                # print baseline_age_note verbatim in the Outcome Delta section
                # (guard-2841 — a silently-dropped row is indistinguishable from a
                # source that was never read).
        ELSE:
            outcome_prior = {}  # first report — deltas appear as "initial"
            baseline_age_note = None
        # Compute per-source deltas. Each source contributes exactly these
        # three keys (consumer contract — do not omit any):
        #   available: bool     — was the source present in outcome_now?
        #   moved:     bool     — did any observable field change value vs prior?
        #   delta_summary: str  — one-line human description for the report
        # Never error if a key is missing from outcome_prior or outcome_now —
        # the source shape is domain-specific and may change.
        # Sources nest under the top-level `sources:` key in outcome-metrics.yaml
        # (header written by outcome-metrics-collect.sh). A top-level .get("git")
        # returns {} for BOTH sides, every source reads moved=false, and the
        # divergence warning fires FALSELY on any >=5-goal window (observed
        # 2026-07-16 g-001-04 run — caught by reading the raw file before trusting
        # the empty delta).
        prior_src = outcome_prior.get("sources", {}) or {}
        now_src   = outcome_now.get("sources", {}) or {}
        git_delta       = compute_source_delta(prior_src.get("git", {}),      now_src.get("git", {}))
        ci_delta        = compute_source_delta(prior_src.get("ci", {}),       now_src.get("ci", {}))
        operator_delta  = compute_source_delta(prior_src.get("operator", {}), now_src.get("operator", {}))
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

## Message Board — {total messages} in {hours}h (or "lifetime")
  # Renders board_messages, collected in Phase 2 Step 10. This section was
  # ABSENT from Phase 3 until 2026-08-09 while Step 10 still stored the data:
  # board_messages was written and never read, so the rendering survived on the
  # executing agent's inference rather than on instruction. It happened to keep
  # working, which is why nothing surfaced it. Same lesson as Step 10's own —
  # what is not written here is not what the next agent executes.
  {For each channel in board_messages that has at least one message:}
    - {channel} ({N}): {For each message, max 10, most recent first:}
        {timestamp} {author}: {text, 120 chars}
      {IF N > 10:} ... and {N - 10} earlier messages
  {IF every channel is empty:}
    Do NOT write an unqualified "board quiet". Name the control that
    established the zero (e.g. "0 at 24h AND 0 at a 720h re-read") — an
    unqualified zero here IS the false zero Step 10 warns about, and this
    section is where it reaches the user as a claim about fleet coordination
    health. A genuinely quiet board is plausible, so nothing else will prompt
    a re-check.
  (Channels with zero messages are omitted.)

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
        when last is not null} -- {IF hits > 0: "a non-zero lifetime catch count is
        evidence the cadence is load-bearing, not busywork." ELSE: "a clean lifetime
        across {runs} runs -- the sweep has held its cadence and found nothing to
        report, which is the guard working, not the guard idling."}
     {THE hits == 0 CASE IS OWNED HERE, DELIBERATELY (g-115-4086). The predicate
      above is `runs > 0`, so a 17-run/0-hit goal IS selected -- but the original
      trailing clause asserted a non-zero catch count for every selected row, which
      is FALSE for that goal, and GRACEFUL OMIT below fires only when NO goal has
      runs > 0. So runs>0 && hits==0 fell between the two branches: the predicate
      said PRINT and the prose said the line meant nothing. Measured consequence
      (g-001-04, 2026-07-30): the report omitted the tally for a 17-run goal and
      published a WRONG reason -- "a partial write, not a measured zero" -- and one
      hour later that goal's deep close wrote hits=1, proving the absence had been a
      true zero all along. Do NOT "fix" this by tightening the predicate to
      `hits > 0`: that re-suppresses exactly the row this branch exists to print,
      and lines 553-554 above are the reason -- a sweep that returns 0 today is why
      a regression did not ship. This is NOT the "0/0" the GRACEFUL OMIT forbids;
      that prohibition is about a tally with no RUNS behind it, where nothing has
      been measured at all.}
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
   # VERIFY THE WRITE (g-001-04, 2026-08-24). The previous form ended in
   # `|| true`, which made three different outcomes byte-identical to every
   # later reader: cp succeeded, cp failed, step never ran. That is the
   # verify-before-assuming.md rule-4 silent-failure shape applied to a
   # baseline, and its cost is paid by the NEXT report, which diffs against a
   # stale file with no way to know. Measured: the 2026-08-22T16:53 run wrote
   # the report, the timestamp and the digest and left this snapshot at its
   # 2026-08-21 value; the staleness surfaced 35h later only because a reader
   # went looking. Read the copy BACK and compare `updated_at` — the echo of a
   # write is not evidence the intended content is on disk.
   Bash: source core/scripts/_paths.sh && \
         if [ -f "$WORLD_DIR/outcome-metrics.yaml" ]; then \
           cp "$WORLD_DIR/outcome-metrics.yaml" \
              "agents/<agent>/session/last-outcome-snapshot.yaml" && \
           echo "live:  $(grep -m1 '^updated_at:' "$WORLD_DIR/outcome-metrics.yaml")" && \
           echo "saved: $(grep -m1 '^updated_at:' agents/<agent>/session/last-outcome-snapshot.yaml)"; \
         else echo "SKIP: no live outcome-metrics.yaml (fail-open, expected on a fresh world)"; fi
   # The two `updated_at` lines MUST match. If they differ, or either is empty,
   # the baseline did not land — say so in the report rather than proceeding
   # silently; the next report's Outcome Delta depends on it.
```

Note: `agents/<agent>/COMPLETION-REPORT.md` is the single latest-pointer report, overwritten each cycle. Its git history (committed every iteration) is the permanent archive — there is no separate timestamped archive directory. Do NOT recreate a `reports/` directory; the file-model normalization abolished it and the L1 allowlist gate denies writes there (see `core/config/conventions/temp-store.md`). `last-outcome-snapshot.yaml` lives under `session/` and is overwritten each report (delta baseline only, machine-local, regenerable).

## Phase 5: Save Report Timestamp

```
1. Write current timestamp to agents/<agent>/session/last-report-timestamp:
   Bash: echo "$(date +%Y-%m-%dT%H:%M:%S)" > agents/<agent>/session/last-report-timestamp
```

## Phase 5.5: Notify the User — send the FLEET DIGEST, not the report

The completion report is a primary user-visibility event — the user should
receive it, not just find it on disk. But the file written in Phase 4 is BY an
agent FOR agents (forensic, trap-numbered, no goal listed by name); the user's
2026-08-17 direction is: "I do like receiving what goals are blocked or
assigned to me through the completion report email — make them easier to
read, be sure everything I need to quickly understand how it has been going is
in there — as long as I receive one every day or two that is good." So the
EMAIL is the deterministic user-facing digest, and the on-disk report stays
the agents' forensic record.

```
1. Build the digest (framework script reads the stores; do not hand-write it):
   Bash: bash core/scripts/completion-digest.sh --agent <agent> \
           --since <the since-timestamp Phase 1 resolved> \
           [--notes-file agents/<agent>/session/digest-notes.md] \
           --out agents/<agent>/session/fleet-digest.md \
           --html-out agents/<agent>/session/fleet-digest.html
   Both files carry the same data: the .md is the plain text the gates, ledger
   and dedup read; the .html is what the user actually opens (cards, tables,
   real bold — the 2026-08-17 "all text, hard to read, weird line breaks"
   feedback).
   --notes-file is OPTIONAL and bounded (<=12 lines): write it ONLY when you
   have something the reader needs that the stores cannot say -- a decision
   you took that he might override, a risk you see. Never restate the numbers.

2. Send it through the framework chokepoint (routing gate + fleet-wide
   prior-outreach dedup + sent ledger; the domain transport is the
   world/scripts/notify-transport.sh slot):
   Bash: bash core/scripts/notify-user.sh --agent <agent> --category user-digest \
           --subject "Fleet digest — $(date +%Y-%m-%d)" \
           --message-file agents/<agent>/session/fleet-digest.md \
           --builder-arg=--html-file --builder-arg=agents/<agent>/session/fleet-digest.html \
           --builder-arg=--disproof-waived \
           --builder-arg="deterministic store-derived digest (completion-digest.sh); goal titles and question text are quoted verbatim from the records, whose claims were gated when filed"
   The waiver is REQUIRED, not optional: the finding-disproof gate in
   notify-build-payload.py matches universal/causal wording, and a digest
   quoting 50 goal titles will always contain some. The digest is not an
   agent-authored finding — the builder is the author and it makes no claims of
   its own. Do NOT use this waiver for anything you wrote yourself.
   rc 0 = sent.  rc 4 = another agent (or world) already sent a digest inside
   the 20h window -- CORRECT, not a failure: the user asked for one every day
   or two, not one per agent per cadence. Do NOT --allow-duplicate a digest to
   get YOUR mail sent -- that is the failure mode, and it is the common one.
   DO override, with --allow-duplicate and the reason, when this digest carries
   something the prior send could not have: a finding measured AFTER it. The
   gate's own message names the operative test ("does this ADD anything he has
   not been told") -- answer it with the fleet-wide prior-outreach ledger scan
   the standing directive requires, positive-controlled so a zero is real, then
   log the override as a reversible pending question. A prior send is DATED
   EVIDENCE, NOT COVERAGE (guard-3438), and a do-not-re-relay note guards the
   content while saying nothing about the address (guard-3944). Measured
   2026-08-22 (zeta, g-001-04): 58 ledger rows, ZERO on the topic against a
   positive control of 21 -- the refusal was correct and the override was too.
   rc 3/5/6 = see /notify-user Step 2; never block on it, the report is on disk.
```

Category is `user-digest` (ALWAYS_SEND in `notification_routing_gate.py`) —
NOT `completion`, which is the status-blurb category the routing gate
suppresses fleet-wide (processor runs, rollbacks, per-goal closes). The digest
is the batched "what needs you / what is blocked / how it is going" list that
suppression re-routes INTO, so it is the one email the policy exists to keep.

The digest builder consumes the stores directly (`--message-file` semantics
preserved: the file on disk IS the deliverable, never a re-summarised prose
blurb — the 2026-05-20 blank-email incident). `notify-build-payload.py`
refuses a too-short body with rc=2, so a missing digest fails loud.

If `core/scripts/notify-user.sh` reports rc 5 (no transport slot in this
world), fall back to a `participants: [agent, user]` goal via
`aspirations-add-goal.sh` with title `"User Notice: Fleet digest available"`
and `origin_signal: "idea:completion-report-available"`. Never block
completion-report generation on notification failure.

The skill ends here. Goal status management (if any) is the caller's responsibility.

## Chaining

- **Called by**: User directly, OR by other skills (e.g., status report wrappers)
- **Calls**: `core/scripts/completion-digest.sh` (digest builder) then `core/scripts/notify-user.sh --category user-digest` (framework dispatcher → domain transport slot) in Phase 5.5
- **Modifies**: `agents/<agent>/session/last-report-timestamp`, `agents/<agent>/COMPLETION-REPORT.md`, `agents/<agent>/session/last-outcome-snapshot.yaml`, `agents/<agent>/session/fleet-digest.md`

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is Phase 5.5's `notify-user.sh` invocation (or its
`aspirations-add-goal.sh` fallback). The report files on disk are the
deliverable; do not append a text summary.
