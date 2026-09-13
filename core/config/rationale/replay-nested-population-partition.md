# Replay: the candidate pool and the resolved corpus are NESTED

Extracted from `.claude/skills/replay/SKILL.md` on 2026-09-12 (foxtrot,
`hostname` LAPTOP-3IOFCNEO, `uname -r` 6.18.33.2-microsoft-standard-WSL2).
It was written into that skill during g-001-05 cycle 89 and had to move: the
skill was already 65,596 B against the 65,536 B `on-demand-skills` injection
ceiling in `core/config/hot-path-budget.yaml`, so prose appended there reaches
the model truncated. The finding is unchanged; only its home is.

## The defect

⚠ **THE POOL AND THE CORPUS ARE NESTED, SO "POOL RATE vs CORPUS RATE" IS A
SUBSET-vs-SUPERSET DIFFERENCE — CONVERT IT TO A PARTITION BEFORE READING
ANYTHING OFF IT.** Every `--replay-candidates` id is a member of
`--stage resolved ∪ --stage archived` (verified in-run 2026-09-11: 807 of 807).
Differencing the two therefore compares a subset against a superset that
CONTAINS it, which attenuates the delta by the containment ratio and makes the
stratum attribution unreliable.

Measured, g-001-05 cycle 89:

| construction | delta | rc>=3's apparent share |
|---|---|---|
| pool vs union (nested — what the step used to prescribe) | **+15.81pp** | 84% |
| in-pool vs NOT-in-pool (partition) | **−55.37pp** | **34%** |

Same instant, same box. **3.5x the magnitude and the composition inverts.**

This is NOT the population-substitution class documented above it in the skill:
every reading in that six-point series named its fetch command correctly and was
still measuring an attenuated quantity, because naming two populations does not
establish that they are DISJOINT.

## The rule

**Before differencing two rates, state whether the populations are disjoint,
nested, or overlapping, and verify it in the same call
(`len(set(a) & set(b))`). If nested, difference `in-A` vs `not-in-A` within the
superset instead.**

## Expect the complement to be your own footprint

Of the 297 not-in-pool scoreable records:

- **192** were `encoded_via_chronic`. Step 3.6's predicate IS `rc>=3 AND
  CORRECTED`, so that stratum is 100% CORRECTED definitionally.
- **67** were in Step 4.5 cooldown — the fleet's violation-first batches, 64/67
  at `surprise>=5`, mean surprise 6.10 against the pool's 4.14.
- **25** were at the `rc>=5` archive cap, 0% CORRECTED.

Drop the definitional stratum before scoring anything: the residual was
**+22.74pp** (pool n=743 vs n=105) at **exceedance 0.0%**, which is the real
number.

The check that the correction is right: rule-1 enrichment quoted against the
`rc<3` bases — the stratum neither the Step 3.6 write nor its exclusion touches
— makes the two arms CONVERGE (+30.24pp pool / +29.02pp union, versus +31.84 /
+29.35 against the ALL bases).
