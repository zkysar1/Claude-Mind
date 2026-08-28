# Rationale: sq-013 Work-Discovery Relay (and why its dedup must reach terminal statuses)

Referenced from `.claude/skills/aspirations-spark/SKILL.md` (Worker Spark Replay
block). Explains why a work-discovery relay files a GOAL from the reducer's
replay, and why the dedup that runs before that filing must scan completed and
skipped goals rather than open ones only.

## Why work-discovery relays file goals here

(2026-08-16, goal-completion audit D1.) worker-loop's filing ruling (g-306-250,
Case B) tells a worker that finds NEW SCOPE any Body could observe to "relay via
spark_capture — the relay loses nothing but time". Until this block existed that
was false: the five spark handlers make rb / guardrail / gotcha artifacts and
NONE of them files a goal, so a Case-B relay arrived as a lesson and never as
WORK.

Workers now execute most units (7 SIDs vs 1 reducer on alpha, 2026-08-16), so
this was the largest hole in "completion seeds the next goal" — guard-3880: the
reducer is the LAST moment a relayed finding can acquire an owner. The reducer is
unconstrained by the worker ruling and files freely.

## Why the dedup must scan terminal statuses, not only open ones

(g-115-8007.) The dedup sentence cited guard-1204 / guard-2228 / guard-3738 —
all three about the PHRASING axis — and said nothing about the STATUS axis.
Measured 2026-08-27, first-hand, twice in one session:

    01:37:19  g-326-711 completed  (denominator instrumentation)
    02:01:19  g-326-712 completed  (server-side log join)
    02:12:21  g-326-714 FILED by the reducer spark replay as their duplicate
    02:20:54  g-326-714 skipped by an alpha worker Body as MOOT ON ARRIVAL

The dedup query was correct and its answer was TRUE: zero LIVE owners. Both
owners had COMPLETED, so an open-only scan could not see them.

The cost is worse than redundant work, which is why the fix is not tidiness.
g-326-714's scope prescribed "one log line on the null branch of
pickNearbyPlayer". The goal it duplicated, g-326-711, exists precisely to
FALSIFY that remedy — the composer's `player == null` branch is UNREACHABLE
because the composer is entered only when the scorer already found a player. So
the duplicate carried the exact remedy the completed goal had just proven wrong,
and an executor who trusted it would have shipped a permanent zero that reads as
a 100% rate. A dedup miss handed a live trap to the next Body.

## Why the window is a min(), not a single anchor

The race was ELEVEN MINUTES, not days, so a fixed 24h lookback is the wrong
SHAPE rather than the wrong number: a session running longer than the lookback
loses its own early completions, while a session shorter than it would become
blinder than the plain lookback. Both are floors, so the probe takes the EARLIER
of session-start and the fixed lookback. An UNDATED terminal goal counts as
in-window: it is ambiguous, not old, and counting it out would reproduce the
original defect on exactly the records whose timestamps a writer forgot to stamp.

## Why this is not the goal-duplication gate's job

That gate has a `recent_completions` check and it is NOT the backstop here, for
two independent reasons:

1. guard-4938 measured its completed-side coverage as PARTIAL and says
   explicitly that it "must not be treated as the backstop".
2. `core/scripts/gates/goal_duplication.py` skips every entry whose
   `completed_by` equals the filing agent. On a one-mind-many-bodies fleet the
   filer and the completer are routinely the SAME agent-name (7 worker SIDs vs 1
   reducer, all "alpha"), and team-state `recent_completions` carries no SID to
   tell two Bodies apart — so in the measured incident BOTH owners were
   invisible to it by construction. Measured 2026-08-27: `alpha` owned 7 of the
   50 live `recent_completions` entries, and all 7 are unreadable to alpha.

That filter is deliberate ("N-agent correct") and is NOT changed by g-115-8007;
the probe is a separate, earlier check that does not care who completed the work.
Whether the gate should discriminate by SID rather than agent-name is a distinct
question and is not settled here.

## Cross-references

- guard-5176 — a dedup/ownership probe run before filing must scan
  recently-completed goals, not only open ones (the general rule)
- guard-4938 — a status-filtered dedup corpus is blind to completed owners by
  construction; the gate's completed-side coverage is PARTIAL
- guard-4166 — a fix whose effect is that something STOPS APPEARING needs a
  positive control that does not flip, and the mutation proof must show it
- guard-1204 / guard-2228 / guard-3738 — the PHRASING axis, unchanged
- g-306-250 — worker-loop's filing ruling (Case A / B / C)
- `core/scripts/sq013-dedup-probe.py` — the probe;
  `core/scripts/tests/test_sq013_dedup_probe.py` — its regression test
