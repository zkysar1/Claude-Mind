# Audit Baselines (`meta/audit-baselines.yaml`)

An **advisory ratchet** for drift metrics that can be measured but shouldn't
hard-gate everyday work. Each baseline records a single drift count that is
allowed to shrink but never grow.

## When to use

Create a new baseline entry ONLY when all three hold:

1. The metric is a **non-negative integer count of drift items** (dangling
   references, schema violations, stale docs — things that monotonically
   improve as they're fixed).
2. There is a **canonical audit script** that computes the current count
   deterministically in bounded time.
3. Hard-gating would be premature (historical drift exists; fixing it is
   someone's future work, not a release-blocker).

If the metric is a ratio, latency, or anything continuous — use a different
mechanism (gates, thresholds, alerts). Not this file.

## Schema

```yaml
<metric_key>:                    # unique, kebab-case (e.g., learning_routing_drift)
  baseline: <int>                # lowest count ever recorded
  last_recorded: <ISO timestamp> # local system time, %Y-%m-%dT%H:%M:%S
  last_verdict: seeded | stable | ratcheted | regressed
  history:                       # bounded — last 50 entries
    - recorded_at: <ISO>
      drift_total: <int>
      verdict: <string>
      breakdown: {<component>: <int>, ...}  # optional, domain-specific
```

Multiple metric keys coexist in one file. Writers append to `history` and
rewrite `baseline` / `last_recorded` / `last_verdict` atomically
(`.yaml.tmp → rename`).

## Verdicts

- `seeded` — first run; baseline = current count. Future runs compare against it.
- `stable` — current == baseline. No change.
- `ratcheted` — current < baseline. Baseline shrinks to current (one-way).
- `regressed` — current > baseline. Baseline **does not grow**. Surfaces as a warning.

## Integration with /verify-learning

Each baseline gets one check line in `.claude/skills/verify-learning/SKILL.md`:

```
Check: <metric> stable or ratcheted down. Bash: `bash core/scripts/<name>-ratchet.sh`
→ expect exit 0 and a status line starting with `STABLE:` or `RATCHETED:`.
A `REGRESSED:` line means new drift was introduced since the last baseline.
```

Default exit-0-always keeps verify-learning runs unblocked. Opt-in hard-gating
via `VERIFY_LEARNING_DRIFT_HARD_GATE=1` in the env if a specific metric has
matured enough to be load-bearing.

## Reference implementation

`core/scripts/learning-routing-ratchet.{py,sh}` — the first baseline, tracking
cross-reference drift across reasoning-bank, guardrails, pipeline, experience,
pattern-signatures, and the knowledge tree. Baseline seeded 2026-04-23 at 0.

## Anti-patterns

- Baselining a ratio or continuous metric (wrong tool — use a gate)
- Letting the baseline grow on regression (defeats the ratchet)
- Keeping unbounded history (current cap: 50 entries, enforced by writer)
- Using this file as a dashboard replacement (it's a guard, not a feed)
