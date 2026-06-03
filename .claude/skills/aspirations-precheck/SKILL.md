---
name: aspirations-precheck
description: "Runs pre-selection checks at the start of every aspirations-loop iteration: completion runners, aspiration health, guardrail checks, blocker resolution re-probe, zombie-aspiration scan (Phase 0.5.0a), and recurring-goal surfacing. Use whenever the aspirations loop starts a new iteration and needs to tidy state before /aspirations-select runs. Internal sub-skill — invoked only from inside the orchestrator, never by the user."
user-invocable: false
parent-skill: aspirations
conventions: [aspirations, infrastructure, goal-schemas]
minimum_mode: autonomous
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-aspirations-precheck-f82982"
previous_revision_id: null
---

# /aspirations-precheck — Pre-Selection Checks

Runs all checks that must happen BEFORE goal selection each iteration.
Ensures completion runners fire, aspiration health is maintained,
guardrails are checked, blockers are resolved, and recurring goals are tracked.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter.

**Step 0a: Budget Meter Start (Magic Wand 2 — g-115-509)** — initialize the
precheck wall-clock cost meter. The meter tracks elapsed-ms across sweeps and
returns "drop" for deferrable sweeps when the zone is `tight` OR cumulative
elapsed exceeds `precheck.budget_pct * iteration_budget_ms` (config in
`core/config/aspirations.yaml`). Always-run sweeps (Phase 0-pre, 0-pre2,
0-pre3) NEVER drop. Decisions log to `agents/<agent>/session/precheck-drops.jsonl`.

```
Bash: bash core/scripts/aspirations-precheck-budget-meter.sh start
```

Before any sweep that the table below marks as `medium` or `deferrable`, call
`meter check <sweep-name>` and skip the phase when the response is `drop`.
Always-run sweeps skip the check (they never drop). At the bottom of this
SKILL.md (after Phase 1) call `meter end` to write the summary record.

| Phase | Sweep name (for `meter check`) | Tier |
|---|---|---|
| 0-pre | tree-debt-gate | always-run |
| 0-pre2 | experience-archival-gate | always-run |
| 0-pre3 | fresh-eyes-code-gate | always-run |
| 0 (Recurring Safety Net) | aspirations-recover-recurring | medium |
| 0 (Monitor Stale) | monitor-stale-check | medium |
| 0.5.0 | precheck-eval | medium |
| 0.5b.0.5 | blocker-recheck | medium |
| 0.5b.1b | inbox-alert-age-check | medium |
| 0.5b.3 | precondition-defer-recheck | medium |
| 0.5b.4 | defer-recheck | medium |
| 0.5b.5 | pending-questions-sweep | deferrable |
| 0.5b.6 | parent-supersession-sweep | deferrable |
| 0.5b.7 | unblock-parent-status-sweep | deferrable |
| 0.5c | recurring-precondition-sweep | deferrable |
| 0.5e | fresh-eyes-cadence | deferrable |
| 0.5e.5 | fresh-eyes-program-cadence | deferrable |
| 0.5e.7 | fresh-eyes-tree-cadence | deferrable |
| 0.5f | felt-sense-cadence | deferrable |
| 0.5g | l1-skew-cadence | deferrable |

Drop semantics — the meter ONLY drops sweeps when:
1. `tier == always-run` → never drop
2. `zone == tight` AND `tier ∈ zone_drop_rules.tight` (default `[deferrable]`)
3. `tier == deferrable` AND cumulative `elapsed_ms > cap_ms` (budget overrun)

Fail-open: any meter error returns `run`. The meter is velocity optimization,
not safety gating — never block the loop on a meter bug.

## Inputs (from orchestrator)

- Aspirations compact data (loaded at loop entry or refreshed here)

## Outputs (to orchestrator)

- Compact aspirations refreshed in context
- Blockers updated in working memory
- Any auto-completed goals logged

## Phase 0-pre.0: Live Partner Snapshot

Read the live cross-agent snapshot once per iteration (cheap — single YAML
read with file lock). Surface partner.in_flight in the iteration header
so the LLM sees it before any reasoning about partner state.
See `core/config/conventions/coordination.md` "in_flight Field".

```
Bash: team-state-read.sh --json
partner = read agent_status.<partner-name>  # bravo if MIND_AGENT=alpha, vice versa
IF partner.in_flight is non-null:
    age_min = (now - partner.in_flight.claimed_at).total_seconds() / 60
    Output: "▸ Partner ({partner-name}): in_flight {partner.in_flight.goal_id} '{partner.in_flight.title[:40]}' phase={partner.in_flight.phase} ({age_min:.0f}m ago)"
ELSE IF partner.last_active:
    age_min = (now - partner.last_active).total_seconds() / 60
    Output: "▸ Partner ({partner-name}): no in_flight | last_active {age_min:.0f}m ago"
# Stash partner snapshot in iteration context for downstream phases (select drops
# partner.in_flight.goal_id from candidates; execute Phase 4 re-reads for the
# claim-conflict gate — re-read keeps single source of truth even if partner
# transitioned between this read and the claim attempt).
```

## Phase 0-pre.0b: Boredom Signal Surface (observability, no action)

Surfaces the routine-drift counters that Phase 4.1 of the aspirations loop
mutates (Blocks A/B/C of signal mutation — see
`core/config/aspirations-loop-digest.md` §Signal Mutation and
`core/config/rationale/signal-mutation.md`). The auto-deep flips still fire
in Phase 4.1 whether or not this display runs — this phase is pure
observability so the LLM sees its own pattern-matching risk BEFORE selecting
the next goal. Mirrors the bash-emitted one-line pattern of Phase 0-pre.0.

No state mutation. Fail-open: if `wm-read.sh loop_state` returns `null`
(first iteration, fresh session, or counters not yet seeded), skip the
display entirely.

```
Bash: wm-read.sh loop_state
Parse:
    global     = loop_state.session_signals.routine_streak_global   (int, default 0)
    per_goal   = loop_state.routine_streaks                          (dict, default {})
    total_rt   = loop_state.session_signals.routine_count_total      (int, default 0)
    completed  = loop_state.goals_completed_this_session             (list, default [])

IF loop_state is null OR (global == 0 AND no per_goal value > 0):
    SKIP — clean streak state, no signal to surface

# Build one-line summary:
top3       = sorted per_goal entries by value desc, keep value > 0, take 3
per_goal_s = ", ".join(f"{gid}={n}" for gid,n in top3) or "none"
ratio_s    = f"{total_rt}/{len(completed)} routine" if completed else f"{total_rt} routine"

IF global >= 6:
    # Near-threshold warning. Auto-deep flip at 8 is two iterations away.
    # Bravo's complaint (2026-04-23): "When 7 routines stack up, I start
    # pattern-matching rather than reasoning." This surface makes the
    # counter visible BEFORE the flip, not silently after.
    Output: "▸ ⚠ BOREDOM: routine_streak_global={global} (auto-deep at 8) | per-goal: {per_goal_s} | session: {ratio_s} — pattern-matching risk, deepen reasoning on next selection"
ELSE:
    # Informational only — streak exists but below near-threshold.
    Output: "▸ Boredom: routine_streak_global={global} (auto-deep at 8) | per-goal: {per_goal_s} | session: {ratio_s}"
```

## Phase 0-pre.0c: Stash Carryover Probe (g-115-1133)

Cheap per-iteration observability surface in the same cluster as Phase 0-pre.0
(partner snapshot) and 0-pre.0b (boredom). Surfaces non-empty `git stash`
entries, which are invisible to `git status` and therefore an undetectable
cross-session/cross-agent working-tree carryover risk. Origin incident
(g-115-1127): an externally-created stash (92543f81 at 12:15) silently shelved
~10 min of alpha-LLM working-tree work before manual recovery via
`git checkout 92543f81 -- <files>`. Defense-in-depth observability, NOT a gate
— it never blocks, defers, or mutates state; it only warns.

No state mutation. Fail-open: any git error (no repo, detached state) is
swallowed — never blocks the loop. Quiet on the common empty case.

```
# Single local git read (sub-second; reads .git/refs/stash).
Bash: git stash list 2>/dev/null
IF output is non-empty:
    count  = number of stash entries (lines)
    first5 = first 5 entries
    Output: "▸ ⚠ STASH CARRYOVER: {count} non-empty git stash(es) — invisible to `git status`, possible cross-session/cross-agent working-tree carryover. First {min(5,count)}:"
    FOR EACH entry in first5: Output: "    {entry}"
    Output: "    Recover via `git stash show -p stash@{N}` then `git stash pop`, or `git stash drop` once confirmed obsolete (g-115-1127)."
# ELSE: clean — emit nothing (quiet on the common empty case).
```

## Phase 0-pre: Tree-Debt Critical Gate (g-115-81, source-dispatch g-115-721)

Runs BEFORE Phase 0 completion checks. Consumes the `force_tree_maintain` WM
signal set by:
  - iteration-close.sh learning-gate when tree debt exceeds
    `tree_debt_check.debt_threshold * 3` (default 60). Heavy: invoke
    /tree maintain --backlog.
  - tree-encoding-drift-gate.py dual-write (g-115-700) when encoding-drift
    threshold crossed (default 3). Lightweight: drift acknowledged via
    log-and-clear; no /tree maintain invocation. Without source-based
    dispatch, encoding-drift fires /tree maintain --backlog every 3 closes
    regardless of actual tree-debt (witnessed iter-19/21/23 of bravo session,
    backlog 145 stays static, 0-action passes accumulate). g-115-721 fix.

Prevents the LLM-abbreviation drift that grew backlog 85→151 over sessions
48-50 — the obligation is now bash-emitted and mandatory, not an advisory
INFO line that gets skipped under context pressure.

```
Bash: wm-read.sh force_tree_maintain --json
IF signal is not null:
    source = signal.get("source", "tree-debt-critical")
    IF source == "encoding-drift":
        Output: "▸ TREE-ENCODING-DRIFT GATE: force_tree_maintain source=encoding-drift threshold={threshold} — drift acknowledged, no /tree maintain invocation (lightweight path, g-115-721)"
        # No /tree maintain. The encoding work was supposed to fire via
        # aspirations-state-update Step 8 force_tree_encoding consumer but
        # the routing path bypassed it. The dual-write (g-115-700) was meant
        # as a backstop but /tree maintain --backlog is the wrong action for
        # encoding-drift (it does decompose/distill backlog drain, not
        # missing-encoding catch-up). Acknowledging the drift via clear-only
        # path lets the agent continue without 0-action heavy maintenance.
        # When tree-debt actually becomes critical (debt > threshold * 3 = 60),
        # iteration-close.sh learning-gate writes the signal with
        # source=tree-debt-critical (or missing), routing to the heavy path.
    ELSE:
        # source=tree-debt-critical or missing (legacy/learning-gate path)
        Output: "▸ TREE-DEBT GATE: force_tree_maintain set (source={source}, count > threshold) — invoking /tree maintain --backlog before goal selection"
        invoke /tree maintain --backlog
    # Clear signal after handling (one-shot; next iteration's drift gate or
    # learning-gate re-sets if still indicated).
    echo 'null' | Bash: wm-set.sh force_tree_maintain
    # Continue to Phase 0 (compact data may now be stale — Phase 0.5 re-reads it).
```

## Phase 0-pre2: Experience Archival Gate (rb-428)

Runs AFTER tree-debt gate, BEFORE Phase 0 completion checks. Consumes the
`force_experience_archival` WM signal set by `experience-staleness-check.sh`
when `experience.jsonl` is stale beyond the configured threshold (default 12h,
tunable via `EXPERIENCE_STALENESS_HOURS` or `experience_archival_gate.staleness_hours`
in `core/config/aspirations.yaml`). Forces the LLM to compose and submit the
missed experience record before goal selection proceeds. Prevents the 30–76h
drift that g-248-07 surfaced — the bash canary (g-248-16) detects it, this
gate forces action.

Pattern mirrors the tree-debt gate above verbatim — wm-read → if non-null →
action → wm-set 'null'. One-shot retry: if composition fails, the sentinel
persists and the gate fires again next iteration until the LLM succeeds.

```
Bash: wm-read.sh force_experience_archival
IF signal is not null:
    Output: "▸ EXPERIENCE-ARCHIVAL GATE: force_experience_archival set (last entry {last_entry_id}, {age_hours}h stale) — composing missed experience record before goal selection"
    # Compose the missed experience record per Phase 4.25 instructions
    # (execute-protocol-digest.md §4.25). Use the most-recent deep goal's
    # context reconstructible from:
    #   - working memory: wm-read.sh goals_completed_this_session
    #   - prior iteration's journal entry: agents/<agent>/journal/YYYY/MM/YYYY-MM-DD.md
    #   - retrieval-session.json (tree_nodes_loaded)
    # If no deep goal is reconstructible (e.g., routine-only streak), write a
    # minimal placeholder record with a short `note` explaining the gap, AND
    # clear the sentinel — do not loop.
    <compose experience-add.sh JSON payload>
    echo '<payload-json>' | Bash: experience-add.sh
    # Clear signal after successful write (one-shot).
    echo 'null' | Bash: wm-set.sh force_experience_archival
    # Continue to Phase 0.
```

The gate does NOT write `stop-requested` or `stop-loop` — it blocks goal
selection via the precheck pattern (LLM sees sentinel, acts, clears it) rather
than terminating the loop.

## Phase 0-pre3: Fresh-Eyes-Code Dispatch Gate (g-115-281)

Runs AFTER experience-archival gate, BEFORE Phase 0 completion checks. Consumes
the `fresh_eyes_dispatch_pending` WM signal set by `iteration-close.sh
do_state_update` when `post-state-update-gate.sh` fires (default thresholds:
≥3 core files changed OR ≥150 LOC OR new script under `core/scripts/`).
Forces /fresh-eyes-code dispatch before goal selection so the deferred review
backlog cannot accumulate across iterations the way it did before this gate
existed (33 core files / 605 LOC silently buffered when no consumer was wired).

Pattern mirrors Phase 0-pre and Phase 0-pre2 verbatim — wm-read → if non-null →
action → wm-set 'null'. The signal payload is the full gate JSON
(`{"fired":true,"core_count":N,"loc_changed":N,"reason":"...","files":[...]}`),
so the dispatcher has the file list + reason without re-running the gate.
Cross-references: rb-428 (sentinel-lifecycle pattern), guard-343 (post-state-update
review enforcement), g-115-280 (gap discovery — Phase 0-pre/0-pre2 had consumers,
this slot did not).

```
Bash: wm-read.sh fresh_eyes_dispatch_pending --json
IF signal is not null AND signal.fired == true:
    Output: "▸ FRESH-EYES-CODE GATE: fresh_eyes_dispatch_pending set ({signal.core_count} core files, {signal.loc_changed} LOC, reason={signal.reason}) — invoking /fresh-eyes-code before goal selection"
    invoke /fresh-eyes-code with files = signal.files
    # Clear signal after dispatch (one-shot; next iteration's iteration-close
    # re-fires the gate if state-update produces new substantive changes).
    echo 'null' | Bash: wm-set.sh fresh_eyes_dispatch_pending
    # Continue to Phase 0.
```

The gate does NOT write `stop-requested` or `stop-loop` — same precheck pattern
as Phase 0-pre and Phase 0-pre2. Reversibility: post-state-update-gate.sh has
its own cooldown logic (compares current file set against last-fire snapshot in
WM); flipping its threshold envs (`CORE_FILE_THRESHOLD`, `LOC_THRESHOLD`) to
unreachable values stops sentinel writes without touching this consumer.

## Phase 0-pre4: Metric-Encoding Dispatch Gate (g-115-724, rb-917)

Runs AFTER fresh-eyes-code dispatch gate, BEFORE Phase 0 completion checks.
Consumes the `force_metric_encoding_pending` WM signal set by
`iteration-close.sh do_state_update` when `post-state-update-metric-gate.sh`
fires on deep-outcome closures. Content-gate sibling to the rb-428 counter-gate
family (tree-debt, experience-archival, fresh-eyes-code, tree-encoding-drift)
— catches "LLM did the encoding step on the wrong content" rather than
"LLM skipped the encoding step entirely."

Canonical incident (g-115-707): an agent closed a deep-outcome goal with
measurable production metrics in outcome_note prose. Goal had
`verification: null` — the Verified Values lived in free-form outcome_note. No
bash gate inspected the content. Encoding lagged ~50 min until a partner
agent's refresh sweep caught it manually. Filed g-115-707 Investigate →
rb-917 + content-vs-counter-gate decision rule + g-115-724 Apply.

Pattern mirrors Phase 0-pre/0-pre2/0-pre3 verbatim — wm-read → if non-null →
action → wm-set 'null'. The signal payload includes `candidates`,
`candidate_node_key`, `candidate_node_file`, `distinct_count`, `reason`,
so the LLM has the extracted findings + recommended target node without
re-running the gate or re-scanning prose.

```
Bash: wm-read.sh force_metric_encoding_pending --json
IF signal is not null AND signal.fired == true:
    Output: "▸ METRIC-ENCODING GATE: force_metric_encoding_pending set ({signal.distinct_count} distinct findings, target node={signal.candidate_node_key}, reason={signal.reason}) — encoding into tree before goal selection"
    # LLM action: encode the extracted findings as Verified Values into the
    # recommended tree node (candidate_node_file). Use the candidates list
    # (each entry "value :: context") to assemble the Verified Values block.
    # If the recommended node feels wrong for the findings (e.g., category
    # match was weak), pick a better-fit node — the gate's suggestion is
    # advisory, not authoritative. tree-edit-since.py probes mtimes, so any
    # in-iteration tree edit clears the gate's preconditions for next time.
    Edit {signal.candidate_node_file} OR /tree edit {better-fit-key}:
        Add a Verified Values entry naming each candidate
        Include the source goal-id + completion timestamp
        Cross-reference rb-917 / g-115-707 for the pattern lineage
    # Clear signal after encoding (one-shot; next iteration's iteration-close
    # re-fires the gate if state-update produces new substantive metrics).
    echo 'null' | Bash: wm-set.sh force_metric_encoding_pending
    # Continue to Phase 0.
```

The gate does NOT write `stop-requested` or `stop-loop` — same precheck pattern
as Phase 0-pre/0-pre2/0-pre3. Reversibility: edit `post-state-update-metric-
gate.sh`'s `DISTINCT_COUNT_THRESHOLD` to a high value (e.g. 999) to stop
sentinel writes without touching this consumer.

## Phase 0: Automated Completion Checks

Run completion check runners to auto-detect completed goals.

### File Existence Checks
For each goal with `verification.checks` containing `type: "file_check"`:
- If `goal.recurring`: skip
- If file exists at path: mark goal completed, log

### Pipeline Count Checks
For each goal referencing pipeline counts:
- If `goal.recurring`: skip
- `Bash: pipeline-read.sh --counts` — if threshold met: mark completed

### Config State Checks
For each goal referencing config fields:
- If `goal.recurring`: skip
- Read config file, check field value — if matches: mark completed

### Readiness Gate Checks
Check each readiness gate from `aspirations-read.sh --meta`.
`Bash: aspirations-meta-update.sh --source world readiness_gates '<JSON>'`

### Recurring Goal Safety Net
```
Bash: aspirations-recover-recurring.sh --source world
Bash: aspirations-recover-recurring.sh --source agent
```
Bash-enforced (g-001-160, rb-295): the script recovers both (a) `recurring=true
AND status=completed` and (b) shape-recurring corrupted goals (`recurring=false
AND status=completed AND interval_hours AND lastAchievedAt`). Idempotent — safe
to call every iteration. Prints JSON `{recovered: N, goals: [...]}`.

Previously an LLM-executed pseudocode step with archive-sweep as the only
bash backstop; drift across iterations let corrupted goals persist for days
(g-226-22 went 6 days before iter-51 of bravo session 51 caught it manually).
The bash command is now the primary path; the archive-sweep recovery branch
remains as a full-cycle fallback.

### Monitor Stale Check (g-240-37)
```
Bash: monitor-stale-check.sh --apply
```
Bash-enforced: for each pending/in-progress Monitor goal whose title contains
`proc-NNNNNNNNNN`, compares the embedded proc-ID against the current run_dir
reported by `processor-run.sh check-complete`. If the goal's ID is older AND
its age exceeds 48h, the script auto-completes it with `outcome_note:
superseded-by-newer-run`. Fails open: if processor-run.sh returns no run_dir
(fresh install, no completed runs yet), the sweep skips entirely. Prevents
Monitor goals from lingering as permanent pipeline debris after their target
runs complete.

### Hypothesis Expiration Checks
```
For each goal with hypothesis_id AND status pending/in-progress:
    if now > goal.resolves_by:
        mark status = "expired"
        move pipeline file to archived/
```

## Phase 0.5: Aspiration Health Check

```
Bash: load-aspirations-compact.sh → IF path returned: Read it
active_count = count of aspirations with status "active"
if active_count < 2:
    invoke /create-aspiration from-self --plan
    log "Aspiration health: below minimum, created new aspirations"

# Consolidation health snapshot — writes the consolidation_health WM slot
# consumed by /create-aspiration Step 1 (consolidation-gate), /aspirations-
# select Phase 2.55 (near-complete/stalled bias), and /aspirations-evolve
# Step E (gap-analysis refusal when portfolio fragmented). Before this
# write, all three readers fail-open on missing slot — gates were silently
# no-op. Fix: 2026-04-22 signal-lifecycle-gate finding.
# Single-writer rule: this is the ONLY writer for consolidation_health.
# Fail-open: wrapper is pure computation + wm-set; errors log to stderr
# and do not block the loop.
Bash: bash core/scripts/consolidation-health.sh --write >/dev/null || echo "[precheck] WARN: consolidation-health snapshot failed (non-fatal; gates will fail-open this iteration)"
```

## Phase 0.5.0-pre2: Self-Drift Gate (Tranche C.5 — rb-390)

Escalation path for class_balance drift. When work_class distribution stays
below a critical fraction of its configured target, file an Unblock goal
forcing the agent to correct the mix or retune the target. Config:
`aspirations.yaml → self_drift_gate`. Natural-gated (SSOT, rb-395): inactive
until BOTH `class_balance.targets` is non-empty AND
`self_drift_gate.target_aspiration_id` is set. Fail-open: no targets, no
target aspiration, or insufficient iterations all exit 0 with no side
effects. Cooldown-gated so the same drift does not spawn goal after goal.

```
Bash: bash core/scripts/self-drift-gate.sh 2>/dev/null || true
# Reads wm.goals_completed_this_session + aspirations.yaml config.
# When fires: writes one Unblock goal per drifted work_class via
#   aspirations-add-goal.sh --source agent, posts a coordination board
#   status, logs to agents/<agent>/session/self-drift-log.jsonl for cooldown.
# Cheap — just fraction arithmetic. No external calls.
# MUST invoke the .sh wrapper — `bash self-drift-gate.py` parses the
# Python docstring as shell and silently no-ops under `|| true`.
```

## Phase 0.5.0-pre: Signal-Refresh Hook (Tranche C — rb-390)

Hook slot for domain-supplied signal refresh before goal selection. Consumed by
goal-selector.py criterion 7d (`user_signal_boost`) — when a fresh snapshot is
present the scorer picks up inbox replies, pending-question silence, directives;
when absent the scorer contributes zero. Fail-open at every layer — a missing
or broken hook does NOT block iteration entry.

Pattern B hook slot (`signal-refresh`). See
`core/config/conventions/domain-hooks.md`. Core names the slot, the world
convention (if it exists) names what to run.

```
Bash: load-conventions.sh signal-refresh → IF path returned: Read it
# Procedural convention — gate on file EXISTENCE, not load status.
Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/signal-refresh.md" && echo "exists"
IF exists:
    Follow each Step in the convention.
    Any step that fails SHOULD be logged and swallowed — never abort precheck.
ELSE:
    # No domain signal-refresh convention exists (fresh agent). Nothing to do.
    # goal-selector's user_signal_boost dimension will see no snapshot and
    # contribute zero — correct fail-open behavior.
```

## Phase 0.5.0: Scripted Precheck

A single Python invocation performs the entire precheck sweep (zombies,
pipeline depth, hypothesis health, accuracy, consolidation, unproductive
cycles, user-goal reclassification). Read the JSON summary, act on each
flag, then SKIP directly to Phase 0.5a.

This is the ONLY path. The previous toggle + LLM-fallback pseudocode was
removed 2026-04-20 — the script is the single source of truth. Any bug in
`precheck-eval.py` MUST be fixed in the script, not patched around by
reintroducing a shadow LLM path here.

```
Bash: bash core/scripts/execution-diary.sh phase-start phase-0.5.0-scripted
Bash: bash core/scripts/precheck-eval.sh run-all
Bash: bash core/scripts/execution-diary.sh phase-end phase-0.5.0-scripted
# Parse the JSON. `flags[]` entries are prefixed with their subcommand
# (e.g. `zombies:needs_complete_review`) because run-all merges sub-reports.
# Action data lives in `results.<subcommand>.*` — the flag signals which
# action block to enter; the payload lives with the subcommand's result.
#
#   zombies:needs_complete_review      → for each entry in results.zombies.zombies[]:
#                                        invoke /aspirations-complete-review Phase 7.4
#                                        with that aspiration and reason="zombie-scan"
#   pipeline-depth:thin_pipeline       → invoke /create-aspiration from-self
#   hypothesis-health:stalled_pipeline → /review-hypotheses --resolve
#   accuracy:accuracy_low              → file Investigate goal targeting
#                                        results.accuracy.worst_strategies
#   consolidation:shallow_portfolio    → consolidation pressure — state-update
#                                        scoring handles it (no direct action)
#   consolidation:stalled_aspirations  → surface results.consolidation.stalled[]
#                                        in output for user visibility
#   cycles:cycles_detected             → for each entry in results.cycles.cycles[]:
#                                        file Investigate goal, using entry.reason
#                                        ("repeated_failure" or "zero_learning_velocity")
#                                        to shape the goal description
#   user-goals:reclassifiable_user_goals → for each entry in
#                                        results.user-goals.candidates[]: invoke
#                                        aspirations-update-goal.sh participants '["agent"]'
#   temp-pressure:temp_drain_needed    → file results.temp-pressure.suggested_goal
#                                        via aspirations-add-goal.sh (title/priority
#                                        HIGH/participants ["agent"]/description from
#                                        the payload) into asp-001, then the loop
#                                        executes /drain-temp. Dedup is already done
#                                        by the check (it suppresses this flag when an
#                                        open drain goal exists — emits temp_drain_pending
#                                        instead). temp_pressure_warn / temp_drain_pending
#                                        are visibility-only — surface in output, no goal.
```

## Phase 0.5a: Pre-Selection Guardrail Check

```
Bash: bash core/scripts/execution-diary.sh phase-start phase-0.5a-guardrails
Bash: matched=$(bash core/scripts/guardrail-check.sh --context any --phase pre-selection --type both 2>/dev/null)
IF matched.matched_count > 0:
    FOR EACH guardrail in matched.matched:
        Bash: <run {guardrail.action_hint}>
        IF output reveals issues:
            → invoke CREATE_BLOCKER(affected_skill, issue_description, ...)
Bash: bash core/scripts/execution-diary.sh phase-end phase-0.5a-guardrails
```

## Phase 0.5b: Blocker Resolution Check

```
Bash: bash core/scripts/execution-diary.sh phase-start phase-0.5b-blockers
Bash: wm-read.sh known_blockers --json
IF known_blockers is non-empty:
    FOR EACH blocker WHERE resolution is null:
        # PRIMARY: Did unblocking goal complete?
        IF blocker.unblocking_goal completed: resolve

        # Pre-probe retrieval (G5 / R7): load prior probe-attempt RB before
        # firing the canonical companion script. Without this, repeated
        # blocker re-probes use the same synthetic shape session after session
        # and miss the canonical-probe lesson encoded in rb-246 / guard-147.
        # Per .claude/rules/retrieve-before-deciding.md decision point 4
        # ("re-probing a blocker") and .claude/rules/probe-with-canonical-code-path.md.
        Bash: retrieve.sh --category "blocker probe {blocker.component} canonical companion script" --depth shallow
        From the returned JSON:
          - guardrails[] mentioning canonical-probe, companion-script, synthetic-probe
          - reasoning_bank[] entries describing prior probes of {blocker.component}
            or its sibling components (same skill family)
        Surface to the probe step below:
          - The exact companion_script name(s) the canonical probe should call
          - Failure modes of synthetic probes for this component (rb-246, rb-225)
          - Any prior probe-attempt RB that already established the component
            is fail-open / no-probe (skip the probe, go straight to resolution)
        Fail-open: if retrieve.sh errors, log and proceed to the probe.

        # SECONDARY: Probe infrastructure — but no_probe means "unknown", not "broken"
        Bash: result=$(bash core/scripts/infra-health.sh check {component})
        IF status == "ok": resolve
        ELIF status == "provisionable": attempt provisioning
        ELIF status == "no_probe":
            # No probe exists — can't verify either way.
            # After 3 sessions, clear the blocker and let goals fail-fast if still broken.
            # Re-encountering the failure will re-create the blocker with fresh evidence.
            IF blocker.detected_session + 3 <= current_session:
                resolve with reason "no_probe: cleared after 3-session expiry (fail-open)"
                Log: "BLOCKER EXPIRED (no_probe): {blocker.blocker_id} — letting goals attempt"
        ELSE: log probe failed

    # Phase 0.5b.0.5: Capability Recheck Sweep (see core/scripts/blocker-recheck.sh)
    # For aged blockers routed to [user] or [agent, user], re-run the capability
    # gate against the original failure_reason. If the gate now matches an
    # agent-provisionable capability that was overlooked at creation time,
    # auto-clear the blocker and write an Investigate goal so the retrieval
    # lapse gets learned from instead of buried.
    #
    # Runs BEFORE Phase 0.5b.1 so the user is not notified about a blocker
    # that is actually agent-fixable right now. Dry-run by default — pass
    # --apply to actually clear. Recommended cadence: every aspiration-loop
    # iteration, max-age-hours = config.proactive_escalation.blocker_age_hours.
    IF known_blockers is non-empty:
        Bash: bash core/scripts/blocker-recheck.sh \
                --max-age-hours {config.proactive_escalation.blocker_age_hours} \
                --apply
        # The script's JSON output includes `cleared` count and
        # `investigate_goals_created`. If cleared > 0, those blockers are
        # already resolved in working memory and their unblocking goals are
        # pending; skip to next iteration.

    # Phase 0.5b.1: Proactive escalation for aged blockers
    IF config.proactive_escalation.blocker_age_hours:
        Bash: wm-read.sh proactive_escalation_log --json
        FOR EACH blocker WHERE resolution is null:
            age_hours = hours_since(blocker.detected_at)
            IF age_hours >= config.proactive_escalation.blocker_age_hours:
                last_escalation = find entry in proactive_escalation_log where blocker_id == blocker.blocker_id
                IF last_escalation is null OR hours_since(last_escalation.sent_at) >= config.proactive_escalation.blocker_age_hours:
                    Notify the user:
                        category: blocker
                        subject: "Blocker persisting {age_hours:.0f}h: {blocker.reason}"
                        message: |
                            Blocker {blocker.blocker_id} has been active for {age_hours:.0f} hours.
                            Type: {blocker.type}
                            Affected goals: {blocker.affected_goals}
                            Unblocking goal: {blocker.unblocking_goal}

                            The one thing that would unblock this:
                            {action_description_based_on_blocker_type}
                    echo '{"blocker_id":"{blocker.blocker_id}","sent_at":"{now}"}' | Bash: wm-append.sh proactive_escalation_log

    echo '<updated_blockers_json>' | Bash: wm-set.sh known_blockers
Bash: bash core/scripts/execution-diary.sh phase-end phase-0.5b-blockers
```

## Phase 0.5b.1b: Inbox-Alert Age Escalation (g-115-848 — closes g-115-822 finding 2)

Sibling to Phase 0.5b.1 — but the surface scanned is the GOAL QUEUE
(asp-115) instead of the working-memory `known_blockers` slot. When
`world/scripts/alert-sweep.sh` files an Unblock goal for an inbound alert
email it stamps `origin_signal=f"alert-email:{s3_key}"`. If no agent claims
that Unblock within a few hours, the alert silently ages — no upstream
escalation existed before this phase. The bash gate consolidates the
scan + cooldown + notify logic into a single script call (rb-428 pattern).

Severity ladder (config: `proactive_escalation.inbox_alert_age_hours`):
  - age >= `high` (default 4h)   → fire HIGH-severity notification
  - age >= `medium` (default 12h) → fire MEDIUM-severity notification
  - Cooldown via `wm.proactive_escalation_log` keyed on
    `inbox_alert_<goal_id>`. Re-fire interval matches the severity's
    threshold (HIGH re-notify every 4h, MEDIUM every 12h) so urgency
    cadence tracks severity. A goal aging FURTHER into HIGH after a
    prior MEDIUM fire re-notifies under the HIGH schedule.

Fail-open at every layer: missing config, daemon unreachable, missing
asp-115, missing alert-sweep ledger, and per-goal email-send failures all
log to stderr and continue. `inbox-alert-age-check.py` is the single
writer for the cooldown log entries it produces; the SKILL.md call is the
single invoker.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check inbox-alert-age-check)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.2
Bash: bash core/scripts/inbox-alert-age-check.sh --apply
# Iterates asp-115 (the alert-sweep target queue). For each pending/in-progress
# Unblock goal with origin_signal=alert-email:* whose age >= threshold:
#   - Reads wm.proactive_escalation_log (caller-side cooldown).
#   - On miss: fires email-send.sh notification (subject "Unclaimed alert >Nh:
#     <goal title>", body includes goal_id + classifier subject + age + severity).
#   - Appends a cooldown entry regardless of email-send outcome (prevents
#     retry storm on transient email failures — same fail-open contract as
#     blocker-recheck.sh / pending-questions-sweep.sh).
# JSON output includes `applied`, `skipped_cooldown`, and `failed`. Failure
# counts are stderr-noted only — they do NOT block precheck.
```

Tests: `core/scripts/tests/test_inbox_alert_age_check.py` (3 cases — no
aged alert noop, aged HIGH alert fires, cooldown active noop). Closes the
acceptance criterion "Tests: 3 cases" from g-115-848.

See `core/scripts/inbox-alert-age-check.py` `_classify_severity` for the
threshold ladder and `_on_cooldown` for the cooldown discipline.

## Phase 0.5b.2: Dependency Timeout Escalation

Proactive escalation for dependency-blocked goals approaching the `dependency_timeout_hours`
threshold. This catches goals stuck behind unresolved `blocked_by` chains — the fail-open
timeout in goal-selector.py will eventually clear them, but this gives early warning and
takes differentiated action based on the root cause.

```
Read core/config/aspirations.yaml → multi_agent.dependency_timeout_hours (default 48)
escalation_threshold = dependency_timeout * 0.75  # Notify at 75% of timeout (36h default)

Bash: goal-selector.sh blocked
parsed_blocked = parse JSON output
dependency_blocked = [e for e in parsed_blocked.blocked_goals WHERE block_reason == "dependency"]

IF dependency_blocked is empty: SKIP to Phase 0.5c

Bash: load-aspirations-compact.sh → IF path returned: Read it
Bash: wm-read.sh proactive_escalation_log --json

FOR EACH entry in dependency_blocked:
    blocked_goal = look up entry.goal_id in aspirations compact
    blocked_age = hours_since(blocked_goal.blocked_since)
    IF blocked_age is null: SKIP  # No timestamp — will expire at next goal-selector run (fail-open)
    IF blocked_age < escalation_threshold: SKIP  # Not yet approaching timeout

    # Trace root cause from bottleneck data
    root = find entry in parsed_blocked.bottlenecks where goal_id matches root of chain
    root_goal = look up root.goal_id in aspirations compact

    # Differentiated escalation based on root cause
    IF root goal has participants containing "user":
        # User-action dependency approaching timeout — notify user
        last_dep_esc = find entry in proactive_escalation_log where blocker_id == "dep_{entry.goal_id}"
        IF last_dep_esc is null OR hours_since(last_dep_esc.sent_at) >= escalation_threshold:
            Notify the user about the stale dependency.
            (Check world/forged-skills.yaml for a skill whose triggers match
            "notify the user" and invoke it with a blocker-category payload:
              subject: "Dependency stale {blocked_age:.0f}h: {entry.goal_id} waiting on user"
              message:
                Goal {entry.goal_id} ({entry.title}) has been blocked for {blocked_age:.0f}h
                waiting on {root.goal_id}: {root.title}

                That goal requires user action (participants: [user]).

                If not resolved within {(dependency_timeout - blocked_age):.0f}h,
                the dependency will be cleared automatically (fail-open) and the
                blocked goal will attempt execution.

                The one thing that would help:
                {root_goal.description or root.title}
            If no matching skill is registered, fall back to a participants: [agent, user]
            goal via aspirations-add-goal.sh. Never block on notification failure.)
            echo '{"blocker_id":"dep_{entry.goal_id}","sent_at":"{now}"}' | Bash: wm-append.sh proactive_escalation_log

    ELIF root goal has participants containing "agent" AND root goal status == "pending":
        # Agent-resolvable — boost the blocking goal's priority
        IF root_goal.priority != "HIGH":
            Bash: aspirations-update-goal.sh --source {root_source} {root.goal_id} priority HIGH
            Log: "DEPENDENCY ESCALATION: boosted {root.goal_id} to HIGH (blocking {entry.goal_id} for {blocked_age:.0f}h)"

    # All causes: log the aging dependency for visibility
    Log: "DEPENDENCY AGING: {entry.goal_id} blocked {blocked_age:.0f}h by {root.goal_id} ({root.cause})"
```

## Phase 0.5b.2b: Handoff Aging Escalation (Item 3)

Cross-agent handoff goals that sit in the world queue past `handoff_aging.escalate_hours`
get a board post + proactive notification so the target agent doesn't miss them.
Goal-selector already applies an escalating scoring bonus after `warn_hours`; this
phase is the visibility escalation beyond that.

```
Read core/config/aspirations.yaml → handoff_aging.escalate_hours (default 72)
Bash: wm-read.sh proactive_escalation_log --json
Bash: load-aspirations-compact.sh  # world goals in context

FOR EACH aspiration IN world compact data:
    FOR EACH goal IN aspiration.goals:
        IF goal.status NOT IN ("pending", "in-progress"): continue
        ht = goal.get("handoff_to")
        IF NOT ht OR ht == AGENT_NAME: continue  # only goals routed elsewhere
        created = goal.get("handoff_created_at")
        IF NOT created: continue
        age_h = hours_since(created)
        IF age_h < handoff_aging.escalate_hours: continue

        escalation_key = f"handoff_{goal.id}"
        last = find entry in proactive_escalation_log where blocker_id == escalation_key
        IF last AND hours_since(last.sent_at) < handoff_aging.escalate_hours:
            continue  # cooldown to avoid spam

        msg = f"Handoff aged {age_h:.0f}h: {goal.title} [{goal.id}] waiting on {ht}"
        echo "$msg" | Bash: board-post.sh --channel coordination --type status --tags handoff-aged,{goal.id},{ht}
        echo '{"blocker_id":"'$escalation_key'","sent_at":"'$(date +%Y-%m-%dT%H:%M:%S)'"}' | Bash: wm-append.sh proactive_escalation_log
        Log: "HANDOFF AGING: {goal.id} waiting on {ht} for {age_h:.0f}h — posted to board"
```

Note: the target agent picks this up via its boot-time pending-handoffs scan
(boot/SKILL.md Step 1.7) and via goal-selector's escalating handoff_bonus.

## Phase 0.5b.3: Structured Precondition Auto-Clear Sweep

Counterpart to the pre-claim re-check in aspirations-execute. Scans goals
deferred due to unmet structured preconditions and auto-clears the deferral
when the predicates now pass. Without this sweep, a goal deferred by the
pre-claim re-check would only re-enter the pool after the 120h
`defer_reason_timeout_hours` fail-open — unacceptable latency when the
dependency is actually satisfied minutes later.

See `core/config/conventions/preconditions.md` for predicate semantics.

Bash-enforced (g-302-02): the previous LLM-iterated loop was replaced by a
single Python pass that calls `predicate.evaluate_all` in-process. This
avoids the vacuous-truth bug at the CLI exit-code level — when a goal's
preconditions list contains ONLY string/free-form entries, the pre-filter
yields zero structured predicates and the script SKIPS rather than
clearing (zero predicates ≠ "all pass"). Mirrors `defer-recheck.sh /
blocker-recheck.sh / monitor-stale-check.sh / pending-questions-sweep.sh`
bash-consolidation pattern (rb-428 family).

```
Bash: bash core/scripts/precondition-defer-recheck.sh --max-age-hours 2 --apply
# Iterates world + agent active queues. For each goal with defer_reason
# starting "precondition_unmet:" and age >= --max-age-hours:
#   - Pre-filters verification.preconditions for dict-structured entries
#     (mirrors goal-selector.py L967-981).
#   - If pre-filter empty: SKIP with reason="no structured preconditions
#     to evaluate — defer is free-form, LLM judgment required". Do NOT clear.
#   - Else: predicate.evaluate_all(struct_pcs, mode="all"). When ALL pass,
#     clear defer_reason + defer_reason_set_at via aspirations.py update-goal.
# Metrics log: <WORLD_PATH>/precondition-defer-recheck-metrics.jsonl
# (run_summary per call; precondition_defer_cleared per actual clear).
# Fail-open at every layer: exits 0 even on partial failures.
```

Cost: one Python pass over active queues per iteration, in-process predicate
evaluation. Cheap predicates (`file_exists_after`, `goal_completed_after`,
`file_check`) are local I/O. `command_succeeds` is subject to its configured
timeout and allowlist; `metric_threshold` invokes its allowlisted script.

## Phase 0.5b.4: Stale-Defer Dependency Sweep (g-115-154)

Counterpart to Phase 0.5b.3 for free-form dependency defers. Scans goals
whose `defer_reason` names one or more dependency goal-ids (`g-NNN-NN`) and
auto-clears the defer when ALL cited deps are `status: completed`.

Motivation (session 55 iter 80): the agent's own g-115-71 sat deferred on
g-115-87 for 3 days after g-115-87 completed — no mechanism re-probed
the dependency chain until a manual inspection cleared it. This sweep
mirrors `blocker-recheck.sh` (Layer C for participants:[user] blockers)
and 0.5b.3 (structured preconditions), extending the same re-probe pattern
to the LLM-authored free-form `defer_reason` surface.

Conservative by design: skips non-pending goals (status filter), requires
ALL cited deps to be completed (partial completion stays deferred), and
uses two distinct regex patterns — structured (`blocked_on_dependency: g-X`)
and proximity (`g-X <verb>` / `<verb> g-X`). Free-form defers that don't
match either pattern are reported but not cleared.

```
Bash: bash core/scripts/defer-recheck.sh --max-age-hours 2 --apply
# Reads world+agent queues, sweeps eligible goals in one pass.
# `cleared` count in JSON output names the goal-ids whose defer was lifted.
# Fail-open: script exits 0 on all paths; a non-zero exit is a script bug.
```

See `.claude/rules/probe-before-defer.md` and the rb-428 bash-consolidation
drift family for the upstream pattern this sweep counters.

The three STRUCTURED_DEFER_PREFIXES (defined in `core/scripts/gates/defer_classifier.py`)
each have their own auto-clear path: `precondition_unmet:` is handled by
Phase 0.5b.3 (precondition-defer-recheck.sh), `blocked_on_dependency:` is
handled here in Phase 0.5b.4 (defer-recheck.sh dependency regex), and
`Circuit breaker:` is filed by the aspirations loop's Phase 5.5
(per `core/config/aspirations-loop-digest.md`) when
`consecutive_goal_failures >= 3`, and cleared on the next successful
attempt. All three bypass the capability-gate's
narrative-defer check via `is_narrative_defer()` so machine-written
internal markers never keyword-collide with forged skills.

## Phase 0.5b.5: Pending-Questions Sentinel-Lifecycle Sweep (g-115-486)

Closes the gap discovered by g-115-485 / g-001-226: pending-questions whose
`source_goal` field names a goal that has since completed/superseded silently
linger forever (canonical incident: a publish-related pending-question
lingered 12d after both its origin goal and a follow-up superseder both
completed). The sweep adds a
`source_goal-completed` heuristic and a `--apply` mutation flag — same single-
writer, idempotent, fail-quiet pattern as `defer-recheck.sh --apply`,
`blocker-recheck.sh --apply`, and `monitor-stale-check.sh --apply`. Cheap:
single Python pass over <50 entries.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check pending-questions-sweep)
IF decision == "drop": SKIP this phase; continue to Phase 0.5c
Bash: bash core/scripts/pending-questions-sweep.sh sweep --apply
# Reads world+agent aspiration queues to build the completed/superseded
# goal-id set, evaluates the heuristic chain, and (when --apply) atomically
# marks verdict=auto_resolve entries as status=resolved with timestamp.
# Fail-open at every layer: missing files, parse errors, write failures
# all yield empty results without aborting the sweep.
```

See `core/scripts/pending-questions-sweep.py` `_h_source_goal_completed` for
the heuristic and `_apply_auto_resolve` for the atomic mutation. Future
sentinel-lifecycle gaps that also need same-iteration cleanup belong in this
sweep, not in a new precheck phase.

## Phase 0.5b.6: Parent-Goal Supersession Sweep (g-248-85)

Closes the supersession-blindness gap that produced g-268-10 (rb-842): a
parent "Apply: X" goal carried `defer_reason: blocked_on_design` for
hours while two sibling goals (Design + Apply decomposition) completed
the same intent ABOVE it in the queue. The defer-recheck sweep cleared
the defer but the parent re-emerged at high score because its
description no longer pointed at unfinished work — leading to spurious
selection. This sweep catches that incident shape at parent-supersession
time, BEFORE the defer-recheck loop has a chance to re-promote it.

Heuristic shape: parent goal carries `Apply:` title + has a temporal
reference (`defer_reason_set_at` or `created_at`) + ≥2 sibling goals in
the same aspiration with `Design:`/`Apply:` titles completed AFTER that
reference timestamp. Sprint-scope guard: only aspirations with
`≤max_aspiration_goals` (default 50) qualify — large recurring
aspirations like asp-115 (611 goals) produce false positives at any
threshold because parents and unrelated completions co-exist by design.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check parent-supersession-sweep)
IF decision == "drop": SKIP this phase; continue to Phase 0.5c
Bash: bash core/scripts/parent-supersession-sweep.sh --max-age-hours 24 --min-siblings 2 --apply
# Iterates world + agent queues. For each Apply:-parent with reference
# timestamp + ≥2 superseding Design/Apply siblings (sprint-scope only),
# marks parent status=completed with outcome_note "superseded by sibling
# decomposition". Single-writer, idempotent, fail-quiet — same pattern as
# defer-recheck.sh / pending-questions-sweep.sh.
# Metrics log: <WORLD_PATH>/parent-supersession-sweep-metrics.jsonl
```

See `core/scripts/parent-supersession-sweep.py` `_find_superseding_siblings`
for the heuristic (sibling lookup + temporal guard) and
`_mark_superseded` for the atomic mutation. Tests at
`core/scripts/tests/test_parent_supersession_sweep.py` pin the 8-case
contract (canonical incident + 7 false-positive rejections).

## Phase 0.5b.7: Unblock-Parent-Status Sweep (g-250-76, rb-908)

Closes the Layer-D-auto-Unblock-outlives-parent gap that produced
g-250-73 (rb-908). When `capability-gate.py --suggest-unblock` files an
auto-Unblock at defer-write time and the parent goal then lands in a
terminal non-execution state — `skipped` (WRONG LAYER finding),
`completed`, `superseded`, or `archived` — the Unblock survives as
actionable work even though its premise has dissolved. Layer D writes
synchronously and never re-probes the parent; this sweep is the
re-probe.

Canonical incident: g-250-73 'Unblock: behavior for g-250-69' filed at
T+0s, g-250-69 SKIPPED at T+72s with WRONG LAYER finding ("bumping
<JAVA_CLASS_NAME> weights would violate
<JAVA_CLASS_NAME>:461; fix routes through
<JAVA_CLASS_NAME>.scoreCandidate fallback instead"). Without this
sweep, g-250-73 would have lingered as a pending Unblock until manual
inspection caught it.

Heuristic shape: Unblock-titled goal + parseable parent goal-id (from
`origin_signal "unblock:<g-id>"`, title `"Unblock: <verb> for <g-id>"`,
or `discovered_by` field) + parent.status in
{skipped, completed, superseded, archived}. Title-anchored ("Unblock:"
prefix) — does NOT match `Investigate:`/`Idea:`/`Apply:`/`Recurring:`
goals that happen to carry `origin_signal: "unblock:..."` for unrelated
reasons (false-positive shape observed in g-249-06 / g-250-77).

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check unblock-parent-status-sweep)
IF decision == "drop": SKIP this phase; continue to Phase 0.5c
Bash: bash core/scripts/unblock-parent-status-sweep.sh --apply
# Iterates world + agent queues. For each pending "Unblock:" with a
# parseable parent goal-id whose parent.status is terminal, marks the
# Unblock status=skipped with outcome_note
# "parent resolved without action needed (parent_id=<X>, parent.status=<Y>)".
# Single-writer, idempotent (outcome_note prefix check), fail-quiet —
# same rb-428 pattern as defer-recheck.sh / pending-questions-sweep.sh /
# parent-supersession-sweep.sh.
# Metrics log: <WORLD_PATH>/unblock-parent-status-sweep-metrics.jsonl
```

See `core/scripts/unblock-parent-status-sweep.py` `_parse_parent_id` for
the three-source extraction priority and `_mark_skipped` for the atomic
mutation. Tests at `core/scripts/tests/test_unblock_parent_status_sweep.py`
pin the 12-case contract (canonical g-250-73 shape, three extraction
paths, idempotency, terminal-state set, title-prefix discipline).

## Phase 0.5c: Recurring-Goal Precondition-Filter lastAchievedAt Sweep

Closes the "shape-recurring trap" (bravo reasoning-channel musing
2026-04-21): recurring goals with STRUCTURED preconditions that fail at
candidacy time never reach aspirations-execute, so their `lastAchievedAt`
never advances. The goal-selector's urgency formula then inflates
`overdue_ratio` unboundedly; when the precondition finally unlocks, the
goal fires with massive urgency on trivially-met evidence, closes routine,
and feeds cargo-cult.

Distinct from 0.5b.3 (which clears explicit `defer_reason:
precondition_unmet:*`). This sweep targets recurring goals that are NOT
deferred — they silently drop out of COLLECT at the selector's predicate
filter (goal-selector.py L680–692), leaving `lastAchievedAt` frozen.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check recurring-precondition-sweep)
IF decision == "drop": SKIP this phase; continue to Phase 0.5e
Bash: py core/scripts/recurring-precondition-sweep.py
# Iterates world + agent queues. For each recurring goal past its time
# gate with a failing structured precondition, advances lastAchievedAt
# to now via aspirations.py update-goal. Does NOT increment
# consecutive_routine (the goal was shelved, not closed).
# Fail-open: always exits 0. Output is one line per advance.
```

Companion to cargo-cult auto-extend in core/scripts/cargo-cult-detector.py:
auto-extend fixes the "detector fires too often" symptom; this sweep fixes
one of the root causes for precondition-gated goals.

## Phase 0.5e: Fresh-Eyes Cadence Check

Periodic autonomous portfolio review. Every 25 completed goals (configured in
`core/config/aspirations.yaml` → `fresh_eyes_review.goal_cadence`), produce a
local portfolio-direction briefing examining:

1. Is the current Self still right?
2. Are we working on the right problems?

The briefing is archived to `agents/<agent>/reports/` and the agent self-assesses
the outcome (act_now / act_later / no_change). No email is sent and no
pending-question is filed — the user reviews via git log and tracked
signals at their own pace.

The cadence gate is script-enforced (`fresh-eyes-cadence-check.sh`) and
fail-open — if it errors, the loop continues without review. Cadence is
purely goal-count-based (the `skip_if_pending` gate was removed
2026-05-19 — see fresh-eyes-cadence-check.py header).

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check fresh-eyes-cadence)
IF decision == "drop": SKIP this phase; continue to Phase 0.5e.5
Bash: core/scripts/fresh-eyes-cadence-check.sh
IF exit 0 (fire):
    Output: "▸ Fresh-eyes review cadence crossed — assembling briefing"
    Invoke /fresh-eyes-review --cadence
IF exit 1 (noop):
    # Cadence not crossed OR config disabled — continue silently (no output)
    continue
Bash: echo "aspirations-precheck phase documented"
```

Distinct from sq-012 (post-goal, narrow) and strategic-scan S3b
(autonomous, category coverage only) — this is the periodic local
self-audit that produces a portfolio-direction briefing on a deliberate
low-frequency cadence. See `.claude/skills/fresh-eyes-review/SKILL.md`.

## Phase 0.5e.5: Fresh-Eyes PROGRAM Cadence Check

Sibling to Phase 0.5e. Every 100 completed goals (configured in
`core/config/aspirations.yaml` → `fresh_eyes_program.goal_cadence`),
produce a local briefing targeting `world/program.md` (The Program — the
world's shared purpose) examining two questions:

1. Is The Program still the right shared purpose for this world?
2. Do both agents' Selfs still serve The Program, or have they drifted?

Same infrastructure as fresh-eyes-review (`fresh-eyes-cadence-check.sh`,
`fresh-eyes-record-tick.sh`), parametrized via `--config-block
fresh_eyes_program` so the two rituals use disjoint config blocks and WM
slots. Closes the "program.md has no systematic evolution path" gap called
out in `core/config/conventions/domain-hooks.md` → Directional Context.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check fresh-eyes-program-cadence)
IF decision == "drop": SKIP this phase; continue to Phase 0.5f
Bash: core/scripts/fresh-eyes-cadence-check.sh --config-block fresh_eyes_program
IF exit 0 (fire):
    Output: "▸ Fresh-eyes PROGRAM cadence crossed — assembling shared-purpose briefing"
    Invoke /fresh-eyes-program --cadence
IF exit 1 (noop):
    # Cadence not crossed OR config disabled — continue silently (no output)
    continue
```

Lower-frequency than fresh-eyes-review (100 vs 25) because shared purpose
changes much less frequently than individual agent identity. See
`.claude/skills/fresh-eyes-program/SKILL.md`.

## Phase 0.5e.7: Fresh-Eyes TREE Cadence Check (S5)

Sibling to Phase 0.5e and 0.5e.5. Every 200 completed goals (configured in
`core/config/aspirations.yaml` → `fresh_eyes_tree.goal_cadence`), produce a
local briefing targeting the knowledge tree's top-level taxonomy (the L1 set)
examining:

**Are the four L1s still the right top-level cuts for the knowledge tree?**

Same infrastructure as the two sibling rituals (`fresh-eyes-cadence-check.sh`,
`fresh-eyes-record-tick.sh`), parametrized via `--config-block fresh_eyes_tree`
so the three rituals use disjoint config blocks and WM slots. If taxonomy
changes are desired, the user invokes the S8 apply scripts
(`l1-domain-rename.sh`, `l1-domain-add.sh`) manually.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check fresh-eyes-tree-cadence)
IF decision == "drop": SKIP this phase; continue to Phase 0.5f
Bash: core/scripts/fresh-eyes-cadence-check.sh --config-block fresh_eyes_tree
IF exit 0 (fire):
    Output: "▸ Fresh-eyes TREE cadence crossed — assembling L1 taxonomy briefing"
    Invoke /fresh-eyes-tree --cadence
IF exit 1 (noop):
    # Cadence not crossed OR config disabled — continue silently (no output)
    continue
Bash: echo "aspirations-precheck phase documented"
```

Distinct from `/tree maintain` (per-node structural ops, autonomous +
manual), `/reflect` Step 7 Tree Health Lint (per-node staleness + width),
and `l1-skew-check.py` (S1 passive measurement at 50 goals). Those check
NODE health or accumulate quantitative skew evidence; this ritual asks
the structural-design question that no automated gate ever surfaces.
Lower-frequency than program (200 vs 100) because L1 changes are
high-blast-radius. See `.claude/skills/fresh-eyes-tree/SKILL.md`.

## Phase 0.5f: Felt-Sense Check-In Cadence

Periodic autonomous structured 7-lane self-audit. Every 75 completed
goals (configured in `core/config/aspirations.yaml` →
`felt_sense.goal_cadence`), the agent runs the full sweep:
memory hygiene, out-of-cycle completions, unblocks, forward backlog,
framework-test gaps, meta tuning, and the felt-sense question ("where
is the pain, what would I change"). Material Self findings route
through `guard-380` post-notification; cosmetic findings journal only.

The cadence gate is script-enforced (`felt-sense-cadence-check.sh`) and
fail-open — if it errors, the loop continues without the sweep.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check felt-sense-cadence)
IF decision == "drop": SKIP this phase; continue to Phase 1
Bash: core/scripts/felt-sense-cadence-check.sh
IF exit 0 (fire):
    Output: "▸ Felt-sense check-in cadence crossed — running 7-lane sweep"
    Invoke /felt-sense-checkin --cadence
IF exit 1 (noop):
    # Cadence not crossed OR config disabled — continue silently
    continue
Bash: echo "aspirations-precheck phase documented"
```

Distinct from fresh-eyes-review (Phase 0.5e — joint user+agent meta-Q&A,
asks user for answers) and sq-012 (post-goal, narrow). This is an
autonomous structured self-audit that WRITES outputs directly (tree
nodes, guardrails, goals, verify-learning checks, meta edits) and
notifies the user only when Self changes materially. Lower frequency
(75 vs 25) because the seven lanes are expensive and Lane 7 needs
room to feel earned rather than ritual. See
`.claude/skills/felt-sense-checkin/SKILL.md`.

## Phase 0.5g: L1 Distribution Skew Check (S1 — Tree Taxonomy Review)

Periodic passive observability check. Every 50 completed goals (configured
in `core/config/aspirations.yaml` → `l1_skew_check.goal_cadence`), compute
per-L1 distribution (structural mass, retrieval volume, mature capability
mass) and post a coordination-board `findings` message when any ratio
exceeds the threshold (default 5x).

NOT a user-facing ritual — no email, no pending-question. The board post
gives partner agents and /fresh-eyes-tree (S5) cross-session visibility
into when the L1 boundaries look wrong. Quiet on balanced state.

The cadence gate and the check itself are both inside `l1-skew-check.py`
— a single script, one bash call from this phase. Fail-open: any error
prints to stderr and the loop continues. Exit code 1 on noop is silent.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check l1-skew-cadence)
IF decision == "drop": SKIP this phase; continue to Phase 1
Bash: core/scripts/l1-skew-check.sh --cadence --post-board
IF exit 0 (fire — cadence crossed, check ran):
    # Script printed its JSON verdict to stdout (LLM context). Board post
    # already fired if any_flagged. Continue silently to Phase 1.
    continue
IF exit 1 (noop — cadence not crossed):
    continue
IF exit 2 (stats read error):
    # Stderr already noted. Fail-open. Continue.
    continue
Bash: echo "aspirations-precheck phase documented"
```

Distinct from `/tree stats` (one-shot, depth-only) and `/reflect` Step 7
Tree Health Lint (per-node staleness + cross-refs + width). Those check
NODE health; this checks TAXONOMY shape. The output feeds the /fresh-eyes-tree
ritual (S5) which assembles the briefing when the cadence-300-goal ritual
fires — board posts make the L1 skew visible BEFORE that joint review,
so partners (alpha/bravo) have signal to interpret on their own iterations.
See `core/scripts/l1-skew-check.py` and `core/scripts/tree.py
_compute_by_l1_stats`.

## Phase 1: Recurring Goal Check

```
check_recurring_goals()
# Ensures recurring goals are properly tracked and due goals are flagged
```

## Phase 2: Budget Meter End (Magic Wand 2 — g-115-509)

Finalize the precheck budget meter — writes a summary record (sweeps_ran,
sweeps_dropped, total_elapsed_ms) to `agents/<agent>/session/precheck-drops.jsonl`
and clears the per-iteration state file. One-shot — do not call without a
preceding `meter start` (Step 0a).

```
Bash: bash core/scripts/aspirations-precheck-budget-meter.sh end
```

## Chaining

- **Called by**: `/aspirations` orchestrator (every iteration, first phase)
- **Calls**: `aspirations-read.sh`, `aspirations-meta-update.sh`, `guardrail-check.sh`, `infra-health.sh`, `wm-read.sh`, `wm-set.sh`, `aspiration-trajectory.sh` (cycle detection), `aspirations-add-goal.sh` (cycle detection, hypothesis pipeline, accuracy gate), `aspirations-query.sh` (user-goal reclassification), `aspirations-update-goal.sh` (user-goal reclassification), `world-cat.sh` (capability-routing convention), `pipeline-read.sh` (hypothesis pipeline + accuracy health), `fresh-eyes-cadence-check.sh` (Phase 0.5e gate), `recurring-precondition-sweep.py` (Phase 0.5c), `/create-aspiration` (health + pipeline depth), `/fresh-eyes-review --cadence` (Phase 0.5e fire), CREATE_BLOCKER protocol
- **Reads**: Aspirations compact, working memory (blockers), guardrails, trajectory data (cycle detection), pipeline meta (hypothesis counts + accuracy), `core/config/aspirations.yaml` (pipeline_low_water_mark, hypothesis_pipeline_low_water_mark, accuracy_critical_threshold, accuracy_min_sample)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the last `wm-set.sh`, `aspirations-read.sh`, or
`aspirations-add-goal.sh` call. Never end with a text summary.
