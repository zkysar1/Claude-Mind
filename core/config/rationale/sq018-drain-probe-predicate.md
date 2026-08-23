# Rationale: the sq-018 DRAIN_GOAL probe matches a TUPLE of prefixes

Referenced from `.claude/skills/aspirations-spark/SKILL.md` step 2.5 (lane-depth gate),
`DRAIN_PREFIXES` / `isdrain`. Explains why the predicate is a tuple and must not be
narrowed back to one string.

## Why a tuple and not one prefix

The gate routes on three branches: file a singleton (shallow lane), APPEND to the open
drain goal, or file a new drain goal. Only the APPEND branch preserves a proposed check
when the lane is deep, and reaching it depends entirely on `DRAIN_GOAL` being non-empty.

`isdrain` matched exactly one prefix — `maintain:drain-verify-learning-check-lane` — which
is the convention introduced by defect (c) in the gate's own comment block. The drain goal
that actually exists predates it: **g-115-5920** carries
`maintain:verify-learning-checkproposal-batch-20260811`.

So the probe reported `DRAIN_GOAL=''` against a live, open, correctly-filed drain goal.

## What that cost, measured

2026-08-22 (echo, hostname cc-03, uname -r 6.8.0-137-generic), asp-115 at 2148 goals /
1769 open:

```
LANE_DEPTH=15   (title proposes ADDING a check — the branching predicate)
LANE_WIDE=93    (any mention; diagnostic only)
LANE_NARROW=13  (origin_signal proxy; diagnostic only)
DRAIN_GOAL=''   <- FALSE. g-115-5920 was open the whole time.
DRAIN_RECENT=''
```

`LANE_DEPTH=15` is not `< 15`, and `DRAIN_GOAL` read empty, so control fell to the ELSE
branch: file a new drain goal. `aspirations-add-goal.sh` refused it —
`goal_duplication_blocked`, check `pending_queue`, `strategies=structural_overlap`, 16
overlapping goals.

That is the **self-sealing failure the lane-depth gate exists to prevent, reproduced by the
gate itself.** The gate's premise is that singletons cannot drain a deep lane because
structural_overlap blocks each new arrival against its siblings, and that consolidation is
therefore the only path that shrinks it. A drain goal necessarily *enumerates* its siblings,
so it overlaps them by construction — meaning the escape hatch is blocked by the same
mechanism, whenever the probe fails to find the drain that already exists.

Before/after on the live queue, same data:

```
OLD predicate → DRAIN_GOAL count 0     (mis-routes to ELSE, filing gets blocked)
NEW predicate → DRAIN_GOAL count 1     ['g-115-5920']  → routes to ELIF, appends
```

## Why the fix had to be in the probe, not in a note

This was the **third** recorded occurrence. g-115-5920's own description already carried an
accurate diagnosis from a prior agent, in the gate's own vocabulary:

> the handler's DRAIN_GOAL predicate ... cannot see THIS goal, whose origin_signal is
> `maintain:verify-learning-checkproposal-batch-20260811`. The duplication gate blocked the
> new filing twice, correctly. guard-1802 class: the probe is narrower than the population
> it models.

And it recurred anyway. The asymmetry is the whole lesson: **a note written ON a goal cannot
reach the probe whose defect is that it fails to FIND that goal.** Any reader who could have
been warned by the note is a reader who already located the goal — i.e. precisely the reader
who did not need warning. This is the mechanical case for guard-1984 (when a lesson is about
a specific passage of pseudocode, edit that passage), and it is why widening
`DRAIN_PREFIXES` — not a fourth note — is the remedy.

## Maintenance

Adding a new drain-goal naming convention means adding its prefix here. The tuple is the
registry; there is no other reader. A single-string regression silently re-arms the seal and
fails in the direction that *looks* like ordinary gate behavior (a refused filing), so it is
unlikely to be noticed as a probe defect.

## Cross-references

- `guard-1802` — a self-limiting predicate narrower than the population it models
- `guard-1984` — put the warning in the instrument, not only in a guardrail or a note
- `guard-1818` — date a marked number, and re-measure the whole sentence when fixing one
- `g-115-5920` — the drain goal the probe could not see; carries the appended check list
- `.claude/skills/aspirations-spark/SKILL.md` step 2.5 — the consumer, defects (a)–(d)
- `.claude/rules/learning-philosophy.md` rule 5 — the additive-only ratchet this gate fights
