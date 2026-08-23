# Rationale: A Full-Suite Run Must Finish Inside the Unit That Launched It

Referenced from `.claude/skills/worker-loop/SKILL.md` Phase -0.3 and Phase 3.8.
Explains why a worker may not leave a `run-full-suite.sh` run executing across a
work-unit boundary, and why BOTH of this loop's `iteration-push.sh` call sites
void such a run even though only one of them is named "push".

## Why a merge voids a suite run at all

`run-full-suite.py` compares the repo HEAD at launch against HEAD at finish. If
they differ it emits `VERDICT: INVALID (tree-moved)`, and that check OUTRANKS
every other verdict — a run whose tree moved reports nothing usable no matter
how many tests passed. This is correct behaviour: a result measured across two
different trees is not a measurement of either.

`rb-8554` already names one door into this: commit FIRST, then launch, because a
mid-run commit moves HEAD. That guidance is right and insufficient — it names
the door the RUNNER walks through, and there is a second door the LOOP walks
through on the runner's behalf.

## Why BOTH call sites merge, though only one is named "push"

The flags differ on their PUSH behaviour and are identical on their MERGE
behaviour, which is exactly the asymmetry that makes the hazard invisible:

| Phase | Call | Documented as |
|---|---|---|
| -0.3 | `iteration-push.sh --no-push` | "Fetch + integrate, then STOP before the push decision." |
| 3.8 | `iteration-push.sh --push-worker-ref` | "Fetch + integrate, then push HEAD to refs/workers/<agent>/<sid> and STOP" |

Both begin "Fetch + integrate". In `iteration-push.sh` the `git merge --no-edit`
(line ~1057 as of 2026-08-20 — the ordering is the durable fact, not the number)
sits ABOVE both flag branches, which exit at the same integrate/push seam
(`--no-push` at ~1251, `--push-worker-ref` at ~1283). The seam placement is
deliberate and documented in the script: it is "the one place a push mode cannot
be added below and silently escape the shared-branch guard." A consequence
nobody wrote down is that it is also the one place a push mode cannot be added
below and escape the MERGE.

So `--no-push` is a misleading name to reason about here. It suppresses the
push, not the merge. A reader checking "does the carrier move HEAD?" will read
Phase 3.8's existing note — which explains why `--push-worker-ref` does NOT
contradict `--no-push` on contention grounds — and come away reassured about the
wrong axis.

## Why the structural half is worse than the one-off

The originating incident (measured 2026-08-20, cc-07) was a single unlucky
ordering: commit `e8fcd7889`, launch the suite, then ship the carrier — which
integrated 7 origin commits and moved HEAD to `7761c7197` mid-run. The worker
killed the run rather than wait ~30 minutes for a guaranteed `INVALID`.

The durable problem is Phase -0.3. It runs at the TOP OF EVERY CYCLE, so ANY
suite still executing when the next work unit begins is voided by the loop's own
preamble — automatically, silently, and indistinguishably from a careless
commit. A ~30-minute suite against a 15–92 minute unit cadence makes the
collision LIKELY rather than exotic. Confirmed live on cc-08 2026-08-20: a
Phase -0.3 pull in an ordinary preamble integrated 7 origin commits.

Hence the constraint, which is a property of the loop and not of the runner:
**a full-suite run must complete inside the unit that launched it.**

## Why documentation rather than a deferral sentinel

The alternative considered was mechanical: `run-full-suite.py` records its launch
HEAD in a sentinel, and `iteration-push.sh` defers the merge while a run is live.
It was rejected for now on cost/benefit, not on principle:

- A deferred merge has its own failure modes. A crashed runner leaves a stale
  sentinel that blocks every future push, so the mechanism needs a staleness
  escape — and a staleness escape that fires wrongly re-creates the very silent
  merge it was built to prevent.
- The detection half ALREADY WORKS. The runner correctly refuses to certify a
  tree-moved run; nothing is being missed. What was missing is anything that
  PREVENTS the collision or warns the launcher it is coming, and a launcher who
  knows the rule does not need to be stopped by a sentinel.
- The failure is loud and cheap when it happens (a voided run, ~30 minutes),
  not silent and expensive. That ratio does not justify an invasive change to
  the one script every Body's preamble depends on.

If the constraint is later measured to be violated repeatedly despite being
written down, that is the evidence that would justify the sentinel — and the
staleness escape is the part to design first.

## Cross-references

- `rb-8554` — commit-before-launch; names the RUNNER's door into `tree-moved`.
  This file names the LOOP's door. Whoever updates one should cross-link the
  other, or the second door gets re-discovered from the first (g-306-335
  outcome 3).
- `g-306-335` — the goal that filed this constraint; relayed by an alpha worker
  Body via the sq-013 capture lane and filed at reducer spark replay.
- `guard-1150` — never pipe the runner; a trailing pipe replaces its exit code.
  Same family of "the measurement was destroyed by how it was invoked".
- `.claude/rules/run-full-suite-after-deep-code.md` — the suite's own method
  guide, including why `VERDICT` outranks the numbers.
- `.claude/skills/worker-loop/SKILL.md` Phase -0.3 and Phase 3.8 — the two
  consumers of this rationale.
