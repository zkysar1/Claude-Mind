# Rationale: The S2a Attach Stop-Condition

Referenced from `.claude/skills/aspirations-strategic-scan/SKILL.md` Phase S2a.
Explains why the attach step carries a mechanical read-before-append gate rather
than the judgment call ("differs materially") it replaced.

## Why the gate had to become mechanical

The S2a marker that precedes it stopped the goal-spam it was written for, and
redirected the identical pressure into ONE FIELD.

Measured 2026-09-07 (bravo, hostname cc-05, uname -r 6.8.0-138-generic):
`g-115-5462`'s `progress_note` carried **13 distinct `[appended:...]` S2a fresh
counts from 5 agents in ~6 days** and stood at **38,198 B**, while its own newest
entry said the deliverable was "unchanged at 28 suspect". The goal had never been
executed.

That is `rb-4502` exactly — removing a false-negative exposes the next-layer gap —
and it is `guard-2034`'s shape (a periodic ritual that does not count its own
prior outputs), with the accumulation moved from the QUEUE, where a duplication
gate can see it, into a FIELD where none can.

## Why "differs materially" was doing no work

A scan that re-measures the same corpus every ~4h almost always differs by a goal
or two, so the predicate was satisfiable on nearly every pass. It read as a
judgment gate while functioning as an unconditional append. Replacing it with
token-presence (numerator, member keys, hostname) makes the same question
decidable without judgment: if your own tokens are already in the note, your
measurement is a REPRODUCTION, not news.

Cross-box reproduction IS real evidence — it just belongs in
`core/config/strategic-scan-readings.md` as one row (where this phase already
routes every other reading), not in the executor's note, which is a work
instruction rather than a control surface.

## Why the fix went in the instrument

A guardrail cannot outvote the instrument it guards (`guard-1984`), and the
ritual honestly recomputes S2a every scan — so with nothing in the instrument
saying the finding is known, each pass re-derives it as new. A note that must be
READ before it is written to cannot grow unboundedly by construction; the 13
appends each skipped that read.

## Cross-references

- `rb-4502` — removing a false-negative exposes the next-layer gap
- `guard-2034` — a periodic ritual must count its own prior outputs
- `guard-1984` — a guardrail cannot outvote the instrument it guards
- `rb-5818` — stale-by-construction queue entries
- `.claude/skills/aspirations-strategic-scan/SKILL.md` Phase S2a — the consumer
- `core/config/strategic-scan-readings.md` — where a reproduction row belongs
