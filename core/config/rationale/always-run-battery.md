# Rationale: Always-Run Lane Battery

Referenced from `.claude/skills/aspirations-precheck/SKILL.md` Phase 0-pre.0e.
Why the five standalone always-run precheck lanes are dispatched by one script
that prints findings only, and why a quiet run is not the same as a clean one.

## Why a battery at all

The five standalone always-run lanes (0.5b.1b inbox-alert-age, 0.5b.1c
user-blocker-escalation, 0.5b.2 dependency-timeout, 0.5b.2b handoff-aging,
0.5g.7 completed-not-closed-drain) emit **22,752 B** of JSON between them on a
quiet iteration — measured cc-07, 2026-08-17. The battery collapses that to
~285 B by printing FINDINGS ONLY: **98%**.

That number is the whole argument, because of where these lanes live.
`aspirations-precheck/SKILL.md` is 172 KB / 2,695 lines / 50 phases, and the
per-iteration skill text the loop is asked to reload is ~631 KB — more than one
context window. Measured on the reducer (cc-04, session ed59f154, 25h):
`Skill(aspirations)` re-entered 52 times, `Skill(aspirations-precheck)` invoked
**12 times (23%)**. After each compaction the loop runs the always-run calls it
*remembers* — two or three — and selects. The rest of the lanes are dark.

So an always-run lane that exists only as an LLM-orchestrated phase inside a
172 KB file is skippable by construction, and no honor-system imperative beats
that economics. A hard gate forcing the 172 KB load every iteration would only
raise the compaction rate. One line that survives summarization, and that
re-derives the full lane set from a registry on every run, is the shape that
actually holds.

## Why the output must re-derive the lane set

The battery prints its lane roster from an in-script registry rather than from
whatever the caller remembered to pass. A lane can therefore never silently
fall out of the protocol: adding a row to the registry adds it to every run and
to every report, and a lane that is *not* in the registry is visibly absent
from a roster the reader can count against the tier table.

`_SENTINEL_DISPATCHED` names the four always-run rows this battery deliberately
does NOT cover (they belong to the sentinel battery, Phase 0-pre.0d), so the
uncovered set is stated rather than left as a gap a reader has to notice.

## Why status and completeness are separate fields

This is the load-bearing design decision, and it is guard-4093:

> a zero with ANY blind lane is UNREACHABLE, not EMPTY.

A battery that collapsed "found nothing" and "saw everything" into one verdict
would report a timed-out or crashed lane as *clean*, which is the precise
failure the aggregation exists to prevent — and it would do so most confidently
on the runs where something is actually wrong.

So the report carries two orthogonal fields:

| field | question it answers | values |
|---|---|---|
| `status` | did I find anything? | `findings` / `clean` |
| `completeness` | did I see everything? | `complete` / `partial` |

and the aggregator branches on `if ANY lane blind`, never `if ALL`. Mutating
that ANY to ALL correctly turns four of the 23 tests RED —
`test_one_reachable_clean_lane_cannot_outvote_a_blind_one` is the
discrimination test, and it exists because ALL-semantics is the mutation a
future editor is most likely to make while thinking they are simplifying.

## Why 0.5g.7 is dispatched but not replaced

The other four lanes are notification lanes: the battery runs them with
`--apply`, the escalation fires, and the finding line IS the dispatch. Phase
0.5g.7 is different. Its obligation is a **per-item LLM disposition**, so the
battery reports `slate=N` — a count — and the phase re-runs
`completed-not-closed-slate.sh` itself to get the rows. A count is not the
rows, and a battery that reported the count as if it had discharged the
obligation would silently retire a lane that requires judgment.

That lane also carries the meter-name-vs-script-name trap: its sweep name is
`completed-not-closed-drain` and its script is `completed-not-closed-slate.sh`.
They are separate fields in the lane spec (`meter_name` vs `script`) precisely
because keying the meter on the script name would silently create a sweep the
meter has never heard of, which the meter would then never drop *and* never
account for.

## Why `--apply` is not optional at the call site

The battery defaults to dry-run so that manual runs and tests cannot fire real
escalations. That default makes the loop's call site load-bearing: invoking it
bare from Phase 0-pre.0e would turn all four notification lanes into no-ops
while still printing a confident `all 5 lanes clean`. The phase body therefore
names `--apply` explicitly, and the tier table's Invocation column mirrors it.

## Why the standalone forms stay in the tier table

Unlike the sentinel-battery rows — whose lanes have no standalone scripts at
all — these five scripts exist independently and are what a blind or
wrapper-failed battery falls back to. Deleting the standalone form from the
Invocation column on the grounds that it "duplicates the battery" would remove
the only recorded fallback for the case the battery itself is designed to
announce.

## Cross-references

- `guard-4093` — a zero with any blind lane is UNREACHABLE, not EMPTY
- `guard-614` — structured output on every path, including failure
- `guard-1224` — no bash-side arg parsing in a thin wrapper; one parser owns
  the flag surface
- `guard-1760` / `guard-1641` — a report that never says what it declined to
  look for reads as coverage; a 0 is ambiguous between counted-zero and
  never-ran
- `g-115-6468` — `iteration-open.sh`, the full entry orchestrator (9 lanes,
  later 39, plus rc table, selection candidates and a NEXT ACTION line) that
  should COMPOSE this battery rather than re-implement its lane registry
- `.claude/rules/rationale-extraction.md` — why this file exists here rather
  than inline in the hot-path SKILL.md
- `.claude/skills/aspirations-precheck/SKILL.md` Phase 0-pre.0e — the consumer
