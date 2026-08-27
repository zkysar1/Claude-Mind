# Rationale: DISTILL Action Routing (the read-cap arm)

Referenced from `.claude/skills/tree/SKILL.md` § "1.75. DISTILL" step 2.a0.
Explains why the read-cap arm routes away from REGROUP, where the 26413-token
floor comes from, and why the split-overcap fork may not be shortened.

## Why these candidates say "regroup" and must NOT go to REGROUP

A node with `trigger == oversized_not_append_grown` carries
`recommended_action: regroup` because it is too big to Read but has too few
dated sections for the rb-2085 archive + keep-newest-N rollup to have anything
to roll up. The label is misleading, and following it is wrong.

REGROUP (§1.5) is DORMANT BY DESIGN and cannot fire on them (g-115-4147,
measured 2026-07-30, alpha, cc-04). REGROUP triggers on `child_count > K_max`,
and `core/config/tree.yaml:9` raised K_max 4 -> 40 by user directive
2026-07-14, stating verbatim: "count-based DECOMPOSE/REGROUP pressure is
effectively retired; regroup on SEMANTIC incoherence only."

Measured consequence: `tree-read.sh --redistribute-candidates` returns `[]`
against the whole 1299-node tree, and all four live arm members report
`child_count: None` — two are LEAVES, which have no children to regroup at
all, so the old routing was categorically wrong for them rather than merely
unmet.

The cost was not theoretical: the arm holds the two MOST-RETRIEVED nodes in the
tree (framework-guardrails-and-gates, 438 retrievals / 30,633 est. tokens;
product-world-model, 247 / 29,535), both past the ~25k Read cap — so the
read-cap arm was silently exempting exactly the nodes the fleet reads most.

## Why the est_tokens floor is 26413 rather than the cap itself

This arm's trigger is PROACTIVE and splitting an under-cap node is a net harm
(guard-2006 / rb-5894). The arm fires at `token_trigger` = 0.8 * 25000 = 20000,
i.e. deliberately BEFORE the cap. That earliness is free for the distill arm
(an early rollup is non-destructive) and NOT free here: structural surgery on
an already-readable node fragments coherent content and buys nothing.

`est_tokens = chars/2.3` is the LOW end of the measured 2.31-2.43 bytes/token
band, so it runs high by up to ~5.7%, and a node reported at 100-106% of cap
may be UNDER it. 26413 is that band's floor (25000 * 2.43 / 2.3) — at or above
it the node is over cap at ANY ratio within the band.

Measured 2026-07-30: of the 4 live arm members, `checker-input-assumption-
defects` (21680 est / 50156 chars) is UNDER cap and must NOT be split; the
other three (27820 / 29535 / 30633) clear 26413 and take the fork.

## Why the fork may not be shortened to "call split-overcap"

Its step 0 is load-bearing: boundaries are INPUT, never inferred. Skipping the
fork and splitting on an inferred boundary buries cross-cutting content in one
shard, which the fork exists to prevent. A leaf with 0 children normally takes
the DISTILL branch, not the split branch.

## Why step 2.a0 exists at all

The read-cap size test fires INDEPENDENTLY of append-grown-ness — that
decoupling is the whole of g-115-4058. Without this routing, the nodes in the
regroup arm would each receive the destructive-shaped procedure that is wrong
for them: the false-positive class the conjunction originally existed to
prevent. A branched action that no caller reads changes no behavior.

## Cross-references
- `.claude/skills/tree/SKILL.md` § 1.75 DISTILL step 2.a0 — the consumer
- `guard-2006` / `rb-5894` — splitting an under-cap node is a net harm
- `guard-1478` — judge the read cap in TOKENS, never bytes
- `rb-2085` — the archive + keep-newest-N rollup these candidates cannot use
- g-115-4058 — decoupled the read-cap test from append-grown-ness
- g-115-4147 — measured the dormant REGROUP arm
- `core/config/tree.yaml` `pruning` — the live threshold values
- `.claude/rules/rationale-extraction.md` — why this file exists rather than
  living inline in the SKILL.md
