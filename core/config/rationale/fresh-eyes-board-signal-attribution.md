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

## Measured evidence for the (a-pre) receipt filter

Relocated VERBATIM from `.claude/skills/fresh-eyes-review/SKILL.md` Phase 2.3b on
2026-09-03 (bravo, cc-05, g-357-41 iteration). The skill was 66,208 B against a
65,536 B injection ceiling — over the ceiling a skill reaches the model TRUNCATED,
so its later content is silently absent, and the `commit-msg` hot-path size gate
refuses any commit that grows it further. The four measurements below are what the
predicate is DERIVED from; the skill keeps the imperatives and the copyable regex.
Nothing was deleted — read this section before weakening, re-deriving, or
"simplifying" any part of the (a-pre) predicate.

corrected fire. This is the echo-N=19 finding measured verbatim ("one
sq-012 self-signal counted twice because its own 10-minute correction
is a separate post"), which was recorded there and never encoded in
the predicate. Measured 2026-08-16 (bravo N=58, `hostname` cc-05,
`uname -r` 6.8.0-137-generic) over all 97 self_evolution/self-drift
findings unread in 30d: survivors 19 → 17, dropping exactly two, BOTH
ritual corrections — `msg-20260816-165514-bravo-5085` (mine) and
`msg-20260729-170301-foxtrot-5015` (foxtrot's N=7 correction, 18 days
earlier), so this is fleet-wide and long-standing, not one agent's
quirk. Negative control, same run: a substantive non-ritual correction
("CORRECTION: the env-server retry budget is 3, not 5") still
SURVIVES, which is what keeps this a receipt filter rather than a
correction filter — the `[^\n]{0,80}?` leash is what requires the
opening clause to NAME the ritual. Widening measured over the full
live population before adoption, never against the motivating example
(guard-2499).
The sentence directly above says "match the SUFFIXED forms too", which
invites building the suffix INTO the pattern; do not. Measured
2026-08-12 (echo, N=54, cc-03 / Linux 6.8.0-137-generic) on the same
corpus, same run, only the pattern differing: a regex requiring the
suffix (`fresh[- ]eyes[- ](review|code|tree|program)`) dropped **16 of
82**, where the anchored form dropped **70 of 82 (85.4%)** — survivors
66 vs 12, again in the false-`act_later` direction. The bare
`Fresh-eyes <n>-><n>` and `Fresh-eyes N=<k>` shapes named at the top of
this block carry no suffix at all, so a suffix-requiring pattern misses
the majority of the receipts the block exists to catch. Measured
2026-08-01 (alpha, N=21, cc-04, over every self_evolution/self-drift
finding in the 30d window): opening tokens split **287 lowercase vs 40
capital** — `fresh-eyes-code` ×267, `FRESH-EYES-CODE` ×40, `Fresh-eyes`
×27, `FRESH-EYES` ×13, `fresh-eyes` ×12, `fresh-eyes-review` ×4,
`sq-012 tentative` ×4 (lowercase, against the `sq-012 TENTATIVE`
literal above), plus `Fresh-eyes-TREE`, `fresh-eyes-tree`,
`FRESH-EYES-PROGRAM`, `Fresh-eyes-program`. A reader who implements the
shape list LITERALLY — which is what a pseudocode literal invites —
catches ~12% of the receipts it was written to catch.
This was measured BY this defect firing: alpha's own N=14 receipt
(`fresh-eyes N=14 (alpha, cc-04): COMPLIES at 42.7%...`, lowercase f)
survived a literal (a-pre) implementation, reached (a0), matched its own
`alpha` tag, and landed in `board_signals` as a self-evolution signal —
the exact echo-N=19 convergence above, reproduced one day later by the
fix meant to prevent it. The case gap lands PRECISELY on own-authored
receipts, because (a-pre) is the only test that can reach them (see the
next paragraph), so a case miss here is never harmless.
Note the direction, which is why this cannot wait for a tidier fix:
every escaped receipt INFLATES `self_evolution_signals_count`, and
Phase 5.5 reads that count as change-pressure — so the failure always
pushes toward a false `act_later`, never toward a missed one.
**Why this shape test sits ABOVE (a0):** (a0) short-circuits on the
agent tag, and the tag on a receipt is the AUTHOR'S OWN name — so for
exactly the agent that posted them, the test that would catch them was
unreachable, and the ritual reads its own output as input, converging
on a permanent act_later. Measured echo N=19: filter kept 4, all 4
echo's own ritual output; honest partner-authored count 0. Line 91's
intent is preserved — a genuine own-authored FOLLOWUP still counts.
Owned by `g-115-4087`. Rationale:
core/config/rationale/fresh-eyes-board-signal-attribution.md

## Cross-references

- guard-1877 — tags decide before prose; the order is load-bearing
- guard-1984 — a guardrail cannot outvote the instrument it guards
- g-115-2922 — partner-authored untagged sq-012 regression (zeta)
- g-115-4087 — own-receipt shape test placement (echo N=19)
- rb-1279, g-115-1214, g-115-2486 — the board-as-self-evolution-surface lineage
- `.claude/skills/fresh-eyes-review/SKILL.md` Phase 2.3b — consumer
