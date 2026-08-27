# Rationale: The Third-Class Watermark Gate

Referenced from `.claude/skills/drain-temp/SKILL.md` Phase 2.5. Explains why the
stamp gate is a three-step transaction rather than a pre-check, and why the
retraction restores a prior value instead of deleting the marker.

## What the watermark licenses

`agents/<agent>/temp/.drain-watermark` asserts: *a completed drain pass
enumerated everything present at least 60 minutes before this drain finished.*
That assertion is what permits `temp-drain-purge.sh` Lane 1 to mechanically
delete THIRD-CLASS files — suffixes that are neither drainable nor enumerated
ephemera (`.jsonl`, `.note`, `.patch`, `.desc`, `.eml`, `.tsv`, `.remote`,
`.local-preserve-*`) — when they are older than the stamp.

The assertion is **structurally false for exactly those suffixes**. Phase 1's
census is `ls temp/*.md temp/*.json`, so a third-class file is never enumerated
by the pass whose completion stamps the marker. The two halves of Phase 2.5 were
written against different populations; the gate is what reconciles them.

## Why the check must run AFTER the stamp

A pre-stamp `--dry-run` reads `would_purge` under the **OLD** watermark. But the
hazard is that stamping is precisely what CHANGES that value — so a pre-stamp
reading predicts nothing about the state the stamp creates. The check tested the
wrong state and passed on the exact case it was written to catch.

The step read "Run this check FIRST" from 2026-08-22 to 2026-08-23, one line
above its own measurement recording that stamping moved the value `0 -> 31`.
The contradiction was visible in the paragraph and still shipped: a gate that
names its hazard can still be wired to a reading taken before the hazard fires.

Two measurements, opposite boxes, same mechanism:

| date | box | pre-stamp | post-stamp | what it would have destroyed |
|---|---|---|---|---|
| 2026-08-22 | alpha, cc-04 | (not read) | 31 | `experience.jsonl.local-preserve-20260818`, `vanished-goals-recovery-2026-08-20.jsonl`, `experience.jsonl.remote`, an unapplied `npc-hours-fix.patch` |
| 2026-08-23 | zeta, cc-02 (`uname -r` 6.8.0-137-generic) | **0 — gate PASSES** | **3** | `coord720.jsonl` (5.8 MB), `board.jsonl` (2.5 MB), `bf.jsonl` (737 KB) |

The 08-23 row is the one that indicts the pre-check specifically: the gate as
written returned 0, licensed the stamp, and the stamp then condemned three
board/coordination captures that no `*.md`/`*.json` census had ever seen. Both
rows are archive-before-delete violations produced by following the step
literally.

## Why this is guard-2590's rule, not a new one

guard-2590: *evaluate the predicate as written (must FAIL) and again with the
cutoff moved (must PASS) — one arm alone cannot distinguish a gate that refuses
from a gate that cannot fire.* A stamp-then-recheck-then-retract sequence IS the
two-sided evaluation applied to this gate: the stamped arm reveals what the
license covers, the retracted arm confirms the retraction worked. Neither arm is
informative alone.

## Why retraction restores rather than deletes

`rm -f` on the marker is safe in the sense that matters most — it removes a
LICENSE, never data — but it drops `watermark_source` to `absent` and discards a
still-valid EARLIER stamp. An older watermark is strictly more conservative than
a newer one (it licenses a smaller set) and strictly more useful than none, so
the correct retraction copies the saved prior value back. `rm -f` remains the
right move only when there was no prior marker to restore.

Retraction must itself be verified: re-run the dry-run and confirm `would_purge`
returned to 0. An unverified retraction is the same class of error as the
unverified stamp.

## Cross-references

- guard-2590 — two-sided proof; the general form of this gate's fix
- guard-1529 — a gate comparing a PERSISTED marker against LIVE state must
  handle the marker's own mutation, not merely the marker-missing case
- guard-4864 — the 2026-08-22 measurement that motivated the gate's existence
- guard-1984 / rb-7613 — why the correction lives in the instrument (Phase 2.5)
  and only the WHY lives here; a same-day pass had already refused this stamp
  and recorded it in an outcome_note, where the next executor never read it
- `.claude/rules/archive-before-delete.md` — the protocol both incidents violate
- `core/scripts/temp-drain-purge.sh` — `_purge_find_predicate` header is the
  SSOT for which suffixes are third-class
- `core/config/conventions/temp-store.md` § The third-class watermark
