# Aspirations Loop — Single-Iteration Digest

Compact reference for the per-iteration body of `/aspirations loop`. Boot and
session-end sections stay in `.claude/skills/aspirations/SKILL.md`; the rest
of the iteration body is condensed here so LOOP_CONTINUE re-reads pay
~2k tokens instead of ~15k. For rare edge cases (full pseudocode, historical
notes, fallback branches) read the SKILL.md directly.

## Iteration State Variables (persisted in `loop_state` WM slot by bash gates)

Seven counters plus `routine_streaks`. All MUST be re-loaded after autocompact.

```
goals_completed_this_session        (int)      — all goals, incremented at learning-gate
productive_goals_this_session       (int)      — deep outcomes only
evolutions_this_session             (int)
last_evolution_goal_count           (int)
goals_since_last_alignment_check    (int)
aspirations_touched_this_session    (set[str])
session_signals                     (dict)     — see sub-keys below
routine_streaks                     (dict[goal_id → int])  — per-goal anti-drift
```

`session_signals` sub-keys: `routine_streak_global`, `productive_streak`,
`routine_count_total`, `goals_since_last_tree_update`,
`consecutive_goal_failures`, `last_failed_goal_id`,
`consecutive_blocked_sleeps`.

### Bash-owned fields (DO NOT include in LOOP_CONTINUE serialization)

These fields are written by bash gates DURING the iteration, NOT by the LLM at
LOOP_CONTINUE. The LLM serializing them clobbers bash mutations and re-introduces
the silent-corruption class Magic Wand #1 was designed to eliminate. Phase -0.5
of the next iteration restores loop_state from WM, picking up bash values.

| Field | Single writer | Trigger |
|---|---|---|
| `signals.goals_since_last_tree_update` | `tree-encoding-drift-gate.py` | iteration-close.sh state-update |
| `routine_streaks[goal.id]` | `recurring-loop-state-mutate.py` | recurring-close.sh (Block A) |
| `signals.routine_streak_global` | `recurring-loop-state-mutate.py` | recurring-close.sh (Block B/C) |
| `signals.routine_count_total` | `recurring-loop-state-mutate.py` | recurring-close.sh (Block B) |
| `signals.productive_streak` | `recurring-loop-state-mutate.py` | recurring-close.sh (Block B) |
| `signals.consecutive_blocked_sleeps` | `recurring-loop-state-mutate.py` (deep reset) + LLM (Phase B7 backoff increment in all-blocked) | mixed |
| `goals_completed_this_session` | `recurring-loop-state-mutate.py` (recurring) + LLM (non-recurring Phase 12) | recurring-close.sh + LLM |
| `productive_goals_this_session` | `recurring-loop-state-mutate.py` (recurring) + LLM (non-recurring Phase 4.1 Block D) | recurring-close.sh + LLM |
| `touched` (aspirations_touched_this_session) | `loop-state-bump-counters.py --goal-id` | iteration-close.sh state-update — every close (g-115-1561) |
| `alignment_check_at` increment (goals_since_last_alignment_check) | `loop-state-bump-counters.py --goal-id` | iteration-close.sh state-update — every close (g-115-1561) |
| `alignment_check_at` reset to 0 | `loop-state-bump-counters.py --reset-alignment` | aspirations-select Self-Alignment Check fires (g-115-1561) |
| `evolutions` + `last_evolution_at` (evolutions_this_session, last_evolution_goal_count) | `loop-state-bump-counters.py --evolution-fired` | aspirations-evolve, every invocation (g-115-1561) |

For the recurring path, the LLM does NOT need to apply Phase 4.1 Block A/B/C/D
manually — `recurring-close.sh` invokes `recurring-loop-state-mutate.sh` BEFORE
the four iteration-close phases and uses the post-flip outcome. The LLM continues
to apply Block A/B/C/D for NON-recurring goals (path is unchanged).

### LLM-owned fields (the only loop_state writes the LLM still makes)

Two narrow, slot-specific read-merge-writes remain — NOT a full-slot mirror:

- `signals.consecutive_goal_failures` + `signals.last_failed_goal_id` —
  circuit-breaker pair, overlaid at the aspirations-learning-gate LOOP_CONTINUE
  (Phase 12). No bash writer yet; tracked as a follow-up to g-115-1561.
- `idle_fallback_created` + `signals.consecutive_blocked_sleeps` — overlaid by
  aspirations-all-blocked (B2.5 idle_fallback / B7 backoff). Reached only on the
  all-blocked path.

g-115-1561 moved `evolutions` / `last_evolution_at` / `alignment_check_at` /
`touched` OUT of this set into bash ownership (table above). They were orphaned
BECAUSE the LLM-side serialize was unreliable — zeta's g-115-1557 investigation
found `touched=[]` universally despite 68-76 goals/agent. The LLM no longer
overlays them.

### Read-merge-write discipline for the remaining LLM-owned fields

Where an LLM-owned field above IS overlaid (learning-gate circuit-breaker pair;
all-blocked idle/backoff), the LLM MUST read loop_state fresh from WM, overlay
ONLY those specific keys onto the loaded dict, and write back. NEVER write
loop_state from a stale snapshot held since the start of the iteration — bash
gates have run since. NEVER overlay a bash-owned field (the table above) — that
re-introduces the clobber class g-115-1561 fixed.

## Phase Ordering (skip rules inline)

```
  # Tier 0 phase-cost telemetry: every Phase block wrapped by phase-start /
  # phase-end markers emits a greedy FIFO-paired duration record to
  # agents/<agent>/session/execution-diary.jsonl. phase-cost-report.py consumes these.
  # On early-return (LOOP_CONTINUE or RETURN) emit phase-end before returning;
  # unmatched starts show as `in_flight` in the report (detectable, not fatal).
  Phase 0-1.  Bash: execution-diary.sh phase-start phase-0-precheck
              Skill(aspirations-precheck)
              Bash: execution-diary.sh phase-end phase-0-precheck
  Phase 1.5.  Strategic scan — IF scan_due (goal_cadence, recurring_settling, OR time_cadence)
              THEN Bash: execution-diary.sh phase-start phase-1-strategic-scan;
                   Skill(aspirations-strategic-scan);
                   Bash: execution-diary.sh phase-end phase-1-strategic-scan.
              Skill owns its own last_strategic_scan cadence stamp
              (single-writer per guard-155).
  Phase 2-2.9 Bash: execution-diary.sh phase-start phase-2-select
              Skill(aspirations-select) → goal | selection_reason | source
              IF selection_reason starts with "all_blocked":
                  Bash: execution-diary.sh phase-end phase-2-select
                  Skill(aspirations-all-blocked) → RETURN (yield turn)
                  # all-blocked sub-skill owns the B-ladder. Quiescence gate
                  # (B6.5) branches three ways: rc=1 denied → B6.7 targeted deep
                  # work; rc=0 approved + approved_but_drainable=true → B6.8 drain
                  # ONE hygiene unit (decompose/hypothesis/finding) then B7.2
                  # sleep; rc=0 approved + drainable=false → straight to B7.2
                  # quiescent sleep (back-compat preserved). (g-303-28)
              IF goal is None:
                  Bash: execution-diary.sh phase-end phase-2-select
                  /create-aspiration from-self --plan; fallback /research-topic + /reflect
                  LOOP_CONTINUE
              Bash: execution-diary.sh phase-end phase-2-select --goal {goal.id}
  Phase 3.    IF compound: /decompose goal.id; if status==decomposed → LOOP_CONTINUE
  Phase 4.    Claim-conflict gate (live partner snapshot — see coordination.md
              "in_flight Field"): Bash: team-state-read.sh --field
              agent_status.<partner>.in_flight.goal_id --json. IF the returned
              goal_id == goal.id, the partner already in_flight on this goal —
              do NOT post a claim, do NOT write team-state, journal the abort
              ("claim-conflict: <partner> in_flight on {goal.id}"),
              and LOOP_CONTINUE.   # no phase-end — phase-start is written AFTER the claim (below)
              # Same-surface coordination probe (g-305-03): the in_flight gate above
              # catches a partner CURRENTLY claiming this goal; it does NOT catch a
              # partner who already SHIPPED overlapping work and released (canonical
              # 2026-05-13 race — zeta shipped g-115-697, alpha claimed same-surface
              # g-115-696 4h later). Before claiming, run the two-probe advisory
              # check (git log --since=2h over the goal's surface + partner last_active):
              Bash: bash core/scripts/goal-pickup-coordination-check.sh --goal-id {goal.id} --source {source} --output json
              IF race_risk == true: read the named overlapping commit(s). IF the goal's
              outcome is already shipped → mark it completed (superseded) via
              aspirations-update-goal.sh + journal the supersession, and LOOP_CONTINUE
              (do NOT claim). ELSE (surface overlaps but the goal is genuinely still
              open) proceed to claim — the probe is ADVISORY, never a hard gate
              (fail-open, exit 0; heuristic affected-paths inference must not freeze work).
              IF source==world: aspirations-claim.sh
              (conflict → journal abort + LOOP_CONTINUE; no phase-end — phase-start not yet written).
              # phase-start AFTER the claim (g-115-1371 / rb-1533): the phase-4-execute marker
              # MUST post-date claimed_at so the Phase -0.5c.1 stranded-claim sweep's "diary
              # entry after claimed_at" in-flight signal survives a long post-claim pause
              # (backgrounded tests, stop-hook re-entry). The prior order (marker BEFORE the
              # claim) false-released legitimately-in-flight goals. phase-start also means
              # "Phase 4 began": an autocompact orphan that never reached this line lacks the
              # marker and is correctly released; a paused-but-working goal has it and is kept.
              # (A pre-claim diary window or in_flight match would NOT discriminate — both
              # signals predate the autocompact and persist into the orphan, permanently
              # freezing the canonical empty-diary orphan.)
              Bash: execution-diary.sh phase-start phase-4-execute --goal {goal.id}
              Bash: team-state-in-flight.sh --agent <self> --goal-id {goal.id}
              --title "{goal.title}" --phase 4   # live snapshot for partner
              Bash: aspirations-update-goal.sh status in-progress; started today.
              board-post.sh claim.
              Bash: load-execute-protocol.sh → Read; follow inline.
              IF infrastructure_failure: aspirations-release.sh;
              team-state-clear-in-flight.sh --agent <self>;
              Bash: execution-diary.sh phase-end phase-4-execute --goal {goal.id}; LOOP_CONTINUE.
              Write iteration-checkpoint.json (phase_completed=execute, last_updated=now).
              Bash: execution-diary.sh phase-end phase-4-execute --goal {goal.id}
  Phase 4.1.  SIGNAL MUTATION (anti-drift). TWO SEPARATE BLOCKS — re-evaluate
              outcome_class between them. Flipping in Block A must reach Block B.
              # RECURRING-PATH SHORTCUT (Magic Wand #1, alpha session-60):
              # When the about-to-close goal is recurring AND the LLM is using the
              # recurring-close.sh shortcut (collapses Phase 5/8/12 into one bash call),
              # SKIP this Phase 4.1 entirely. recurring-close.sh invokes
              # recurring-loop-state-mutate.sh which applies Block A/B/C/D atomically
              # under the WM lock and returns the post-flip outcome on stdout. The
              # bash-side mutation is the single writer; running this Phase 4.1 in
              # parallel double-counts streaks and corrupts cargo-cult detection.
              # NON-RECURRING goals continue to use the manual Block A/B/C/D below.
              # Block A — per-goal streak:
              IF outcome_class == routine:
                routine_streaks[goal.id] += 1
                IF routine_streaks[goal.id] ≥ 5:
                    outcome_class = deep    # FLIP — Block B sees this
                    routine_streaks[goal.id] = 0
              ELIF outcome_class == deep:
                routine_streaks[goal.id] = 0
              # Block B — session signals (re-reads outcome_class after Block A):
              IF outcome_class == routine:
                session_signals.routine_streak_global += 1
                session_signals.routine_count_total += 1
                session_signals.productive_streak = 0
              ELSE:
                session_signals.routine_streak_global = 0
                session_signals.productive_streak += 1
                # Rationale (WHY these resets): core/config/rationale/signal-mutation.md
                session_signals.consecutive_blocked_sleeps = 0
                IF goal.id != session_signals.last_failed_goal_id:
                    session_signals.consecutive_goal_failures = 0
              # Block C — global + ratio anti-drift (may flip again):
              IF routine_streak_global ≥ recurring.routine_streak_global_ceiling
                 (default 5; was 8 before 2026-05-12):
                  outcome_class=deep; routine_streak_global=0
              IF outcome_class==routine AND goals_completed ≥6
                 AND (routine_count_total / goals_completed) > 0.80: outcome_class=deep
              # Block D — count productive only AFTER all reclassification:
              IF outcome_class == deep: productive_goals_this_session += 1
  Phase 4.55. Reasoning snapshot write (pre-verify flush). Renamed from 4.5
              to disambiguate from aspirations-execute Phase 4.5 (Knowledge
              Reconciliation) — g-240-41.
  Phase 4.7.  Bash: load-iteration-close-digest.sh → IF path returned: Read it.
              (context-reads dedup; fires once per session then no-ops.)
  Phase 5.    Bash: iteration-close.sh --phase verify --goal {goal.id}
              --status {completed|blocked|skipped|...} --source {world|agent}
              --outcome {deep|routine} [--summary "..."]
              # --outcome is REQUIRED so do_verify persists outcome_class to
              # the goal record (g-248-72). Recurring-close.sh already passes
              # it; LLM-driven non-recurring verify calls MUST too — without
              # it, outcome_class never lands and portfolio analysis stays blind.
              Checkpoint: phase_completed=verify, started_at preserved.
  Phase 5.3.  Attribution — IF world+completed: aspirations-complete-by.sh
              ELIF world+non-terminal: aspirations-release.sh
              # Rationale (WHY this increment pairs with Block B reset):
              #   core/config/rationale/circuit-breaker.md
              IF verify_outcome != completed:
                  IF goal.id == session_signals.last_failed_goal_id:
                      session_signals.consecutive_goal_failures += 1
                  ELSE:
                      session_signals.last_failed_goal_id = goal.id
                      session_signals.consecutive_goal_failures = 1
  Phase 5.5.  Circuit breaker — IF session_signals.consecutive_goal_failures ≥ 3:
              defer current goal + board escalation (circuit_breaker_notify
              user notify) + session_signals.consecutive_goal_failures = 0
              (reset on escalation so next-attempt counts fresh).
  Phase 5.7.  Review gate — world+completed+code-category → board review-request.
  Phase 6.    Bash: execution-diary.sh phase-start phase-6-spark --goal {goal.id}
              Skill(aspirations-spark) — deep: full; routine: creative+hypothesis (bounded).
              Bash: execution-diary.sh phase-end phase-6-spark --goal {goal.id}
              Checkpoint: phase_completed=spark.
              # RECURRING SHORTCUT NOTE (g-115-977): recurring-close.sh wraps
              # Phase 5/8/12 but NOT Phase 6. For recurring goals closed via
              # the shortcut, follow recurring-close.sh's outcome-aware terminal
              # imperative — when stdout reports OUTCOME=deep, fire
              # Skill(aspirations-spark) BEFORE Skill(aspirations) LOOP_CONTINUE.
              # When OUTCOME=routine, spark is skipped per the standard rule.
              # The post-flip outcome on recurring-close.sh stdout is authoritative
              # (Block A/C may have flipped routine→deep — the imperative reflects
              # the FINAL classification).
  Phase 7-7.6 Completion review — aspiration_fully_complete → /aspirations-complete-review;
              recurring-only leftovers → /aspirations-complete-review functionally_complete=true.
  Phase 8.    Bash: iteration-close.sh --phase state-update …
              # state-update internally chains (in order): meta last_updated,
              # work_class/recurring lookup, wm-append goals_completed_this_session,
              # team-state recent_completions append, journal-append.sh, then
              # iteration-commit.sh on deep outcomes (g-280-03 — wraps
              # post-execution.md Step 2 commit ceremony in PROJECT_ROOT; routine
              # outcomes no-op; commit failures are fail-open and logged).
              Checkpoint: phase_completed=state_update.
  Phase 8-stop IF session-signal-exists.sh stop-requested: rm checkpoint;
               goals_completed_this_session += 1; LOOP_CONTINUE (Phase -1.4 handles stop).
  Phase 8.0.5 / 8.0.6 BASH-ENFORCED (g-248-75) — iteration-close.sh state-update
              fires tree-encoding-drift-gate.sh, which is the SINGLE WRITER
              for goals_since_last_tree_update. The gate increments the
              counter by 1 and, on threshold cross (config knob
              tree_encoding_drift_threshold, default 3), sets
              force_tree_encoding="true" sentinel + resets counter to 0.
              LLM no longer responsible. DO NOT include
              goals_since_last_tree_update in the loop_state JSON written
              at LOOP_CONTINUE — bash owns it; LLM serialization would
              clobber bash writes within the same iteration. The next
              iteration's Phase -0.5 restore picks up the bash value.
  Phase 8.1   IF asp.id new this session: aspirations_touched_this_session.add(asp.id); asp.sessions_active += 1.
  Phase 8.7   Every `tree_debt_check.interval_goals` AND debt>threshold: /tree maintain
              (--backlog when debt > threshold*3).
              # Rationale (WHY the g-115-81 backstop exists):
              #   core/config/rationale/maintenance-tick.md
  Phase 8.8   MAINTENANCE TICK:
                # "tight" = pct_to_autocompact >= 85 (see context-budget-status.py).
                # Before skipping on tight: Bash: bash core/scripts/context-budget-banner.sh
                # and quote its output. No banner → no skip. NEVER narrate "climbing"
                # or "zone fresh" without the banner line in your response.
                # TREE MAINTENANCE CADENCE (single-writer rule: _tree.yaml.maintenance.last_maintain_at
                # is the sole source of truth; rb-254, guard-155). g-246-02 wired the reader.
                Bash: tree-read.sh --maintenance
                Parse last_maintain_at + read maintenance_cadence.tree_maintain.hours_cadence
                  + tree_debt_check.debt_floor from core/config/aspirations.yaml.
                IF (last_maintain_at older than hours_cadence) OR (debt >= debt_floor):
                  # NOTE: maintenance_cadence.tree_maintain.tight_zone_skip defaults to false —
                  # tree-debt is structurally critical; runs even in tight. Check the flag
                  # in aspirations.yaml before skipping.
                  → /tree maintain (--backlog when debt >= debt_floor * 3)
                # EVOLUTION CADENCE (single-writer: last_evolution_at_time WM slot,
                # written by aspirations-evolve; rb-family single-source-of-truth).
                Bash: wm-read.sh last_evolution_at_time
                Parse timestamp + read maintenance_cadence.evolution.hours_cadence from config.
                IF (last_evolution_at_time is null OR older than hours_cadence)
                   AND (not tight — evolution.tight_zone_skip defaults true)
                   AND (evolutions_this_session < max_evolutions_per_session):
                  → /aspirations-evolve
  Phase 9.    EVOLUTION — check_cadence_triggers() + (deep-only) check_performance_triggers();
              each fired trigger → /aspirations-evolve (respect max_evolutions_per_session).
  Phase 10.   session-state-get.sh — IF not RUNNING → BREAK → session-end consolidation.
  Phase 11.   WORKING MEMORY MAINTENANCE:
                sensory_buffer>20 → overflow (≥0.40 → encoding_queue, <0.15 → discard);
                encoding_queue ≥2 → drain top 1 item to tree;
                every 5 goals → /tree maintain;
                wm-ages.sh (flag stale >30m); wm-prune.sh.
  Phase 12.   LEARNING GATE — goals_completed_this_session += 1;
          Bash: iteration-close.sh --phase learning-gate …;
          residue: meta-signal question, forced-encoding catch, unreflected /review-hypotheses,
          every 5 goals periodic reflection, every 15 productive goals /reflect --full-cycle.
        Productivity check — Bash: iteration-close.sh --phase productivity-check.
        rm -f agents/<agent>/session/iteration-checkpoint.json.

LOOP_CONTINUE:
  # g-283-04/05/06 + g-115-1561: the orchestrator's LOOP_CONTINUE makes NO
  # loop_state write. Bash gates own the counters + accumulators:
  # recurring-loop-state-mutate.py (routine_streaks, signals, *_this_session via
  # recurring-close), loop-state-bump-counters.py (goals_completed,
  # productive_goals, AND evolutions / last_evolution_at / alignment_check_at /
  # touched — g-115-1561 — via iteration-close + aspirations-select/-evolve).
  # The only residual LLM loop_state writes are slot-specific read-merge-writes
  # in the learning-gate (circuit-breaker pair) and all-blocked (idle/backoff)
  # sub-skills — see "LLM-owned fields" above. Pinned by
  # test_compact_restore_loop_state_shape.py (g-283-03),
  # test_loop_state_counter_advance.py (g-283-06 + g-115-1561).
  Skill('aspirations') with args='loop'
```

## LOOP_CONTINUE contract (non-negotiable)

1. Last action MUST be the `Skill()` tool call (not text output).
2. `loop_state` counters + accumulators are bash-gate-owned (no full-slot
   LLM mirror). The bash gates write the iteration state variables including
   `routine_streaks` and — as of g-115-1561 — `evolutions` / `last_evolution_at`
   / `alignment_check_at` / `touched`. The ONLY remaining LLM writes are the two
   narrow slot-specific read-merge-writes named in "LLM-owned fields" above
   (learning-gate circuit-breaker pair; all-blocked idle_fallback/backoff). If a
   bash-owned counter silently corrupts, that's a bash-gate bug to fix in the
   writer, not by reviving a full LLM mirror.
3. NEVER substitute inline text or a plain comment for the `Skill()` call.
4. The Skill() re-enters `/aspirations loop` which re-runs Phase -1.4
   (stop-requested), Phase -0.5 (restore), then this iteration body.
