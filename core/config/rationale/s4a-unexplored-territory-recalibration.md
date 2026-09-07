# Rationale — S4a unexplored-territory recalibration (g-115-3996)

Referenced from `.claude/skills/aspirations-strategic-scan/SKILL.md` Phase S4a.
Why the predicate moved from a cross-namespace set difference to subtree staleness,
and why the two obvious alternatives were rejected on measurement.

## What was wrong

S4a computed `unexplored = all_L2_cats - explored_cats`, where `all_L2_cats` holds
TREE NODE KEYS and `explored_cats` holds free-text GOAL CATEGORY strings. Those are
different namespaces, so the difference measured vocabulary mismatch rather than
exploration. It flagged a near-constant supermajority and therefore carried no
information:

| date | box | reading |
|---|---|---|
| 2026-08-11 | zeta, cc-02, 6.8.0-136-generic | 57 of 65 = **88%** |
| 2026-09-06 | echo, cc-03, 6.8.0-138-generic | 60 of 72 = **83.3%** |

Same non-discriminating signature `g-115-1410` removed from S2a (93%) and S2b (96%);
S4a was left out of that pass. Under `max_signals_per_scan: 10` a permanently-firing
LOW signal also crowds real ones out of the cap.

## The namespaces are PARTIALLY disjoint, not fully — and that matters

The originating description says the two vocabularies "only coincide by accident."
Measured 2026-09-06, they coincide **12 of 72 times (17%)**: `ayoai-core-engine`,
`ayoai-game-integration`, `ayoai-operator`, `ayoai-platform-services`, `ayoai-web-app`,
`daemon-only-architecture`, `fleet-topology-roster`, `npc-intelligence`, `performance`,
`self-program-evolution`, `system`, `system-constraints-loop`.

That partial overlap is why the defect survived review for so long: a reader spot-checking
a handful of keys can find real matches and conclude the comparison is sound.

## Why token-matching was REJECTED (the tempting fix)

The description's option (b) was to map goal categories to tree keys. The cheapest form
is token overlap, and it fails in the opposite direction. Measured: **50 of the 60**
flagged keys (83%) share at least one token with some goal category, so a
`>=1 shared token` rule would flag almost nothing. Inspect the matches and they are
mostly spurious:

| flagged key | "matched" category | shared token |
|---|---|---|
| `ayoai-marketing-site` | `ayoai-operator` | `ayoai` |
| `cold-snapshot-recovery-layer` | `agent-config-override-layer` | `layer` |
| `ci-failure-alert-routing` | `capability-routing-enforcement` | `routing` |
| `ayoai-architecture` | `framework-architecture` | `architecture` (genuine) |

Trading an 83% false-POSITIVE rate for an ~83% false-NEGATIVE rate is `guard-2499`
exactly: it converts a VISIBLY broken detector into an APPARENTLY fixed one, retiring
the symptom that would have prompted the next investigation. A hand-maintained explicit
mapping avoids the spurious matches but adds an artifact that must be kept in sync with
two moving vocabularies, and a stale mapping masquerades as unexplored territory — the
failure mode the description itself warns about.

## What shipped: ask the question in the tree's own namespace

S4a's stated purpose is "identify tree areas that have zero or minimal recent work."
The tree already records `last_updated` per node, so the question needs no second
vocabulary at all: **an L2 subtree is unexplored when no node anywhere beneath it has
been updated within the window.** No goal categories are read, so outcome 1 of
g-115-3996 ("no longer differences tree node keys against goal category strings") is
satisfied structurally rather than by tuning.

Measured on the live tree (echo, cc-03, 6.8.0-138-generic, 2026-09-06, 1576 nodes /
72 L2 roots):

| window | flagged | share |
|---|---|---|
| 30d | 22 | 30.6% |
| 60d | 16 | 22.2% |
| 90d | 13 | **18.1%** |
| 120d | 6 | 8.3% |
| 180d | 0 | 0.0% |

Every window is a minority, and the ranking is legible: at 90d the flagged set is
research and competitive-analysis material nobody has revisited in months
(`indie-ai-launch-strategies` 149d, `dave-mark-iaus-theory` 141d, `game-ai-monetization`
128d, `nvidia-ace-competitive-analysis` 123d). That is a real answer to the question the
phase asks.

Default window is `3 x knowledge_staleness_days` (90d here) rather than a new required
config key. It DEFAULTS rather than raising for the `guard-4653` promotion-coupling
reason the sibling temp-pressure check documents: S4a is a LOW observational signal, so
a lagging config on a promoted box must degrade to a working default, not brick the
whole S4 phase.

## The traversal trap — walk DOWN, and positive-control it

`tree-read.sh --summary` node records carry `children` but **no `parent`**. A
parent-chain walk therefore returns nothing for every node below depth 2, and the
subtree of each L2 root collapses to the root itself. That failure is silent and
produces plausible numbers: the first measurement of this fix read
"30 of 72 flagged at 60d" with **every one of the 72 subtrees having size 1** on a
1576-node tree — self-refuting only if you print the sizes.

So the predicate walks down via `children`, and the phase asserts a positive control:
distinct nodes reached from the L2 roots must equal the node count. Corrected run:
max subtree 1576 (the root), median 4, 1576 of 1576 reached, 34 genuine singletons.

## Scope — what this does NOT fix

Per `rb-1791` (scope a calibration fix to the axis it restores), this touches the S4a
axis only. The S4b limb was recalibrated separately by `g-115-3853` on 2026-08-30 and
measured healthy in the same run that produced the numbers above: 15 of 78 mature
entries qualified (19.2%), top `rb-2264` at `utilization_score_v2` 0.0208. `g-115-4840`
still owns collapsing the duplicate S4a/S4b goal pile — this recalibration removes the
defect those goals describe but does not close them.

## Cross-references

- `g-115-3996` — this goal; `g-115-1410` — the S2a/S2b calibration that is the template
- `g-115-3853` / `core/config/rationale/s4b-cross-pollination-recalibration.md` — sibling limb
- `g-115-3246`, `g-115-4600`, `g-115-5435`, `g-115-4537`, `g-115-4840` — the duplicate pile
- `guard-2499` — a quiet detector reads as a fixed one; `guard-3830` — a count travels
  with its population; `guard-1984` — edit the passage, do not file a guardrail about it
- `rb-1791` — scope calibration-fix verification to the axis the fix restores
