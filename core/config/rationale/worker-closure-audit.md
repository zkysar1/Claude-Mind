# Rationale: Worker-Closure Sampling Audit

Referenced from `.claude/skills/aspirations-complete-review/SKILL.md` Phase 7.7.
Why the reducer samples worker closures at all, why it sits after Phase 7.6, and why
its checks are deliberately narrow.

## Why the audit exists

The Mind/Body convergence (asp-306) makes a worker Body execute a unit and judge it.
That is the point — the reducer was re-deriving 50+ closures a day from notes it did
not write, the largest per-goal reducer cost in the loop. But the same move removes
the only second pair of eyes: the Body that did the work becomes the Body that grades
it. g-306-417 names this half "the load-bearing half of the split — the outside reader
that self-grading removes."

This audit is the compensating control. It does not restore per-goal review (that
would reinstate the cost the split exists to remove); it makes self-grading drift
OBSERVABLE, so a systematic problem surfaces as a trend in a readable store instead of
never surfacing at all.

It was built BEFORE its producer, deliberately. worker-loop Phase 4a does not yet
invoke `/aspirations-verify` with `scope=own-unit`, so no closure carries an LLM
verdict field. Shipping the auditor first means the invariant CONSTRAINS that wiring's
design rather than being retrofitted onto whatever it happens to emit — the ordering
`close-review-gate.py` used against its own producer (rb-4452). It is not idle in the
meantime: every check runs against fields worker closures carry TODAY.

## Why it sits after Phase 7.6 rather than inside Phase 7.5

Phase 7.5 is where goal data is already gathered, so 7.5 looks like the natural home.
It is the wrong one. Phase 7.4's success path ends `PROCEED to Phase 7.5.9`, vaulting
over 7.5.1, 7.5.2, 7.5.2b and anything added beside them — so a step placed inside 7.5
would silently skip every intent-satisfied closure, which is exactly the population
most worth auditing (an aspiration closed on trailing-blocked goals). guard-1843 names
this as the default outcome of the normal editing motion rather than a typo: each
earlier phase still points where it was correct when written.

After Phase 7.6 both routes have converged — the normal 7.5 walk and the 7.4 → 7.5.9
jump — and neither can bypass it. Anyone adding a Phase 7.8 inherits the same
obligation: audit every continuation pointer in the chain, not just the one above.

## Why the checks are narrow, and why they carry confidences

Each check maps to a guardrail earned from a live failure, and each carries a
confidence rather than a flat pass/fail, because they are not equally decisive:

| check | guardrail | confidence |
|---|---|---|
| `empty_note` | guard-2852(a) | high |
| `note_done_status_disagrees` | guard-2852(b) | high |
| `remainder_language` | guard-4007 | medium |
| `criterion_unrun` | guard-1968 | low |

A HIGH is an objective contradiction inside the record and reads DISAGREE. MEDIUM/LOW
read REVIEW — a signal for a reader, never a finding on its own. The LOW check is a
keyword heuristic over free prose and will over-report; confining it to REVIEW means it
can never manufacture a DISAGREE.

Two calibration findings, both measured against 110 live worker closures on 2026-09-03,
are why the checks look narrower than the guardrails they implement:

- **Recurring goals rest at `pending` by design.** A recurring goal runs, records DONE,
  and returns to pending for its next interval. Before that carve-out existed, all 12
  DISAGREE verdicts on asp-115 were recurring goals (`achievedCount` up to 426) — the
  entire high-confidence bucket on the largest aspiration was noise. The carve-out is
  scoped to `note_done_status_disagrees` alone: an empty note is a defect whatever the
  goal's cadence, and a blanket exemption would drop every recurring closure out of the
  audit.
- **Closure notes cite sibling goal ids constantly.** "g-115-4846 … still pending"
  describes a DIFFERENT goal. Generic queue vocabulary (`still pending`, a bare
  `remainder`) matched 6 times and was wrong every time, so only self-referential,
  quantified forms survive. Under-matching is the correct direction: a missed remainder
  costs one unaudited goal, while a checker that cries wolf on ordinary cross-references
  gets ignored wholesale.

The successor suppressor follows from guard-4007's own prescribed remedy — "file the
successor FIRST and name its id in the outcome_note". A note that does so is COMPLIANT,
so flagging it punishes the exact behaviour the guardrail asks for (measured on
g-115-8667). The defect is a remainder with NO tracker, which is what the check now
tests.

## Why report-only, always

`rc` is 0 even when every sampled closure disagrees. An audit that can wedge the
completion review it audits would be traded away the first time it misfired, and the
remedy for a real finding already exists: Steps 7.5.4-7.5.5 route outstanding work into
goals. Blocking archival would be a second, worse path to the same place.

## A known limitation, stated rather than hidden

Completion review fires when an ASPIRATION completes, so a worker closure waits for its
whole aspiration before being sampled. Against a 50+/day closure rate that is poor
time-to-detection, and the fleet's own directive prefers reducing time-to-detection over
explaining improvement (`learning-philosophy.md`). The placement follows g-306-417's
explicit instruction to keep this with complete-review; if the latency proves to matter,
the sampler is a standalone script and can be called from a faster cadence without
changing its contract.

## Cross-references

- `core/scripts/worker-closure-audit.py` — the sampler
- `core/scripts/tests/test_worker_closure_audit.py` — pins both directions, including
  the two calibration regressions above
- guard-1843 — continuation pointers; the reason for the placement
- guard-2852, guard-4007, guard-1968 — the checks' source incidents
- rb-4452 — ship a dep-blocked governance invariant before its dependency
- `core/config/rationale/worker-verify-own-unit.md` — the sibling half of the split
