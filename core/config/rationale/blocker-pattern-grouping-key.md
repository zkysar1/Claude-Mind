# Rationale: Blocker-Pattern Grouping Key (B6.7 Target 1)

Referenced from `.claude/skills/aspirations-all-blocked/SKILL.md` Step B6.7
Target 1. Explains why blocked goals are grouped by the CLASS PREFIX of
`blocker_ref.external_id` (the text before the first colon) and not by the
full id, by `blocker_ref.type`, or by `by_reason`.

## Why the class prefix, not the full external_id

`external_id` is near-unique per goal, so grouping by the full string gives
`affected_count ≈ 1` for every group and the `>= 2` gate below it is
unreachable BY CONSTRUCTION — the PRIMARY synthesis target could never fire
(g-115-4923, filed by omni from ZDS-Mind and reproduced on the frontier).
omni measured 76 ids / 76 distinct / max-sharing 1. Measured 2026-08-04
(bravo, hostname cc-05, uname -r 6.8.0-136-generic) on a live 169-goal
blocked queue: 159 distinct of 169, max-sharing 3 — so a rare group CAN reach
2, which made the defect intermittent rather than total and is exactly why it
survived. Same vacuous-gate class as the `covered_patterns` bug documented
beside it: a guard on a loop body that never runs.

The prefix IS the blocker class. All 169 external_ids carried a colon; the
prefixes were `hypothesis-gate` 99, `dependency` 25, `structured-defer` 19,
`precondition` 9, `time-gate` 8, `not-my-lane` 5, `narrative-defer` 1,
`explicit-status` 1, plus 2 colon-less create-blocker ids. Ten groups, SIX at
>= 2 — the signal the step wants is present and abundant; only the key was
wrong.

## Why not blocker_ref.type

It reads like the schema-backed choice and is DEGENERATE on the same queue:
167 of 169 are `resource`, giving 3 groups. It collapses every distinct
blocker class into one bucket and would synthesize one meaningless
aspiration. (This was a hypothesis, falsified by measuring.)

## Why not by_reason

`by_reason` — returned by the same `goal-selector.sh blocked` call — is valid
but STRICTLY COARSER: 6 groups, because it collapses `structured-defer`,
`time-gate` and `narrative-defer` into one `deferred` bucket. Those want
different Unblock aspirations, so prefer the prefix. A cross-tab confirms the
prefix refines `by_reason` rather than cutting across it.

## Judge the pattern before synthesizing

The step was inert for months, so its output had never been reviewed in
practice. On the queue above the top three candidates were `hypothesis-gate`
(99), `dependency` (25) and `structured-defer` (19), and the per-iteration cap
of 3 means those are exactly what a first live firing would create. A
hypothesis gate is a TIME LOCK that resolves on its own schedule, so
"Unblock: hypothesis-gate (99 goals waiting)" is noise, not work; `time-gate`
and `not-my-lane` are likewise self-resolving or routing artifacts. This was
"the operator's judgment call at synthesis time" until g-115-4966 landed the
hard exclusion list as `core/config/aspirations.yaml` →
`idle_supply.blocker_pattern_exclusions` (2026-09-02).

## Measure against the population the code reads

Measure both keys against `goal-selector.sh blocked`, NOT against
`aspirations-query.sh --goal-status blocked` — the two populations differed by
30x (169 vs 5), because `status: blocked` is a narrow field while this step's
notion of blocked includes deferred, hypothesis-gated and dependency-blocked
goals. Measuring the narrow one first produced a confident, wrong reading of
this very defect ("2 ids, no colons, the prefix fix is inert here") before
re-measuring on the population the code actually reads. guard-1802 class: a
probe predicate narrower than the production path's.

## Cross-references

- g-115-4923 — the vacuous full-id grouping defect
- g-115-4966 — the exclusion list (now `idle_supply.blocker_pattern_exclusions`)
- guard-1802 — probe predicate narrower than the production path
- `core/scripts/gates/aspiration_supply.py` — the synthesized `Unblock:`
  aspiration carries `blocker_pattern:<class>` and must cite the blocked goal
  ids as `supply_evidence.checked`
