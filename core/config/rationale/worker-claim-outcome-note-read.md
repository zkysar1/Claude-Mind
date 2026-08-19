# Rationale: Why a worker reads `outcome_note` at CLAIM, from the record it already holds

Referenced from `.claude/skills/worker-loop/SKILL.md` Phase 2.9. Explains why the
worker path needs its own outcome_note read, why it is a READ of the claim
response rather than a new call, and why the reducer's instrument cannot serve it.

## Why the reducer's instrument does not reach here

The outcome_note instrument lives in `aspirations-select` Phase 2.9. The worker
loop routes through neither `aspirations-select` nor `aspirations-execute` Phase
2 — its Phase 1 calls `goal-selector.sh` and its Phase 2 calls
`aspirations-claim.sh` directly. So the instrument is not weakly wired on this
path; it is **structurally absent** from it.

Measured 2026-08-18 (cc-07, and independently reconfirmed on cc-08 while
implementing this): `outcome_note` appeared 7 times in worker-loop/SKILL.md and
every occurrence was at line 809 or later — Phase 3.7's stranding note, Phase
3.9's closure evidence, Phase 4a's `do_verify`. All write-side. Phases 1 and 2
named the field zero times.

The consequence is an ordering inversion: **the loop's only prompt to touch that
field is the one that WRITES it**, so the read happens after the work it would
have prevented.

## The concrete cost, observed rather than hypothesised

A worker claimed `g-115-6468`, whose record carried an 11,454-char outcome_note
written by the *same SID* a day earlier. It re-measured all five verification
criteria from scratch and drafted a ~7KB narrative, then discovered the existing
note only when `closure-evidence-write.sh` REFUSED TO CLOBBER IT at Phase 3.9.

Two more instances the same day: `g-335-1291` (a fix built to completion while
the identical fix sat merged on `origin/main`) and `g-364-17` (PR #210 built
while PR #209 shipped and merged four minutes into the build).

None of these is a discipline failure. The read the discipline prescribes was
not wired into the path most work takes.

## Why this is the majority population, not an edge case

Workers execute most units now — 7 worker SIDs against 1 reducer on alpha
(2026-08-16). So the population this instrument cannot see is the **majority** of
claims, which also qualifies any effect size `g-115-6129` measures for the
reducer-side instrument: that measurement is over reducer claims only.

## Why a READ of the held record, and not a new call

`aspirations-claim.sh` already returns the full goal record on success — verified
by observing its own stdout at claim time. So the cheapest correct fix is to read
the object the phase is **already holding**, not to add a lookup.

This matters beyond cost. A new call would be a second retrieval of state the
loop just fetched, and the framework's repeated lesson here is the opposite one:
*read the whole record you are handed, not the one field you have a habit of
checking* (self.md's fifth shipped-read). Adding a call would also give the step
its own failure mode — a lookup that can time out, 404, or race with the claim —
where a field read on an in-hand object has none.

It is also why the step cannot be a `--check-note` flag on the claim script:
the information is already in the response, so a flag would only re-present it.

## Scope boundary against the sibling goals

- `g-115-6129` — asks whether naming outcome_note in `aspirations-select` Phase
  2.9 lowers the re-derivation rate, i.e. whether the instrument WORKS where it
  already is. This is about its POPULATION excluding workers.
- `g-115-6663` — the PUSH-boundary half: work that lands *mid-build*.
- This (`g-115-6695`) — the CLAIM-boundary half: work that already landed
  *before* the claim.

Complementary, not overlapping. A fix to any one of the three leaves the other
two intact.

## Why the imperative is short and lives in the hot path anyway

`.claude/skills/worker-loop/SKILL.md` is a hot-path budgeted member
(`core/config/hot-path-budget.yaml`): every byte is paid on every compaction
cycle of every agent. The convention's prescribed shape is to keep the
imperative there and route the reasoning to an on-demand home — which is this
file. A reader who needs only "what do I do" never loads this; a reader asking
"why does Phase 2.9 exist" gets the whole story.

## Cross-references

- `g-115-6695` — this goal; relayed from `g-115-6468` by a worker Body (cc-07)
- `g-115-6129`, `g-115-6663` — the sibling halves above
- `rb-5669` / `guard-1719` — retrieve against the REMEDY a goal prescribes, not
  only its problem; this goal instructed exactly that in its own text
- `.claude/rules/retrieve-before-deciding.md` #11 — filing a goal that prescribes
  a fix
- `core/config/conventions/hot-path-size-budget.md` — why the imperative is terse
