# Rationale: Phase 4.1 Signal Mutation

Referenced from `core/config/aspirations-loop-digest.md` Phase 4.1. The digest
holds the pseudocode; this file explains WHY the structure looks the way it does.

## Why four separate blocks (A/B/C/D)

Anti-drift defense-in-depth. Each block applies a different correction axis:

| Block | Axis | What it does |
|---|---|---|
| A | per-goal streak | Flips a single goal from routine→deep after 5 consecutive routine outcomes on that goal. Prevents the selector from locking on a coasting goal. |
| B | session signals | Updates streak counters AFTER Block A's flip, so B sees the current outcome_class, not the stale one. Separates reads from writes across the two concerns. |
| C | global + ratio | Flips again if the whole session is trending routine (≥8 global streak, or routine ratio > 80%). Catches the case where no individual goal is bad but the portfolio drifts. |
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

## Cross-references

- Reset of `consecutive_goal_failures` pairs with the counter INCREMENT in
  Phase 5.3 (see `core/config/rationale/circuit-breaker.md`). Together they
  form a single signal-lifecycle loop.
- Fix for `rb-signal-lifecycle-gate-F2`.
