# Rationale: Why Verification Splits Into a Per-Unit Half and a Reducer Residue

Referenced from `.claude/skills/worker-loop/SKILL.md` § "The phase split" and
from `WORKER_PHASES` / `LIFECYCLE_DISPOSITIONS` in `core/scripts/worker_execute.py`.
Explains why `verify` — a phase that was wholly reducer-only from Phase 2A until
2026-09-03 — now has a worker-side half called `verify-own-unit`, why that half
is safe under the convergence invariant that forbids N reducers, and why the
judgement and the mechanical status write are one step in two calls rather than
one call doing both.

## Why a worker may verify at all, when it may not encode

The convergence invariant is about **shared knowledge**, not about all writes. A
worker is forbidden from encoding, reflecting, consolidating and updating state
because each of those folds this Body's experience into the stores every other
Body reads — run per-worker, they produce N reducers, which is the exact defect
`asp-306` exists to prevent.

Verification is not that shape. Deciding whether the ONE goal this Body just
executed met its criteria writes **per-goal state** — that goal's hypothesis
outcome, its Q1/Q2/Q3 escalation, the `blocked_by` clears on goals that named
it. Those writes are keyed to a unit that exactly one Body executed, so two
Bodies verifying their own units never contend and never merge. The reducer was
doing this work only because the phase split predated the question, and the cost
was real: the Body holding the evidence in context handed the judgement to a
Body that had to reconstruct it later from the store.

The split therefore follows the ownership of the evidence, not the seniority of
the Body.

## Why the reducer keeps a residue rather than the whole phase

Three things in the old `verify` phase are genuinely cross-Body and stay
reducer-side:

| Residue | Why it cannot move to a worker |
|---|---|
| The reducer's own units | It executes goals too; nothing changes for those |
| Cross-Body streak tracking | A streak is a property of the fleet, not of one unit |
| A SAMPLED review of worker closures | A self-graded closure needs an outside reader |

The third is the load-bearing one. Letting each Body grade its own work removes
the second pair of eyes that the reducer-side phase supplied for free, so the
split is only sound if something still samples those closures. Without it the
change would trade a latency cost for a quality cost and call it a win.

## Why two named rails bound it

- **guard-4638 — closing is not landing.** A worker that verifies its own unit
  is the Body most likely to conflate "I finished executing" with "the change
  reached `main`". The per-unit verification must never assert delivery it did
  not observe; the carrier-ref push and its `--verify-delivery` read are what
  speak to landing.
- **guard-3034 — an outside-world reading is a timestamped observation, not a
  settled fact.** Verification frequently rests on a probe of something outside
  the repo. The worker records what it observed and when, and does not promote
  that reading to a standing truth about the world.

## Why it is a scoped CALL, never a transcription

`guard-1867` and `guard-2676` (the no-transcription contract) require that a
worker capability be a scoped call into the shared component naming a `mode`,
never a restatement of that component's steps inside this loop. So the phase
resolves to `/aspirations-verify` invoked in an own-unit mode — the scope lives
as a mode *inside* the verify skill, where the skill's own evolution carries it.
A transcription would be a second implementation that drifts silently when the
component changes, and nothing fails when it does.

The disposition's `kind` is `SCOPED_CALL` and its `mode` field is mandatory at
import; `worker_execute.py` raises on a scoped call that names no mode, which is
what keeps this from degrading into a transcription by accident.

## Why the mechanical close is a SECOND call, not folded into the verify skill

Phase 4a is two calls in one step — `/aspirations-verify scope=own-unit` decides,
then `iteration-close.sh --phase verify` writes — and the separation is load-bearing
in both directions.

The write half exists because of a measured failure. Until 2026-08-16 the worker
loop said only "do not run verify" and left the goal at in-progress "for the
reducer to close at generalize-down" — but no reducer lane ever flipped a worker
goal's status (`worker_retrospective.py` has no close lane; `body-merge.py` only
NAMES ids), so every worker completion stayed in-progress forever. Measured that
day (alpha reducer, cc-04): 360 of 361 open alpha claims were finished work nobody
closed, held by 7 worker SIDs, 261 by dead Bodies; the parent aspirations never
completed, no successor goals were generated, and `goal-selector`'s `SKIP_STATUSES`
hid every one of them from every Body (g-115-6337; guard-4000 class — a KEEP that
never consults age grows without bound).

`iteration-close.sh do_verify` is the ONLY writer of that status transition and of
everything hanging off it: it routes recurring goals through
`aspirations-complete-by.sh`, stamps `completed_date` + `outcome_class`, posts the
"Completed:" board message the reducer and partners read, and clears `in_flight`
ONLY when the row names THIS goal (`--if-goal` compare-and-swap, so a live
reducer's row is never blanked). Its checkpoint write routes to THIS Body's
`sessions/<sid>/` dir (`body_state_path`), not the agent-wide one. guard-2523
names it as the sole writer for exactly this reason — a judgement phase that also
wrote status would be a second writer of a transition that already has one.

So the verify skill judges and writes per-goal verification state; the close writer
writes the status and its side effects. Neither is the other's step, and inlining
either into the other would reproduce a defect the fleet has already paid for.

## What `pending_goal` bought while the wiring was missing (history)

From 2026-09-03 until this wiring landed, the disposition row carried
`pending_goal="g-306-417"` and the CLI rendered it `[PENDING g-306-417]`. A
disposition table is read downstream as a statement of fact about what the loop
does, so an aspirational row written as a fact is worse than no row — it reads as
evidence the wiring exists and suppresses the goal that would build it. A test
pinned the marker by name and goal id and was written to FAIL when the wiring
landed, forcing whoever wired it to remove both in the same change. That is what
happened, which is the outcome the mechanism was for: a pending marker nobody is
compelled to remove becomes permanent, and then the table lies in the other
direction. The mechanism itself remains available for the next declared-but-unbuilt
row; only this row's use of it is finished.

Two things the carrier-ref implementation of this same change recorded that the
main-line one did not, kept here because they generalize (g-306-284 merge of
`refs/workers/alpha/2fda1f3e`, where both were written independently):

**The split into two increments was not ceremony.** part1b changes the live loop
under every worker Body in the fleet at once; part1a added an unreferenced table
row. Those are different risks, and pairing them would have put the second one's
blast radius on the first one's review.

**The general form: a marker that can only ever be ADDED is a ratchet, not a
tripwire. Both transitions have to be loud.** That is why the test that fired
when the wiring landed was REPLACED rather than deleted — its successor asserts
the row is NOT pending, so a silently re-added marker is as loud as a missing
one, in the other direction.

## Why `verify` still reads "reducer-only" beside a worker that verifies

`REDUCER_ONLY_PHASES` still contains `verify`, and `WORKER_PHASES` contains
`verify-own-unit`; the two are different scopes, not a contradiction. The
mechanical status write for the unit a worker executed was already sanctioned
separately as worker-side (2026-08-16, g-115-6337) — it is Phase 4a's scoped
call to the shared close writer, and it is not this phase. `verify-own-unit` is
the LLM judgement that precedes that mechanical close.

`test_worker_runs_verify_own_unit_but_not_the_reducer_verify_phase` asserts both
halves together on purpose: a change that collapsed them — "simplifying" by
letting a worker run `verify` — would keep the positive assertion green while
destroying the design, so the negative is pinned beside it.

## Cross-references
- guard-4638 — closing is not landing
- guard-3034 — an outside-world reading is a timestamped observation
- guard-1867, guard-2676 — the no-transcription contract (scoped call + mode)
- g-115-6337 — the earlier, separate sanction for the worker-side mechanical close
- `core/scripts/worker_execute.py` — `WORKER_PHASES`, `REDUCER_ONLY_PHASES`,
  `LIFECYCLE_DISPOSITIONS`, `PHASE_LIFECYCLE_STAGE` (the contract SSOT)
- `.claude/skills/worker-loop/SKILL.md` Phase 4a — the invocation site: this
  phase's judgement, then the mechanical close it precedes
- guard-2523 — `iteration-close.sh --phase verify` is the only writer of the
  status transition; guard-4000 — a KEEP that never consults age grows unbounded
- `core/config/rationale/suite-run-voided-by-loop-merge.md` — sibling extraction
  from the same skill
