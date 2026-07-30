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
answer. The honest complication is that **no usable marker exists today**:

- `severity`: present on 284/1398 (20%). 1114 carry none. Only **3** are
  CRITICAL. Case is inconsistent — `HIGH`/`high`, `MEDIUM`/`medium`,
  `LOW`/`low`, `CRITICAL`/`critical`, plus a stray `info`.
- `applies_to`: present on 3/1398 (0.2%) — effectively an rb-only field.

Selecting on `severity == CRITICAL` today would load 3 guardrails and silently
treat 1395 as droppable. So Phase 2 names `severity` as *the* marker — fixing
the mechanism — while stating that it is unusable until populated, and keeps the
100%-coverage index as the floor in the meantime. The floor is not a stopgap
that degrades safety: it drops no entry at all. Populating and case-normalizing
`severity` is separate work, and it is genuinely separate — it is a judgement
pass over 1398 records, not a side effect of this policy change.

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
