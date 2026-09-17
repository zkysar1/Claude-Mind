# Rationale: Capture-Carrier Delivery Check

Referenced from `.claude/skills/worker-loop/SKILL.md` Phase 3.7, from
`core/scripts/worker_execute.py check-capture-carrier`, and from
`core/scripts/body_capture_carrier.py::verify_delivery`. It explains why a worker
Body reads its capture fast lane back from the store once per work unit, and why
the check has the shape it has (g-115-9852, outcome 3).

## Why the loop needs this at all

A load-bearing capture reaches the reducer before the Body closes through one
channel: `record_local` appends it to `world/body-carriers/<agent>/<sid>-fastlane.jsonl`,
and `push()` PUTs the whole file to the store. The push swallows its own
failure. The daemon's response carries `carrier_pushed: true`, but that means
only that the store write returned without raising. It is an attempt, not a
delivery. The one failure message is printed once per daemon process, into the
daemon's log.

Measured on cc-07 (2026-09-12): the daemon log recorded the carrier push failing
on a lost race (`ConflictError`). The carrier then stayed unchanged for 53
minutes, across two work units. Every `wm-append` in that window returned rc=0
and every goal closed clean. Whether each of those appends was flagged was not
recorded. The wedge was found by a Body that happened to stat the file and read
the daemon log, not by the loop. Outcome 3 asks for the opposite: the unit that
wedged the lane should be the one told.

## Why a store content read, not an mtime

The goal first proposed "carrier mtime before and after one append". That
measures the local append, and the local append succeeds whether or not the push
lands. On cc-07 the local file and the store copy were identical, and both were
stale, so no local reading could have caught it.

Byte comparisons against the store fail too, in the other direction. `backend-cat.sh
head` compares the size and ETag of the stored object with an md5 of the local
plaintext. For an object the store holds transformed, that comparison reads
DRIFT on identical content (guard-2245, amended with four stores measured 2026-09-07).

So the check reads the store copy through `read_authoritative_bytes`, which
decodes the transform and never touches the local mirror. It matches each entry
by `body-merge._content_hash`, the same identity `capture_fast_lane` and
`generalize_down` dedup on. The check and the consumer therefore cannot
disagree about whether an entry is present. This is guard-3221: gate on the
artifact the consumer receives, not on the store the producer wrote. The match
key is (slot, hash), because the consumer merges per slot.

## Why the fork baseline is subtracted

A Body's working memory starts life as a byte copy of the agent-wide WM. That
copy includes the flagged capture entries the reducer had already merged, and
this Body never appended any of them, so none are in its carrier.

Measured on cc-09 (2026-09-14, alpha, SID 7593d753): the Body WM held 1,395
flagged capture entries, and 1,245 of them came from the fork. A check without
the subtraction reported 1,245 missing on a carrier that was healthy. With the
fork baseline (`forked-wm-baseline.yaml`, immutable from the fork on)
subtracted, 150 entries were this Body's, and the store held all 150.

Two consequences follow.
- No baseline means no verdict. Without it the two populations cannot be told
  apart, so the check reports `unverified` rather than guessing.
- An entry evicted from the WM is not checked. It is archived (g-115-9852
  outcome 2), and the carrier is append-only, so a store copy holding rows the WM
  no longer has is normal.

## Why it runs once per unit, at Phase 3.7

The pushes happen inside the `wm-append` calls of Phases 3.5 to 3.66, so by
Phase 3.7 the store copy reflects every push this unit made. A per-append
read-back would put a full store GET for each flagged append on the daemon's
request thread, which is the expensive verify inside a bounded call that
guard-5002 warns against. The unit is also the granularity the goal asks for.

It is a separate verb, not a class or a flag on `check-outputs`. The fast lane
is not an output class: the staged Body WM still carries every capture at close.
And `--verify-delivery` must, by its contract, run after the Phase 3.8 push
(g-115-6368). One flag with two timing contracts would be right at one moment
and wrong at the other.

## Why it never holds the goal open

`check-outputs` rc 1 means that an output cannot reach the reducer, so the goal
stays in-progress. A wedged fast lane is a different fault. The entries remain
in the Body WM, evictions are archived, and the staged WM delivers them at
close. What fails is delivery before close, which is what `load_bearing`
exists for (guard-6181).

Holding goals open on a persistent wedge would turn a slower learning relay into
claims that are never released. So `undelivered` prints its remedy and asks for
one coordination-board escalation per Body session. The board is the channel
that survives when the capture relay is the broken one (guard-997); relaying
the finding as a capture would ride the lane that is wedged. The detail also
reports how many of the missing entries the local carrier holds:
- If it holds them, the push is failing.
- If it does not, the local file lost them too, and no later push can deliver
  them.

`unverified` exits 3, never 0. A check that could not run has learned nothing,
and reporting that as a pass is the silence this goal exists to remove
(guard-1760).

## Cross-references

- guard-2245: verify by content, not bytes; head DRIFT on transformed objects
- guard-3221: gate on what the consumer receives
- guard-6181: `load_bearing` is the delivery predicate; the carrier is flagged-only
- guard-1760: an unrunnable check must not report as a pass
- guard-997: the board survives the partition that the broken lane is
- guard-5002: no expensive verify inside a bounded call
- `worker-verify-own-unit.md`: closing is not landing; delivery reads speak to landing
- `.claude/skills/worker-loop/SKILL.md` Phase 3.7: the consumer of this rationale
