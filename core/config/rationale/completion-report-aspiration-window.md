# Rationale: The Completion Report's Aspiration Window (Phase 2 step 2)

Referenced from `.claude/skills/agent-completion-report/SKILL.md` Phase 2 step 2.
Explains why the step is a window count only, why it reads the live file as well
as the archive, and why it compares dates instead of timestamps.

## Why it is a window count only

When `since` is null (the "Lifetime" branch at Phase 1) the window filter goes
vacuous and the step degrades into a record enumeration over archive ∪ live,
which no longer reaches the lifetime population. The 2026-08-14T12:56
metric-neutral eviction moved 5,003 terminal goal records out of the live world
queue into each aspiration's `archived_census`, and the evictor DELIBERATELY does
not re-append them to the archive store (its docstring says why), so they survive
only in `.history` blobs.

Measured cc-08 2026-08-15 (g-001-04): enumeration over archive (370 asps) + live
(30 world, 1 agent) = 2,679 completed, against 7,084 done of 9,854 from the
census-folded `--summary` at step 3 — a 4,405-goal gap, all of it invisible to the
enumeration. So lifetime totals come from step 3's `--summary`, which folds the
census, and the enumeration is kept for the WINDOW count, where it stays exact.
Window safety was verified separately: of the 4,972 goals present in the
08-14T12:53 `.history` blob but absent from BOTH live and archive, ZERO have
`completed_at >= 2026-08-13T20:25:22`. Lifetime figures are NOT comparable across
the 2026-08-14 boundary by either method — say that, rather than reporting a drop.

## Why it reads the live file as well as the archive

What each read returns (read from `mind_api/src/endpoints/aspirations.py`,
2026-10-02):

- `--archive` returns the archive file of ONE source: the world archive by
  default, the bound agent's with `--source agent`.
- `--active` and `--active-compact` return rows with `status == "active"` only,
  so a completed row that is still in the live file is in neither.
- `--summary` prints one line per live row whatever its status (`[COMPLETED]` for
  a completed one). `--id <asp-id>` reads one full row, live file first, archive
  second.

So an aspiration that completed in the live file and has not reached the archive
yet is visible to `--summary` and `--id` only. Measured by g-001-04 (foxtrot,
occ 83, 2026-09-30): asp-373 was completed in the live world queue
(`--source world --id asp-373`) and absent from the `--archive` dump, and the
prior report's gather still listed it as active (140 of 162), so it closed inside
the successor window. Re-measured 2026-10-02 (alpha, hostname cc-09,
`uname -r` 6.8.0-142-generic): asp-373 is in the archive (`archived: true`) and
the live summary holds 23 rows, all `[ACTIVE]`, so the lag closes when the row is
archived. The live read costs one call the report already makes (step 3's
`--summary`) and is the only read that sees a close during the lag.

An aspiration id lives in one of FOUR stores: world live, world archive, agent
live, agent archive (rb-9964). The agent lane is the BOUND agent's queue only
(guard-6408); a report about another agent pins `MIND_AGENT=<agent>`. When one
id appears in both the live file and the archive, the archive row is the
disposition of record (rb-8064).

## Why it compares dates

Aspiration-level `completed_at` is date-only on most rows, while goal-level
`completed_at` carries a full timestamp (guard-3690, zeta 2026-09-27: aspiration
level date-only on 330 rows, a full timestamp on 1, absent or empty on 53;
goal level a full timestamp on 2,407 of 2,408 — the same key name in opposite
shapes one level apart). Re-measured 2026-10-02 (alpha, hostname cc-09,
`uname -r` 6.8.0-142-generic) on one `aspirations-read.sh --archive` read, 384
rows, 5,431,168 B: 337 rows are `status: completed`, with `completed_at` date-only
on 330, a full timestamp on 1 and absent on 6; the other 47 are `status: retired`,
none with `completed_at`.

A string compare `completed_at >= since` against a full-timestamp `since`
therefore drops every row that closed on the since-day, because `'2026-09-27'`
sorts before `'2026-09-27T18:25:59'` (the shorter string is a prefix, so it sorts
first). It errs in one direction, toward zero, which reads as "nothing closed".
Re-run over the window opening at `2026-09-27T18:25:59` on 2026-10-02, the old
filter kept 0 of 337 completed rows, although asp-373 had closed on 2026-09-27
(`completed_at` `'2026-09-27'`, `intent_satisfaction.claimed_at`
`'2026-09-27T20:09:48'`). The placement rule in the step kept it as in-window.

The rule is the smallest one that never invents a time. A date-only stamp can be
placed against a full-timestamp `since` only when its date differs from the
since-day, so a date-only row ON the since-day is BOUNDARY and is listed apart
instead of being counted in or out. A timestamped field the row carries can place
it: `intent_satisfaction.claimed_at` exists on 7 of the 337 completed archive
rows, so BOUNDARY is the normal answer on the since-day, not a rare one. The
chain is `completed_at` (full timestamp) → `intent_satisfaction.claimed_at` →
BOUNDARY (guard-4673: a filter on a field with a fallback chain must use the
chain). Rows with `status: completed` and no `completed_at` are UNPLACEABLE and
are counted, not listed by title, because they carry no date at all (6 on
2026-10-02). The step selects on `status`, never on the presence of
`completed_at` (guard-5156).

## Cross-references

- rb-9964 — an aspiration id lives in one of four stores
- rb-8064 — the archive row is the disposition of record on a duplicate id
- rb-12518, guard-3690 — date-only stamp shape and the date-granularity remedy
- guard-6408 — `--source agent` reads the bound agent's queue only
- guard-5156 — read `status`, not `completed_at`
- guard-4673 — a filter on a field with a fallback chain must use the chain
- g-115-11679 — the fix; g-001-04 — the discovery; g-115-9794 (forge goal for
  window-scoped completion-report gathering) — records the same requirement
- `core/scripts/aspirations-read.sh`, `mind_api/src/endpoints/aspirations.py` —
  the read semantics cited above
- `.claude/skills/agent-completion-report/SKILL.md` Phase 2 step 2 — the consumer
