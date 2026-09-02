# Judge Canary Set — skill-quality scoring (pilot v1)

Measured 2026-09-01 (g-306-403, asp-306). Method from arXiv:2608.23875 §7.2:
a judge that never fails is indistinguishable from a judge that never looks.
Seed items with objectively-planted flaws, run them through the judge's real
path, and measure how many it lets through.

**Scope: this is a PILOT on one surface, not a canary framework.** One surface,
six canaries, one miss-rate number.

## How to re-run

All six run through the canonical companion script in its **read-only** mode
(`cmd_derive` — "Print auto-derived grades without writing"), so re-running
pollutes no store and needs no quarantine:

```bash
bash core/scripts/skill-quality-score.sh derive --skill <skill> --goal <id> \
  --outcomes-met N --outcomes-total N --episode-chain-count N --guardrail-violations N
```

## The canaries and what they measured

| # | Planted flaw (objectively demonstrable) | Dimension | Expected | Measured | Verdict |
|---|---|---|---|---|---|
| C0 | none — clean control | all | all good | all good | baseline |
| C1 | **zero verification outcomes declared** (nothing was verified) | completeness | not top grade | `good` | **MISS** |
| C2 | 0 of 4 outcomes met | completeness | poor | `poor` | HIT |
| C3 | 5 guardrail violations | safety | poor | `poor` | HIT |
| C4 | skill name that has never existed | (canonicalization) | rejected | `400 invalid_skill` | HIT |
| C5 | 5 episode chains (heavy retry) | executability | poor | `poor` | HIT |
| C6 | any real skill, any execution | maintainability | should vary | `good`, always | **MISS** |

**Measured miss rate: 2 of 6 (33%).** Both misses are in the same direction —
toward the top grade — and neither is a threshold that could be tuned.

### C1 — an unverified execution scores identically to a perfect one

`grade_from_ratio` returns `"good"` when `total <= 0` (`# No verification
outcomes defined → pass-through`). C1 and C0 produce **byte-identical** output:

```
{"safety":"good","completeness":"good","executability":"good","maintainability":"good"}
```

So "all four outcomes met" and "no outcomes were ever declared" are the same
record. This is the `null-fused-into-threshold-verdict` class: absence of
evidence is scored as evidence of quality. An honest judge has three states
(good / poor / *unknown*) and this one has two.

### C6 — maintainability is a constant, not a measurement

`derive_maintainability` defaults base skills to `"good"` without consulting
anything, and looks forged skills up in the registry — but **0 of 82 registry
entries carry a `quality_at_forge` key** (measured; positive-controlled against
`forged_date`, present 81 times in the same file). It additionally fails open to
`"good"` on six distinct paths: no yaml module, registry absent, load exception,
no recognised top-level key, skill not present, entry not a dict.

Net: every one of the 133 canonical skills derives `maintainability: good`, on
every execution, forever. The dimension carries 0.15 of the aggregate weight and
contributes zero information to it. C4 shows the fail-open is not even reachable
by an unknown name — canonicalization rejects first — so the six fallbacks
defend a case that cannot occur while the live case is unmeasured.

## The premise correction this pilot produced

The goal treats skill-quality as an LLM-judged surface. It is mostly not:
**four of the five dimensions are deterministic derivation from integer
arguments**, and only `cost_awareness` is LLM judgment. That inverts the
finding's meaning — these are not judge misses to be fixed with better
prompting, they are threshold bugs with exact line numbers.

And the one genuinely LLM-judged dimension **cannot be canaried by this
method at all**: `cost_awareness` is *supplied as an argument* by the same
agent that would plant the flaw, so a self-administered canary on it is not
blind and measures nothing. A blind judge is required, and that is a
different piece of work.

## Rotation note — why static canaries decay

These six are now written down, which is what makes them re-runnable and also
what starts them decaying:

1. **Selection with memory.** Once a canary is in the repo, later changes are
   made by agents who have read it. The set stops sampling "flaws the judge
   misses" and starts sampling "flaws we already knew about" — it measures
   fixes to itself, not judge health.
2. **A fixed set cannot detect a narrowed judge.** Patching exactly these two
   misses drives the measured rate to 0% while every unprobed shape stays
   unmeasured. 0% here means "these six pass", never "the judge is sound".
3. **The misses are structural, so they will not stay novel.** C1 and C6 are
   one-line threshold facts; once fixed they are permanent HITs and carry no
   further signal, leaving four controls and no probes.

So treat the 33% as a **dated reading, not a metric to optimise**. A later pass
should generate canaries the current set does not contain — and should weight
the *dimension-level* result (2 of 4 derived dimensions structurally blind)
above the per-canary rate, since the per-canary rate moves with how many
controls you happen to include.

## Cross-references

- `core/config/conventions/skill-quality.md` — the five dimensions, weights,
  and the g-306-394/g-306-400 judge-provenance fields this sits beside
- `core/scripts/skill-quality-score.py` — `grade_from_ratio` (C1),
  `derive_maintainability` (C6)
- `.claude/rules/verify-before-assuming.md` — C1 is a null scored as a pass
