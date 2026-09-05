# Rationale: Why a Worker's `precondition_unmet:` Prefix Is Not Enough

Referenced from `.claude/skills/worker-loop/SKILL.md` Phase 4a (the release-path
defer). Explains why that step demands a structured predicate *in addition to*
the prose defer it already required.

## Why the prefix reads as sufficient (and is not)

The instruction has always said "write the **structured** defer in the SAME
step". `structured` there names the `precondition_unmet:` PREFIX — the token
that routes the goal through `STRUCTURED_DEFER_PREFIXES` and suppresses it from
the selector. A reader who writes that prefix has satisfied the sentence and
reasonably believes the structure requirement is met.

It is not. The prefix governs SUPPRESSION. Nothing about it governs CLEARING.

`precondition-defer-recheck.py` — Phase 0.5b.3, the only sweep that clears this
class — evaluates `goal.verification.preconditions` through
`predicate.evaluate_all`. It reads **nothing** in `defer_reason`. A goal with
zero structured predicates is counted `skipped_free_form` with the reason "no
structured preconditions to evaluate — defer is free-form, LLM judgment
required". That skip is CORRECT: the script's explicit vacuous-truth guard
refuses to read "zero predicates" as "all predicates pass", which would clear
every prose defer in the queue.

So the machinery is sound and simply starved of input.

## The two liveness requirements (guard-5155)

A stored re-probe needs BOTH, and satisfying one is worth nothing alone:

- **L1 — a sweep that is INVOKED.** Phase 0.5b.3 is a *medium*-tier lane; it
  runs only when the reducer's precheck continues past the always-run battery.
- **L2 — a predicate that sweep can EVALUATE.** Only `verification.preconditions`
  qualifies.

Measured, twice, on two boxes nine days apart:

| box | date | scanned | eligible | evaluated | skipped_free_form | cleared |
|---|---|---:|---:|---:|---:|---:|
| cc-04 | 2026-08-26 | — | 104 | 3 | 101 | 0 |
| cc-13 | 2026-09-04 | 2880 | 84 | 3 | 81 | 0 |

**Exactly three evaluable defers both times.** ~96% of the lane's own eligible
population fails L2, so the sweep can act on 3–4% of it no matter how often it
runs. The gap is fleet-wide and stable, not one box's artifact.

The three that DO carry a predicate were genuinely evaluated and correctly
reported "still failing" (`command_succeeds` exit 1/exit 2, `file_check` found
0 need 1). End-to-end the mechanism works — which is why fixing the WRITE side
is the whole fix.

## Why this is the write side, not a new worker phase

The obvious reading of "a worker can set a defer but nothing clears it" is to
give the worker a clearing phase. Two measurements argue against it:

1. **The scoping the goal asks for is not expressible.** The schema carries
   `defer_reason` and `defer_reason_set_at` — there is no `defer_reason_set_by`
   or SID. "Clear the defers IT set" cannot be scoped without a schema addition,
   and `precondition-defer-recheck.sh` has no agent/SID flag (`--max-age-hours`,
   `--apply`, `--output`, `--metrics-log`).
2. **Unscoped, it is the Nth-reducer defect in a new place.** A worker running
   the sweep with `--apply` clears defers fleet-wide from its own unmerged
   state — a new authority the convergence forbids, to fix a problem the write
   side removes at the source.

Fixing L2 instead means the EXISTING reducer sweep clears the defer with no new
phase, no new field, no new lane and no new authority. The goal record is
already a covered carrier class (`worker_execute.py carriers` → `goal-record`),
so the pending re-probe reaches the reducer by the channel that already exists.

The residual limitation is real and deliberate: a worker still cannot clear its
own defer mid-session. It hands the re-probe up instead.

## Why "evaluable on ANY box"

guard-4306's corollary: state the clearing condition so it can be evaluated
WITHOUT the box that wrote it, or only the party who cannot see the goal can
clear it. This is not hypothetical — of the three live structured predicates,
two are Windows-specific (`platform-windows`, `native-windows-directwrite`) and
evaluate as "still failing" on every Linux box forever. A box-specific predicate
read on the wrong box returns a confident FALSE that is indistinguishable from a
genuinely unmet precondition.

The same failure reached the prose side the morning this was written: a worker's
own `precondition_unmet:` defer on g-115-8280 named a re-probe against a sweep
journal that `scheduler.py` pins box-locally, so it was re-derivable from
exactly one machine.

## Cross-references

- `guard-5155` — L1/L2, and the cc-04 measurement
- `guard-4306` — a structured defer SUPPRESSES; the author owns re-probing it
- `guard-2676` — worker capabilities are scoped CALLs into the shared component
- `core/scripts/predicate.py` — the predicate vocabulary (SSOT)
- `core/scripts/precondition-defer-recheck.py` — the sweep and its vacuous-truth guard
- `core/scripts/tests/test_worker_set_defer_clears.py` — the worker-set → cleared path
- `g-306-434` — the goal this landed under
