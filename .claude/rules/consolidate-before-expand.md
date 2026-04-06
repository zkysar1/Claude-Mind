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

## Anti-patterns

- Creating new aspirations because existing ones feel "stuck" (investigate first)
- Switching aspirations every iteration for "variety" (variety is not progress)
- Treating 1/15 goals complete the same as 14/15 (completion matters)
- Responding to plateaus with new directions instead of deeper investigation
- Letting interestingness filters reject improvement work as "too similar"

**Enforcement:** Scoring criteria `completion_pressure` and `depth_bonus` in
goal-selector.py. Consolidation gate in aspirations-precheck.
Interestingness rebalancing in aspiration-generation-strategy.
