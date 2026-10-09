# Rationale: A worker /stop closes the Body

Referenced from `.claude/skills/stop/SKILL.md` Step 0.6, the worker branch. It
explains three things: why a worker `/stop` releases, relays, pushes to its worker
ref and then CLOSES the Body instead of parking it, why the steps run in that
order, and why the stop LANDS the session in reader or assistant mode.

## Why close instead of park (replaces the g-306-477 park for /stop, 2026-10-07)

The park rested on two premises. The first was that a stopped Body intends to
resume. The second was that park expiry would eventually stage the learning of a
Body that never resumed. Both measured false on 2026-10-07, in an operator-directed
review run from the worker Body that made this change:

- **A stopped SID cannot resume.** `/start` refuses any session whose fork file
  `sessions/<SID>/working-memory.yaml` exists. It refuses at W-pre in the RUNNING
  branch and at 0-pre / 0-pre2 in the IDLE branch, all with
  `EX_WORKER_FORK_PRESENT`, and the fork file outlives a stop. Running the agent on
  that box again therefore takes a fresh terminal, which means a new SID and a new
  Body.
- **A stopped Body never reaches park expiry.** worker-loop Phase -0-stop reads
  `sessions/<SID>/stop-requested` before the park-due gate and stands down
  (g-115-9461). The Phase 1 expiry close therefore never runs for a stopped Body.
- **As a result, the park staged the WM late or never.** Staging came only from
  the stale-binding sweep after more than 24h of mtime silence. Until then, every
  unflagged capture the Body wrote reached the fleet through no channel at all
  (guard-6181).

The g-115-7309 ruling, "stopping one box is not retiring that Body", answered a
turn-end trap. The only stand-down valve a worker could fire was `body-closing`,
which closed the Body as a side effect of ending the turn. The session-scoped
`stop-requested` file fixed that trap. Closing is now a deliberate LAST step after
the release, the relay and the push, and is no longer a side effect. The operator
approved the reversal in the same review.

## Why this order

1. **`stop-requested` first.** Every later step is fire-and-forget, and the turn
   must be able to end even if one of them hangs.
2. **Release before the learning pass.** A claim held through the relay keeps a
   goal away from other Bodies for no benefit. Before this change, a worker stop
   released no claim at all, so its goals stayed locked for the 4h lease or until a
   reducer sweep found them. The stranded-claim sweep sees only `in-progress` claims
   older than its 120-minute foreign-SID grace. It also skips a stopped Body whose
   heartbeat carrier still reads `fresh-correct`.
3. **Relay before the commit,** so any file the relay writes rides the same commit.
4. **Summary, then commit, then push to the worker ref.** The old stop pushed the
   shared branch, which made the stop the one place a worker wrote main. The worker
   contract reserves that for the reducer (worker-loop Phase 2.9 and 3.8). The
   worker ref is the carrier Phase 3.8 already uses, and the reducer merges it.
5. **Flush, then telemetry and binding, BEFORE the close.** Under the park, the
   telemetry close and the binding cleanup ran after `body-manifest.py park`.
   parked-body-gate.py then denied their Bash calls (measured twice on 2026-10-07),
   so every stopped worker kept an `active` telemetry record.
6. **The landing, then `body-closing`, last, in one call.** The stop-hook consumes
   the sentinel at turn end, and `close-body-on-genuine` snapshots the WM at that
   moment. A WM write after the snapshot is divergence the reducer never merges.

## Why the session lands, and why through its own binding (2026-10-07)

Closing ended the Body but left its session behaving as a worker, because the
binding still said `autonomous`. Measured on the Body that made this change, after
its own stop on 2026-10-07:

- every Bash call still exported `BODY_WM_PATH` and `BODY_ROLE=worker`;
- the heartbeat tick kept the carrier fresh;
- `session-mode-get.sh` returned the box's agent-wide `autonomous`;
- the post-compaction banner said to continue the worker loop, and to resume the
  goal the stop had released.

The fresh carrier also blocks the worker-ref retire gate, which needs it stale for
180 minutes, so a stopped session kept open kept its ref unretirable. The operator
asked that the agent land in assistant mode when the stop is done.

**It lands through the session's own binding, never `agent-mode`.** The stop
rewrites only its own binding's mode, to `reader` or `assistant`. `agent-mode` and
`session-mode-set.sh` are agent-wide, so writing them would change every other
session of the agent on the box.

**One predicate defines landed:** `_session_binding.landed_mode_in`. A session is
landed when it forked a working-memory.yaml AND its binding mode is `reader` or
`assistant`. Five consumers read it:
- the Bash inject hook;
- the heartbeat tick;
- `session.py mode get`;
- `session-mode-get.sh`, which mirrors it by hand;
- the restore banner.

**It keys on the binding, never on `body_state`.** The g-306-210 ruling forbids
routing a live session on state another process writes, and `body_state` is such
state. The binding is not: only the session's own `/stop` writes a reader or
assistant mode onto a forked session, at its own command boundary. `/start` refuses
that bind on an ex-worker SID in all three branches.

**An unreadable binding keeps worker routing.** A live worker misread as landed
would lose `BODY_WM_PATH` and write the agent-wide WM. A landed session misread as
a worker only keeps a closed Body's env. The second error is the cheap one.

**The landing shares the close's call.** The close cannot be undone, so no stop
arms it without trying the landing first. A failed landing still closes, and the
session stays unlanded until a repeat `/stop` lands it. That repeat's step 0 skips
the steps that act on work the close already staged, because a relay written after
staging would be lost.

## Why the relay writes spark_capture for almost everything

In its normal mode, encode-session writes the shared stores. On a worker that is
the Nth-reducer defect, so relay mode writes WM capture lanes instead. The lanes
are not equally consumed:

- **`spark_capture` is the only lane read for every entry.** The reducer's Worker
  Spark Replay (aspirations-spark) replays every entry, files sq-013 relays as
  goals, and drains by goal_id.
- **`exp_capture` and `encoding_capture` are read only for completed goals.**
  worker_retrospective.py reads them only for goals in `merged_goal_ids`, the goals
  a Body COMPLETED. An entry keyed to anything else is never read.
  `encoding_capture` also has no cap, so those entries pile up: 931 entries across
  481 goals on one Body, measured 2026-08-24 (worker_retrospective.py,
  `drain_consumed_captures` docstring).

Relay mode therefore uses those two lanes only for a goal this Body completed.
Everything else becomes a spark:

- a lesson goes in with `sq_trigger` null;
- owned work, or a tree fact, goes in as an sq-013 filing, which the reducer turns
  into a goal and executes.

## Why unit leases are not released

A unit lease (unit-claim.sh) marks an artifact in flight, such as the open PR for
one unit, and that artifact outlives the session. Releasing the lease at stop would
reopen the duplicate-unit race the lease was added to close (g-306-322). The lease
expires on its own after `claim_timeout_hours`.

## What this does not cover

- **Container Bodies.** They are stopped by their own sidecar and recipe, not by
  this skill, so this release-and-close does not run for them.
- **Live Bodies holding one claim for a long time.** Such a Body is invisible to
  both claim-recovery lanes: `_abandoned_claim.py` skips it because it still has an
  in-flight row, and `stranded-claim-sweep.py` skips it because its holder is alive.
  On 2026-10-07 two recurring goals were measured held for 17.9h and 22.2h with both
  carriers `fresh-correct`. That is a detection gap, not a stop defect.

## Cross-references

- `core/scripts/body-claims-release.py`: the release step and its verdicts
- `core/scripts/_session_binding.py` `landed_mode_in`: the landed predicate
- g-306-210: never route a live session on `body_state` (`bash-agent-inject.py`)
- `.claude/skills/encode-session/SKILL.md` § Relay Mode
- `core/config/rationale/worker-stop-standdown.md`: Phase -0-stop
- `core/config/rationale/worker-park.md`: the park this replaces for `/stop`
- guard-4900: the turn-end trap
- guard-6251: `stop-requested` is a turn-end permit only
- guard-6181: `load_bearing` is the delivery predicate
- guard-2676: a scoped call, not a transcription
- guard-1579 / guard-6254: the flush prunes the agent dir
