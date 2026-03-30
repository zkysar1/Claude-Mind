# Precision Encoding Convention

Tree nodes include a `## Verified Values` section containing exact technical data extracted
during memory encoding. These are quantitative anchors that survive narrative compression —
numbers, thresholds, formulas, code references, and measurements that would otherwise be
paraphrased away when qualitative insights are compressed into prose.

## Precision Manifest Schema

Each extraction produces a precision manifest — a list of items:

```yaml
- type: "{threshold|formula|constant|reference|measurement|config_value}"
  label: "{descriptive name}"
  value: "{exact value — VERBATIM}"
  unit: "{if applicable}"
  context: "{where this applies}"
```

Types:
- **threshold** — numeric boundary values (rate limits, timeouts, capacity caps)
- **formula** — equations, algorithms, scoring formulas
- **constant** — fixed configuration values, magic numbers
- **reference** — file paths, line numbers, commit hashes, code refs
- **measurement** — latencies, sizes, counts, percentages
- **config_value** — configuration settings with their exact values

## Extraction Heuristics

Extract any of the following from execution context, experience verbatim_anchors, or
reflection observations:

- Numbers with units (latencies, sizes, counts, percentages)
- Error codes and HTTP status codes
- Thresholds, limits, quotas, timeouts
- Formulas, algorithms, scoring expressions
- File paths and line numbers
- Configuration values and their keys
- Commit hashes, version strings
- API responses and status codes

Rule: **when in doubt, INCLUDE.** An over-specified Verified Values section is far more
useful than an under-specified one. Empty manifest `[]` is only correct when the insight
is purely qualitative with genuinely no precise values.

## Output Format in Tree Nodes

```markdown
## Verified Values

- **{label}**: `{value}` {unit} — {context}
- **{label}**: `{value}` {unit} — {context}
```

Each entry is a single line. The value is wrapped in backticks to prevent rendering
ambiguity. Unit and context are optional but strongly encouraged.

## When to Extract

Precision extraction runs at five enforcement points:

1. **State Update Step 8a** — after goal execution, scan execution context for exact values
2. **Hypothesis Reflection Step 2.7** — build precision_manifest before encoding queue
3. **Consolidation Step 2b** — extract precision from encoding queue items
4. **Tree Update Step 2** — extract precision from minor insights during reflection
5. **Execution Reflection** — extract precision during refinement writes

Source data comes from: execution context, experience `verbatim_anchors`, hypothesis
`data_signals` and `actual_outcome`, encoding queue `precision_manifest` field.

## Precision Density Bonus

Encoding score adjustments for precision density:
- 3+ precision items: +0.10 to encoding_score (capped at 1.0)
- 1-2 precision items: +0.05 to encoding_score (capped at 1.0)

This incentivizes capturing exact values during reflection encoding.

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
