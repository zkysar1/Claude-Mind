# Rationale: Worker Park (the resumable wind-down)

Referenced from `.claude/skills/worker-loop/SKILL.md` Phase 0.5 (reducer-liveness
poll), Phase 1 (SELECT returned no goal) and Phase 2 (CLAIM resumes). Why a worker
Body PARKS instead of closing, why the park never stages, why the recorder call
is load-bearing, and why resume happens at the claim rather than at the poll.

## Why park rather than close — reducer gone (g-306-291)

The wind-down DECISION is right and unchanged: with no reducer, claiming and
executing accumulates work nobody will ever merge (the reducer is the only Body
that runs generalize-down). Its TERMINALITY was the defect. Measured
2026-08-14/15: a reducer stalled 15.7h, the worker closed durably, and when the
reducer RETURNED THE WORKER STAYED CLOSED — `body_state: closed-pending-merge`
makes Phase -0's gate refuse every further unit and only a user-only `/start` of
a NEW session reopens work. Recovery cost is one human `/start` PER worker, and
the incident's own email says it verbatim: "I cannot run it from a closed worker
Body here." The pre-g-306-291 wording called that cost "parks the remaining queue
on a human who does not know they are needed" — `park` then meant STRAND; it now
names the RESUMABLE state, which is why the close path says "strands" instead.

## Why supply exhaustion parks too (g-353-73)

"A worker with no work is finished, not waiting on anything" held for a single
Body and is false on a fleet. Measured 2026-08-29 on zc-03 (reducer + 7 workers):
an aspiration's tail had every pending goal claimed or dependency-blocked, so
within minutes four workers read 0 SELECT candidates, took the genuine-close path
and sat dead at the idle prompt; 25 minutes later SELECT ranked 18 candidates
(sibling claims released, a new aspiration filed). A same-SID `/start` re-kick
could not reopen them (Phase -0 closure gate); recovery was a fresh session per
worker, by hand. The supply gap was TRANSIENT and the close was PERMANENT — the
same asymmetry as the reducer case, from the other side. A wrong park costs one
hourly poll; a wrong close costs a human who does not know they are needed.

Both triggers therefore share one mechanism, one manifest state, one cap, one
recorder path, and one wakeup shape. The close conditions NARROWED twice and
never widened: g-306-291 made rc=1 park instead of close; g-353-73 made
SELECT-exhausted park instead of close. Exactly ONE sentinel writer remains — an
EXPIRED park — so the invented-close conservatism (guard-3479) is untouched, and
parking must never acquire a soft edge for anything else (context pressure, unit
count, "the next goal will not fit" are neither close nor park conditions).

## Why resume happens at CLAIM, not at the poll (guard-4184)

`body-manifest.py resume` CLEARS `parked_at`, and `park` on an already-parked
Body preserves the ORIGINAL stamp — the cap is a PATIENCE cap (total elapsed
duration), not a liveness clock. If the hourly re-entry resumed on "reducer
alive" and then re-parked on "no goal", every poll would restart the clock and a
supply park could never expire. So the poll never resumes: rc=0 means "continue
to SELECT", and the Body is resumed by the thing that ends the wait — a
successful claim in Phase 2. Until a claim lands the Body IS still idle, so the
reason file (below) correctly keeps reading EXPECTED_IDLE.

## Why the recorder call is not optional bookkeeping

It is what stops the fleet sweeper emailing the user that this box is DEAD.
Measured against the installed `world/scripts/fleet-liveness-sweep.py`:
`classify()` returns EXPECTED_IDLE on `session/last-stop-reason` BEFORE it
reaches the heartbeat-age branch, and its `--stale-min` default is 45 while a
park re-polls at 3600s. 60 > 45, so with no reason file a correctly-parked Body
is DEAD_LOOP for ~15 minutes of every hour — the exact inversion the parking work
exists to remove: the user must read "parked", never "dead". `worker-body-parked`
is in the recorder's NO_NOTIFY_PATHS, enforced INSIDE `record()`, so a forgotten
flag cannot mail the user hourly about a box that is fine and self-resuming. The
EXPIRED close records `worker-park-expired`, which DOES email: from a closed Body
`/start` is user-only and a human now genuinely has to act.

## Why the reason file is cleared on resume

Every OTHER writer of `session/last-stop-reason` stops the loop for good and
hands recovery to `/start`, which clears it via `session-manifest-clear.sh` — a
park→resume never goes through `/start`, so nothing else would ever remove it. A
resumed Body would then work normally for days while the sweeper kept reading
EXPECTED_IDLE, suppressing the alert for a LATER genuine death. Dropping the
`--clear` disables a detector rather than leaving litter. It is idempotent and
never raises, so it is safe on a resume that never parked through the recorder.

## Why the park never stages the WM

The goal that filed g-306-291 asked for "the SAME durable handoff as today:
board post, staged WM, pushed ref". Staging queues the Body for merge, and a
Body queued for merge that then keeps working is the hazard
`close_body_on_genuine` exists to prevent, in its own words: the reducer "merges
+ marks `merged`, then the worker keeps diverging into a now-merged manifest
that the sessions-pass never revisits." A parked Body intends to resume BY
CONSTRUCTION. Divergence is still safe: an abrupt death mid-park stages via the
stale-binding path, and an EXPIRED park runs the ordinary genuine-close path,
which stages and pushes through the one existing writer.

## Why expiry fails toward staying parked

An unreadable or missing `parked_at` reports not-expired. Treating it as expired
would durably close a Body on a field-format problem, and a wrong close is the
unrecoverable direction; a long park is visible on the board and costs an hourly
poll. The cap is `body-manifest.PARK_MAX_HOURS` (60h) for either trigger.

## Why the poll is the right place, and not a new detector

Measured 2026-08-08: the reducer is ALREADY observable cross-box by two
independent means — its per-SID body-heartbeat carrier (`heartbeat-tick.sh`
writes it for EVERY Body including the reducer, peer-readable) and the
runner-claim endpoint, which publishes agent_state + heartbeat age and which the
poll already reads every cycle. Detection was never the gap. The gap is that no
layer converts an observation into something a human or a queue sees: Layer C
(`trailing-text-detector.py`) has exactly one runtime caller, `stop-hook.sh`, and
the Stop hook is what a text-death prevents from firing; the peer-side
WorkerStallProbe runs ON the reducer, so when the reducer is what died the
reporter is the corpse. The poll's rc=1 branch is the one place a live process
holds the fact that the reducer is gone — hence "post FIRST, then park". Post on
the FIRST park only: an unconditional post emits ~24 identical messages per day
per worker into the channel a human reads to find out the reducer is gone.

## The three terminal shapes

A completed work unit ends on the deadman PAIR (`ScheduleWakeup(…, 600)` then
`Skill(worker-loop)` last). A PARK ends on `ScheduleWakeup(<park-resume prompt>,
3600)` and nothing after it — that wakeup IS the auto-resume, and the platform
keeps ONE pending wakeup (replace-slot), so the park poll is also the net. A
genuine CLOSE (an expired park, a user stop) ends on a Bash echo and arms
nothing: nothing should wake a closed Body. A park turn that forgets to arm is
indistinguishable from a close and needs exactly the human `/start` this
mechanism exists to remove. The stop-hook worker-net stands down on the parked
manifest (`gate=worker-net-body-parked`), so the park turn-end is ALLOWed.

## Never-promote

No rc of the liveness poll yields "become the reducer". Every ambiguous signal
resolves toward wind-down, because winding down loses nothing (the Body's
divergent WM stays live and unstaged across the park and reaches the reducer at
the eventual close — genuine, expired, or the stale-binding path) while
continuing without a reducer accumulates work that is thrown away. Parking makes
the bias cheaper still: the cost of a WRONG wind-down used to be a human `/start`
per worker and is now one hourly poll. Relaxations of NEVER-PROMOTE live behind
the gates in `core/config/conventions/reducer-promotion.md`, not here.

## Cross-references

- g-306-291 — the park (reducer-gone) landing; g-306-303 — the manifest valve
  replacing the sentinel-file valve (guard-4184 patience-vs-liveness)
- g-353-73 — supply-exhaustion parks too; the 2026-08-29 zc-03 measurement
- guard-3479 — invented stop conditions; guard-4184 — a threshold's meaning is
  defined by what resets its stamp
- rb-9659 — an unattended session at the idle prompt with a complete plan is a
  dead worker Body (the Zak-Code side of the same incident)
- `core/scripts/body-manifest.py` (`park`, `resume`, `park-expired`,
  `PARK_MAX_HOURS`), `core/scripts/stop-reason-record.py` (`NO_NOTIFY_PATHS`),
  `core/scripts/deadman-directive.sh` (resumable branch),
  `core/scripts/stop-hook.sh` (`gate=worker-net-body-parked`)
- `deadman-switch.md` — the sibling rationale for the 600s net
