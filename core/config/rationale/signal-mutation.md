# Rationale: Phase 4.1 Signal Mutation

Referenced from `core/config/aspirations-loop-digest.md` Phase 4.1. The digest
holds the pseudocode; this file explains WHY the structure looks the way it does.

## Why four separate blocks (A/B/C/D)

Anti-drift defense-in-depth. Each block applies a different correction axis:

| Block | Axis | What it does |
|---|---|---|
| A | per-goal streak | Flips a single goal from routine→deep after 5 consecutive routine outcomes on that goal. Prevents the selector from locking on a coasting goal. |
| B | session signals | Updates streak counters AFTER Block A's flip, so B sees the current outcome_class, not the stale one. Separates reads from writes across the two concerns. |
| C | global + ratio | Flips again if the whole session is trending routine (global streak ≥ `routine_streak_global_ceiling`, default 5, or routine ratio > 80%). Catches the case where no individual goal is bad but the portfolio drifts. |
| D | deep-only counting | `productive_goals_this_session` increments ONLY at the end, after all reclassification. Counting earlier would double-count any goal that Block A or C flipped. |

## Why the session_signals resets in Block B

```
ELSE:  # productive completion
  session_signals.consecutive_blocked_sleeps = 0
  IF goal.id != session_signals.last_failed_goal_id:
      session_signals.consecutive_goal_failures = 0
```

Any productive completion means the prior blocked / failure streaks were
resolved (the agent got un-stuck). Without these resets, the backoff schedule
in `aspirations-all-blocked` / B7 would inherit stale counts across unrelated
blocked episodes — an hour of good work would still cap out at long backoff
sleeps the next time anything blocked, because the counter was never cleared.

## Non-recurring path: counters without the flip (g-115-1785)

Signal mutation is bash-owned on BOTH paths, but by DIFFERENT writers at
DIFFERENT points in the iteration, and that asymmetry is deliberate:

| Path | Writer | When | Outcome flip? |
|---|---|---|---|
| recurring | `recurring-loop-state-mutate.py` (via `recurring-close.sh`) | BEFORE the four iteration-close phases | YES — echoed on stdout, so Block A/C routine→deep reaches verify/spark |
| non-recurring | `loop-state-bump-counters.py --recurring false` (via `iteration-close.sh` state-update) | Phase 8 (state-update), AFTER verify/spark | NO — counters + ceiling-RESET only |

The recurring writer runs FIRST and echoes the post-flip outcome precisely so a
coasting recurring goal that trips Block A (5 routines) or Block C (global
ceiling / >80% ratio) is reclassified deep and fires a spark THIS iteration —
the anti-drift correction lands where it matters.

The non-recurring writer runs inside state-update, which is Phase 8 — AFTER
Phase 5 verify and Phase 6 spark have already run. A routine→deep flip computed
here could not retroactively fire the spark that already didn't fire, so
propagating it would buy nothing while introducing a verify-vs-counter
inconsistency (verify persisted `outcome_class=routine` on the goal record;
bash would count the goal productive). Two facts make the omission not just
acceptable but correct:

1. **Non-recurring goals are classified `deep` by default.** Per
   `execute-protocol-digest.md` Outcome Classification, `routine` is reserved for
   recurring-and-succeeded-with-no-new-info; a non-recurring goal is `deep`
   unless it fails. So the routine→deep FLIP essentially never has an input on
   this path — there is nothing to flip. The routine branch of the non-recurring
   Block A/B is defensive (handles the rare hand-classified-routine non-recurring
   goal) but inert in practice.

2. **The primary bug was a missing RESET, not a missing flip.** The observed
   drift (g-115-1785: `routine_streak_global` "stayed 4 after a deep goal, should
   be 0") is a non-recurring DEEP close failing to reset the global streak —
   because no non-recurring bash writer existed and the LLM's Phase 4.1 manual
   patch was discarded at LOOP_CONTINUE (the contract forbids the loop_state
   mirror). Block B's deep branch (`routine_streak_global = 0; productive_streak
   += 1`) is exactly the reset that was missing. The Block C ceiling-RESET is
   kept as the anti-runaway backstop; the ceiling FLIP-to-deep is dropped
   (nothing to flip). The Phase 0-pre.0b boredom surface still warns at
   `global >= 4` before the NEXT selection, so the "force a deeper reasoning
   pass" signal is preserved for the human-in-the-loop LLM even without a
   retroactive flip.

Gating: `iteration-close.sh` passes `--recurring false` ONLY for a confirmed
non-recurring goal (the aspiration-lookup `recurring` field == "false"). An
unknown/failed lookup omits the flag → the streak block is skipped (fail-safe:
the counter simply doesn't advance that once, exactly as before g-115-1785 — no
corruption). Recurring goals ("true") always omit the flag so the recurring
writer stays the sole owner; a double-apply would corrupt cargo-cult detection.

## Cross-references

- Reset of `consecutive_goal_failures` pairs with the counter INCREMENT in
  Phase 5.3 (see `core/config/rationale/circuit-breaker.md`). Together they
  form a single signal-lifecycle loop.
- Fix for `rb-signal-lifecycle-gate-F2`.
- g-115-1785 — non-recurring streak ownership (`loop-state-bump-counters.py
  --recurring false`); closed the last recurring/non-recurring split-brain in
  signal mutation.
