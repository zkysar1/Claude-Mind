# `ranked_goals[0]` Is a HOIST, Not the Argmax

Referenced from `.claude/skills/aspirations-select/SKILL.md` Phase 2.94.
Owning goal: g-115-9424. Encoded decision this defers to: **guard-5135**.

## The claim being corrected

Three places in `aspirations-select/SKILL.md` called `ranked_goals[0]` "the
scorer's top pick". Read naturally that means *highest-scoring*, and it is not.
Every consumer of that sentence is positional, so the mis-naming is the whole
defect: the array is fine, the prose was wrong.

## Mechanism (read from source, `core/scripts/goal-selector.py`)

Three sites hoist ONE element to index 0 by the identical idiom, each stamping
its own marker key on the goal it moves:

| line | marker key | lane |
|---|---|---|
| 6236 | `drain_lane_pick` | drain lane |
| 6391 | `strategic_focus_pick` | strategic focus |
| 6552 | `reducer_only_pick` | reducer-only |

Two carry the comment "`scored` is already sorted, so this is the best one" —
the sort is present and CORRECT; the hoist deliberately displaces index 0
afterwards. All three are conditional, so a run reaching none of them reads
perfectly sorted. That conditionality is the "intermittency" the goal's title
named, not noise.

The sidecar follows the hoist: `goal-selector.py:6679` takes `top.get("goal_id")`
and `:7146` takes `scored[0]["goal_id"]` — both positional. `scorer-verdict-gate.py`
(inside `aspirations-claim.sh`) then enforces that value.

## Measurement (echo, cc-03, Linux 6.8.0-139-generic, 2026-09-14T00:0x)

Three consecutive `goal-selector.sh` runs, ~1735 candidates each:

| run | array[0] | score | marker | argmax (idx 1) | score | first inversion | total inversions |
|---|---|---|---|---|---|---|---|
| 1 | g-373-60 | 13.42 | — | g-115-9607 | 17.09 | 0 | 1 |
| 2 | g-115-817 | 16.20 | — | g-115-9607 | 16.85 | 0 | 1 |
| 3 | g-373-45 | 15.31 | `strategic_focus_pick` | g-326-899 | 17.15 | 0 | 1 |

Run 3 carries the positive control: exactly ONE element of 1736 carries any
marker, `sorted for every idx>=1` is True, and the marker names WHICH lane
hoisted. So the shape is "argmax-sorted list with one deliberately hoisted
head" — not a shuffled array and not a comparator bug.

Ties are excluded as the explanation: the index-0/index-1 gaps were 3.67, 0.65
and 1.84 points.

`top_goal_id` agreed with `array[0]` on every checked run (g-373-60/13.42,
g-115-817/16.20) and was the argmax on none of them. Emitted order and sidecar
agree with EACH OTHER; neither is the argmax. Those are separate questions and
this is the separate answer.

## How to tell a hoist from the argmax, in one draw

Read the marker keys on `array[0]`. If any of `drain_lane_pick`,
`strategic_focus_pick`, `reducer_only_pick` is set, index 0 is an intentional
hoist. An empty marker list IS a reading — it means no hoist fired and index 0
is the argmax. No sampling across iterations is required.

Do NOT re-sort the emitted array by `score` to "find the top pick" (guard-5135):
a score-sort silently returns a different goal and reads as authoritative.

Separately, `exploration_noise` makes a single draw an unreliable measure of any
goal's RANK (guard-4831) — subtract the `exploration_noise` component from
`score` for the deterministic value. Rank instability and top-pick promotion are
different mechanisms; do not conflate them.

## Why the ordering was NOT "fixed"

The hoist is policy, not a bug: three lanes exist precisely to override raw
score. Removing it would delete those lanes. guard-5135 already encodes the
decision ("emits ITS CHOSEN TOP PICK AT INDEX 0 ... NEVER re-sort"), so the
sanctioned half of g-115-9424's outcome 3 is to correct the SKILL.md claims,
which is what was done.

## Open, deliberately

Whether an agent claiming the true argmax should need a `--deviation` code at
all is a Scorer Sovereignty design call, not a measurement. guard-5817's
action_hint currently says "claim `top_goal_id` ... never `--deviation
force-override`", which over-generalises from a refusal incident into a blanket
rule that would forbid all ten sanctioned deviation codes Phase 2.94 enumerates.
That tension is unresolved here and is NOT inherited as settled.

## Moved out of the SKILL.md (hot-path budget)

Phase 2.94's fuller justification, compressed inline to pay for this
correction's bytes: *"This is a single-point computation (compare the finalized
selection to the scorer top) — NOT a variable threaded through the divergence
phases above — so a sanctioned divergence can never silently reach the claim
without a code."* The imperative survives inline; this is the reasoning behind
it. Also moved: "Never re-sort by score", which is guard-5135's own rule and is
stated in full above.
