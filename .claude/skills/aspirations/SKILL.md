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
2. If found: ATTEMPT it. If succeeds: skip pq-XXX
3. If fails: write pq-XXX with `attempted_solutions` + `autonomous_search_done: true`

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

# Phase -1: Initialize Working Memory (runs once, idempotent)
Bash: wm-read.sh --json
if all slots null:
    Read core/config/memory-pipeline.yaml
    Bash: wm-init.sh
    echo '{"session_id": "session-{session_count}", "session_start": "<today>"}' | Bash: wm-set.sh active_context
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

# Phase -0.5a: Background Agent Result Collection
IF agents/<agent>/session/pending-agents.yaml EXISTS:
    # list --json prunes stale agents (past timeout_minutes) before returning.
    # Stale agents are removed from the file and excluded from output.
    Bash: `pending-agents.sh list --json`
    FOR EACH non-stale agent: collect results, deregister. IF none pending: clear.

# Phase -0.5c: Compact Checkpoint Processing
# Full protocol: core/config/conventions/compact-recovery.md
IF agents/<agent>/session/compact-checkpoint.yaml EXISTS:
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
# {scanned: 0}). With --apply: releases the claim, flips status to
# pending, clears matching team-state.in_flight. The 5-min stale threshold
# guards against race conditions where Phase 4 was just starting.
# Direct py -3 invocation (not bash wrapper) — see rb-225/rb-247 for the
# Windows bash subprocess hang.
Bash: `py -3 core/scripts/stranded-claim-sweep.py --apply`

# Phase -0.5c.2: Pending Phase-6 Spark Sentinel (g-115-1174)
# Consumes the `pending_phase_6_spark` WM slot written by recurring-close.sh
# at end-of-script — AFTER the four iteration-close phases, just before the
# terminal imperative (NOT during Block C/D, which runs earlier). Corollary:
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
# clear via `echo null | wm-set.sh`. One-shot. Lifecycle below.
Bash: wm-read.sh pending_phase_6_spark --json
IF signal is not null:
    expires_at = signal.get("expires_at")
    now_iso    = current ISO timestamp
    IF expires_at AND now_iso > expires_at:
        # Sentinel aged past its 60-min TTL — log + clear silently. Possible
        # cause: ran during a long pause or pre-existing stale entry. Do NOT
        # fire Phase 6 spark on stale signals — they may not reflect current
        # work.
        Output: "▸ PENDING-PHASE-6-SPARK: expired (set {expires_at}, age > 60min) — clearing without action"
        Bash: `echo 'null' | wm-set.sh pending_phase_6_spark`
    ELIF signal.outcome == "deep":
        # Fire Phase 6 spark for the named recurring goal. The sentinel
        # carries goal_id + source + summary so the spark handler has the
        # context that the in-bg stdout would have provided.
        Output: "▸ PENDING-PHASE-6-SPARK: outcome=deep for {signal.goal_id} — firing Skill(aspirations-spark) before precheck"
        invoke /aspirations-spark with: goal_id={signal.goal_id},
                                        source={signal.source},
                                        outcome_class=deep,
                                        summary={signal.summary}
        # Clear AFTER spark dispatch (one-shot).
        Bash: `echo 'null' | wm-set.sh pending_phase_6_spark`
    ELSE:
        # outcome=routine — Phase 6 is skipped by the standard skip-rule.
        # Clear silently; the sentinel just records the close attempt.
        Output: "▸ PENDING-PHASE-6-SPARK: outcome=routine for {signal.goal_id} — Phase 6 skipped per skip-rule, clearing sentinel"
        Bash: `echo 'null' | wm-set.sh pending_phase_6_spark`
    # Continue to Phase -0.5e.

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
    Bash: wm-read.sh blocked_sleep_until
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
2. **STATE**: `Bash: iteration-close.sh --phase state-update ...` + digest § STATE-UPDATE
3. **MAINTAIN**: Working memory maintenance — sensory buffer, aging, prune (already bash)
4. **LEARN**: `Bash: iteration-close.sh --phase learning-gate ...` + digest § LEARNING-GATE
5. **PRODUCTIVITY**: `Bash: iteration-close.sh --phase productivity-check`

**Recurring-goal shortcut**: when the just-executed goal has `recurring: true`,
collapse steps 1, 2, 4, 5 into a single call:
`Bash: recurring-close.sh <goal-id> <routine|deep> [--source world|agent] [--summary "..."]`
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
