# Consolidate Before Expand

## Principle

Before pursuing new directions, strengthen what exists. Depth of understanding
in fewer areas produces more durable knowledge than shallow coverage of many.
An aspiration 90% complete is worth more than three aspirations 10% complete.

## Rules

1. **Completion has gravity**: Goals in near-complete aspirations score higher
   than goals in new aspirations. The closer to done, the stronger the pull.
2. **Depth before breadth**: Continuing within the same aspiration is the
   default. Switching requires justification (current aspiration is blocked,
   or new work is genuinely more urgent).
3. **New aspirations require health check**: Before creating a new aspiration,
   assess existing aspiration completion rates. If average completion is below
   25%, explain why new work is warranted.
4. **Plateaus mean dig deeper, not pivot**: When learning velocity drops,
   first investigate root causes, then try a different approach within the
   same domain — only pivot to new directions as a last resort.
5. **Improvement is not redundancy**: "Improve X" is not "too similar to X."
   Deepening, hardening, and quality-improving existing work must not be
   penalized by interestingness or novelty filters.
6. **Tail pulls harder than aspiration alone**: As an aspiration nears
   completion, the final remaining goals each carry extra pull — not just
   the aspiration as a whole. Frontier work (data-pending, infra-blocked,
   hypothesis-gated) tends to cluster in the tail. Boosting the tail forces
   it to surface for execution or conversion to an Unblock goal, instead
   of silently coasting at high completion %.
7. **Zombies violate consolidation**: Aspirations with completion_ratio
   ≥0.8 where only blocked-and-stale goals remain must be evaluated for
   intent satisfaction (aspirations-complete-review Phase 7.4) before
   new aspirations are created. Zombie aspirations distort
   `completion_pressure` and `tail_bonus` scoring and hide learning signal —
   they must close (via intent satisfaction) or be unblocked, not tolerated.

## Anti-patterns

- Creating new aspirations because existing ones feel "stuck" (investigate first)
- Switching aspirations every iteration for "variety" (variety is not progress)
- Treating 1/15 goals complete the same as 14/15 (completion matters)
- Responding to plateaus with new directions instead of deeper investigation
- Letting interestingness filters reject improvement work as "too similar"

**Enforcement:** Scoring criteria `completion_pressure`, `tail_bonus`, and
`depth_bonus` in goal-selector.py. Consolidation gate in aspirations-precheck.
Interestingness rebalancing in aspiration-generation-strategy.
Zombie scan in aspirations-precheck Phase 0.5.0a routes matching aspirations to
aspirations-complete-review Phase 7.4 (intent-satisfaction pre-gate), which closes
them via `aspirations-complete-intent.sh` when evidence gates pass.
