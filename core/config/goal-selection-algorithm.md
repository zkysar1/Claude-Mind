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

3. SCORE (multi-criteria weighted):
   priority_score:     HIGH=3, MEDIUM=2, LOW=1          (weight: 1.0)
   deadline_urgency:   +3/+2/+1 for 1/3/7 day deadlines (weight: 1.0)
   agent_executable:   +2 if current agent eligible       (weight: 0.8)
   variety_bonus:      +1.5 if different aspiration       (weight: 0.7)
   streak_momentum:    +0.5 if same aspiration this session (weight: 0.5)
   novelty_bonus:      +1.0 if achievedCount == 0         (weight: 0.6)
   recurring_urgency:  1.5 base + overdue ratio, cap 5.0  (weight: 0.8)
   recurring_saturation: -(ratio * 4.0) penalty           (weight: 0.8)
   reward_history:     aspiration success rate             (weight: 0.5)
   evidence_backing:   resolved hypothesis support         (weight: 0.7)
   deferred_readiness: +1.5 if deferred and now due        (weight: 0.6)
   context_coherence:  +2.0 if same category (non-tight)   (weight: 1.0)
   skill_affinity:     quality-weighted skill preference    (weight: 0.4)
   exploration_noise:  random(0,1) * epsilon * noise_scale  (weight: varies)

   TOTAL = sum(score * weight) + exploration_noise

4. SELECT: highest total score
   Tiebreak: lower aspiration number, then lower goal number
```
