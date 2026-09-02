---
name: aspirations
description: "Runs the autonomous aspiration loop — the perpetual heartbeat that selects, executes, verifies, reflects on, and evolves goals forever. This is the core orchestrator for autonomous mode. Use whenever /boot finishes in autonomous mode, when re-entering the loop after autocompact recovery, or when the user runs /aspirations {sub-command} (status, next, loop, evolve, complete, add). Must NEVER be invoked from reader or assistant mode — loop restrictions enforce this."
user-invocable: false
triggers:
  - "/aspirations"
sub_commands:
  - status
  - next
  - loop
  - evolve
  - "complete <goal-id>"
  - "add <title>"
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [aspirations, pipeline, goal-schemas, session-state, handoff-working-memory, infrastructure, reasoning-guardrails, experience, goal-selection, coordination]
minimum_mode: autonomous
revision_id: "skill-bootstrap-aspirations-cb577a"
previous_revision_id: null
---

# /aspirations — Perpetual Goal Loop Engine (Slim Orchestrator)

The heartbeat of the continual learning agent. Delegates ALL phase work to sub-skills
that load on-demand and fall out of context at compaction. The orchestrator stays as thin
as possible — phase bodies belong in sub-skills, not here — to minimize re-entry cost after
autocompact. (Further idle-path cost savings live in `core/scripts/idle-tick.sh`, which
short-circuits the turn before this file is even loaded when `blocked_sleep_until` is set.)

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter.

**Step 0.5: Sub-Command Dispatch** — Only the `loop` sub-command runs the
perpetual body below. For any other sub-command (`status`, `next`, `evolve`,
`complete`, `add`), the body below is cold weight. Gate it:

```
IF args is set AND args != 'loop':
    Read core/config/aspirations-subcommands-digest.md
    Follow the section matching <args>. RETURN.
# ELSE: args == 'loop' or unset → continue to the perpetual body.
```

<!-- DO NOT inline sub-command bodies back into this SKILL.md. The dispatch
     above diverts ~800 tokens worth of sub-command pseudocode off the hot
     path for every `/aspirations status|next|evolve|complete|add` invocation,
     and they never need the perpetual body. Keep them in the digest. -->


## Core Principle: No Terminal State

The system ALWAYS has work to do. If it doesn't, it creates work.
Completion of one thing seeds the next thing.

The loop never exits, but the loop's *work* may be observation rather than
generation. When the queue is structurally user-gated (every blocked goal
carries a valid `blocker_ref`), the quiescence gate
(`core/scripts/quiescence-gate.py`) may approve a longer sleep instead of
forcing B3/B4/B5 work generation every all-blocked cycle. See
`core/config/modes/autonomous.md` → "Core Design Principle: No Terminal State"
for the full doctrine and `core/config/conventions/goal-schemas.md` →
"Blocker Reference Schema" for the structural requirement that prevents
narrative-laundering into quiescence.

## Aspiration Update Notification (MANDATORY)

Whenever goals are added to an existing aspiration, notify the user with a summary.
If unable to reach user, create a `participants: [agent, user]` goal. Do NOT block.

## Autonomous Solution Attempt Protocol (ASAP)

Before writing ANY entry to `agents/<agent>/session/pending-questions.yaml`:
1. Search knowledge tree, reasoning bank, guardrails, experience archive
1a. **Check the standing-grant table FIRST** — `world/conventions/capability-routing.md`
    (`grant-NNN` rows + the Matching rule under them). A pending question asks the
    user to do something; a standing grant is the record that they already delegated
    it. This store is authoritative and cheap to read, and the four stores in step 1
    do NOT include it. `bash core/scripts/world-cat.sh conventions/capability-routing.md`
1b. If step 1a finds nothing, check the **decisions board** for a grant made but not
    yet materialized into the table:
    `bash core/scripts/board-read.sh --channel decisions --since 720h`
    (filter for grant / authorization / "go ahead" / "full permission" language).
    A hit here means the grant is real but the table is stale — materialize it into
    `capability-routing.md` as part of this same iteration, so the next agent finds
    it at 1a instead of re-deriving it from a 30-day board scan.
2. If found: ATTEMPT it. If succeeds: skip pq-XXX
3. If fails: write pq-XXX with `attempted_solutions` + `autonomous_search_done: true`

Why 1a precedes 1b (g-115-3636, measured 2026-07-28): the originating report assumed
grants live ONLY on the board. They do not — `capability-routing.md` carries the full
`grant-001..009` table with provenance, matching, and revocation rules, and the specific
grant that motivated this fix (grant-007, product-repo PR merge, 2026-07-18) was already
materialized there. Grant-RECORDING is working; the defect was that ASAP's four-store
list omits the store the grants are recorded IN. The board scan is the net for the
recording lag, not the primary lookup.

A grant is not a capability — it is a permission that CHANGED at a point in time, so
`capability-before-user.md` and `probe-before-defer.md`, which consult a static
capability catalog, do not catch it. This step is where a standing grant gets seen.
Sibling gate at report time: `guard-1066` (re-verify a pending item's premise before
presenting it to the user).

## `loop` — The Perpetual Heartbeat

Select and execute goals continuously until explicitly stopped:

```
Bash: aspirations-read.sh --meta → get session_count
Bash: load-aspirations-compact.sh → IF path returned: Read it

# Phase -1.5: Agent State Gate Check (MANDATORY — call ALONE, do NOT batch)
# ════════════════════════════════════════════════════════════════════════════
# CRITICAL: Run THIS Bash call alone in its own message. Do NOT batch with
# Phase -1.4 (stop-requested check) or Phase -0.5 (loop-active set, heartbeat
# tick) into one Bash invocation. Batching makes the gate result return AFTER
# the signals are on disk, leaving state=IDLE + loop-active=set + heartbeat=
# fresh — exactly the `heartbeat_without_running` desync flagged in
# `core/config/session-manifest.yaml`. Other observers (recovery-gate, partner
# agents reading team-state, /stop) will mis-classify the agent as alive when
# it has been recovered to IDLE.
#
# Canonical incident (2026-05-13, alpha session cbb27ab3): after a hung-
# autocompact recovery flipped state RUNNING→IDLE at 05:17, the LLM resumed
# via "continue", batched 4 Bash calls into one, and wrote loop-active +
# ticked heartbeat AGAINST an IDLE state before noticing. `heartbeat-tick.sh`
# now ALSO refuses bare ticks when state=IDLE (defense-in-depth — accepts
# `--bypass-state` only from /start), but the LLM-side discipline lives here.
# ════════════════════════════════════════════════════════════════════════════
Bash: `session-state-get.sh`
state = stdout (trimmed)
IF state != "RUNNING":
    # Recovery occurred during the pause, OR /stop completed, OR an observer
    # session somehow reached the loop body. Either way: STOP — no signal
    # writes, no heartbeat tick.
    IF agents/<agent>/session/recovery-notice EXISTS:
        Read agents/<agent>/session/recovery-notice → capture the recovery cause
    # Terminal tool call (guard-454): the orchestrator's abort MUST end on
    # a tool call, not prose. A Bash echo surfaces the diagnostic AND
    # satisfies return-protocol; the iteration ends because no follow-up
    # Skill(aspirations) is emitted.
    Bash: `echo "Loop will NOT start — agent-state=<state>. Auto-recovery likely fired while session was paused. [Include recovery-notice cause if read above.] Run /start <agent-name> --mode autonomous from a fresh terminal to resume."`
    RETURN  # No further tool calls — loop death here is intentional.

# Phase -1.45: Runner-Identity Gate (multi-runner ejection, 2026-05-23)
# ════════════════════════════════════════════════════════════════════════════
# The state check ABOVE reads session-state-get.sh → the SHARED agent-level
# agent-state file. EVERY session of this agent reads the same "RUNNING" the
# real runner wrote, so that check answers "is this AGENT running?", NOT "am I
# THE runner?". The loop self-re-enters every iteration via Skill(aspirations).
# When two+ terminals run the same agent (e.g. a second /start auto-recovered
# during a stale-heartbeat window and took over running-session-id while THIS
# session kept looping), the non-runner terminals pass the state check forever
# and iterate indefinitely — confusing the real runner with concurrent writes
# to shared world/agent state. stop-hook.sh Gate 0 ALLOWS a non-runner to stop,
# but nothing ever TELLS it to. This gate is the active eject point.
#
# runner-identity-check.sh compares this session's $MIND_SID (injected into
# every Bash call by bash-agent-inject.py) against running-session-id.
#   exit 0 = I am the runner, OR ambiguous (fail-open) → CONTINUE
#   exit 1 = definite non-runner mismatch              → EJECT
# SELF-HEALING: running-session-id holds exactly one SID, so at most one session
# passes; when a new session claims runner, the old one ejects on its NEXT
# iteration. FAIL-OPEN by design — exit 1 ONLY on a definite mismatch (both SIDs
# known and different); any ambiguity (either SID empty) continues, so a
# transient-empty running-session-id never kills the legitimate runner.
# This is the FIRST and ONLY runner-identity checkpoint needed: because the loop
# re-enters through this phase every iteration, one gate here is checked every
# cycle. DO NOT scatter additional per-session checks mid-loop — the re-entry
# chokepoint already covers all iterations.
# ════════════════════════════════════════════════════════════════════════════
Bash: `bash core/scripts/runner-identity-check.sh`
IF exit code == 1:
    # This session is NOT the runner. The script printed an actionable
    # diagnostic (which SID owns the loop, what to run instead) to stderr.
    # That Bash call IS the terminal tool call (return-protocol satisfied):
    # do NOT emit Skill(aspirations). Loop ejection here is intentional and
    # self-healing — the live runner is unaffected and keeps iterating.
    RETURN  # No further tool calls — non-runner exits the loop.

# Phase -1.4: Graceful Stop Handler (delegated)
# CRITICAL INVARIANT — do NOT re-add budget/context self-stop triggers here.
# The loop NEVER self-stops. Only /stop (user command) may set stop-requested;
# /stop writes stop-target-mode FIRST, then the signal, so aspirations-graceful-stop
# D7 can always read the target mode. Any new caller that sets stop-requested
# without writing stop-target-mode will crash D7 by design (fail loud, don't mask).
# Context pressure is handled by soft degradation in OTHER phases:
#   - Phase 8.8: skips evolution when zone == tight
#   - aspirations-select: smaller batches at higher zones
#   - aspirations-execute: caps episode chaining by zone (reads context-budget.json)
# See core/config/stop-skip-conditions.md — "Context filling up — autocompact
# handles it" is explicitly listed as a NON-stop-condition.
Bash: `session-signal-exists.sh stop-requested`
stop_signal = (exit 0)
IF stop_signal:
    Skill(aspirations-graceful-stop)
    RETURN  # Sub-skill completes the stop (D1-D7); DO NOT continue the iteration loop.
            # On next turn the agent is IDLE and the Session Start Protocol handles re-entry.

# Phase -1.35: Directive-Hold Pin (g-306-386)
# `stop-requested` survives turn boundaries; a USER DIRECTIVE does not — measured
# twice with a human waiting LIVE. Same survival pattern, OPPOSITE polarity. Placed
# after -1.4 so a real /stop still wins. Fail-open (rc=1) by contract.
# Rationale + coach field trace: core/config/rationale/directive-hold-pin.md
Bash: `bash core/scripts/interrupt-task.sh check`
IF exit code == 0:   # pinned — the command printed the task and the imperative
    Complete the task, then `interrupt-task.sh release --reason "<what happened>"`;
    or if genuinely blocked, say so TO THE USER and release with that reason.
    Do NOT proceed to Phase -1 / precheck / selection with the pin open.

# Phase -1: Initialize Working Memory (runs once, idempotent)
Bash: wm-read.sh --json
if all slots null:
    Read core/config/memory-pipeline.yaml
    Bash: wm-init.sh
    # session_start is a TOP-LEVEL WM slot — the slot session_artifacts_count.py reads for
    # the productivity-gate encoding_ratio — NOT active_context (whose canonical schema is
    # {summary, experience_refs, retrieval_manifest}; writing session metadata there both
    # targets a slot the gate never reads AND clobbers active_context's real schema). /start
    # is the canonical seeder (start/SKILL.md) and wm-reset preserves session_start, so seed
    # here ONLY as a fresh-WM fallback and ONLY when unset, to avoid overwriting the /start
    # run-start stamp with a mid-run timestamp. (g-115-2828 — the claimed "unset session_start
    # silently degrades encoding_ratio" did not reproduce: the unset case is loudly handled by
    # g-115-2179/guard-1090; the real bug was this misplaced write.)
    Bash: wm-read.sh session_start → IF the value is null: `date +%Y-%m-%dT%H:%M:%S | wm-set.sh session_start`
    Seed recent_violations, pending_resolutions from scripts
    IF handoff.yaml has known_blockers_active: seed known_blockers

# Phase -0.5: Session Marker + Loop State Restoration
Bash: `session-signal-set.sh loop-active`
# Per-iteration heartbeat tick — single call advances BOTH the local
# runner-heartbeat file mtime AND the cross-agent team-state.last_active
# field together. SSOT lives in the script, not here: future heartbeat
# additions (new fields, new endpoints) edit core/scripts/heartbeat-tick.sh
# and apply in-flight to every running agent on its next iteration —
# changes here would not take effect until autocompact reloads SKILL.md
# or the user runs /start --recover. See script header for the staleness
# threshold and the audit-existing-writers rationale (rb-399).
Bash: `bash core/scripts/heartbeat-tick.sh`

# Restore loop state from WM (persists across self-reinvocations and compaction)
Bash: wm-read.sh loop_state --json
IF loop_state is not null (not "null" string, not empty):
    # Continuing from previous iteration — restore counters
    goals_completed_this_session = loop_state.goals_completed
    productive_goals_this_session = loop_state.productive_goals
    evolutions_this_session = loop_state.evolutions
    last_evolution_goal_count = loop_state.last_evolution_at
    goals_since_last_alignment_check = loop_state.alignment_check_at
    aspirations_touched_this_session = set(loop_state.touched)
    session_signals = loop_state.signals
ELSE:
    # First iteration of session — initialize + increment session_count
    # IMPORTANT: session_count increments HERE (not above) because LOOP_CONTINUE
    # re-invokes this skill every iteration. Only the first iteration should count.
    Bash: aspirations-meta-update.sh --source world session_count <N+1>
    goals_completed_this_session = 0
    productive_goals_this_session = 0
    evolutions_this_session = 0
    last_evolution_goal_count = 0
    goals_since_last_alignment_check = 0
    aspirations_touched_this_session = set()
    session_signals = {
        routine_streak_global: 0,
        productive_streak: 0,
        routine_count_total: 0,            # total routine outcomes this session (never resets mid-session)
        goals_since_last_tree_update: 0,   # encoding drift counter (all outcome types)
        consecutive_goal_failures: 0,      # circuit breaker counter (same goal failing repeatedly)
        last_failed_goal_id: null,          # tracks which goal is looping
        consecutive_blocked_sleeps: 0,     # exponential backoff counter for blocked-idle sleep
        productivity_cooldown_streak: 0    # cool-down ladder level (Fix E — productivity-stop-gate.sh)
    }

# Phase -0.5a0: Orchestrator Entry Battery (g-115-2550 — ONE call replaces the
# per-phase presence checks of -0.5a / -0.5c / -0.5c.2 / -0.5e Branch B)
# Consumer action of CONFIRMED hypothesis 2026-07-16_sentinel-battery-survives-
# compaction: the precheck battery (g-115-2303) provably killed the omission
# class for precheck sentinels, but THIS entry sequence stayed LLM-enumerated —
# fresh miss 2026-07-18T00:07: a post-compaction re-entry ran the precheck
# battery correctly yet skipped Phase -0.5c (compact-checkpoint.yaml sat
# unconsumed 25min). Post-autocompact, "run the entry battery" is the only line
# that must survive summarization; the output re-derives the full entry
# protocol (actionable dispatches + the always-run footer in protocol order).
Bash: `bash core/scripts/orchestrator-entry-battery.sh`
IF output says "all 4 entry checks clean — no dispatches":
    The presence-gated phases below (-0.5a, -0.5c, -0.5c.2, -0.5e Branch B)
    have nothing to do — SKIP their preamble checks. STILL run the always-run
    calls the footer lists, each ONCE, at its own phase (-0.5c.1 stranded
    sweep, -0.5e.0/-0.5e.0b cache checks, -0.5e idle-tick, -0.5e' verify-wake,
    -0.5d identity restore).
FOR EACH "▸ ENTRY: <name> (phase <phase>) payload=<json> → dispatch: <section>" line:
    Handle it via the NAMED phase section below. The payload is on the line —
    do NOT re-run wm-read for pending_phase_6_spark / blocked_sleep_until (the
    battery read IS the read; wm-read mutates accessed_at). The phase bodies
    keep ownership of action + clear (the battery is READ-ONLY).
IF output reports wrapper_failed or error=...:
    Fall back to the per-phase checks below unchanged. The battery must never
    block the loop.

# Phase -0.5a: Background Agent Result Collection
IF agents/<agent>/session/pending-agents.yaml EXISTS:  # battery: pending_agents line
    # list --json prunes stale agents (past timeout_minutes) before returning.
    # Stale agents are removed from the file and excluded from output.
    Bash: `pending-agents.sh list --json`
    FOR EACH non-stale agent: collect results, deregister. IF none pending: clear.

# Phase -0.5c: Compact Checkpoint Processing
# Full protocol: core/config/conventions/compact-recovery.md
IF agents/<agent>/session/compact-checkpoint.yaml EXISTS:  # battery: compact_checkpoint line
    # Step 1: Restore ALL WM slots from checkpoint (not just 4 — includes dynamic slots like loop_state)
    Bash: `compact-restore-slots.sh`
    # Step 2: Process encoding queue (precision-first — queue items contain precision_manifests)
    Process encoding queue (budget: min(5, queue_length)).
    # Step 3: One-shot consumption — handled by the script (g-115-962).
    # compact-restore-slots.py deletes the checkpoint at all clean success
    # paths (stale-skip, empty-checkpoint, restored-clean) and PRESERVES it
    # on the wm-uninitialized error exit for next-iteration retry. The
    # previous LLM-owned delete step silently dropped on the stale-skip
    # path; the script-side deletion closes that gap.

# Phase -0.5c.1: Stranded-Claim Sweep (g-115-1044)
# Autocompact between aspirations-claim.sh success and Phase 4 start can
# leave an in-progress claim with no execution-diary entry — the next
# iteration's selector sees the goal as "owned" and skips it, the goal
# sits frozen until /felt-sense-checkin (75-goal cadence) catches it.
# Runs unconditionally — safe when no goals are claimed (exits 0 with
# {scanned: 0}). ITS COST IS NOT CONSTANT, and that sentence alone read as
# though it were (g-115-6071): the sweep is O(THIS agent's outstanding
# claims), and each foreign-sid claim past the 120m grace costs one REMOTE
# round trip. Measured 2026-08-13 — zeta on cc-02, scanned=1, 1.26s; alpha on
# cc-04, scanned=324 / carrier_checks=307, 215s (the BACKGROUNDED --apply run;
# a separate same-day DRY-RUN read 234/249, and pairing that count with this
# wall clock inflates per-check cost 31%), and it was killed twice at
# the 120s and 150s Bash bounds. Same code, 170x apart, because alpha held 318
# claims to everyone else's <=2. The probe is now memoized per (sid,
# fresh_minutes) within a run — those 318 carried only SIX distinct sids — so
# the read count is bounded by distinct HOLDERS, not by claim count. Read
# `carrier_checks` vs `carrier_reads` in the output: their difference is the
# saving, and a large `carrier_reads` is the signal that this agent's claim
# backlog is the thing to fix, not the sweep.
# With --apply: releases the claim, flips status to
# pending, clears matching team-state.in_flight. The 5-min stale threshold
# guards against race conditions where Phase 4 was just starting.
# Unconditional --apply stays CORRECT here (g-115-4004): the sweep now
# compares each claim's claimed_by_sid (stamped by aspirations-claim.sh,
# g-115-3176) against this process's MIND_SID, so a claim held by another
# LIVE INSTANCE of this same agent on another box is KEPT rather than
# released — previously "stranded" was inferred from diary activity alone,
# so a peer instance's live claim could read as abandoned. Fixed at the root
# instead of by adding dry-run-first ceremony to this line; a 120m grace
# still prevents a genuinely dead instance from freezing a shared goal.
# THE DIARY IS NOT BOX-LOCAL — an earlier version of this comment said it
# was, and the correction is load-bearing below: session-manifest.yaml gives
# execution-diary.jsonl `sync_tier: continuity` (remote-authoritative,
# per-AGENT read-through cache) and its entries carry no session id, so a
# peer instance's entries appear here as soon as this box pulls (g-306-132-c).
# Direct py -3 invocation (not bash wrapper) — see rb-225/rb-247 for the
# Windows bash subprocess hang.
# SILENCE IS NEVER A CLEAN NO-OP HERE (g-115-6071). main() emits its entire
# result in ONE terminal print immediately before `return 0`, and an awk scan
# of its whole body finds ZERO early returns — no `return`, no `sys.exit`, no
# `raise SystemExit`. So a genuine scanned=0 run STILL prints its summary, and
# a zero-byte result can only mean the run did not finish. That empty result
# is byte-indistinguishable from the "safe when no goals are claimed" contract
# above, which is why two kills on cc-04 read as clean passes and nothing
# reported that stranded claims went unreleased for the whole session.
# The wrapper below is the surfacing the goal asked for: the orchestrator used
# to discard rc entirely. It prints the summary unchanged on the happy path
# and a loud line otherwise — including the rc=0-with-no-output case, which
# the rc check alone would miss and which was observed at least once.
# INVOKE THE .sh WRAPPER, NEVER THE BARE .py (g-115-6188). The wrapper is a pure
# exec passthrough — it sources _paths.sh and execs the same script with "$@" — so
# stdout, args and the rc contract above are unchanged. What it adds is the env:
# MIND_WORLD/WORLD_PATH come from the per-agent local-paths.conf that ONLY
# _paths.sh reads, while STORAGE_BACKEND is set globally in .claude/settings.json.
# A bare `py -3` therefore has STORAGE_BACKEND but no mappable world root, so the
# sweep takes the own-cloud branch, fails to build the backend, and silently reads
# every shard from the LOCAL MIRROR — on the orchestrator hot path, every
# iteration, on every agent. Measured on cc-02: identical args, identical output
# shape, `shard_provenance` local-mirror (bare) vs authoritative (wrapper). This
# sweep decides whether a claim held by a live peer instance is released, so mirror
# data here is exactly the stale input that makes it release a live claim or keep a
# dead one. See guard-3864 / rb-7918.
Bash: `out=$(bash core/scripts/stranded-claim-sweep.sh --apply 2>&1); rc=$?; printf '%s\n' "$out"; { [ $rc -eq 0 ] && [ -n "$out" ]; } || echo "[stranded-claim-sweep] DID NOT COMPLETE (rc=$rc, bytes=${#out}) — it always prints on every exit path, so this is NOT scanned=0: stranded claims were NOT released this iteration. Re-run it alone before trusting any claim state, and check this agent's outstanding-claim count."`

# READING THE `possible-displacement` VERDICT (g-306-142) — IT IS A PROMPT TO
# CHECK, NEVER A CONCLUSION TO ACT ON. A record carrying
# `"verdict": "possible-displacement"` with `"ambiguous": true` (counted in
# `summary["possible_displacement"]`) means that claim's claimed_by_sid is not
# this session's WHILE the agent's diary shows activity for the goal. That fits
# BOTH "another instance of me won the claim merge and I was displaced" AND the
# ORDINARY case "a peer instance legitimately holds it and I never worked it" —
# and no box-local store separates them, because the diary is not box-local
# (see the sync_tier note above). The ordinary case is the COMMON firing.
displacement_seen = any record in the sweep output with
                    verdict == "possible-displacement"
IF displacement_seen:
    Bash: `board-read.sh --channel coordination --since 24h`
    Look for a claim post on that goal_id from a DIFFERENT session_id — claim
    posts are append-only, so BOTH claims survive the merge overwrite that
    destroyed the evidence in the claim record itself (guard-1460).
    IF such a post exists AND its session is live:
        You were displaced. ABORT your work on that goal and do NOT re-select
        it this iteration. Do NOT release the claim — post-merge it belongs to
        the WINNER, and releasing would re-open the goal to a third instance,
        widening the conflict you just detected.
    ELSE:
        Ordinary peer-claim case. Continue the iteration unchanged.
# NEVER wire an automatic abort onto this verdict: under the ordinary reading
# it would abort a peer's legitimate work on every firing. The sweep stays
# report-only for the same reason — the consumer is this orchestrator, not the
# script. g-306-143 owns finding a sound per-session discriminator; only if
# that lands may this branch be strengthened.
# Rationale (WHY the verdict is hedged and why the diary cannot decide it):
#   core/config/rationale/possible-displacement-verdict.md

# Phase -0.5c.2: Pending Phase-6 Spark Sentinel (g-115-1174)
# Consumes the `pending_phase_6_spark` WM slot written by TWO producers:
# (1) recurring-close.sh at end-of-script — AFTER the four iteration-close
# phases, just before the terminal imperative (NOT during Block C/D, which
# runs earlier); (2) iteration-close.sh do_state_update for NON-recurring deep
# completions (g-115-2416 origin — script-enforced parity; the sentinel WRITE
# was MOVED from do_verify to do_state_update by g-115-2848 because do_verify's
# --outcome is optional and an omission silently no-op'd the write, g-115-2839
# — do_verify still emits the in-turn Phase-6 stdout imperative. Previously
# Phase 6 for non-recurring deep closes rode on LLM memory alone and drifted,
# observed miss g-115-2404). Both producers emit a stdout imperative AND the sentinel;
# spark-fire-dedup makes in-turn firing + sentinel consumption idempotent.
# Corollary:
# a NULL read here while the bg recurring-close is still mid-phase is EXPECTED,
# not a bug — the sentinel lands only once the bg reaches end-of-script.
# When recurring-close.sh's wall-clock
# exceeds the Bash 2-minute timeout the call backgrounds, the harness fires
# the stop hook before bg completes, and the LLM re-enters /aspirations loop
# never seeing the stdout outcome-aware imperative — Phase 6 spark was
# silently bypassed on deep recurring closes (observed 2/2: g-115-760
# bfzr7dvyk + g-115-754 bo42a8rld). This consumer decouples Phase 6
# dispatch from bg-completion: the sentinel persists on disk, visible to
# next-iteration entry regardless of when recurring-close.sh's stdout
# arrives.
#
# Sentinel-lifecycle pattern (rb-428): wm-read → if non-null → action →
# clear via `echo null | verified-wm-set.sh`. One-shot. Lifecycle below.
#
# The clear routes through verified-wm-set.sh, not a bare wm-set.sh
# (g-115-3698). THIS slot is the specimen the defect was measured on: a clear
# that did not land left the sentinel SET, and because the write was
# fire-and-forget nothing noticed — the next iteration re-entered this consumer
# against already-handled state. It was distinguishable from a producer re-arm
# only because `set_at` was UNCHANGED, which is a forensic accident, not a
# signal anyone can rely on. verified-wm-set.sh writes, reads the slot back,
# asserts equality, retries once, and fails loud. rc alone is not sufficient;
# the read-back is the assertion. All four clears below share this rationale —
# it is stated once, here.
signal = Phase -0.5a0 battery payload for pending_phase_6_spark
         (fallback only if battery errored: Bash: wm-read.sh pending_phase_6_spark --json)
IF signal is not null:
    expires_at = signal.get("expires_at")
    now_iso    = current ISO timestamp
    IF expires_at AND now_iso > expires_at:
        # Sentinel aged past its 60-min TTL — log + clear silently. Possible
        # cause: ran during a long pause or pre-existing stale entry. Do NOT
        # fire Phase 6 spark on stale signals — they may not reflect current
        # work.
        Output: "▸ PENDING-PHASE-6-SPARK: expired (set {expires_at}, age > 60min) — clearing without action"
        Bash: `echo 'null' | verified-wm-set.sh pending_phase_6_spark`
    ELIF signal.outcome == "deep":
        # Dedup against the fast-stdout-path double-fire (g-115-1203).
        # recurring-close.sh writes THIS sentinel AND emits a stdout outcome-
        # aware imperative. On the fast path the LLM already fired
        # Skill(aspirations-spark) in-turn from that stdout, and aspirations-
        # spark Step 0.5 recorded the firing via spark-fire-dedup.py. If this
        # goal was sparked AT/AFTER this sentinel's set_at, this sentinel is a
        # redundant re-fire — SKIP it and just clear. NOT a strict comparison:
        # the helper widens the window's LOWER bound by MAX_BG_CLOSE_DURATION_MIN
        # (currently 15), so a fire up to 15 min BEFORE set_at still counts as
        # THIS close's consumption. That is deliberate — the in-turn spark fires
        # at Phase 6 while set_at is written later (bg recurring-close, or Phase 8
        # for non-recurring), so a proactive fire legitimately predates set_at.
        # Expect "skip" on a fire that predates set_at by minutes; that is the
        # designed behavior, not an inversion. On the bg-timeout path
        # (the LLM never saw the stdout, so no in-turn fire happened) the check
        # returns "fire" and Phase 6 dispatches normally. Fail-open: on any
        # error the check prints "fire", so a dedup bug never suppresses spark.
        # Pipe the wm slot through the pure helper in ONE bash call (the helper
        # must not spawn `bash wm-*.sh` itself — rb-225/rb-247 hang). Pass the
        # sentinel's set_at so dedup is CONSUMPTION-BASED, not a fixed window:
        # the prior 5-min window false-fired when the bg-timeout wall-clock
        # between the in-turn fire and this check exceeded 5min (g-115-1404 /
        # rb-1674). Older sentinels without set_at pass empty → fall back to the
        # (now 60-min, TTL-aligned) window in spark-fire-dedup.py.
        # Pass `producer` too (g-115-3351). On the NON-recurring path the
        # sentinel is normally NOT WRITTEN AT ALL when an in-turn spark already
        # fired — iteration-close.sh do_state_update now checks
        # spark_fired_session before writing — so this consumer usually sees no
        # sentinel for that path. This flag is the defense-in-depth backstop for
        # the degraded case where that write-side probe failed open: for
        # producer="nonrecurring-state-update" the consumption window's LOWER
        # bound is DROPPED, because that gap is the LLM's unbounded Phase-6 span
        # and a non-recurring goal closes exactly once (no prior close to
        # mis-match). A sentinel with no producer field (recurring-close.sh, or
        # any pre-g-115-3351 sentinel) keeps the bounded behavior, which is
        # correct for it — there the lookback models a bounded script runtime.
        # --diary-file (g-115-4201): set_at/producer decide WHICH close a fire
        # belongs to; neither helps when there is NO RECORD, and a fire that was
        # never recorded returns "fire" forever. Rationale + incident: that
        # script's `check --help`.
        Bash: dedup=$(bash core/scripts/wm-read.sh spark_fired_session --json | py -3 core/scripts/spark-fire-dedup.py check {signal.goal_id} --sentinel-set-at "{signal.set_at}" --producer "{signal.producer}" --diary-file "agents/$MIND_AGENT/session/execution-diary.jsonl")
        IF dedup == "skip":
            Output: "▸ PENDING-PHASE-6-SPARK: dedup-skip for {signal.goal_id} — spark already fired in-turn (fast-stdout path) at/after this sentinel's set_at; clearing sentinel without re-firing (g-115-1203 / g-115-1404)"
            Bash: `echo 'null' | verified-wm-set.sh pending_phase_6_spark`
        ELSE:
            # "fire" — bg-timeout path: no in-turn fire, so the set_at
            # comparison finds no same-close fire. (The legacy "5-min window
            # elapsed" false-fire is fixed by the set_at basis — g-115-1404.)
            # Fire Phase 6 spark for the named recurring goal. The sentinel
            # carries goal_id + source + summary so the spark handler has the
            # context that the in-bg stdout would have provided.
            Output: "▸ PENDING-PHASE-6-SPARK: outcome=deep for {signal.goal_id} — firing Skill(aspirations-spark) before precheck"
            invoke /aspirations-spark with: goal_id={signal.goal_id},
                                            source={signal.source},
                                            outcome_class=deep,
                                            summary={signal.summary}
            # Clear AFTER spark dispatch (one-shot).
            Bash: `echo 'null' | verified-wm-set.sh pending_phase_6_spark`
    ELSE:
        # outcome=routine — Phase 6 is skipped by the standard skip-rule.
        # Clear silently; the sentinel just records the close attempt.
        Output: "▸ PENDING-PHASE-6-SPARK: outcome=routine for {signal.goal_id} — Phase 6 skipped per skip-rule, clearing sentinel"
        Bash: `echo 'null' | verified-wm-set.sh pending_phase_6_spark`
    # Continue to Phase -0.5e.0.

# Phase -0.5e.0: Quiescence-Cycle Fast-Path Short-Circuit (g-303-12)
# Runs BEFORE idle-tick.sh. When the loop quiescence-approves repeatedly under
# an UNCHANGED blocker set, each re-entry otherwise re-runs the full precheck/
# select/all-blocked/quiescence-gate chain only to re-derive the same "all
# blocked, approve, sleep" decision. This fast path re-validates the LAST
# gate-approved cycle (same blocker hash, nothing expired, no new work, no
# pending blocker signal, under the consecutive-short-circuit cap) and on a HIT
# re-sleeps directly WITHOUT loading the heavy skill chain. The script does ALL
# the validation: a cheap active_snapshot gate (skips the selector entirely when
# not mid-quiescence) THEN a single live blocker-hash recompute. Every path
# fails open to a MISS (empty stdout) so a bug here can only fall back to the
# normal full cycle, never wrongly short-circuit. Common path (not in
# quiescence) is ONE cheap script call returning empty.
Bash: py -3 core/scripts/quiescence-cycle-cache.py check
IF stdout contains "=== QUIESCENCE CACHE HIT ===":
    # The directive names the EXACT single tool call to emit: an
    # interruptible-sleep with run_in_background=true carrying the cached
    # sleep_seconds. Emit ONLY that Bash call as the terminal action and RETURN.
    # Do NOT run idle-tick.sh, do NOT load Skill(aspirations), do NOT run
    # precheck/selection/execution. The bg sleep IS the terminal tool call;
    # when the harness notifies you of its exit, re-enter via Skill(aspirations)
    # args='loop'. Do NOT ScheduleWakeup to poll the sleep — the harness
    # auto-notifies on bg completion (schedule-wakeup-correctness.md
    # Anti-pattern A). This mirrors idle-tick.sh Branch A's terminal contract.
    Emit the directive's single `Bash(... interruptible-sleep.sh {sleep_seconds}, run_in_background=true)` call. RETURN.
# ELSE: cache MISS (empty stdout) — fall through to the dry-idle cache check below.

# Phase -0.5e.0b: Dry-Idle-Cycle Fast-Path Short-Circuit (g-115-2084-d, Layer 4)
# Runs right AFTER the quiescence-cache check (dry and quiescence are mutually
# exclusive — _dry_idle.is_dry_state is False whenever quiescence approved) and
# BEFORE idle-tick.sh. When the loop is in a persistent DRY trough (zero
# executable goals + quiescence denied/na), each re-entry otherwise re-runs the
# full precheck/select/create-aspiration/re-select chain only to re-derive
# "still dry, sleep the backoff curve". This fast path re-validates the last
# dry-idle-tick decision (still dry per the dry_idle signal, goal count
# unchanged, no timer elapsed, no pending wake signal, under the
# consecutive-short-circuit cap) and on a HIT re-sleeps the SAME dry-curve
# backoff directly WITHOUT loading the heavy skill chain. Unlike the quiescence
# cache (which caps each short-circuit at 600s for health cadence), the dry cache
# honors the full exponential curve (up to 2h) because long sleeps under a
# stable-empty queue ARE the point of the dry-idle backoff (Layers 1-3);
# soundness comes from capping the emitted sleep at earliest_wake_at (the next
# defer/recurring/blocker timer) instead. Every path fails open to a MISS (empty
# stdout). Common path (not in a dry trough) is ONE cheap script call (one WM
# read, no queue scan) returning empty.
Bash: py -3 core/scripts/dry-idle-cycle-cache.py check
IF stdout contains "=== DRY-IDLE CACHE HIT ===":
    # Same terminal contract as the quiescence cache above: emit ONLY the
    # directive's single Bash(... DRY_SLEEP=1 ... interruptible-sleep.sh
    # {sleep_seconds}, run_in_background=true) call and RETURN. Do NOT run
    # idle-tick.sh, do NOT load Skill(aspirations), do NOT run precheck/
    # selection/execution. The bg sleep IS the terminal tool call; when the
    # harness notifies you of its exit, re-enter via Skill(aspirations)
    # args='loop'. Do NOT ScheduleWakeup to poll the sleep — the harness
    # auto-notifies on bg completion (schedule-wakeup-correctness.md
    # Anti-pattern A). guard-967/882: the DRY_SLEEP=1 bg sleep is the sanctioned
    # Tier-A registered-mechanism pacing — interruptible-sleep.sh registers it as
    # a background job (type dry-idle-sleep) so stop-hook Gate 2.6 ALLOWs the
    # turn-end. This mirrors the quiescence cache's terminal contract exactly.
    Emit the directive's single `Bash(... interruptible-sleep.sh {sleep_seconds}, run_in_background=true)` call. RETURN.
# ELSE: cache MISS (empty stdout) — fall through to idle-tick.sh below.

# Phase -0.5e: Blocked-Sleep Recovery — cheap-path sentinel + gated digest load.
# DO NOT inline the digest here. Loading it every iteration (vs. only when a
# directive fires or a timer is set) adds ~2k tokens to the hot path. The
# common path (no blocked timer, no idle-tick directive) exits this phase
# with only one cheap script call and zero digest load.
# DO NOT call idle-tick.sh or wm-read.sh blocked_sleep_until a second time
# inside the digest — idle-tick is time-dependent (recomputes REMAINING on
# every call) and wm-read.sh mutates accessed_at. The outputs from the two
# calls below MUST be the only ones the digest consumes.
Bash: core/scripts/idle-tick.sh
IF stdout contains "=== IDLE TICK ===":
    # Branch A — idle-tick directive. Digest names the exact sleep to run.
    Bash: core/scripts/load-blocked-sleep-recovery.sh → IF path returned: Read it
    Follow Branch A from the digest. RETURN.
ELSE:
    value = Phase -0.5a0 battery payload for blocked_sleep_until
            (fallback only if battery errored: Bash: wm-read.sh blocked_sleep_until)
    IF value is not "null" and not empty:
        # Branch B — post-sleep residual / checkpoint-wake / expired-timer.
        Bash: core/scripts/load-blocked-sleep-recovery.sh → IF path returned: Read it
        Follow Branch B from the digest inline.
    # ELSE: no timer set — fall through to Phase -0.5d (identity restoration).

# Phase -0.5e': Quiescence Wake-Verify (Change 2)
# On re-entry after any sleep, check whether quiescence was active and verify
# that external state drift matches the agent's actions. The gate reads its
# own active_snapshot from WM — calling when no snapshot exists is a no-op.
# rc=0 clean, rc=2 drift detected (gate logs audit record AND increments the
# misses counter; 3 consecutive misses auto-disable quiescence for session).
# Safe to call unconditionally; the script handles the no-snapshot path.
Bash: MIND_AGENT=<agent> py -3 core/scripts/quiescence-gate.py verify-wake
# Parse JSON stdout; on rc=2 (miss), file an Investigate goal naming the
# drifted external_id so the next iteration resolves the missed signal.

# Phase -0.5d: Identity Context Restoration
Read agents/<agent>/self.md
Bash: world-cat.sh program.md  # skip if empty/missing
```

## ═══ PER-ITERATION OBLIGATIONS (MANDATORY — never skip) ═══

After every goal execution, these MUST complete before `LOOP_CONTINUE`.
Each obligation is a `Bash: iteration-close.sh --phase <name>` call PLUS the
LLM-residue checklist from `core/config/iteration-close-digest.md`. The bash
handles the mechanical bookkeeping (status updates, streaks, journal, WM,
team-state, diary, board posts); the digest directs the LLM-only judgment steps.

Before the first VERIFY call in a session, load the residue digest:
`Bash: load-iteration-close-digest.sh → IF path returned: Read it`
(`context-reads.py check-file` dedups — cheap to call every iteration; only
the first firing per session actually reads the file into context.)

1. **VERIFY**: `Bash: iteration-close.sh --phase verify ...` + digest § VERIFY
   (Gate D §4.6 [omni bless amendment 2026-06-11]: when `bash core/scripts/gate-d-check.sh`
   returns "on", prefix the verify call — or the recurring-close.sh shortcut — with the
   primary-outcome env vars: `GATE_D_VERIFY_FIRST_PASS=<true|false>
   GATE_D_VERIFY_ESCALATION_DEPTH=<0-3> GATE_D_RETRY_COUNT=<n>`.
   first_pass = Phase 5 verify accepted on the FIRST attempt (no Q1→Q2 escalation,
   no re-execution). **A goal whose final status is `blocked` or `skipped` did NOT
   first-pass — set GATE_D_VERIFY_FIRST_PASS=false for it** (amendment 6: the first
   two pilot goals reported first_pass=true while ending blocked — a blocked goal
   cannot have passed verification; compute the value from what actually happened,
   never default it to true). These fields are arm-blind — never name or infer the
   arm. Without this prefix the OUTCOME record's primary endpoint is null and the
   goal is dropped at analysis join time.)
2. **STATE**: `Bash: iteration-close.sh --phase state-update ...` + digest § STATE-UPDATE
3. **MAINTAIN**: Working memory maintenance — sensory buffer, aging, prune (already bash)
4. **LEARN**: `Bash: iteration-close.sh --phase learning-gate ...` + digest § LEARNING-GATE
5. **PRODUCTIVITY**: `Bash: iteration-close.sh --phase productivity-check`

**Recurring-goal shortcut**: when the just-executed goal has `recurring: true`,
collapse steps 1, 2, 4, 5 into a single call:
`Bash: recurring-close.sh <goal-id> <routine|deep> [--source world|agent] [--summary "..."] [--tree-updated] [--tree-updated-override] [--artifacts-count N] [--encoding-score X] [--findings-count N]`

The five § STATE-UPDATE quality flags (g-115-3192) are forwarded to the wrapped
`--phase state-update` ONLY. Pass them on THIS call for a deep close — guard-1235
rules out any post-hoc amend, because re-running state-update to add them re-fires
journal-append and iteration-commit. Omitting them leaves iteration-close.sh at its
argparse defaults, exactly as before.
This wraps the 4 phases atomically AND advances `lastAchievedAt` (via
`aspirations-complete-by.sh` inside `iteration-close.sh do_verify`) AND
maintains the cross-session `consecutive_routine` counter on the goal AND
fires `cargo-cult-detector.py` at threshold (`recurring.cargo_cult_threshold`,
default 3 in `core/config/aspirations.yaml`) AND — Magic Wand #1, alpha
session-60 — applies signal-mutation Block A/B/C/D atomically via
`recurring-loop-state-mutate.sh` BEFORE the four iteration-close phases.
The bash gate is the single writer for `routine_streaks[goal.id]`,
`signals.routine_streak_global`, `signals.routine_count_total`,
`signals.productive_streak`, `signals.consecutive_blocked_sleeps` (deep
reset only), `goals_completed_this_session`, and
`productive_goals_this_session`. **The LLM MUST NOT also patch these
fields** — the previous manual workflow ("after recurring-close, bump
goals_completed and update routine_streaks manually") is retired and
double-counts streaks if reintroduced.

Without this shortcut, recurring goals re-select forever at high score
because `lastAchievedAt` never advances through the plain
`iteration-close.sh status completed` path. Step 3 (working memory
maintenance) still runs separately around the wrapper.

**Phase 6 spark is NOT wrapped by recurring-close.sh** (g-115-977 / g-115-965).
The shortcut collapses Phase 5/8/12 but Phase 6 (aspirations-spark) sits
between Phase 5 (verify) and Phase 8 (state-update) in the larger iteration
body and must be invoked separately for deep outcomes. recurring-close.sh
emits an OUTCOME-AWARE terminal imperative on stdout: when its final
classification (post Block A/C flip) is `deep`, the imperative directs
`Skill(aspirations-spark)` FIRST then `Skill(aspirations)` LOOP_CONTINUE;
when `routine`, only the LOOP_CONTINUE imperative fires. Follow the script's
terminal imperative verbatim — DO NOT skip Phase 6 on deep recurring closes,
and DO NOT fire it on routine closes. The previous (pre-g-115-977) imperative
unconditionally said "Skill(aspirations) only", which silently bypassed Phase 6
on forced-flip deep outcomes — the docs-vs-impl drift class flagged by the
universal reasoning-bank entry "Docs-vs-impl drift in framework shortcut
wrappers".

The sub-skills `aspirations-verify`, `aspirations-state-update`, and
`aspirations-learning-gate` remain on disk for `/boot`, consolidation, and
edge cases that need the full protocol — they are NOT a hot-path fallback.
The hot path is always `iteration-close.sh`; if it fails, fix the script.

Skip rules:
- Spark: SKIP if outcome_class == "routine"
- Completion review: SKIP if aspiration not fully complete
- Evolution: respects per-session cap (max_evolutions_per_session)

If ANY obligation is skipped without justification, log "OBLIGATION SKIPPED: {phase}".

## Loop Continuation Protocol (LOOP_CONTINUE)

Every `LOOP_CONTINUE` means a single step: call `Skill('aspirations') with args='loop'`.

LLM-side `wm-set loop_state` mirror retired (g-283-04/05/06 closed). Bash gates
are the sole writers of loop_state. The trio: `recurring-loop-state-mutate.py`
(routine_streaks, signals, *_this_session counters via recurring-close.sh),
`loop-state-bump-counters.py` (goals_completed / productive_goals via
iteration-close.sh do_state_update — fires for BOTH recurring and non-recurring
goals), and `iteration-close.sh` (top-level orchestrator that calls both).
Regression tests pin the contract: shape invariance via
`test_compact_restore_loop_state_shape.py` (g-283-03), counter advance via
`test_loop_state_counter_advance.py` (g-283-06).

This is NOT optional. The `Skill()` call is what keeps the loop alive.
NEVER produce text-only output at a `LOOP_CONTINUE` point.
NEVER skip the `Skill()` call. NEVER say "continuing..." without calling it.
NEVER substitute with inline code instead of the `Skill()` call.

```
# ═══ SINGLE ITERATION (self-reinvoking via LOOP_CONTINUE) ═══
# Body condensed to a digest — Phases 1.5, 3, 4, 4.5, 5, 5.3, 5.5, 5.7, 6,
# 7, 8, 8-stop, 8.0.5, 8.0.6, 8.1, 8.7, 8.8, 9, 10, 11, 12 all live in
# core/config/aspirations-loop-digest.md. Reload cost ~2k tokens vs. ~15k
# for the full body. Follow the digest inline.
Bash: load-loop-digest.sh → IF path returned: Read it
# The digest is the authoritative per-iteration spec. It covers:
#   - Iteration State Variables (7 counters + routine_streaks) — bash gates
#     own all writes to loop_state (no LLM mirror; see retirement note above).
#   - Phase ordering + skip rules, signal-mutation table, anti-drift rules.
#   - LOOP_CONTINUE contract (just Skill('aspirations'); no preceding wm-set).
# Edge cases (CREATE_BLOCKER structural JSON, Cognitive Primitives, full
# pseudocode for rare fallbacks) live in the relevant sub-skill SKILL.md
# files (aspirations-execute, aspirations-all-blocked, etc.) — read those
# directly when a branch requires full detail. The digest points you at the
# right sub-skill for each phase.
# ═══ END SINGLE ITERATION ═══
```

## Single-Iteration Body — Where It Lives

- **Primary spec**: `core/config/aspirations-loop-digest.md` (loaded via
  `load-loop-digest.sh`). The digest IS the source of truth for the
  per-iteration body — no companion SKILL.md copy exists to drift against.
- **Full pseudocode**: Sub-skills own their own phase bodies
  (`aspirations-precheck`, `aspirations-select`, `aspirations-all-blocked`,
  `aspirations-execute`, `aspirations-verify`, `aspirations-spark`,
  `aspirations-state-update`, `aspirations-evolve`,
  `aspirations-learning-gate`, `aspirations-consolidate`, `aspirations-graceful-stop`).

<!-- The remainder of this SKILL.md is session-end consolidation and
     reference sections. The single-iteration body itself is NOT here — it
     lives in the digest above. Do not re-inline it: reload cost is the
     whole point of the extraction. -->

### Session-End Consolidation Pass

Run when the loop stops (Phase 10 BREAK). Hippocampal "sleep replay".

```
# Graduated consolidation: check encoding queues before loading full skill
Bash: consolidation-precheck.sh
# Returns JSON: {"verdict":"FAST"|"FULL", "total": N, ...}

IF verdict == "FULL" (total > 0):
    invoke /aspirations-consolidate with: session_count, goals_completed_this_session, evolutions_this_session
ELIF verdict == "FAST" (total == 0):
    # Load ~150-line housekeeping digest instead of full ~497-line skill
    Bash: load-consolidation-housekeeping.sh → IF path returned: Read it
    # Follow housekeeping steps. If encoding work appears mid-consolidation
    # (rare), invoke /aspirations-consolidate for the full pipeline.
ELSE:
    # Precheck error — safe fallback
    invoke /aspirations-consolidate with: session_count, goals_completed_this_session, evolutions_this_session

# Clear loop_state — clean slate for next session
echo 'null' | Bash: wm-set.sh loop_state
```

Consolidation MUST NOT call session-state-set.sh.
/stop invokes with stop_mode=true (skips tree rebalancing, reporting, user recap, restart).

### Auto-Session Continuation Protocol

**Within a session**: Stop hook (`.claude/settings.json`) blocks unconditionally when RUNNING.
No tiers, no counter, no safety valve. Just BLOCK + re-invoke `Skill('aspirations') with args='loop'`.

**Self-reinvocation**: Each iteration ends with LOOP_CONTINUE → saves state to WM → calls
`Skill('aspirations') with args='loop'`. The agent re-reads this SKILL.md fresh every iteration.
State persists via the `loop_state` WM slot.

**Across consolidation cycles**: Consolidation invokes `/boot` → detects handoff.yaml → continuation mode.

**Signal files**: `loop-active`, `stop-loop`, `handoff.yaml`

**Stopping**: /stop → IDLE + stop-loop → hook allows → loop exits. Ctrl+C also works.

---

## Reference Docs (loaded on-demand)

- **Completion check runners**: `Bash: load-completion-runners.sh` → `core/config/completion-check-runners.md`
- **Goal selection algorithm**: `core/config/goal-selection-algorithm.md`
- **Stop/skip conditions**: `Bash: load-stop-skip-conditions.sh` → `core/config/stop-skip-conditions.md`
- **Execute protocol**: `Bash: load-execute-protocol.sh` → `core/config/execute-protocol-digest.md`
- **Single-iteration loop body**: `Bash: load-loop-digest.sh` → `core/config/aspirations-loop-digest.md` (~112 lines — reloaded every LOOP_CONTINUE instead of the full body)
- **Output format**: `core/config/status-output.md`
- **State update protocol**: Defined in /aspirations-state-update

---

## Chaining Map

Hot-path phases (per iteration): `/aspirations-precheck` → `/aspirations-select`
→ `/aspirations-execute` → `/aspirations-verify` → `/aspirations-spark` (deep only)
→ `/aspirations-state-update` → `/aspirations-learning-gate`. Conditional /
cadence-triggered: `/aspirations-evolve` (cadence), `/aspirations-complete-review`
(aspiration near-complete), `/aspirations-strategic-scan` (cadence),
`/aspirations-all-blocked` (selector returned no goals). Session boundaries:
`/boot`, `/aspirations-consolidate`, `/aspirations-graceful-stop`. Full table
with call-site and return semantics: **`core/config/aspirations-chaining-map.md`**.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The orchestrator's terminal action is always LOOP_CONTINUE (Skill call) or a Bash
write (wm-set, session-signal-clear). Never end the loop with a text summary.

### Deadman's-switch terminal-pair (DEFAULT-ON since 2026-06-23)

The silent-loop-death gap — a turn ending on trailing text → no Stop event →
loop dies and sits dead for hours (observed 2026-06-21: 5 of 6 agents dead
1.5–4h) — is closed intrinsically by self-arming a resurrection wakeup. By
DEFAULT (Stage 5 onward — unless the per-agent opt-out flag
`agents/<agent>/session/deadman-disabled` is present), the iteration's terminal
response emits TWO batched tool calls in this EXACT order:

1. `ScheduleWakeup(prompt="<<autonomous-loop-dynamic>>", delaySeconds=600)` —
   re-arm the deadman net. Single replace-slot (each iteration's re-arm
   replaces the prior); never fires on a healthy loop (the session is never
   idle 600s — the Skill chain re-arms it forward first); fires ONLY if a
   text-death leaves the session idle past 600s, resurrecting the loop via the
   sentinel.
2. `Skill(aspirations)` with `args='loop'` — the PRIMARY re-entry, the LAST
   call, continues the loop NOW exactly as before.

`Skill(aspirations)` is and stays the primary re-entry; the ScheduleWakeup is a
NET behind it, NOT a substitute (reconciles guard-511 / schedule-wakeup-
correctness.md Anti-pattern C). The `iteration-close.sh` / `recurring-close.sh`
imperatives print this pair by DEFAULT; they print `Skill(aspirations)` alone
(the pre-deadman status quo) ONLY when the `deadman-disabled` opt-out flag is
present. BOTH calls are mandatory EVERY iteration — emitting `Skill(aspirations)`
alone keeps THIS iteration alive but leaves the NEXT one unprotected (the net
covering iteration N+1 is the wakeup armed at iteration N's terminal). The arm is
always safe to emit: the deadman is purely additive and fail-safe — worst case
is a slow loop, never a dead one. Q5 (does Skill re-enter after the arm) RESOLVED
favorable over charlie's first 24h (23/23 re-entries, 0 deaths, ARMED-OK). Full
rationale + verified platform facts: `core/config/rationale/deadman-switch.md`.
