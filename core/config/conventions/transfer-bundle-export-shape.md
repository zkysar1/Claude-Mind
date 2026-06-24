# Transfer-Bundle Export Shape (OKF-Aligned)

The export/import contract for `meta/transfer/` bundles — the unit by which an
agent ships WHAT it has learned (portable knowledge) to another agent, org, or
environment, decoupled from the HOW (the source environment's code and infra).
This file defines the SHAPE a bundle must take; it is NOT a producer or consumer
spec. No producer or consumer exists yet — this is forward-compatibility
insurance so the first one targets a stable shape and the format does not drift.

## Why a documented shape now

`meta/transfer/` is currently an unused stub. Defining the export shape before
any producer lands costs one convention file and avoids the expensive
alternative: a producer inventing an ad-hoc shape, a consumer hard-coding to it,
and a painful migration when the second producer disagrees. This file is the
record of the contract; the decision to adopt it was made deliberately (see
Cross-references).

The shape is aligned to **OKF (Open Knowledge Format)** — a vendor-neutral
external spec for representing knowledge as plain markdown files with YAML
frontmatter, with no schema registry, no central authority, and no required
tooling. The knowledge tree already converged independently on the same shape
(one `.md` per concept, YAML frontmatter, git-tracked, `cat`-readable), so
aligning the transfer boundary to OKF makes exported knowledge portable across
any OKF-aware reader at near-zero internal cost. Alignment means borrowing the
*contract*, not binding to the spec's draft field names (see Caveats).

## The contract

A transfer bundle is portable when it honors these invariants. They are stated
as a contract on the SHAPE, not as a field schema — a producer is free to choose
field names; a consumer must not break when it sees names it does not recognize.

1. **Bundle = the unit of distribution.** One bundle is a self-contained,
   hierarchical directory of concept documents. It is git-shippable as a whole:
   if you can `git clone` it you can ship it, and if you can `cat` a file you can
   read it. No proprietary store, no database, no required runtime.

2. **Concept = one markdown document with YAML frontmatter.** Each transferable
   unit of knowledge — a learned pattern, a verified threshold, a reasoning
   lesson, a strategy — is exactly one `.md` file, human- and agent-readable as
   plain text.

3. **Exactly one REQUIRED frontmatter key: a type discriminator.** Every concept
   carries one short string that a consumer routes, filters, and presents on.
   This is the only field a consumer may assume is present; everything else is
   optional.

4. **Consumers MUST preserve unknown keys.** A producer MAY add any custom
   frontmatter keys it needs. A consumer that does not recognize a key MUST carry
   it through unchanged rather than drop it. This single rule is what lets a
   bundle survive schema drift across producer/consumer versions — it is the
   load-bearing forward-compatibility guarantee.

5. **Consumers MUST tolerate missing optional fields and unknown type values.**
   Import never hard-fails on an absent recommended field or an unfamiliar type
   value; unknown is routed to a default, not rejected.

6. **Links are bundle-relative and may dangle.** Cross-references between
   concepts use ordinary markdown links resolved within the bundle. A broken link
   is a frontier marker (knowledge the bundle references but does not yet carry),
   not a validation error — import MUST NOT fail on one.

7. **Optional progressive-disclosure index.** A bundle MAY include a
   per-directory index listing what is available before a reader opens each
   document. Its absence is not an error.

## What a bundle is NOT

- **Not the internal knowledge representation.** The knowledge tree keeps its own
  richer invariants (structural validation, dedup, the capability ladder). A
  bundle is a PROJECTION of knowledge across an export/import boundary for
  portability — deliberately leaner. Importing a bundle MUST NOT require the
  internal store to weaken its invariants to "conform"; the consumer maps the
  lean bundle shape onto the richer internal shape on its own terms.

- **Not a producer or consumer.** This file does not say how to build a bundle
  from internal state, nor how to merge an imported bundle into internal state.
  Those are separate specs for whoever builds the first producer/consumer.

- **Not a field-by-field schema.** Naming exact frontmatter fields here would
  hard-couple every future bundle to today's draft vocabulary. The contract is
  the SHAPE (invariants 1-7); field names are the producer's choice.

## Caveats

- **Borrow the ideas, not the draft field names.** The aligned external spec is
  an explicit DRAFT — a starting point, not a finished standard. Binding to its
  current field names would couple the format to a moving target. Honor the
  contract above; let the producer pick concrete names.

- **Interchange alignment must not regress internal invariants.** The internal
  knowledge store's stronger guarantees are NOT relaxed to match the lean bundle
  shape. The bundle is a boundary format; the tree remains the source of truth.

## Conformance (minimal)

A bundle conforms if: (1) every concept `.md` has parseable YAML frontmatter,
(2) every frontmatter carries a non-empty type discriminator, and (3) any
reserved filenames (e.g. a directory index) follow the bundle's own structure.
A consumer conforms if it accepts missing optional fields, unknown type values,
unknown keys (preserved, not dropped), broken links, and missing indexes.

## Cross-references

- `world/knowledge/tree/intelligence/research-analyst-findings/open-knowledge-format-okf.md`
  — the OKF prior-art node and the GO decision (Item 3) this convention
  implements.
- `intelligence/ayoai-architecture/universal-environment-abstraction` — the
  portability thesis ("copy WHAT an approach learned, not HOW") the export shape
  serves.
- `meta/transfer/` — the bundle root this shape governs (currently a stub; the
  first producer targets this contract).
