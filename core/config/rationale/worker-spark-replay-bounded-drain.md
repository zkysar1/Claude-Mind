# Rationale: Worker Spark Replay's Bounded Drain

Referenced from `.claude/skills/aspirations-spark/SKILL.md` → "Worker Spark
Replay". Why that block drains k GOALS per pass through `wm-drain-goals.sh`
instead of replaying the whole slot and calling `wm-clear.sh`.

All measurements below are from g-115-10001 (alpha, hostname cc-04, `uname -r`
6.8.0-139-generic, own-cloud, live fleet, 2026-09-15 .. 2026-09-17) unless
another box is named.

## Why the original block was jointly unexecutable

It said `FOR EACH entry in the slot` with no bound, then `Do NOT skip the clear`.
Both instructions are correct at the size they were written for (~10 entries) and
contradict each other above it: running N handler passes is not a close-sized
action, and clearing without running them destroys unprocessed observations,
which `archive-before-delete.md` forbids outright.

So a conscientious reducer did NEITHER. That is not hypothetical — at N=3,187,
**eleven consecutive reducer closes** reached this block over three days, each
re-measured the slot, each declined both halves, and each recorded a provenance
line saying so. The slot's only documented exit was one no correct implementation
could ever legitimately reach, and the population sat for a month.

The measured population at its peak: 3,187 entries / 6,487,861 B / 1,242 distinct
goal_ids / 3,165 distinct observation texts (so it was NOT a replay-duplication
artifact), spanning 2026-08-16T22:21:27 .. 2026-09-15T10:52:09. **2,047 of the
3,187 carried `sq_trigger: sq-013`** — work-discovery relays a worker Body found
and could not file. The headline is therefore not "3,187 observations unread" but
"2,047 pieces of discovered work never reached the queue", which is a throughput
claim about the fleet rather than a hygiene claim about a slot.

## Why "the k oldest" must sort by `_item_ts` and is never `slot[:k]`

`body-merge.merge_wm` appends each Body's batch whole and nothing re-sorts across
batches, so the slot is **~41 internally-ascending runs concatenated** — neither
ascending nor descending overall. Measured on the 3,187-entry slot:

```
min_ts 2026-08-16T22:21:27      max_ts 2026-09-15T10:52:09
index[0]  = 2026-09-14T12:30:24  index[-1] = 2026-09-09T09:14:16
adjacent pairs n=3186: ascending 3150 (98.9%), descending 41 (1.3%)
SORTED_ASC=False   SORTED_DESC=False
POSITIONAL slot[:20] vs TRUE oldest-20 by _item_ts: overlap = 0 of 20
```

An implementer who reads "take k oldest" and writes `rows[:k]` drains whichever
batch happens to sit at the front. The 2026-08-16 cohort is never at the front,
so it is never drained — at any k, for any number of rounds. The drain would
report honest, rising drained-counts forever while the oldest month stayed put,
and **nothing in the output would show it**: the counts are real, the handlers
run, the slot shrinks. Same shape as guard-2496 (one append-only file carrying
two append regions in opposite orders, so "read by tail" and "sorted
newest-first" are both true and both wrong), one surface over, with the failure
pointed at the OLDEST end instead of the newest.

Hence the key is pinned in the spec rather than left to the implementer:
`sorted(rows, key=lambda r: r.get("_item_ts") or "0000")`. The `"0000"` default
is the same one `wm._eviction_sort_key` uses (wm.py:336-339), so the drain and
the cap order victims identically instead of two ways. In the measured population
that default was inert (0 unstamped rows) but it must be written anyway —
guard-6289: a row added through the read-modify-write + `wm-set` path carries no
`_item_ts` at all.

## Why the batch unit is the GOAL, not the entry

`wm-drain-goals.sh` subtracts by goal_id. Taking the k oldest ENTRIES and posting
their goal_ids would subtract sibling entries the handlers never saw, so the
batch is instead the k oldest distinct goal_ids ranked by each goal's own oldest
`_item_ts`, processing every entry that carries them. At ~2.6 entries per
goal_id in the measured population this is a small, predictable amplification.

## Why `wm-clear.sh` is the wrong primitive here, in two independent ways

**1. The cap it relies on is INERT, not merely unenforced.** The paragraph this
replaced said a skipped clear "replays the same observations every close until
the 50-item `array_limits` cap starts dropping the oldest". Both halves are
false. 3,185 of 3,187 entries carried `load_bearing: true` and 2 carried truthy
PROSE in that field, making **3,187 of 3,187 eviction-EXEMPT** via
`wm._eviction_sort_key`. Both bounding paths run — append enforces, and merge has
enforced since g-306-309 (guard-6719) — and neither can evict a fully-flagged
lane. A 63.7x-over-cap slot with zero destruction is the signature of a fail-safe
holding the line, not of absent enforcement.

Two corollaries worth carrying:

- The *unenforced* cap is the only reason the 3,187 observations were still
  recoverable. Enforcement at 50 would have destroyed 3,137 of them. Fix the
  DRAIN before the CAP, in that order, or the repair destroys the evidence it
  exists to recover.
- `wm-prune.sh --dry-run` cannot test the eviction path at all: the archive call
  is guarded by `if slot_name in CAPTURE_SLOTS and not dry_run`. A dry run
  reported `pruned_items=3137` with `archive_failures` absent — reading as
  "enforcement is fine" — while a real run may break on victim #1 and evict
  nothing. It is structurally blind to the only failure mode that could explain
  the backlog, and it fails in the reassuring direction.

**2. A verified clear was UNDONE inside the hour.** An archive-then-clear of all
3,187 entries succeeded at 14:08 (WM 13,914,051 → 6,968,622 B, −49.9%), verified
per-row against the sink. By 19:10 the slot was **byte-identical to the pre-clear
set**: same sha256 (`3ee9fe68…`), N=3,187 both, intersection 3,187,
only_in_archive 0, only_in_current 0, and nothing in it newer than the clear.

That falsifies partial-clear and falsifies cap-eviction (eviction removes; it
cannot reinstate 3,187 entries). The surviving explanation is that `merge_wm`
UNIONS list slots, so a Body holding a pre-clear baseline restores the whole set
at the next generalize-down — the donor-resurrection class named in
`archive-before-delete.md` rule 7. It is a blast-radius failure, not an archive
failure, and the mechanism remains INFERRED: the merge itself was not observed.
It is falsifiable — a Body that forked after the clear would carry an empty slot
and could restore nothing, so the restoring Body's fork predates the clear.

**Whether a bounded subtract survives that merge is UNMEASURED.** The union
hazard is a property of `merge_wm`, not of the removal's size, so it must be
assumed to apply to the drain too. What the bound buys is OBSERVABILITY: at k
goals per round a restore shows as `removed` and `kept` that do not move across
consecutive closes, surfacing within one iteration instead of a month. A
non-decreasing `kept` across two passes is the restore signal — stop draining and
fix the baseline, which is what `merge_wm`'s baseline argument exists to express
(a 3-way merge against a POST-clear baseline subtracts the slot where a 2-way
union cannot).

## Why the primitive was already there

`core/scripts/wm-drain-goals.sh` shipped with g-115-7366 as "the drain site
`exp_capture` and `encoding_capture` never had", explicitly "not `wm-clear.sh`"
for exactly this reason, and it re-asserts the predicate INSIDE the write lock
(guard-3881) — which a read-filter-then-`wm-set.sh` in the caller cannot do and
which silently loses any concurrent append. It had **one** caller
(`worker_retrospective.py:747`); the reducer-side consumer was never migrated.
So the fix was a call-site migration plus an ordering guarantee, not new
machinery — no new abstraction was built for this
(`implementation-discipline.md` rule 3).

## The `goal_id: null` residue

162 of 3,187 entries carried no goal_id and so can never appear in the drain's
goal-id set. They are excluded from the batch deliberately: the drain then makes
monotonic progress on what it CAN subtract, and the residue stays countable
instead of being silently re-handled every close. Their drain site is a separate
fix.

## What a second box showed

Measured 2026-09-17T20:3x, bravo, hostname cc-05, `uname -r` 6.8.0-139-generic:
`spark_capture` is `[]` — empty — and `working-memory.yaml` is 35,577 B. The
consumer had run 6 times, recording `worker-spark-replay: checked, 0
observations` each time. Only alpha carries a `capture-evictions-archive.jsonl`
on this box (56.8 MB).

So the backlog is **not fleet-wide**; it is one agent's slot. That does not make
the consumer defect narrower — the block was unexecutable above a threshold on
ANY agent, and bravo simply never crossed it — but it does mean "a month of
cross-Body learning never reaches a reducer" describes alpha, and a fix must not
assume every reducer is carrying a stock. It also leaves open, and unmeasured,
why bravo's Bodies contributed nothing to the lane at all.

## Cross-references

- rb-3804 — a producer buffer without a guaranteed consumer must bound at the
  add-site AND degrade (not error) on overflow; this slot is its unbounded-add
  instance
- guard-5718 — with a live producer the terminating condition is a RE-READ that
  comes back empty, never exhaustion of the opening enumeration
- guard-6289 — `wm-set` does not stamp `_item_ts`; a drain that re-writes the
  remainder itself strips stamps off the rows it preserves
- guard-2496 — two append regions in opposite orders; the ordering trap's twin
- guard-6824 / guard-6321 — the consumed-hash watermark saturates at
  `CONSUMED_HASHES_CAP = 2000`, so once `slot_len > 2000` the never-consumed
  difference is an UPPER BOUND on un-replayed learning, not a count
- guard-2567 — a changing measurement in a goal TITLE makes the goal undedupable;
  three HIGH Unblocks were filed for this one defect
- `.claude/rules/archive-before-delete.md` rule 7 — blast radius / donor
  resurrection
- `core/scripts/wm-drain-goals.sh`, `core/scripts/wm.py:336-339`
  (`_eviction_sort_key`), `mind_api/src/endpoints/wm_write.py::append_slot`
  (the LIVE enforcer; wm.py's copy is parity-only)
- g-115-10001 (this work), g-115-10021 (`load_bearing` is ~100% true and
  therefore carries no information — the cause of the inert cap), g-115-9087
  (cluster-then-route, the branch that makes a large replay tractable)

## Why BOTH branches write a provenance line (moved here 2026-09-17, g-115-10001)

The empty branch's diary write is the load-bearing half and must not be skipped
as "nothing happened" — it is what makes a later ABSENCE readable. A recorder
placed only on the fire path is missing exactly on the population you most need
to account for (guard-2352), so without it a zero-hit grep cannot distinguish
"the consumer ran and had nothing to replay" from "the consumer never ran at
all". Measured 2026-08-07 (zeta, cc-02): that exact ambiguity made the question
undecidable — zero hits fleet-wide across every journal, experience file and
execution diary, with no way to tell which zero it was.

The fire branch's line is written BEFORE the drain, on crash-safety grounds: a
crash there leaves the slot intact to re-replay (safe) plus a record already
written (a harmless over-count), whereas recording after the subtract could lose
the batch AND leave no trace it existed. It names the source goal_ids, because
that is what makes an artifact attributable to the replay path rather than to an
ordinary close — `source_goal` and `encoded_by` are exactly the two fields an
ordinary close already writes, and neither marks a replay.

A third case exists that g-306-251's two branches do not cover and which the
bounded drain now makes rare: the consumer RAN and the population was too large
to process. Eleven closes landed in it. If it recurs, say so explicitly in the
diary line rather than letting it read as either of the other two.

Both writes are their own command, never `&&`-chained to a sidecar (guard-409).
