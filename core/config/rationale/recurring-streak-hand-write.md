# Rationale: Why Hand-Writing a Recurring Streak Field Is Worse Than a Duplicate

Referenced from `.claude/skills/aspirations-verify/SKILL.md` → "Recurring Streak
Logic". Explains why guard-1604 needs a WARNING at point of use rather than a
note, and why the damage cannot be repaired in band.

Extracted 2026-09-03 (g-306-417, part1a) under `rationale-extraction.md`: the
three paragraphs below are multi-paragraph WHY about a non-obvious structural
choice, living in a size-budgeted hot-path file (`loop-skills` ratchet in
`core/config/hot-path-budget.yaml`). The IMPERATIVE and the descriptive
derivation block deliberately stayed inline — a warning is only useful where the
writer is standing.

## Why it is worse than a harmless duplicate

`lastAchievedAt` is an **INPUT to the derivation, read immediately before it is
overwritten.** Setting it by hand to the current time collapses `elapsed` to ~0,
and the canonical close path then derives an UNBROKEN streak from that corrupted
input — so a wrong number arrives looking script-computed. Nothing errors, and
the false value is always the flattering one.

Measured (g-001-02, bravo, 2026-08-04): a true 19.1h gap against a 15.99h break
threshold correctly yields `streak = 1`; the hand-written path recorded 4.

This is what separates it from an ordinary double-write. A duplicate write of an
INERT field is redundant but harmless; a duplicate write of a field that is read
as an input one instruction earlier changes the computation's result.

## Why re-running the close does not repair it

The hand-write destroyed the only copy of the previous `lastAchievedAt`, so a
second run reads the value the first run just wrote, computes `elapsed ≈ 0`
again, and derives the same unbroken streak — deterministically, forever.

Recovery needs the ORIGINAL timestamp from the store's `.history` snapshot.
There is no in-band fix, which is why the guard is phrased as a prohibition
rather than a "prefer the script" preference.

## Why the cost is a learning loss, not a cosmetic one

The streak-break reflector consumes this field. A falsely-unbroken streak
silently removes the very signal that a cadence was missed — so the failure mode
is not "a number looks wrong in a report", it is "the loop stops noticing that it
skipped something". Under `learning-philosophy.md` that is the expensive
direction: a suppressed detector costs more than a wrong display.

## Cross-references

- guard-1604 — the prohibition this file explains
- `.claude/skills/aspirations-verify/SKILL.md` → "Recurring Streak Logic" — the
  pointer source; keeps the imperative and the descriptive derivation inline
- `core/config/rationale/worker-verify-own-unit.md` — sibling rationale for the
  same skill's per-unit / reducer-residue split (g-306-417)
- `.claude/rules/rationale-extraction.md` — the extraction contract applied here
