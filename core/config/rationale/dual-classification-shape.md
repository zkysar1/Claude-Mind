# Rationale: dual_classification Storage Shape

Referenced from `.claude/skills/reflect-on-outcome/SKILL.md` Step 2.5 and
`.claude/skills/review-hypotheses/SKILL.md` Mode 1 Step 4.1. Explains why the
consumer reads TWO shapes and why the FLAT one is canonical.

## Why there are two shapes at all

Two writers, and they do not have the same expressive power.

- **`/review-hypotheses` Mode 1 Step 4.1** composes a whole record at RESOLVE
  time, where a nested object is natural, and writes
  `process_score: {dual_classification, process_quality}`.
- **`/reflect-on-outcome` Step 7.6c** updates an existing record at REFLECT time
  via `pipeline-update-field.sh`, which **rejects dotted paths**
  (`dotted_field_rejected`, g-001-08, 2026-07-12). It therefore *cannot* write
  the nested shape even if it wanted to, and emits flat top-level fields.

They agree on the VALUE — both call `reflect-bookkeeping.sh dual-classification`
— but not on the LOCATION. "Both writers agree by construction" in the
review-hypotheses comment refers to the value only.

## Why FLAT is canonical

It is the shape both writers can express. Adopting it costs one reader change;
adopting nested would require giving Step 7.6c a path the script does not
support. New writers emit flat; reading both is a compatibility affordance for
the legacy nested records, not an invitation to keep producing them.

## What the split actually cost

Measured 2026-08-09 (bravo, cc-05) over all 81 `stage=resolved` records:

| shape | count |
|---|---|
| nested only (`process_score.dual_classification`) | 25 |
| top-level only (`dual_classification`) | 15 |
| both | 0 |
| neither | 41 (35 already `reflected=true`) |

Before the reader was widened, Step 2.5 read only the nested path, so the 15
top-level-only records fell to its "not yet computed" branch and were recomputed.

**Not a correctness defect — a persistence/audit one.** The value was never
wrong, because the fallback re-derives it. What was lost is the persisted audit
trail, and that trail is consumed analytically: the aspirations-meta
`calibration_finding` segments directly on this field. A record carrying neither
shape is indistinguishable from a genuinely unclassified one, so any such
segmentation is sample-biased in a way the reader cannot see.

The discriminator that rules out "it gets written at reflect time anyway": 50 of
75 REFLECTED records lack the nested shape. Reflection demonstrably does not
backfill structure — exactly as Step 4.1's own warning says ("Missing fields
here = missing fields forever").

## What was deliberately left out of the reader fix

The **41 records carrying neither shape**, and the `stage=archived` corpus
(~90% of scoreable records) which has never been measured for this field. Both
fields are pure functions of `(outcome, confidence)`, so they can be recomputed
exactly rather than guessed — but only via
`reflect-bookkeeping.sh dual-classification`, never by hand-evaluating the branch
table (guard-1604: two hand-written records said `unlucky_corrected` where the
script returns `deserved_corrected`, which silently suppressed preventive-guardrail
extraction in the direction that exonerates the process).

Sizing a backfill against the resolved stage alone would size it against ~10% of
the population, and a resolved-only slice of this corpus has inverted a verdict
before (reflect-on-self Calibration, 2026-08-24). Owned by **g-115-8480**:
measure archived first, then decide whether the backfill is worth running.

## Cross-references

- `guard-1604` — never hand-evaluate the dual-classification branch table
- `guard-5118` — ask what the READ SURFACE CAN SEE, not just whether it is populated
- `g-115-5538` — the reader fix; `g-115-8480` — the census + backfill slice
- `core/scripts/reflect-bookkeeping.py` — the canonical computation
