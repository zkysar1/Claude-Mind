# Goal Selection Algorithm

**Implemented by**: `core/scripts/goal-selector.py` (invoked via `goal-selector.sh`).
The script handles all arithmetic scoring including exploration noise.
The LLM reads ranked output and applies Phase 2.5 metacognitive assessment.

## Scoring Formula

```
1. FILTER: active aspirations where status == "active" and cooldown elapsed

2. COLLECT: unblocked goals where:
   - status == "pending"
   - all blocked_by have status "completed" or "decomposed"
   - if recurring: hours_since(lastAchievedAt) >= interval_hours
   - if hypothesis_id: now >= resolves_no_earlier_than
   - if deferred_until set: now >= deferred_until
   - if not agent-eligible by participants: skip (user-only OR other-agent goals)

3. SCORE (26 deterministic + 1 stochastic):
   # Canonical criteria list = the `weights:` keys in meta/goal-selection-strategy.yaml
   # (SSOT, loaded by goal-selector.py load_weights()). Keep this enumeration in
   # sync with that file when criteria are added/removed (last resync: g-115-2538).
   priority_score:       HIGH=3, MEDIUM=2, LOW=1            (weight: 1.0)
   deadline_urgency:     +3/+2/+1 for 1/3/7 day deadlines   (weight: 1.0)
   agent_executable:     +2 if current agent eligible         (weight: 0.8)
   variety_bonus:        +1.5 if different aspiration         (weight: 0.5)
   streak_momentum:      +0.5 if same aspiration this session (weight: 0.5)
   novelty_bonus:        +1.0 if achievedCount == 0           (weight: 0.6)
   recurring_urgency:    base + log2(1+overdue_ratio)*scale    (weight: 0.8, config: recurring.*)
   recurring_saturation: -(ratio * max_penalty) penalty        (weight: 0.8, config: recurring.*)
   recurring_debt_bonus: +bonus to non-recurring during debt   (post-scoring, config: recurring.*)
   reward_history:       aspiration success rate               (weight: 0.5)
   completion_pressure:  (completion_ratio² * 2.5) quadratic  (weight: 0.8)
   tail_bonus:           (ratio-0.70)/remaining*3.0 when ≥70% (weight: 0.8)
   depth_bonus:          +1.0 if same aspiration as last       (weight: 0.6)
   evidence_backing:     resolved hypothesis support           (weight: 0.7)
   deferred_readiness:   +1.5 if deferred and now due          (weight: 0.6)
   context_coherence:    +2.0 if same category (non-tight)     (weight: 1.0)
   skill_affinity:       quality-weighted skill preference      (weight: 0.4)
   directive_boost:      cross-agent priority from directives   (weight: 1.5)
   opportunity_boost:    1.0 discovery_type=opportunity, 0.5 idea-class (weight: 0.5; g-115-2525)
   critical_blocker_surface: surface a high-downstream-unlock bottleneck goal (weight: 1.2; g-305-07)
   user_signal_boost:    user-signal detection (e.g. silent_48h) boost        (weight: 1.2)
   handoff_bonus:        cross-agent handoff routing (raw value IS the bonus)  (weight: 1.0)
   role_affinity:        per-agent work_class preference                       (weight: 1.0)
   class_balance_bonus:  pull under-represented work_class up                  (weight: 0.8)
   per_goal_saturation:  penalty when the SAME goal_id fires rapidly           (weight: 0.8)
   cross_aspiration_support: support for goals that aid other aspirations      (weight: 0.5)
   co_invest_alignment:  pair-iteration co-investment bias (disabled by default) (weight: 0.0; g-115-563)
   exploration_noise:    random(0,1) * epsilon * noise_scale    (weight: varies)

   TOTAL = sum(score * weight) + exploration_noise

4. SELECT: highest total score
   Tiebreak: lower aspiration number, then lower goal number
```

## Reading ONE field from the selector — use `--field`, never a pipe (g-115-8362)

`goal-selector.sh` carries a silent-empty guard: `goal-selector.py` exiting 0 with
zero bytes is a failure to PRODUCE a result (an empty ranking prints `[]`, never
`""`), so the wrapper emits a FATAL on stderr and exits **7**.

That guard was unreachable through both call shapes anyone actually used, because
`select` and `blocked` register **zero** options — so a caller wanting one field had
to hand-roll a parser, and every hand-rolled parser pipes:

| shape | what happens to the exit-7 FATAL |
|---|---|
| `2>&1 \| python3 -c 'json.load(sys.stdin)'` | FATAL merges into the JSON stream → `json.load` raises → caller's except-branch yields EMPTY, pipeline rc 0 |
| `2>/dev/null \| python3 -c 'json.load(sys.stdin)'` | FATAL discarded → `json.load` raises on empty stdin → same EMPTY, same rc 0 |

Both render as "no candidates" — precisely the misreading the guard exists to
prevent, on the MANDATORY selection path. Measured 103x across 8 live sessions.

**Use the flag instead.** It reads the already-captured temp FILE, so no pipe exists
and rc survives:

```bash
bash core/scripts/goal-selector.sh select --field goal_id
```

| rc | meaning |
|---|---|
| 0 | the value, on stdout |
| 7 | silent-empty signature — **RE-RUN the selector**; never read this as "no candidates" |
| 8 | a real, measured ZERO candidates |
| 9 | that field is absent from the payload (stderr names the available keys) |

Handles both legitimate top-level shapes: the ranked LIST (`goal-selector.py:6197`,
rows are FLAT — `goal_id`/`score`/`title` are top-level per row, there is no nested
`goal` object) and the all-blocked DICT (`:6011`).

Parsing the FULL array is still correct when you need the whole ranking — capture
stdout and rc **separately**, never through a pipe, as `iteration-open.py:_selection`
and `dependency-timeout-check.py:_read_blocked` already do. Those two were never
vulnerable, which is why the remedy was a new flag rather than a stdout-contract
change that would have altered behaviour for the safe callers.

Do not discover this flag by running `--help`: it passes through to `goal-selector.py`,
which knows nothing about it. Wrapper flags live in the wrapper's own argument-parsing
case block (guard-2172), and probing `core/scripts` wrappers with `--help` is itself
ruled out (guard-2350, guard-3533).
