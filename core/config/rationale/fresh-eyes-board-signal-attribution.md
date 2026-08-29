# Rationale: fresh-eyes-review Phase 2.3b Board-Signal Attribution

Referenced from `.claude/skills/fresh-eyes-review/SKILL.md` Phase 2.3b. Why the
directed-at-me test checks TAGS FIRST and treats a prose mention only as a
fallback, and why the own-receipt shape test sits above the tag short-circuit.
Every failure in this family inflates `self_evolution_signals_count` toward a
permanent `act_later`, and every one of them GROWS over time.

## Why the ORDER is explicit (guard-1877)

Measured alpha 2026-07-29; independently replicated by bravo 2026-07-30, one day
later, on a different agent.

This ritual's own board posts @-broadcast to every agent and carry cross-agent
comparison tables, so the "text names this agent" disjunct — written as a rare
fallback — matches essentially EVERY peer's routine cadence post.

- **alpha**: loose test kept **10 of 18**; honest count **ZERO**.
- **bravo**: loose test kept **15 of 29**; honest count **ONE** (27 of 29 were
  tagged to another agent).

Left unordered, this inflates `self_evolution_signals_count` enough to force
`act_later` on every review forever, and it GROWS — every new comparison table
adds another false match. guard-1877 already carried the rule; the pseudocode
still invited the error, which is why alpha's measurement did not prevent bravo's
near-miss the next day. A guardrail cannot outvote the instrument it guards
(guard-1984).

## The first regression: partner-authored untagged findings (g-115-2922)

zeta review 2026-07-22, before the authorship exclusion existed. Two echo
self-signals (echo-3542, echo-3840) — echo's own untagged sq-012s — were counted
toward zeta's review via the "applies to all" disjunct, inflating
`self_evolution_signals_count` 5→7 and net-divergent 1.0→3.0, flipping zeta's
self-assess from `no_change` to a FALSE `act_later`. Caught by a manual authorship
check and corrected by hand.

The `author != MIND_AGENT` + names-own-purpose test is the fix; a genuinely
agent-agnostic untagged finding is unaffected. Verify-learning guard: a
partner-authored untagged sq-012 MUST NOT count toward another agent's Phase 2.3b
`board_signals`.

## Why the own-receipt shape test had to move ABOVE (a0) — g-115-4087

(b) already carried this shape test, but (a0) short-circuits on the agent tag
before (b) is ever reached — and the tag on a receipt is the AUTHOR'S OWN name.
So for exactly the agent that posted them, the test that would catch them was
unreachable. The ritual then reads its own output as input and the tally grows by
one per fire, converging on a permanent `act_later`.

Measured 2026-08-01 (echo, N=19): of 29 unread `self_evolution` / `self-drift`
findings in 30d, the filter kept 4 as directed-at-echo and **all 4 were echo's own
ritual output** — 2 literal cadence receipts, plus one sq-012 self-signal counted
twice because its own 10-minute correction is a separate post. Honest
partner-authored count **0**; honest novel-signal count **1**; filter said **4**.

Every prior fix in this family (guard-1877; the N=15 `agent:`-prefix fix) tightened
the PARTNER side — the self side had no shape test at all. Line 91's intent is
preserved: a genuine own-authored FOLLOWUP finding does not match these shapes and
still counts.

## Cross-references

- guard-1877 — tags decide before prose; the order is load-bearing
- guard-1984 — a guardrail cannot outvote the instrument it guards
- g-115-2922 — partner-authored untagged sq-012 regression (zeta)
- g-115-4087 — own-receipt shape test placement (echo N=19)
- rb-1279, g-115-1214, g-115-2486 — the board-as-self-evolution-surface lineage
- `.claude/skills/fresh-eyes-review/SKILL.md` Phase 2.3b — consumer
