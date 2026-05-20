# Rationale: Phase 5.3-5.5 Circuit Breaker

Referenced from `core/config/aspirations-loop-digest.md` Phases 5.3 and 5.5.
The digest holds the pseudocode; this file explains WHY the counter increment
exists where it does.

## Why Phase 5.3 increments consecutive_goal_failures on non-completed verify

```
IF verify_outcome != completed:
    IF goal.id == session_signals.last_failed_goal_id:
        session_signals.consecutive_goal_failures += 1
    ELSE:
        session_signals.last_failed_goal_id = goal.id
        session_signals.consecutive_goal_failures = 1
```

The Phase 5.5 circuit breaker (`IF consecutive_goal_failures ≥ 3: defer + escalate`)
would be vaporware without this increment — it was checking a counter that
nothing in the loop updated. The fix pairs this INCREMENT with the RESET in
Phase 4.1 Block B (on productive completion), closing the lifecycle.

## Why "same goal repeated" vs. "new goal" branching

The counter is supposed to catch a goal stuck in a verify-fail loop. If a
DIFFERENT goal fails right after another one, that's not "this goal failing
three times" — it's two independent failures. The branch resets the count to
1 and tracks the new goal_id, so the circuit breaker only fires on genuine
same-goal repetition.

## Why the reset fires on escalation (not just on success)

Phase 5.5: `session_signals.consecutive_goal_failures = 0` after escalating.
Rationale: once the escalation is posted + user notified, the next attempt
should count fresh. Otherwise the breaker would re-trip on the 4th failure
without giving the user-assisted attempt a clean slate.

## Cross-reference

- Fix for `rb-signal-lifecycle-gate-F2`.
- Paired reset in `core/config/rationale/signal-mutation.md` (Phase 4.1 Block B).
