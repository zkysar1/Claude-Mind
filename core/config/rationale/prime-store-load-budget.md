# Rationale: Prime Store Load Budget

Referenced from `.claude/skills/prime/SKILL.md` Phase 2. Why the guardrail and
reasoning-bank loads are bounded indexes with on-demand expansion, rather than
the full loads Phase 2 used to mandate.

## Why "still load all" was never executable

Phase 2 previously said, of guardrails: *"IF count > 30: note overflow but still
load all (guardrails are safety-critical)."* The reasoning bank carried the same
shape. Measured 2026-07-27 (g-115-3407):

| load | records | bytes | ~tokens |
|---|---|---|---|
| `guardrails-read.sh --active` | 1398 | 2,659,872 | ~665k |
| `reasoning-bank-read.sh --universal` | 4123 | 10,789,367 | ~2.7M |
| **combined mandate** | | **13.4 MB** | **~3.4M** |

Against a 600k window, guardrails ALONE are 5.6x over; the combined mandate is
~22x over. So the instruction was not "expensive" — it was **impossible**, and
had been for some time. Whatever actually happened at prime time was a silent
truncation nobody chose and nobody measured. That is the real defect: not the
cost, but that the policy asserted a coverage guarantee the machine could not
honor, so no reader could tell which guardrails were actually in context.

The growth is not incidental. Guardrail adds vs retires: **1411 adds, 13
retires — a 0.92% lifetime retirement rate**, with zero dated retires in any
month. Monthly adds: 216 (Apr) / 215 (May) / 222 (Jun) / **757 (Jul)** — a 3.4x
acceleration. A store that only grows will re-break any fixed budget, so the
policy has to be a budget with an expansion path, not a bigger number.

## Why an index instead of a ranked slice

The obvious fix is "load the top N." It is the wrong fix here, for two
independent reasons that happen to agree.

**Ranking by utilization is measuring the wrong thing.** `times_active` and
`utilization_score` v1 are *cumulative* counters. guard-841 and rb-1824 both
say plainly: do not use them as retirement, weakness, or selection signals. A
passive always-on rail accumulates fires precisely because it is always on —
guard-054/104/120 carry 1900–2000+ lifetime fires — so a cumulative-count
ranking reads the quietest, most load-bearing rails as the most important *and*
the newest safety-critical entry as the least. Selecting what to load is a
selection signal. The counter cannot serve it.

**A slice of a safety store fails silently.** If Phase 2 loads 30 of 1398
guardrails, the other 1368 are not "deprioritized" — they are absent, and
nothing in context says so. The agent then acts inside their trigger zones
believing it has been primed. That is strictly worse than the honest failure of
loading nothing.

`--summary` avoids both. It is 160 KB (~40k tokens) and covers **100% of active
guardrails — 1398/1398, verified by set-difference against the store**, not
sampled. Every guardrail is present by id and category. The compression comes
entirely from truncating rule *bodies*, not from dropping *entries*.

## Why the index must be treated as unread

This is the part that is easy to get wrong, and guard-1421 measured it across
all active guardrails: a guardrail's rule puts the trigger up front and the
actionable requirement *after* it, so a fixed-width slice reliably shows the
topic while cutting the instruction. The imperative is visible within the first
95 chars only **15%** of the time, and within 200 chars only 30%. `--summary`
lines run ~114 bytes including the `guard-NNN: [category] ` prefix, so they sit
squarely in that failure band.

The consequence is specific: a truncated entry is worse than an omitted one,
because the reader *recognises it as relevant* and still acts wrongly. So the
index buys exactly one thing — knowing that a rule exists and roughly what it is
about — and the spec has to say so in those words. Anything the agent is about
to act inside gets expanded in full via `--id` or `--category`. That is
guard-1421's mode (a), and it is why Phase 2 says "treat the rule text as
ABSENT, not as read."

## Why the always-load core needs an explicit marker it does not yet have

The goal asked which guardrails must always load regardless of budget, and
correctly anticipated that this should be an explicit tier marker rather than a
count threshold. Agreed, and the consult above independently forces the same
answer. The honest complication was that **no usable marker existed**:

- `severity`: present on 284/1398 (20%) as measured 2026-07-27. 1114 carried
  none. Only **3** were CRITICAL. Case was inconsistent — `HIGH`/`high`,
  `MEDIUM`/`medium`, `LOW`/`low`, `CRITICAL`/`critical`, plus a stray `info`.
- `applies_to`: present on 3/1398 (0.2%) — effectively an rb-only field.

Selecting on `severity == CRITICAL` then would have loaded 3 guardrails and
silently treated 1395 as droppable. So Phase 2 names `severity` as *the* marker
— fixing the mechanism — while keeping the 100%-coverage index as the floor
until the field is usable. The floor is not a stopgap that degrades safety: it
drops no entry at all.

### Case is now canonical (g-115-3573, 2026-08-02)

Re-measured at execution on a corpus that had grown 49% in six days — 2123
records, 2084 active, `severity` populated on 529 and **missing on 1594 (75%)**.
408 records carried a non-canonical spelling. All 408 were normalized via
per-record fenced `guardrails-update-field.sh` writes.

Per-record rather than one `locked_modify_jsonl` rewrite, deliberately:
guard-832 prefers fenced per-record updates over a large bulk write whenever a
stable daemon window cannot be guaranteed, and five agents were active within
the preceding fifteen minutes. The `bulk-retire-dead-entries.py` precedent uses
the bulk primitive, which makes it the tempting pattern and the wrong one here.

Verified per guard-1706 (pre/post line counts equal at 2123, delta 0, so no
concurrent append was clobbered; zero per-line parse failures) and per guard-832
(durability confirmed by the owncloud sync-manifest baseline md5 matching the
live file, not merely by a post-write re-read).

Canonical set is **`CRITICAL` / `HIGH` / `MEDIUM` / `LOW`**, uppercase, matching
the `Priority Values` vocabulary CLAUDE.md already declares for this identical
ordinal space. No code reader constrains the choice — `store_registry.py` lists
`severity` as an allowed field with no server-side logic, and the only consumer
is Phase 2's prose — so the tie is broken by not inventing a second case
convention for a value space that already has one. The stray `info` (guard-1021,
a push-failure diagnostic rather than an ordinal rail) mapped to `LOW`. Post
state: HIGH 219 / MEDIUM 295 / LOW 12 / CRITICAL 3, zero non-canonical.

### The CRITICAL admission rule

Case normalization makes the field *parseable*; it does not make the tier
*usable*, because 75% of the corpus is still unmarked. What follows is the
admission rule the population pass must apply — written here so Phase 2 can
point at it rather than leaving each rater to invent a bar.

A guardrail is **CRITICAL** when BOTH hold:

1. **The harm outlives the loop.** Violating it produces damage the agent
   cannot detect and repair on a later iteration — irreversible data loss,
   corruption of a live/production environment, or a constitutional breach.
   This is the discriminator against HIGH: violating a HIGH rail typically
   costs a cycle, and the loop recovers. Nothing recovers a deleted store.
2. **The trigger zone is not self-announcing.** The danger arrives during work
   that looks ordinary, so the agent will not think to expand the relevant
   category first. A rail you would inevitably pull via `--category` before
   acting (because the work itself names the subject) is already covered by the
   index plus on-demand expansion, and does not need always-load status.

Explicitly NOT admission criteria: how important the rule feels, how often it
fires, or any utilization counter. `times_active` and `utilization_score` v1 are
cumulative, and guard-841 / rb-1824 disqualify them as selection signals for
exactly this decision — a passive always-on rail accumulates fires *because* it
is always on.

The three pre-existing CRITICALs are the calibration set, and all three satisfy
both clauses: guard-813 (promotion cycle is mandatory — a skipped stage ships
unvalidated framework to production), guard-939 (archive before any destructive
data operation — the deletion is not undoable), guard-1243 (never write
git-tracked source into a designated write-prohibited upstream environment). In
each, the moment of danger looks like routine work — clause 2 doing its job.

Clause 2 is what keeps the tier small by construction, and that is the point: a
CRITICAL tier that grows to a few hundred entries has become a second budget
problem rather than a solution to the first one. If a population pass produces
a large CRITICAL set, the rule was applied too loosely, not the corpus
discovered to be unusually dangerous.

Populating `severity` across the remaining 1594 records against this rule is a
judgement pass, not a mechanical migration, and remains separate work.

## Reasoning bank: what was given up, said out loud

The old text justified `--universal` as guaranteeing that cross-domain lessons
surface "regardless of the agent's current focus." That guarantee was real and
is now gone, because it cost ~2.7M tokens — `--universal` matches 4123 of 5185
entries (79% of the store), so it never was a budget, it was the store with a
filter that filters almost nothing.

The replacement is `--recent` (498 entries, ~9k tokens) plus Phase 3's existing
category-scoped `retrieve.sh --depth` (DEPTH_LIMITS: shallow 15 / medium 30 /
deep 50) — the budgeted mechanism prime already used for tree nodes. Recency
plus category-relevance is a weaker guarantee than "all universals present," and
Phase 2 says so rather than implying continuity. A weaker guarantee that runs
beats a stronger one that silently truncates.

**Flag footgun:** `--universal --summary` does not compose. `--universal` wins
and `--summary` is silently ignored, so a caller reaching for a bounded universal
index gets the full ~2.7M-token JSON load with no error and no warning (verified
2026-07-27: 10,804,345 bytes, vs 10,789,367 for `--universal` alone). Anyone
extending this policy will reach for that combination first; it does not work.

## Cross-references

- guard-1421 — a truncating survey display leaves the entry UNREAD; the two
  honest survey modes (id+title only, or fewer entries read whole)
- guard-841, rb-1824 — cumulative counters (`times_active`, `utilization_score`
  v1) must not be used as retirement / weakness / selection signals
- `.claude/rules/rationale-extraction.md` — why this reasoning lives here rather
  than inline in a size-budgeted spec
- `.claude/skills/prime/SKILL.md` Phase 2 — the consumer
- `core/config/memory-pipeline.yaml` → `reasoning_bank_routing` — the routing
  the `--universal` guarantee came from
