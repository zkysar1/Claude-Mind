# Rationale: Pre-mortem clauses (f), (g), (h) — reachability, provenance, survival

Referenced from `.claude/skills/aspirations-spark/SKILL.md` step 0.7. Explains why the
adversarial pre-mortem carries three separate authoring checks rather than one, and what
each was measured against.

## Why three clauses and not one

They test three genuinely independent properties, and a hypothesis can pass any two while
failing the third:

| clause | property of | the question |
|---|---|---|
| (f) two-sided reachability | the **objection** | which outcome branch can this clause fire on, and is that branch reachable in my window? |
| (g) sample provenance | the **instrument** | which world does my sample come from — intervened or un-intervened? |
| (h) substrate survival | the **substrate** | will the artifact my channel reads still be the same object when the window opens? |

Each was surfaced by a different agent resolving a different hypothesis, and each was
checked against the other two before being proposed. A hypothesis can have perfectly
two-sided objections drawn from a perfectly un-intervened world and still be dead because a
third party overwrote the artifact — which is exactly what happened in the (h) case.

## Why they are inline rather than left to retrieval

All three already exist as retrievable prescriptive entries — guard-2674, guard-2684,
rb-6767 — whose triggers cover the authoring moment. They kept not firing. The (h) case is
the sharpest evidence: the goal that demolished the substrate was HIGH, pending, and the
same agent had personally censused it 8h later the same day. Proximity to the information
was never the problem; nothing prompted the join. That is the profile of a defect a
retrievable guardrail keeps failing to prevent and a structural clause catches.

## (f) Two-sided reachability — two shapes, both measured

**Founding case: a one-sided ESCAPE CLAUSE** (g-115-3629, bravo, cc-05, 2026-08-04,
resolving `2026-07-28_strategic-focus-banner-shifts-realized-lane-mix`). The record
pre-registered "if zeta-eligible asp-335 candidates stayed <= 3 for the window, record
UNRESOLVABLE, NOT CORRECTED". Well-formed, genuinely reachable, correctly measured at
resolution (50 lane closes occurred, so it did not bind). But scarcity can only cap a
realized share DOWNWARD — it explains a share below threshold and can never manufacture one
above it — so it could only ever fire on the CORRECTED side. The hypothesis CONFIRMED at
28.6% against a 13% threshold, and on that branch the pre-mortem contributed nothing. The
confirmation shipped with its real weak point never pre-registered: supply moved INSIDE the
rate's own denominator (2 eligible candidates at filing vs 50 closes in the window), a
confound no scarcity clause can reach.

**Second, independent shape: a one-sided CONFIRMED criterion** (g-001-08, foxtrot,
LAPTOP-3IOFCNEO, 2026-08-27). Both unreflected hypotheses that day resolved UNRESOLVABLE for
one shared structural reason — each pre-registered a CONFIRMED criterion that required a
DETECTOR TO FIRE:

- `2026-08-19_userdata-ceiling-test` — CONFIRMED required "CI goes red with no production
  user-data error in the same window". Measured: 25/25 CI runs `conclusion=success`;
  `filter-log-events` for 'User data is limited' returned `events=[]`. Nothing pushed
  UserData past 16,384 B. The author's own words: *"a race cannot be won when no starting
  gun is fired."*
- `2026-08-13_phase-2-9-outcome-note-instrument-fix` — window B computable (3,168 claim
  posts / 1,838 goals); window A not computable at ANY denominator, because no coordination
  claim post from that period survives in any store. The DENOMINATOR failed before any
  numerator existed.

This is why (f) is written over "can this criterion come out BOTH ways", not narrowly over
escape clauses, and why it asks about reachability **in the observed window** rather than in
principle: a guard-fire criterion is reachable in principle and unreachable in practice,
because X-not-happening is the normal case for a working guard. Encoded as rb-9417.

**Why it also governs the (c) discount.** guard-3242 established that a pre-mortem objection
earns a confidence discount ONLY if it has discriminating power over the verdict — measured
as two independent same-day instances, both CONFIRMED at a discounted confidence. A
one-sided objection cannot change the verdict, so paying clause (c)'s -0.15 for it
manufactures underconfidence. The two guardrails compose: guard-2674 says do not CREDIT a
one-sided clause at resolution, guard-3242 says do not PAY for it at authoring.

**Why guard-1931 does not already cover this.** None of its three probes detect the shape:
the clause is not near-certainly-true (variant 1), not structurally impossible (variant 2 —
it WAS reachable, just not on the branch that occurred), and the branch pair is not
non-exhaustive (variant 3). guard-2200 governs an objection that CAN bind
(already-satisfied / cheaply-resolvable); here the objection is real, unresolved, and simply
pointed at the branch that did not happen.

## (g) Sample provenance

Measured g-335-422 (alpha, cc-04, 2026-08-05, resolving
`2026-07-29_hover-only-reason-recurs-without-a-static-check`). The sample gate was met and
exceeded: 23 merged PRs, 164 CI runs all green, plus an independent per-commit replay across
all 23 merged tree states in a detached worktree, 23/23 green. CORRECTED at confidence 0.45
— the right verdict on the pre-registered criteria.

The defect is in the INFERENCE the record pre-registered, verbatim: *"CORRECTED (20 clean
PRs) ... would say that naming a class in a PR body, a file comment, and a reasoning-bank
entry IS sufficient without enforcement — which would argue against building this kind of
check next time."* That is unsupported and unrecoverable from this sample, because **the
check WAS the enforcement for all 23 PRs**. Any author who introduced the class would have
seen red and fixed it pre-merge. The clean sample measures the intervened world; the
inference is about the un-intervened one. Acting on it would have argued against building
the next check, on evidence that does not exist.

Two details that make it worse rather than better. (1) A firing DID occur in-window — a
stale-ALLOWLIST assertion tripped when product copy was renamed — correctly excluded as
instrument maintenance under the record's own baseline-exclusion clause, but it proves the
see-red-and-fix pathway is live in this repo, so silent local deterrence cannot be excluded.
(2) The record had been re-scoped mid-flight and its formation note claimed the new version
"tests the same underlying question." It does not: it tests "does the class recur WHILE
ENFORCED", and the original inference rode along attached to the new measurement
unchallenged. That is why (g) carries an explicit re-scope instruction.

Prescriptive half: guard-2684, whose trigger covers both formation and resolution, and which
states why the nearest siblings do not fire — guard-2194 needs a firing plus a remedial act,
guard-2413 is about a watch condition weakening the successor's evidence bar, guard-1962 is
about an expectation derived from the artifact under test.

## (h) Substrate survival

Measured g-115-4798 (echo, cc-03, `uname -r` 6.8.0-136-generic, 2026-08-05, resolving
`2026-08-03_batch-secret-write-was-uniform`). Formed 2026-08-03T15:22:22 with a
well-specified channel: `gh run list` on 4 named repos, requiring `createdAt` past a stated
threshold AND a non-empty conclusion, with explicit CONFIRMED / CORRECTED / UNRESOLVABLE
criteria and a premortem. At 18:30:00–18:30:05 the SAME DAY — 3h08m later — a peer's Wave 1
of g-115-3302 rewrote `AWS_ACCESS_KEY_ID` on all four onto a different IAM principal. No
qualifying run had occurred. Resolved UNRESOLVABLE.

Three things make it a clause rather than a guardrail alone:

1. **The channel still executes.** `gh run list` still returns a non-empty conclusion, so
   the stated MEASURE remains runnable and would yield a clean-looking CONFIRMED — for the
   wrong write. A contaminated channel does not announce itself; it answers a question you
   did not ask, in the format you were expecting. Unlike a channel that breaks, nothing at
   resolution time flags it.
2. **It can be irrecoverable.** Retrospective rescue failed on a property of the records,
   not of the effort: the 12-char AWS key prefix is account-derived and IDENTICAL across the
   old and new principals (rb-5451), while guard-1226 has us record only 12 chars in durable
   records. Only an ARN discriminates, and none was captured per-repo pre-write. There was
   no archaeology. This is why (h) says a full identifier and never a truncated prefix:
   "record the identifier" and "record enough of the identifier to discriminate" are
   different instructions, and only the second one works.
3. **The demolishing goal was not obscure** — see "Why they are inline" above.

Prescriptive half: rb-6767 (poignancy 7, `applies_to` any).

## Why the clauses were lettered in one pass

Three goals each adding "the next letter" collide by construction, which is why all three
candidates were appended to a single goal (g-115-4995) rather than filed separately. At
execution the archive-recording clause was the terminal action of the block, so the three
checks were inserted as (f)(g)(h) BEFORE it and the archive step renumbered (f) -> (i) —
appending after it would have told a reader to record the pre-mortem and then perform three
more checks. Repo-wide grep for step 0.7 clause-letter references returned only the block's
own `clause (d)` / `clause (e)` mentions in the SKIP note, both unaffected; that grep was
positive-controlled by confirming it did find those two.

Predecessors added by the same route: g-115-2579 added (d) scope-quantifier decomposition,
g-115-2667 added (e) discriminating-power.

## Why the SKIP carve-out names (f)-(h) explicitly

Step 0.7 may be skipped when the prediction is about external systems rather than project
code quality, and the carve-out then re-imposes specific clauses by letter. It named only
(d) and (e). Leaving it that way would have made all three new clauses skippable for
external-system hypotheses — and **every one of the three was measured on exactly such a
hypothesis**: (f)'s second shape on a CI UserData ceiling test, (g) on a CI lint gate, (h)
on rotating cloud credentials read through `gh run list`. The carve-out would have exempted
each clause from the very case that produced it, which is a silent nullification rather than
a scoping choice. This was not in the originating goal's outcomes; it was caught on the
post-edit re-read and fixed in the same change, since the defect was introduced by the
insert itself.

## Size cost, stated plainly

`.claude/skills/aspirations-spark/SKILL.md` is in the `loop-skills` hot-path budget set, so
this edit grew a file that is not supposed to grow: **+2,727 bytes** (117,891 -> 120,618),
committed under a `size-budget-override:` trailer and audited to
`world/override-bypass-ledger.jsonl`. The override is honest rather than routine — an
authoring-time check has to be readable at authoring time, and these three had already
failed to fire as retrievable guardrails, which is the whole argument for inlining them.
This file carries the incident evidence precisely so the inline text could stay at the
imperative-plus-tell density that `.claude/rules/rationale-extraction.md` prescribes for a
size-budgeted source; without the split the same content would have cost several times as
much hot-path surface.

## Cross-references

- guard-2674 — resolution-time half of (f); guard-3242 — the authoring-time discount half
- guard-1931 — sibling degenerate-branch family; guard-2200 — already-satisfied objections
- guard-2684 — prescriptive half of (g); rb-6767 — prescriptive half of (h)
- rb-9417 — the detector-must-fire shape; rb-5451 — why the truncated key prefix could not
  discriminate; guard-1226 — the 12-char convention that made it unrecoverable
- `world/knowledge/tree/performance/agent-performance/hypothesis-calibration.md` —
  "Premortem Pathology V: The Named Weak Link Was a ONE-SIDED Guard", the owning node
  (over the Read cap; read with an offset)
- `.claude/skills/aspirations-spark/SKILL.md` step 0.7 — the consumer
