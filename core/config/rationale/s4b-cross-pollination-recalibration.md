# Why S4b samples by category and scores utilization_score_v2

Rationale for Phase S4b of `.claude/skills/aspirations-strategic-scan/SKILL.md`.
Recalibrated 2026-08-30 (g-115-3853; alpha, hostname cc-08, uname -r
6.8.0-137-generic). Read this before changing the S4b sample or predicate.

## The old defect

S4b built its sample with `reasoning-bank-read.sh --recent 10` and fired on
`times_helpful < 2`. `times_helpful` can only accumulate through LATER
retrievals, so the sample was selected by the very variable that guarantees the
predicate passes: a low use-count measured AGE, not transferability. The signal
fired identically whether or not a transfer opportunity existed.

First measured 2026-08-14 (echo, cc-03) at 10/10. Re-measured on the live store
2026-08-30: the old predicate admits **100.0% at every window size — 10, 50,
100, 200 and 400** — so widening the sample was never a fix.

## Why the filed fix (option c) was rejected

g-115-3853 proposed three options and named (c) — score
`times_helpful / retrieval_count` above a floor — as closest to intent.
Measured, (c) does not restore discrimination:

`times_helpful` is a near-DEAD branch — **0 for 77.8% of all 9,356 active
entries corpus-wide, median 0** — so the normalized ratio still admits
**85.2%–99.7%** of the population at every floor from 1 to 50, and gets WORSE as
the floor rises (99.7% at floor 50). This is guard-3563's case reached from the
data: read which branch actually produces a quality-named metric's numerator and
check it is reachable on the live corpus.

Field census over the 9,356 active entries (non-zero share):

| field | non-zero | median | note |
|---|---|---|---|
| `utilization_score_v2` | 81.4% | 0.125 | live, bounded 0–1 — **used now** |
| `times_active` | 79.2% | 1 | live |
| `retrieval_count` | 53.2% | 1 | live |
| `times_helpful` | 22.2% | 0 | near-dead — **was scored** |
| `times_cited` | 16.6% | 0 | |
| `times_inferred_helpful` | 13.1% | 0 | |
| `times_misleading`, `times_recalled`, `times_retrieved` | — | — | vestigial, present on 1–2 records |

## Why recency could not be repaired in place

**Zero of the newest 400 entries have `retrieval_count >= 3`.** So bolting an
opportunity floor onto a recency sample converts an always-fires detector into a
NEVER-fires one (guard-1665). The SAMPLE, not the metric, was the load-bearing
half — which is why the fix changes the selector rather than only the predicate.

## The instrument now used

`utilization_score_v2` is already opportunity-normalized by construction
(`core/scripts/reasoning-bank.py`):

    v2 = (helpful + 0.5*inferred + 0.25*active + 1.0*cited)
         / (max(retrieval, helpful + inferred + active + cited) + 1)

i.e. it is option (c)'s intent built from FOUR evidence sources instead of the
one dead field. Selection is by CATEGORY, which is independent of both age and
utilization, so the selection variable is no longer the scored variable.

`MATURITY = 3` is the opportunity floor: it is what makes a low score mean "not
paying off" rather than "too new to have been used". `CEIL = 0.05` sits well
inside the live spread — 29.0% of active entries are at or below it, 71.0%
above.

## Discrimination, both branches reachable (guard-1665)

Across the six largest categories **19.5%–41.6% of mature entries fire and the
rest do not** (framework-maintenance: 125 mature, 52 fire, 73 not) — against
100% for the old predicate.

Live example the recalibrated detector surfaces in `framework`: **rb-397,
retrieval_count 159 with v2 0.0078** — retrieved constantly, credited almost
never, which IS the stated intent ("this insight keeps being retrieved but never
helps — maybe it belongs elsewhere"). The old predicate instead surfaced entries
40 minutes old.

## A caveat on `times_helpful` that outlives this fix

A worker Body structurally cannot bump it: the sanctioned credit path
(`utilization-feedback.sh`) needs a `retrieval-session.json` that only
`retrieve.sh --goal` writes, and that call returns `{"error":"no_claim"}` off
the runner-claim holder. On a multi-body fleet only the reducer can ever credit
an entry while N-1 Bodies do the reading, so the field partly measures reducer
activity rather than helpfulness. An additional, independent reason not to score
a detector on it.

## Scope

This retires only the S4b limb. g-115-3246 / g-115-4537 / g-115-4840 still own
the S4a half and the duplicate-goal pile; an S4a fire remains a CONFOUND.
