# Rationale: Read the Handoff Fields at Selection

Referenced from `.claude/skills/aspirations-select/SKILL.md` Phase 2.9, which
tells the reader to read `outcome_note`, `outcome_notes` and `progress_note`
whole before any scope reasoning. This file holds why that read sits at
selection rather than in a fourth guardrail, with the incidents behind it. The
text moved here verbatim from the skill so that always-loaded file need not
grow (g-375-157).

## Why at selection, and not a fourth guardrail

THE COST LANDS HERE, ONE PHASE BEFORE guard-2803's OWN TRIGGER: it fires after
aspirations-claim.sh returns — correct, and still too late, because selection
is where "this goal is bigger than it says" gets decided, from the description,
while the answer sits unread in the same record. Three escalating occurrences,
guard-2803 already written and active (times_active 763) for all three:

```
2026-08-05 g-335-818  (bravo) caught AT claim — worked as designed
2026-08-13 g-335-1173 (alpha) ~15 min re-deriving scope already written down
2026-08-13 g-335-1201 (bravo) FULL duplicate implementation of a partner's
                              open PR (#193 vs #194), merged before discovery
```

A guardrail cannot outvote the instrument it guards (guard-1984) — hence these
lines, not a fourth guardrail.

## The tell, which runs against intuition

TELL, counter-intuitive: a re-derived conclusion arriving CORRECT is not
reassurance, it is the signature — it matched because it was already recorded.
g-335-1201's two independent implementations converged on byte-compatible wire
formats, reading as strong validation of the design and ALSO proving that one
of them never needed writing.

## Cross-references

- `.claude/skills/aspirations-select/SKILL.md` Phase 2.9 — the read this explains
- rb-10474 — why justification moves off a hot-path file behind one pointer line
