# enabled_by id resolution (Phase 4.27 producer + temporal-credit consumer)

Why the Causal Enabler Scan resolves an experience record instead of building
its id, and why the producer and consumer had to be fixed together.
Traceability: g-115-4924 (echo, 2026-08-04), addendum g-335-722 (bravo,
2026-08-05), fixed 2026-08-28 (alpha, cc-07).

## The producer defect

Phase 4.27 built the temporal-credit edge by string-concatenating `exp-` onto a
GOAL id:

    experience_id: "exp-{item_source_goal}"

Experience ids are minted by `experience-archive-goal.sh` as
`exp-<goal-id>-<skill-slug>[-<date>]`, so the constructed id frequently matches
nothing. It is NOT reliably wrong, which is worse than being always wrong: some
records genuinely are named `exp-<goal-id>` (measured: `exp-g-306-284` resolves,
`exp-g-115-5775` resolves), so the construction succeeds intermittently and the
failures look like data gaps rather than a systematic defect.

The edge never errors at WRITE time — `experience-update-field.sh` appends
whatever string it is handed and nothing validates the referent. It fails only on
dereference. **A populated-but-dangling edge is worse than an absent one: absence
is visibly a gap, a dangling id reads as coverage.**

## Corpus census (alpha, cc-07, 2026-08-28)

Over `experience.jsonl` + `experience-archive.jsonl` — 1,837 records, 1,613
distinct ids:

| measure | count |
|---|---|
| records carrying the `enabled_by` KEY | 21 |
| records with NON-EMPTY `enabled_by` | 6 |
| total edges | 7 |
| edges that dereference | 4 |
| edges that DANGLE | 3 (43%) |

Each dangle was confirmed against `experience-read.sh --id` (the consumer's own
reader), with a resolving control alongside so the negative is not a
silent-failure read. The three dangles are three DIFFERENT malformed shapes, not
one — which is why a single-shape fix would have been insufficient:

1. `exp-2026-08-12_c7-low-scores-are-imbalance-not-poor-repertoire` — `exp-`
   concatenated onto a HYPOTHESIS id (`YYYY-MM-DD_slug`), i.e. `item.source` was
   not a goal id at all.
2. `g-115-18` — a bare goal id with NO `exp-` prefix.
3. `exp-g-276-04-wire-stdio` — well-formed but absent from this box's corpus.

**Read the 43% as box-local, not fleet-wide.** bravo measured 3 records with the
key and ZERO non-empty on cc-05 and correctly called that a FLOOR rather than a
census; alpha's store has 6 non-empty. A local read of a synced store enumerates
what this box happens to hold. Shape 3 in particular may be another agent's
record, so "dangles" is a property of the reading box.

## Why the consumer had to be fixed in the same change

`state-update-audit.py cmd_temporal_credit` is the ONLY reader of `enabled_by`,
and it was structurally unreachable. It read the record with
`experience-read.sh --goal`, then bailed on `if not isinstance(exp, dict)`.
`--goal` is a FILTER and returns a LIST **even on a single match**, so the guard
fired on every invocation and the function returned upstream of the dereference.

Measured: `--goal g-306-284` -> list of 25; `--id exp-g-306-284-...` -> dict.

The guard was added under g-240-58 after a synthetic `--goal` with no experience
record returned a list. That reasoning is right for the EMPTY case and wrong for
the populated one — it was written believing a real match returns a dict.

This is a **masking pair, not a coincidence**: a dangling edge only fails on
dereference, and the sole dereference site had been unreachable the whole time.
It also means a producer-only fix is unverifiable — a deep close would emit the
identical "no single experience record (got list)" line before and after, so
there is no observable signal distinguishing a working fix from a broken one.

## The resolution rule (both sides)

Resolve, never construct:

- **Producer (Phase 4.27):** `experience-read.sh --goal {item_source_goal}`,
  take the record whose `created` most closely PRECEDES this execution — that is
  the run that actually produced the enabling item. Where the enabling item
  pre-dates every surviving record for that goal there is no artifact to point
  at, and the correct behavior is an explicit, documented SKIP. A silent skip and
  a fabricated id are indistinguishable to a later reader, which is why the
  pseudocode says so in words.
- **Consumer (`cmd_temporal_credit`):** prefer `--id {experience_id}` (the exact
  record for this execution — `run-all` already computes it for
  `cmd_relative_advantage`, so this only threads an argument that existed), and
  fall back to the newest record from the `--goal` list.

Every shape is handled explicitly rather than by one isinstance test, because a
partially-working shape filter is the dangerous case (guard-2290). Note `--id` on
a miss returns `{"error": "not_found"}` — a dict, which would pass a bare
isinstance check.

## Adjacent, not duplicate

Same family, different defects: g-115-4377 (reflect-on-outcome Step 1 reads
`experience_ref` from ONE agent's store while the pipeline record is shared) and
g-115-4876 (Phase 4.25 writes a dict into `active_context.experience_refs`,
double-nesting it). This file covers the id-CONSTRUCTION half.
