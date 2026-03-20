# Decision Rules Convention

Tree nodes can include a `## Decision Rules` section containing actionable IF-THEN
rules extracted from accumulated knowledge. These are the behavioral rules that change
how the agent operates — not just what it knows, but what it does differently.

## Format

```markdown
## Decision Rules

- IF {observable condition} THEN {specific action} — source: {goal-id}
- IF {observable condition} THEN {specific action} — source: {goal-id}
- IF {condition} THEN {action} [promoted: guard-NNN]
```

Rules must be:
- **Concrete**: No vague "consider" or "be careful" — specific observable conditions
- **Testable**: The IF condition must be something the agent can check
- **Actionable**: The THEN action must be a specific step, not a general suggestion
- **Sourced**: Include the goal ID that produced the rule for traceability

## When to Write

Decision Rules are written during:
1. **State Update Step 8e** — after goal execution, if the outcome produced a clear behavioral rule
2. **Consolidation Step 2d.5** — during session-end encoding, if an encoding item contains a rule
3. **DISTILL operation** — preserved as part of the actionable kernel when narrative is archived

Not every goal produces Decision Rules. Only write when a clear IF-THEN emerges from
execution. Quality over quantity.

## Examples

Good rules:
- IF probing service health THEN use /health endpoint not /status — source: g-001-03
- IF accessing remote data THEN use path from env config, not hardcoded paths — source: g-002-02
- IF calling external API THEN use https:// with proper cert handling — source: g-003-01

Bad rules (don't write these):
- IF working with infrastructure THEN be careful (too vague)
- IF something seems wrong THEN investigate (not actionable)
- IF API returns error THEN handle it (obvious, no value added)

## Auto-Promotion to Guardrails

Decision Rules that have proven useful are automatically promoted to guardrails
during `/reflect --curate-memory` (Step 2.5). Promotion criteria:

1. Node has `utility_ratio >= 0.5` AND `retrieval_count >= 5` (node is actually useful)
2. Rule has existed for 2+ sessions (not a one-off)
3. Rule is not already marked `[promoted: guard-NNN]`

When promoted:
- A guardrail entry is created via `guardrails-add.sh`
- The rule in the tree node is marked: `[promoted: guard-NNN]`
- The original rule stays in the tree node for reference

## Relationship to Other Sections

| Section | Purpose | Loaded By |
|---------|---------|-----------|
| ## Decision Rules | What to DO differently | Active retrieval (--active-content) |
| ## Verified Values | Exact technical data | Active retrieval (--active-content) |
| ## Key Insights | Compressed qualitative knowledge | Full retrieval only |
| ## Key Takeaways | Evaluative summary | Full retrieval only |

Decision Rules and Verified Values are "active content" — loaded even for routine
goals via `tree-read.sh --active-content`. Key Insights and Key Takeaways are "passive
content" — loaded only during full retrieval for deep/research goals.
