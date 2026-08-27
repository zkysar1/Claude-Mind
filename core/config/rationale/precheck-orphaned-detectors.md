# Rationale — the three deferrable orphaned detectors (0.5b.19/20/21, g-115-7871)

Wiring for `self-blocked-defer-sweep`, `phantom-goal-audit`, and
`hardcoded-scope-audit`. All three existed as scripts with no call site: built,
correct, and never dispatched. The pseudocode lives in
`.claude/skills/aspirations-precheck/SKILL.md`; the WHY lives here.

## The invocation shapes are NOT guessable, and the goal that filed this got all three wrong

The filing goal prescribed `--output json` (the shape its deferrable neighbours
0.5b.10-0.5b.18 use). Measured 2026-08-26 (zeta, cc-02, 6.8.0-137-generic)
against each parser BEFORE editing — `rb-538`, verify at the parser whitelist,
never by analogy with a sibling:

| detector | prescribed | actual |
|---|---|---|
| self-blocked-defer-sweep | `--output json` | `--json` (+ `--limit N`); no `--apply` |
| phantom-goal-audit | `--json` | **no `--json` flag**; positional `audit` emits JSON natively |
| hardcoded-scope-audit | `--tier/--json` | `--json` works; `--root` repeatable |

Every one of those wrong forms exits non-zero to stderr, which a batched
`2>/dev/null` loop renders as an empty-stdout all-clear (g-115-6207). That is why
the tier table carries the exact command rather than the script name.

## 0.5b.19 — why the exogenous bands are most of the output

Every defer sweep from 0.5b.3 to 0.5b.15 re-probes an EXTERNAL condition. None
asks whether the defer is waiting on anything external at all. A
`precondition_unmet:` naming an action this agent can take is self-blocked: no
external probe can clear it, so it re-defers forever while every sweep correctly
reports it still-blocked. That is `reclaim-routed-work.md`'s RULE axis mechanised.

First run: population 161, in-scope 140, **4 `self_blocked_candidate`** against 72
`exogenous:locus` and 35 `exogenous:other-goal`. A large exogenous band is the
classifier working, not a backlog. Detective-only: the classifier matched TEXT,
and clearing another agent's defer appropriates their queue (guard-1007,
guard-4794 — a finding is a stimulus, not a verdict).

## 0.5b.20 — why report-only, and why the zero needs `schema_verified`

The script CAN `--apply` a deduplicated Investigate. That flag is deliberately
withheld: the population is normally zero (first run **0 live phantoms of 3016
scanned**), so an auto-filing lane would spend its whole life not firing and then
fire unattended.

Read `schema_verified` before believing the zero (rb-245). First run:
`schema_verified: true, have_created_at: 3004, legacy_null_created_at: 12` — those
12 carry PARTIAL provenance and are explicitly NOT phantoms. A zero beside
`schema_verified: false` is a broken probe, not a clean corpus.

## 0.5b.21 — the silent corpus gap, measured both ways

`source core/scripts/_paths.sh &&` is load-bearing. Without it `$WORLD_PATH` is
unset, the scan drops `world/conventions`, and it returns a real-looking number
over a corpus missing its domain half. Positive control, same box, same minute:

| | bare | with `source` |
|---|---|---|
| verdict | `SCANNED_PARTIAL` | `SCANNED` |
| files_scanned | 2,411 | **2,515** |
| roots_skipped | `world/conventions` | none |

This is the `.claude/rules/path-resolution.md` hazard in a new place: hooks do not
rewrite `world/` prefixes for bash arguments.

The projection in the invocation is not an optimisation — an unprojected `--json`
body is **~144 KB**, more context than every other deferrable lane combined.
Extend the projected key list if a new summary field is needed; never drop it.

First full run: 2,515 files, `active-scope: 13`, unclassified 79,
absence-is-signal 54, prose 251, test-fixture 235. The 13 are real signal for a
separate goal, not scope for the wiring one (implementation-discipline rule 6).

## The insertion hazard this wiring hit

Inserting after 0.5b.18 required repointing BOTH of that phase's successor
pointers; otherwise a meter drop skips the whole new span and the lanes are
orphaned invisibly. Encoded as **guard-5171**.
