# Artifact Reference Integrity — the indirection-vs-rewrite decision

Decision record for D3 (`g-306-101`). Filed by the production operator via the
promotion cycle as one of seven goals sharing a parent pattern: *a durability or
safety property is guaranteed by a mechanism that is only CONDITIONALLY ACTIVE,
and nothing verifies the mechanism is active.*

The question as posed: when an artifact becomes load-bearing, does it get a
concept node in the tree owning the single canonical pointer to it (indirection),
or does reference-repair generalise to rewrite every inbound reference when the
artifact moves (rewrite)? The filing notes correctly that these are in tension
and that building both is waste.

**Decision: neither, as posed. The binary is false — it omits the option this
framework already uses.** Measured on this world 2026-07-31 (echo, `cc-03`).

## The measurements

Inbound references were counted from five referencing surfaces — the knowledge
tree, `world/conventions/`, `core/config/`, `.claude/`, and the append-only
JSONL stores — against two classes of referent.

| class | distinct referents | max N | median N | N≥3 |
|---|---|---|---|---|
| **moving** (`agents/*/temp`, `agents/*/reports`) | 305 | 4 | **1** | 4 (1.3%) |
| **stable** (`world/conventions/*.md`) | 69 | **27** | 2 | 33 (48%) |

N is large exactly where artifacts never move, and N=1 exactly where they do —
283 of 305 moving artifacts carry a single inbound reference.

Dangling-reference harm, after dropping globs, directory refs, and placeholders
(254 real references remain), segmented by whether the referencing surface is
LIVE (tree / conventions / core-config / .claude) or APPEND-ONLY (JSONL stores):

| surface | refs | of which **authoritative here** | dangle (authoritative subset) | of those, **rewrite-recoverable** |
|---|---|---|---|---|
| live-reference | 49 | 15 (echo-owned) | **15 (100%)** | **3 (20%)** |
| append-only | 205 | — | — | — |

"Rewrite-recoverable" means the referent's basename still exists somewhere under
a temp tree — i.e. it MOVED, so a rewrite engine would have had a target.

**Measurement caveat — dangling-ness is BOX-DEPENDENT, and only the
same-box subset is evidence.** `agents/<other>/temp/x` lives on that agent's
box; every other box reads it as absent whether or not it was ever purged. Of
the 49 live-surface references, 34 are owned by other agents and are therefore
UNMEASURED from here — only the 15 echo-owned ones can be judged on this box,
and all 15 dangle. `core/scripts/temp-citation-ratchet.py` reaches the same
conclusion from the other direction and is why this caveat is here: it counts
CITATIONS rather than DANGLING citations precisely because a dangling-count
"would report a different number on every machine." Do not restate a local
dangle count as a fleet fact — this document did, in four places, before the
ratchet's docstring corrected it.

## Why not an artifact-anchor node type

1. **It breaks a closed invariant.** `node_type` lives exclusively in
   `_tree.yaml` and is a binary: *"Every node is exactly one of interior or leaf
   — never both"* (`knowledge-conventions.md`). A third value is a schema break
   that validate and retrieval both depend on. Front matter carries no
   general-purpose node type either — a top-level `type:` key appears on exactly
   1 of 1318 nodes (the 1246 other `type:` occurrences are nested keys inside
   other mappings, not a node type).
2. **The anchor function already exists and is deployed.** A `world/conventions/*.md`
   file IS a named canonical location that many stores reference: measured N=27
   across 5 surfaces for the largest. The filing's own sharper framing — *organize
   the taxonomy around the concept, hang the artifact off it* — describes what a
   conventions file already does. Adding a node type would be a second mechanism
   for a job one mechanism already performs.
3. **At N=1 there is nothing to collapse.** Indirection's benefit scales with N:
   it converts N edits-on-move into 1. For the artifacts that actually move, N is
   already 1. The migration cost is real and the benefit is zero.

## Why not a generalised rewrite-on-move engine

On the subset that can be judged from this box, a rewrite engine addresses
**3 of 15** live dangling references. The other 12 point at artifacts that were
**deleted, not moved** — there is no new path to rewrite to. So purge outnumbers
movement 4:1 as the cause, and rewrite-on-move is aimed at the minority cause.
The ratio, not the absolute count, is what carries the argument — and the ratio
is measured on references whose referents this box actually owns.

`core/scripts/tree-inbound-ref-fix.py` already covers the case it was built for
(tree→tree body refs, `.md`, `--reparent`-triggered). Those three limits are
real, and immaterial to the measured harm.

## What the framework already does: FOLDING

`temp-store.md` records the precedent at scale: ~26 tree-node citations that
resolved to on-disk archive files were **folded** — the dangling pointer was
removed, the essential detail was inlined into the citing node, and the source
was marked as recoverable from git history. Neither indirection nor rewrite:
the reference is *dissolved* rather than chased or redirected.

Folding is the correct default for staging artifacts specifically, because it
matches their contract. A temp working doc exists to have its reusable value
encoded into a durable store; once drained, a citation should point at the
encoded knowledge, not at the staging file that carried it. A live surface citing
a temp path is usually a citation that was never folded — not a pointer that
needs a better addressing scheme.

## Scope: append-only stores are excluded

195 of the 254 references live in append-only JSONL stores. A goal description
that cited a since-purged temp file is an accurate record of what was true when
it was written. Rewriting it would corrupt history, and the never-delete /
append-only rules forbid it. Only LIVE reference surfaces — where a reader
arrives by grep expecting a current answer — are in scope for any repair
mechanism. This is the same access-pattern argument `guard-1606` makes about
reference documents.

## Consequences

- **D4 (`g-306-102`) does not proceed as scoped.** The generalised
  rewrite-on-move engine is not built. D4 narrows to its own stated MINIMUM
  VIABLE SUBSET, re-aimed from move-time to delete-time: a reference CHECK that
  warns (or refuses) when purging an artifact carrying ≥1 inbound reference from
  a live surface. That mechanism addresses the purge cause (12 of 15 here)
  rather than the movement cause (3 of 15).
  **Check first whether this is already built**: `core/scripts/temp-citation-ratchet.py`
  (g-115-3946) already guards the WRITE side — it counts new citations into
  purgeable directories across tree / reasoning-bank / guardrails. What it does
  NOT do is gate the DELETE side: `/drain-temp` Phase 1.5 purge has no reference
  check wired into it. Scope D4 to that gap alone, not to a fresh detector.
- **The residual defect is retention, not addressing.** Most live dangles
  exist because a referenced artifact was purged while live references still
  pointed at it. That is the parent pattern verbatim, and it belongs to D5
  (assert the property: every temp file has ≥1 durable copy) and D7 (audit the
  fleet for conditionally-active safety mechanisms) — not to reference repair.
- **No new node type, no new store, no schema change.**

## Cross-references

- `core/config/conventions/temp-store.md` — the folding precedent; temp lifecycle
- `core/config/conventions/learning-routing.md` — where a drained artifact's value goes
- `core/scripts/tree-inbound-ref-fix.py` — the existing, deliberately narrow repairer
- `guard-1247` — grep-and-repoint discipline when retiring/moving a tree node
- `guard-1606` — reference-doc readers arrive by grep at one row; corrections must be inline
- `guard-1710` — a half-applied correction is the worst state (why partial migration was rejected)
