# aspirations store: segmentation design record

Why the world aspirations store is rewritten whole on every goal mutation, what a
segmented layout has to look like for the rewrite to shrink 10x or more, and the
hazards a code-level read of the merge and write paths turned up. Status: DESIGN.
No segmented writer exists and none is registered. Traceability: g-358-202 (goal
ids are ephemeral; this record is the durable form), upstream measurements
g-358-191, key decision by echo 2026-09-21, unblock by bravo 2026-10-02. Measured
2026-10-02 by an alpha worker Body on cc-09 (6.8.0-142-generic).

## The cost being removed

Own-cloud PUTs the whole object, so one goal mutation re-uploads the whole file
(`core/scripts/aspirations.py` `_write_live_under_lock`, daemon
`mind_api/src/endpoints/aspirations_write.py` `_atomic_write_jsonl`). Echo's
2026-09-26 reading of the key: 2,321 PUTs and 22.3 GiB in 24 h, 124.3 GiB retained
in 11,796 versions. The store is now basement MinIO, so the saving is churn, disk
and write latency, not AWS spend.

## Population measured

The local mirror at `world/aspirations.jsonl`, 2026-10-02T13:33Z, one box: 25
aspirations (25 lines), 4,538 goals (4,537 distinct ids), 32,523,820 B. Sizing used
compact JSON and zlib level 6 on the same corpus read through the daemon: whole file
32,126,859 B plain, 11,677,785 B compressed (2.75x). asp-115 holds 3,386 goals and
21.7 MB of the goal bytes. Touch weights come from `max(last_modified, created_at)`:
670 goals touched in 24 h, 2,171 in 7 d. A goal touched twice counts once, so the
weights understate repeat writers; the worst-case column below does not depend on
them.

## Decisions

**D1. Segment every aspiration, not only the large one.** Segmenting asp-115 alone
cannot reach 10x, because the other 24 aspirations stay in one head object of about
10 MB of goals (3.7 MB compressed). Measured at K=250, expected PUT bytes
(compressed) by scope:

| scope | E[PUT] 24 h | ratio vs 11,677,785 B |
|---|---|---|
| all 25 aspirations | 231,651 | 50.4x |
| the 9 aspirations over 0.45 MB of goals | 295,548 | 39.5x |
| asp-115 and asp-374 | 1,011,490 | 11.5x |
| asp-115 only | 1,701,082 | 6.9x |

Uniform also removes a second representation: with a threshold, an aspiration that
crosses it must convert in place under the lock, and every reader handles both
forms forever.

**D2. Key = (holding aspiration, numeric part of the goal id // K), K = 250.** Both
parts are fixed at creation, so the segment is stable under in-place update (echo's
requirement; a write-date key is not). Real compression on real segments, uniform
scope:

| K | objects | E[PUT] 24 h | ratio | largest object | if every PUT hits it |
|---|---|---|---|---|---|
| 100 | 170 | 107,760 | 108x | 291,409 | 40x |
| 250 | 87 | 231,651 | 50x | 583,349 | 20x |
| 500 | 55 | 442,339 | 26x | 1,061,949 | 11x |
| 1000 | 39 | 720,391 | 16x | 1,067,780 | 11x |
| 2500 | 30 | 1,296,688 | 9.0x | 2,119,678 | 5.5x |

Objects are the head plus segments holding live goals. K=250 is the largest K whose
tail still clears the 10x bar with a 2x margin, and the typical case is 50x. Echo's
18x for K of about 250 live goals used the right shape; the measured figure is higher
because id ranges are sparse after eviction: asp-115 has 46 of 48 ranges non-empty
and the newest eight hold 101 to 246 live goals. asp-115 mints 90 goals per 24 h
(365 per 72 h), so a new range opens every two to three days. Read-side object-count
latency is the floor on K and is NOT measured here (guard-6372); objects and ratio at
each K are in the table so it can be priced.

**D3. A goal's key uses the HOLDER, not the id prefix.** `g-115-11836` is held by
both asp-115 (superseded) and asp-378 (pending) with one `alloc_nonce`: a move
looks like a move: the source is superseded and the copy keeps the old id (read off
the two records; the mover was not read). One goal in 4,538, but it means the id
alone does not locate the live copy. Lookup tries the id's own
aspiration first and must fall back to scanning the holder's segments.

**D4. A segment is a partial aspiration record**: `{"id", "goals"}` plus
`archived_census.evicted_ids` for exactly that id range, one JSONL line. Ranges that
hold only tombstones keep their segment, so the round trip below has 89 segments
where the table counts 86 with live goals. The head
keeps every other field in its original key position with `goals` and the id lists
emptied. Tombstones live with their range because a merge handler sees one file
(`merge_aspirations` docstring) and a goal evicted from one segment must not be
re-added from a stale copy of that same segment. Eviction then removes the goal and
adds the tombstone in ONE object, so any prefix of a multi-segment write is a
consistent store, and the metric-neutral invariant holds per segment.

**D5. Derived `progress` is recomputed from the joined goals, never trusted from
the head.** A per-goal mutation then writes exactly one object. The head is written
only by aspiration-level mutations.

**D6. Join is order-free.** All 25 goal lists are in plain string order of the goal
id (0 adjacent inversions against it) and all 64 `evicted_ids` lists are
string-sorted, so sorting reproduces the file. Verified end to end: split at K=250
into 89 segment objects plus a head (51,613 B plain, 18,661 B compressed, with
176,866 B of tombstones now inside segments), each serialized with
`json.dumps(..., ensure_ascii=True)`, parsed back, joined: the rebuilt file is
byte-identical to the 32,523,820 B original, 0 goals sit in a segment other than
the one their id names, and a swapped pair is detected as different.

## Hazards found

**H1. The segment merge handler cannot be the bare parent handler.**
`_merge_goals` displaces a colliding goal to `max(taken) + 1`, computed over only the
ids the merge can see. On a segment that is the segment's own ids. Probe against
`coordination_merge.merge_aspirations`: two distinct goals racing for
`g-115-11999`, the last id of range 47, merge to a goal at `g-115-12000`, which
belongs to range 48. The merge stays commutative; the defect is a goal misfiled in
a segment its id does not name, with a possible id collision against range 48.
Controls: identical sides displace nothing; a stale copy of an evicted goal is
dropped by the co-located tombstone; no head field (`progress`, `selection_count`)
appears on a partial record. 11 live goals carry `displaced_from`, so the path runs
in production. Remedy to prefer: the mint rule skips the last 4 ids of every range
so displacement lands inside it, which leaves `_merge_goals` (its associativity was
hardened in g-115-2367) untouched. Fallback: a range-bounded handler built from the
segment name. Either way the registration test must assert that every output id of a
segment merge names that segment, with at most 4 displacements per range.

**H2. Whole-aspiration archive.** A union cannot represent a delete (guard-1816).
`_aspirations_resurrection.classify` iterates `live_asp["goals"]`; handed a head
record, whose goals are emptied, it reports "not a resurrection" for everything, and
the segments of an archived aspiration would be orphans a stale box can re-add. The
first writer must refuse to archive a segmented aspiration, and the sweep must be
handed joined records.

**H3. Raw-S3 readers of the legacy key**: `_authoritative_goal_lookup` in the daemon
reads it with a raw `get_object`; other raw readers are not yet enumerated.

**H4. Goal-count census** (`aspirations-meta.json` `goal_count`) must compare the
joined total, not the head.

**H5. Mint, eviction and move are three writers of goal ids and tombstones**:
`aspirations.py`, `aspirations_write.py`, `aspirations-evict-completed.py`,
`aspirations-move-goals.py`. Each needs the range rule.

## Staged plan

1. Names, key function and the segment merge handler, registered and dark (outcome
   3). Acceptance: the H1 assertion, tombstone no-resurrection, no head-field
   invention, strict name regex that excludes `aspirations-archive.jsonl` and
   `aspirations-meta.json`, handler non-None for every derived name (guard-6907).
2. Pure split and join with the D6 round trip as a test over a corpus fixture.
3. Integration layer (the fork below). 4. Writer behind a default-OFF flag, a
   `store-cutover-check.sh` entry, migration and rollback; reader-capable code on
   every box and downstream Mind first. 5. Flip, then a fresh 24 h listing for
   outcome 4. 6. Retained versions (outcome 6) after reading the bucket's own
   lifecycle config.

## The open fork: where the join lives

Consumers cannot be left unchanged unless the join happens below them. Census by
regex over non-test python, shell and world scripts (an upper bound, heuristic):
135 files build the store path; 91 open it with `open` or `read_text`, 22 mix a
fileops helper with raw open, 3 use only a helper, 10 only name it, 9 are shell.
The daemon has 20 read-modify-write sites and 23 read sites on the live path, the
CLI 7 write sites (grep of the call text, not a call graph).

(a) Route every consumer through a logical-view helper, plus a ratchet test that no
file reads the legacy path directly: about 113 raw readers to change.
(b) Make the own-cloud layer treat `aspirations.jsonl` as a composite path: remote is
head plus segments, local stays one materialized legacy file, so raw readers and
the 20 daemon sites need no edit; `_put` splits and writes only changed objects,
`_refresh` joins. Not verified: whether `_put`, `_merge_reconcile_put` and the
manifest baseline can host it; none of them was read for this record.

Recommendation: spike (b) first, one unit, answering that question against
`owncloud_backend.py`; fall back to (a) with the ratchet if it cannot. Outcome 5's
wording assumes (a); under (b) every consumer is exercised by running the suite
with the split layer on, which the goal owners should confirm.

## Not established

Read-side per-object latency (the floor on K). Lock contention: a naive grep of the
contention message is contaminated, 15 hits in one corpus dump that merely quotes
it, so count events in lane logs, not strings. Head PUT rate after cutover (D5
predicts it is rare). Retained-version policy.
