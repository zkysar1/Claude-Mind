# Goal Scoring Script Access

Goal selection scoring is implemented by `core/scripts/goal-selector.py` with exploration noise.
The script computes 10 deterministic criteria plus 1 stochastic criterion (`exploration_noise`)
scaled by the developmental stage's epsilon. The LLM never computes goal scores — the script
handles all arithmetic. The LLM reads the ranked output and applies metacognitive assessment
(Phase 2.5), which may override.

| Script | Purpose | Stdin |
|--------|---------|-------|
| `goal-selector.sh` | Score and rank unblocked goals | — |

Output: JSON array sorted by score descending. Each entry:
```json
{"goal_id": "g-001-01", "aspiration_id": "asp-001", "title": "...", "skill": "...",
 "category": "...", "score": 8.7,
 "breakdown": {"priority": 3.0, "deadline_urgency": 0, ..., "exploration_noise": 0.42},
 "raw": {"priority": 3, "deadline_urgency": 0, ..., "exploration_noise": 0.73},
 "exploration_params": {"epsilon": 0.19, "noise_scale": 3.0, "noise_weight": 0.57}}
```

All backed by `core/scripts/goal-selector.py` (Python 3, PyYAML optional).

---

# Goal Verification Schema (Unified)

Goals use a unified `verification` field that replaces the legacy `desiredEndState` +
`completion_check` pair. Both old and new formats are accepted (backward compatible).

```yaml
# NEW format (preferred for all new goals):
verification:
  outcomes:        # Human-readable success criteria (replaces desiredEndState)
    - "L2 knowledge node exists for identified domain"
    - "Node has at least 1 source article"
  checks:          # Machine-verifiable conditions (replaces completion_check)
    - type: file_check
      target: mind/knowledge/tree/_tree.yaml
      condition: "Has at least one L2 node registered"
  preconditions:   # What must be true before execution (checked in COLLECT)
    - "Root domain node exists (from g-001-00 or bootstrap)"

# LEGACY format (still accepted, auto-mapped):
desiredEndState: "At least one L2 node exists..."   # → verification.outcomes[0]
completion_check:                                    # → verification.checks[0]
  type: file_check
  target: mind/knowledge/tree/_tree.yaml
  condition: "Has at least one L2 node registered"
```

`verification.outcomes` = what success looks like (for spark checks, aspiration assessment).
`verification.checks` = how to verify it (for Phase 5 completion, Phase 0 auto-detect).
`verification.preconditions` = what must be true before execution (checked in Phase 2 COLLECT).
`verification.verification_hint` = advisory text suggesting what machine checks to consider when
creating a goal. Present on templates with `checks: []`. Read during goal creation to prompt
the agent to populate `checks` with concrete machine-verifiable conditions. Not enforced —
Phase 5 Verification Escalation handles empty-checks goals structurally.

---

# Recurring Goal Fields

Goals that re-fire on a schedule use these fields:

- `recurring`: `true`/`false` — whether the goal repeats. Set to `false` to permanently stop.
- `interval_hours`: positive number — hours between executions (e.g., 0.25, 4, 8, 24). Default: 24.
- `remind_days`: DEPRECATED — converted to `interval_hours * 24` for backward compatibility. Use `interval_hours` for new goals.
- `lastAchievedAt`: `YYYY-MM-DDTHH:MM:SS` — full ISO 8601 timestamp of last completion. Legacy `YYYY-MM-DD` format is accepted (assumes start of day).
- `achievedCount`: integer — total times completed
- `currentStreak`: integer — consecutive on-time completions. Resets to 1 when `hours_since(lastAchievedAt) > 2 * interval_hours` at completion time (missed interval). First completion always starts at 1.
- `longestStreak`: integer — best streak ever

To permanently stop a recurring goal: set `recurring: false` via `aspirations-update-goal.sh <goal-id> recurring false`.

Phase 0 Recurring Goal Checks resets completed recurring goals to `pending` after `interval_hours` elapses. Phase 7 skips "aspiration fully complete" for aspirations where ALL goals are recurring (perpetual aspirations).

---

# Deferred Goal Fields

Goals that should not execute until a specific future time use these fields:

- `deferred_until`: `YYYY-MM-DDTHH:MM:SS` or `null` — ISO 8601 timestamp. Goal is filtered out of COLLECT if `now < deferred_until`. Once the time passes, the goal competes normally. One-shot: not reset after execution.
- `defer_reason`: string or `null` — why this goal is deferred (e.g., "5-hour stress test started, check results after").

To defer a goal: `aspirations-update-goal.sh <goal-id> deferred_until "2026-03-13T20:00:00"` and optionally `aspirations-update-goal.sh <goal-id> defer_reason "Waiting for test results"`.

Compatible with all goal types including recurring. A recurring goal with `deferred_until` delays only its first execution; subsequent cycles use `interval_hours` normally.
