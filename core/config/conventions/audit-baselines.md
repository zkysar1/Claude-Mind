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

## Seeding

**Measure the seed with the EXACT predicate that ships — never an exploratory
one.** While developing a ratchet you will run several throwaway greps to size
the problem. The number one of those produced is not the seed. Run the shipped
check, read the number IT reports, and seed that.

A seed measured by a different predicate is an answer to a question nobody will
ask again. The lucky failure is what happened in g-115-3560: seeded at 12 from
an exploratory regex, shipped a slightly broader one, and the check landed
**FAIL at 13/12** on its first run — caught immediately because the discrepancy
was loud. The unlucky failure is a seed measured by a *narrower* predicate,
which lands GREEN and silently encodes the wrong population as "no drift".

Seed **after** any repairs the same goal makes, not before, or the baseline
memorialises drift you already fixed. Then re-run the shipped check once and
confirm it reports `STABLE:` against the value you just wrote — a seed you have
not read back is a claim, not a measurement.

Same root as `guard-920` (a regression test must replicate the literal shape its
production call site passes, not the contract-ideal shape) and `rb-245` (verify
the population exists before believing a zero) — measure the real thing, not the
convenient stand-in.

**And check the PREDICATE before you check the number.** Everything above assumes
the shipped check is correct and only the seed is in question. A goal that
commissions a ratchet usually names its check in prose, and that phrasing has
typically never been executed — it was written to describe the defect, not to
bound a population. Measured g-115-9541: the goal prescribed "grep for
BASH_SOURCE or `rev-parse --show-toplevel` under the world scripts dir", which
matches **174 files** on the live corpus, because nearly every script
legitimately uses BASH_SOURCE to locate ITSELF. Seeding that faithfully would
have shipped a permanently-red check (`guard-329`, `guard-574`) whose first
honest reading is "everything is broken". The narrowed four-condition predicate
that shipped reports 8. One `--list` run against the real corpus separates a
wrong number from a wrong question (`guard-5994`).

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

## Merge across boxes

The file is merge-protected: when two boxes' copies diverge,
`coordination_merge.merge_audit_baselines` merges them per metric key. That handler is
the source of truth; each row below was confirmed by running it on synthetic entries.

| Field | Merge rule |
|---|---|
| `baseline` | MIN of the two sides. A merge never grows it. |
| `history` | Content-union of the two lists; identical rows collapse. |
| `last_recorded` | The later of the two. |
| `last_verdict` | The verdict of the side that recorded last. |
| Every other key (`matcher`, `unit`, anything new) | Taken WHOLE from the side whose canonical JSON sorts higher; a key on one side only is kept. There is no per-key rule. |

The last row is the trap for a new field. `baseline` sorts first in that canonical JSON,
so the winning side is typically the one with the HIGHER baseline, and a value stored
beside the scalar can come from a different reading than the MIN baseline next to it.
Measured: with baselines 444 and 446 the merged baseline was 444 and the merged extra
key was the 446 side's. A field that must survive a merge needs its own rule in the
handler first, and that rule must reach every box before any box writes the field.

## Localising a regression

A ratchet records a count, so a regression arrives as "+N" with nothing naming the sites.
Do not persist the members (see above). When the counted corpus is tracked in git,
rebuild the old corpus from its revision, run the CURRENT audit over both, and diff the
audit's own records. For the unchecked-write ratchet,
`bash core/scripts/unchecked-write-audit.sh --new-since baseline` names the sites that
joined (and left) the unverified set since the commit at the recorded baseline reading,
and the ratchet's REGRESSED line prints that command. `--new-since <rev>` and
`--until <rev>` name the revisions explicitly. The command reads this file and writes
nothing. Why this design: `core/config/rationale/unchecked-write-delta-localisation.md`.

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
- Seeding from an exploratory measurement instead of the shipped predicate's own
  output (see § Seeding — a narrower stand-in seeds GREEN and hides the drift)
- Seeding a predicate the commissioning goal NAMED but nobody RAN — that is a
  wrong question, not a wrong number, and it ships permanently red (`guard-5994`)
- Letting the baseline grow on regression (defeats the ratchet)
- Storing a member set, or any new key, beside `baseline`: the merge takes every key it
  has no rule for whole from one side, so it can disagree with the MIN baseline it sits
  next to (see § Merge across boxes)
- Keeping unbounded history (current cap: 50 entries, enforced by writer)
- Using this file as a dashboard replacement (it's a guard, not a feed)
