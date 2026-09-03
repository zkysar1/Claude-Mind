# Rationale: Why Verification Splits Into a Per-Unit Half and a Reducer Residue

Referenced from `.claude/skills/worker-loop/SKILL.md` § "The phase split" and
from `WORKER_PHASES` / `LIFECYCLE_DISPOSITIONS` in `core/scripts/worker_execute.py`.
Explains why `verify` — a phase that was wholly reducer-only from Phase 2A until
2026-09-03 — now has a worker-side half called `verify-own-unit`, why that half
is safe under the convergence invariant that forbids N reducers, and why the row
is deliberately marked `pending_goal` rather than described as built.

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

## Why the row is marked `pending_goal` instead of just written

The phase and its disposition exist; `worker-loop` does not yet invoke the
verify skill. A disposition table is read downstream as a statement of fact
about what the loop does, so an aspirational row written as a fact is worse than
no row — it reads as evidence the wiring exists and suppresses the goal that
would build it. `pending_goal="g-306-417"` says so in machine-readable form, the
CLI renders it as `[PENDING g-306-417]`, and
`test_verify_own_unit_is_marked_pending_until_the_loop_actually_invokes_it`
fails the moment the wiring lands — forcing whoever wires it to remove the
marker and delete that test in the same change. A pending marker nobody is
compelled to remove becomes permanent, and then the table lies in the other
direction.

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
- `.claude/skills/worker-loop/SKILL.md` Phase 4a — the mechanical close this
  phase will precede
- `core/config/rationale/suite-run-voided-by-loop-merge.md` — sibling extraction
  from the same skill
