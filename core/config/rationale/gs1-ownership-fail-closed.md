# Rationale: GS-1 Reverts Only On Positive Evidence

Referenced from `.claude/skills/aspirations-graceful-stop/SKILL.md` Phase GS-1. Explains why
the three-arm ownership cascade became a single arm, and why that is a simplification rather
than a new restriction.

## Why one arm, not three

GS-1 reverts orphaned in-progress goals to pending when a stop finds no iteration checkpoint.
Deciding WHICH goals it may revert is destructive: reverting a goal a live sibling Body is
executing takes that Body's work away mid-flight.

The cascade took the "most specific ownership signal the goal carries", falling back twice:

| arm | test | verdict |
|---|---|---|
| 1 | `claimed_by_sid == $MIND_SID` | correct — the SID identifies the BODY |
| 2 | `claimed_by == <agent>` | **unsound** |
| 3 | `goal.id NOT in partner_claimed` (from `in_flight`) | **unsound** |

Arm 2 is unsound under the Mind/Body split because one agent runs many Bodies and they all
write the same `claimed_by`. The comparison is `alpha == alpha` for a sibling's goal, so it
does not identify ownership at all — it answers "same agent", then reverts on the answer
(guard-1460: key claims on `claimed_by_sid`, NEVER on `claimed_by`).

Arm 3 is unsound because `in_flight` holds AT MOST ONE goal per agent. As a protective filter
it shields one goal and exposes every other one the partner holds — guard-1802's
narrow-predicate class, where the blast radius GROWS the harder partners are working.

Both fallbacks exist to answer "is this mine?" when the specific signal is missing. But a
missing ownership signal is not a question to be answered by a weaker proxy; it is the absence
of evidence, and the safe response to absent evidence about a destructive act is to not act.
So the fallbacks are deleted, not repaired: `ours` requires POSITIVE evidence
(`claimed_by_sid` present, non-null, equal to our SID) and everything else skips.

This is the skill's own stated intent finally matching its code. The cascade already carried
the sentence "GS-1 reverts what it owns and leaves the rest", and deliberately left a dead
Body's claim alone because reclaiming a genuinely stranded claim needs a liveness/age
judgement that belongs to `stranded-claim-sweep`, not to a stop path. Arms 2 and 3
contradicted that sentence.

## Why the degraded projection is now safe without a separate guard

`aspirations-query.sh` returns a six-key projection by default and none of those keys is a
claim field (guard-1424), so a caller that omits `--full` gets rows where every ownership test
is vacuously false. Under the old cascade that made arms 1 and 2 false for EVERY row and
dropped the whole batch into arm 3 — reverting everything it could not identify. That is the
failure g-115-9717 was filed on.

Under positive-evidence-only the same input reverts nothing, with no extra detector to keep in
sync. The fix for the degraded case is not a new branch; it is the absence of the branches that
made it dangerous. Verified live on cc-08, 2026-09-12: the default projection through
`gs1-ownership.sh` yields `revert: []` with `degraded: true`.

`degraded` is therefore ADVISORY — it tells an operator the projection is wrong, and never
guards the outcome. It answers a batch-level question no single row can: sparse JSON omits the
key on genuinely unclaimed goals too, so key-absence on ONE row is ambiguous, but a non-empty
batch of IN-PROGRESS goals where NOT ONE row carries the key is not — an in-progress goal
reached that status by being claimed.

## The premise that had to be measured first

The goal was filed believing the ownership keys are absent from `--full` on some boxes, citing
`--goal-status pending --full` (2576 rows, keys absent) and a claim-integrity run reporting
`absent: 2710` of 2710 non-terminal goals. Measured on cc-08 2026-09-12, both censuses sampled
the wrong population:

* `--goal-status pending --full` — 2,674 rows, 21.6 MB: `claimed_by_sid` present on **5**, and
  those 5 are exactly the fleet's live claims. `status` and `id` present on 2,674 as the
  positive control that the parse works.
* `--goal-status in-progress --full` — GS-1's ACTUAL population: the key is present and
  non-null on 1 of 1.

Pending goals are unclaimed by definition and the canonical writers POP the claim triple on
release (guard-4920), so key-absence there is the HEALTHY shape, not a projection gap. The
claim-integrity reading is consistent with its own verdict, which was literally
`no-live-claims`. Across 2,691 rows (pending + in-progress + blocked) the arm-2 shape —
`claimed_by` non-null while `claimed_by_sid` is null — occurred **zero** times.

So the per-box difference the goal asked to explain is not per-box: it is per-population. The
fix still landed, because the danger was never conditional on the keys actually being absent —
arm 2 reverts a sibling's goal whenever the keys ARE present, which is the common case.

## Why this is a module and not prose

The decision was SKILL.md pseudocode, so nothing could test it and nothing could stop it
drifting — and the goal's own third outcome asks for a regression test, which needs something
executable to bind to. `core/scripts/gs1_ownership.py::decide` is pure (no I/O, no clock, no
environment), matching `reducer_self_fence.decide` and `loop_exhaustion_fence.decide`. The
wrapper `gs1-ownership.sh` exists because SKILL.md must route Python through a `.sh` wrapper
(guard-350). The wrapper writes nothing: keeping the decision and the mutation in separate
processes is what lets the decision be tested without a store to damage.

## Cross-references

- guard-1460 — key claims on `claimed_by_sid`, never `claimed_by` (arm 2's defect)
- guard-1802 — a protective predicate narrower than the population it protects (arm 3's defect)
- guard-1424 — the six-key default projection; guard-4920 — POP vs null-fill on claim clears
- guard-350 — SKILL.md routes Python through a `.sh` wrapper
- `core/scripts/tests/test_gs1_ownership.py` — branch proof; the outcome-3 regression is
  `test_stripped_projection_reverts_nothing`, mutation-proved with measured attribution
- `core/config/rationale/worker-park.md` — sibling extraction for the other stop-adjacent decision
