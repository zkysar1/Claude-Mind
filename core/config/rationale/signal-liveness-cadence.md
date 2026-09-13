# Rationale: Signal-Liveness Gets Its Own Cadence, Not an Audit-Baselines Row

Referenced from `core/scripts/iteration-close.sh` (the signal-liveness canary call
site) and `core/config/aspirations.yaml` § `signal_liveness`. Records the decision
g-318-156 outcome 3 asks for: does signal-liveness get its own cadence, or extend the
existing `meta/audit-baselines.yaml` ratchet?

**DECISION: its own cadence** — the third member of the canary family, run from
`iteration-close.sh` alongside `stale-sentinel-canary.py` and
`cadence-stale-canary.py`, filing an Investigate after `signal_liveness.threshold_iterations`
(3) consecutive DEAD verdicts for a registered signal. It does NOT extend
audit-baselines. Two independent reasons; the first is decisive on its own.

## Why not the ratchet — THE PREDICATE

The ratchet is a CHANGE detector over a tracked count. Its verdicts
(`core/config/conventions/audit-baselines.md`) are exactly four: `seeded` on the first
run (baseline := current), `stable` when current == baseline, `ratcheted` when the
count shrinks, `regressed` when it grows.

A signal that was **never alive** emits a constant number. It seeds at that number and
reads `stable` forever — so the ratchet does not merely miss the corpse, it certifies
it. And the always-reports-CLEAR class specifically INCLUDES never-alive instruments:
this goal's own motivating evidence is a nightly agent-bench that reported success
every night while running no agent and grading no task, for the workflow's entire
life. No tracked number ever moved, because there was never a live reading for it to
move away from. Seeding is the sharp edge — `audit-baselines.md` already records a
case of a baseline seeded from a broken state and blessing it.

Catching that needs a POSITIVE CONTROL: an assertion that the instrument must return a
NON-clear answer to a payload engineered to deserve one. That is what the canary's
registry rows run, and it is a different kind of operation from reading a number and
comparing it to last time. A ratchet cannot be made to do it by tuning.

## Why not the ratchet — THE CLOCK

The audit-baselines / ratchet / scar-tissue family is keyed on **completed-goal count**
(`goal_cadence` 5/25/50/75/100/200 in `core/config/aspirations.yaml`) — a
throughput-proportional clock. `.claude/rules/learning-philosophy.md` already names the
consequence: that family "fires SLOWER exactly as throughput drops."

A fleet whose throughput has dropped is precisely a fleet where a detector is most
likely to be dead and where the cost of not knowing is highest, so keying
signal-liveness to goal count would make it slowest exactly when it matters most. The
canary rides `iteration-close`'s per-iteration cadence instead, which is in the
liveness/stall family that rule describes as degrading gracefully.

This reason is secondary only because it is about latency; the predicate reason above
says the ratchet could never answer the question at any cadence.

## What audit-baselines KEEPS — this is a split, not a rejection

The goal's description is right that "a drastic CHANGE in a tracked number is exactly
its shape," and that remains true. Where a signal's health IS a number that moves —
drift totals, orphan counts, corpus sizes, hot-path bytes — the ratchet is the correct
and materially cheaper home, and nothing here moves those to the canary.

The clean split:

| question | home |
|---|---|
| did this number move? | `meta/audit-baselines.yaml` ratchet |
| can this instrument still produce a non-clear answer at all? | signal-liveness canary |

The two compose rather than compete: one signal may legitimately have both a baseline
row and a canary row, because they answer different questions about it. What is NOT
sound is using a baseline row as evidence of liveness — that is the substitution this
decision refuses.

## Why the threshold is 3, and not independently tuned

Deliberately identical to both sibling canaries. The condition all three guard is
persistent rather than intermittent, so the same small N is right for the same reason,
and three canary families carrying three different thresholds would be three numbers a
reader has to explain the difference between when there is none.

## Unevaluatable is not DEAD

An assertion that cannot be EVALUATED resets the counter and never fires. Always-ALARM
is the same defect as always-CLEAR, one step removed: an instrument that cries wolf
gets muted, which arrives at the identical silence by a slower route. A merge-wedged or
partially-provisioned box legitimately lacks a recent script, and that must never read
as a dead signal.

## Cross-references
- `core/config/conventions/audit-baselines.md` — the ratchet's schema and its four verdicts
- `.claude/rules/learning-philosophy.md` § "The latency asymmetry to design against" — the goal-count clock
- `core/scripts/signal-liveness-canary.py` — the registry and the assertions
- `core/scripts/stale-sentinel-canary.py`, `core/scripts/cadence-stale-canary.py` — the two siblings
- guard-2421, guard-3062 — positive-control discipline (a control licenses the instrument for the TARGET it ran on, not the population)
- guard-4282 — a config value and the prose justifying it are two artifacts
- g-318-156 outcome 3 — the decision this file records
