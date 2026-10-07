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
each K are in the table so it can be priced. (U9 measured it: not binding. The table's E[PUT] and ratio count
the segment alone, 57.5x on the real stream against this table's 50x; with the stored head the figure is 24.6x.
See "What the live PUT stream and the live store answered (U9)".)

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
only by aspiration-level mutations. (Under U2d the head is the commit point and lists every
segment's md5, so every write PUTs it: U9 reads 1.02 changed segments plus the head per mutation.)

**D6. Join is order-free.** All 25 goal lists are in plain string order of the goal
id (0 adjacent inversions against it) and all 64 `evicted_ids` lists are
string-sorted, so sorting reproduces the file. Verified end to end: split at K=250
into 89 segment objects plus a head (51,613 B plain, 18,661 B compressed, with
176,866 B of tombstones now inside segments), each serialized with
`json.dumps(..., ensure_ascii=True)`, parsed back, joined: the rebuilt file is
byte-identical to the 32,523,820 B original, 0 goals sit in a segment other than
the one their id names, and a swapped pair is detected as different.
Correction (U9, 2026-10-03): the order claim holds for the instant it was measured, not for the live file. About
half of the live PUT stream carries goals appended after the sorted body (48.5% of 200 sampled versions refuse to
split, all for order), and a list that is not sorted is not reproduced by sorting.
U10 (2026-10-03) carries that order in the head's `tails`, and every sampled live version splits again: see "The order
exception in the head (U10)".

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

**H3. Raw-S3 readers of the legacy key** (resolved for the goal queue by U3, 2026-10-02).
Every `get_object` site in `core/` and `mind_api/` was read; the only two raw readers of
the queue key, `_authoritative_goal_lookup` (the daemon's persistence read-back) and
`worker_stall._read_queue_lines` (claim map, terminal and known ids, goal meta), now call
`_owncloud_composite.decode_whole`, which joins a head through
`OwnCloudBackend.join_composite`. Read as the file, a head returns a clean wrong answer
(no goal, no claim, an EMPTY census) rather than an error; the head-blind control tests
reproduce it. Left for U4: the copy tools (`owncloud-store-enumerate.py` reads raw bytes
at two `get_object` sites) must enumerate the `.composite` keys.

**H4. Goal-count census** (`aspirations-meta.json` `goal_count`) must compare the
joined total, not the head.

**H5. Mint, eviction and move are three writers of goal ids and tombstones**:
`aspirations.py`, `aspirations_write.py`, `aspirations-evict-completed.py`,
`aspirations-move-goals.py`. Each needs the range rule.

## Orphan GC (U2e)

A content-addressed segment never goes noncurrent, so no lifecycle rule expires an orphan
(outcome 6, condition C1); a collector must. `plan_gc` in `_owncloud_composite.py` is the
pure half, landed with no caller. It names the objects the current head does not list
that have stayed unlisted for `GC_GRACE_S` (14 d, my choice, not a measurement: at least
the head key's noncurrent window, 7 d on MinIO and 14 d on AWS per guard-6837, so an
orphan outlives every retained head version that could name it). The clock is the later
of the first sighting (a ledger the caller keeps) and the object's own `last_modified`.
It refuses, deleting nothing and leaving the ledger alone, when the object is not a
head, the head is unreadable, the head names a segment the listing lacks (a truncated
listing, or a head newer than the listing), or the head names none over a non-empty
listing. A name that is not exactly what `segment_object_name` writes is reported, never
deleted. The grace floor belongs to the enable gate: the planner accepts any grace that
is a number and not negative.

The read-only pass that feeds it, `OwnCloudBackend.composite_gc_enumerate` (U2e-enumerate),
also landed with no caller. It GETs the head, lists the segment prefix to the END of its
pagination, then HEADs the head again; a head that moved or vanished abandons the pass
(`head-moved-during-enumeration`), a store off the allowlist is refused before any S3 call,
and a head that is not a composite head is refused without listing. It returns the plan,
the head ETag the plan was computed against and, for each name the plan would delete, its
size, ETag, last_modified and first sighting: the names-and-sizes enumeration that
archive-before-delete step 1 asks for. It never PUTs, deletes or copies and never touches
the mirror. Nothing persists the ledger or the enumeration yet.

The delete pass, `OwnCloudBackend.composite_gc_apply` (U2e-delete), is behind a flag of its
own, `OWNCLOUD_COMPOSITE_GC`, which names environment ids exactly as the writer's flag does
and is independent of it in both directions (`should_gc`). Unset it returns `gc-not-enabled`
after no S3 call, and a grace window under `GC_GRACE_S` is refused (`gc_grace_ok`: the
default is the floor). Nothing calls it and no environment is named, so nothing any box
reads or writes has changed. In order:

1. Enumerate (above). Any refusal there ends the pass with nothing done.
2. ARCHIVE every object the plan names to
   `<env>/_composite-gc-archive/<run id>/objects/<store>/<name>`: GET it, decode it and
   require the md5 its name carries (an object that is not what its name says is kept,
   as is one that claims gzip and is not), PUT its stored bytes verbatim (gzip encoding and
   metadata too), GET the copy and require the same bytes.
3. Write `RECEIPT.json` at the top of that run's archive prefix: each archived object, its
   size and md5, first sighting, the versions a versioned store's delete will remove (U8), and
   how to put it back.
4. Delete, by single `delete_object` calls in batches of `GC_DELETE_BATCH` (50, my value),
   reading the head before each batch (a name it now lists is kept), HEADing each object just
   before its delete (one gone, re-written since the listing or undated is kept: U6) and
   reading each delete back (an object still present is not counted as deleted). On a
   versioned store each delete names a version, not the key (U8).
5. `composite_gc_restore`: put back from the archive any deleted name the CURRENT head names
   and the store lacks. It reads the live head and the live store, never the receipt's own
   list of deletions, so it is right after a pass that died half way and for a later sweep,
   and it needs no flag: recovery must not depend on the switch that enables deletion.
6. Rewrite the receipt with the outcome and return the ledger for the caller to persist.

What is deleted is the archive's manifest, never the plan. The archive prefix sits beside
`world/`, `meta/` and `agents/`, the only roots the sync layer pulls, so it is never
mirrored. It is the rb-10945 recipe (a server-side graveyard on the same endpoint, the
receipt at its top), except that the bytes pass through the sweeper so each copy is checked
against the md5 in its object name instead of being trusted by ETag. An earlier draft of
this section asked the apply pass to delete only names a previous dry pass had recorded;
that was dropped, because both lists come from the same planner and the intersection can
only delete less. The reviewable artifacts are the dry pass's enumeration
(`composite_gc_enumerate`, which never writes) and each run's receipt; the flag is the switch.

Verified (fake S3, moto, and moto with bucket versioning on): the round trip through the REAL
writer and reader, with the sweep landing after the writer's look at the present object (U6: a
sweep right after the 412 is repaired by the writer itself) and before its head commit, so the
head then names a deleted object, `composite_gc_restore` puts it back, and the read is
byte-identical; every refusal; an object the archive cannot
verifiably hold is kept; the archive and receipt are outside the governed roots and the
segment directory. NOT verified: any listing of the live segment prefix; that the live
principal may PUT under the archive prefix (read from the scope assert and the env-prefix
IAM condition, not measured); the live lifecycle config on either store (unreadable on
AWS; on MinIO the bucket-wide rule is noncurrent-expiry only, which does not touch a current
object, by the provisioning intent and not by a read of the live config).

Still open before any environment is named, in either flag:
- Who runs the pass, how often, and where the ledger lives (one sweeper means a local file,
  several a shared one). The pass needs a lock if more than one box can run it.
- A late `composite_gc_restore` for every receipt not yet old enough to trust, because a head
  can commit after the pass's last re-read and the immediate restore cannot see it.
- Who prunes the archive and when. It keeps what the collector removes, so it bounds the
  standing set in the segment directory but does not shrink the bucket until a pruning
  rule exists; that rule is a deletion and owes its own archive-before-delete.
  Decided in U28 ("The archive pruning rule (U28)" below): the rule and its planner exist; its executor is in the tree, landed dark, and is verified in U31 ("The archive prune executor, verified (U31)" below).
- The 412 window is narrowed by U6 (next section) and U8, not closed: the gaps that remain are listed
  there and in the U8 section.

## The writer's freshen and the delete pass's re-check (U6, items (e) and (f))

The 412 window: the writer's create-only segment PUT answers 412 for an object that is already
there, takes that as 'the same bytes are stored', and commits a head that names it. If the object
is an orphan old enough for a sweep to take, the sweep can delete it between the 412 and the
commit, and the head then names a missing object until `composite_gc_restore` runs. Two changes,
one on each side, narrow that:

- Writer: `OwnCloudBackend._freshen_segment`, called from the 412 branch of `_store_put`. It HEADs
  the object. When it is missing (a sweep took it since the 412) or its last_modified is within
  `FRESHEN_MARGIN_S` (24 h, my choice, not a measurement) of the age the sweep collects at
  (`needs_freshen`: `GC_GRACE_S - FRESHEN_MARGIN_S`, 13 d), it PUTs the same bytes again,
  unconditionally, with the codec kwargs the first attempt built. The name carries the content's
  md5, so the overwrite changes only last_modified, which restarts the planner's clock (git's
  'freshen'). A young object is only looked at. A steady-state write (new segments only, no 412)
  makes no extra call. Cost: one HEAD per 412, and one re-PUT of one segment at most once per 13 d
  however many cold writes find it. The margin only has to exceed the time between a write's first
  segment PUT and its head commit. An undated object is never freshened, as the planner never
  deletes one.
- Delete pass: `composite_gc_apply` HEADs each object before its `delete_object` and keeps it when
  it is gone (`gone-since-archive`), when its last_modified is later than the listed one by more
  than `GC_MTIME_SLOP_S` (2 s: a listing reports milliseconds, a HEAD whole seconds), or when
  either value is unreadable (both `rewritten-since-listing`; it fails toward keeping, as the
  planner does for an undated object). The question is 'did this object change since the plan',
  not a recomputed age, so it needs no clock. Cost: one HEAD per delete, at most `GC_MAX_DELETE`
  (500) a pass.

What this closes, and what it does not. Each line is pinned by a test, so the remainder is asserted
rather than claimed away. CLOSED: a sweep landing right after the writer's 412 (the writer finds
the object gone and PUTs it again); a writer re-PUT before the sweep plans (a fresh object is not
a candidate); a writer re-PUT between the sweep's head read and its re-check (the object is kept).
NOT CLOSED: a sweep landing between the writer's look (it saw a young object and did nothing) and
its head commit, and the gap between the sweep's re-check and its delete call (U8 closed that one on
a versioned store). `composite_gc_restore` repairs both from the archive, byte for byte, so the late
restore stays owed
before any environment is named in `OWNCLOUD_COMPOSITE_GC`. The conditional-delete option this
paragraph first left open (`IfMatch`) was measured on the live store in U7 and does not work there;
a different exact close for the second gap, deleting by version id, did work: see the section after
the U6 one.

(f): `plan_write` takes the held names from `head_object_names`, not its own copy of the
comprehension, inside a handler that treats an unreadable old head as naming nothing (every segment
is PUT, create-only, and the new head replaces it). No current path reaches it: the head cache is
filled only from heads the writer wrote or `read_whole` joined, and `plan_refresh` rejects every
manifest `head_object_names` rejects (pinned for six shapes), as a raw KeyError, TypeError or
AttributeError rather than an IntegrityError. So (f) is defense in depth against a future writer of
the cache. An earlier note in the progress record said a malformed old head would fail every write;
that was written from `plan_write` alone and was wrong.

Verified (fake S3, moto, and moto with versioning; the tests that need an aged object run on the
fake alone): the new test file `test_owncloud_composite_freshen_g358202.py` (62 items, 12 of them
fake-only) with the GC-apply and writer files; mutation proofs by throwaway copies (guard-6701; the live files are md5-identical before and after, and the control run of the three files is 248 tests and 0 red): ten one-token mutants each turn tests red, as intended: never freshens 6, always freshens 14, a missing object not re-created 9, the freshen PUT left conditional 11, the re-check never seeing a move 6, the re-check failing open on an undated object 4, plan_write unguarded 12, the re-check removed 5, a gone object at the re-check raising 3, the freshen never called 26. NOT verified: AWS's own HEAD and
listing timestamps (the live MinIO store's are in the next section); a concurrent stress of writer
and sweeper.

## What the live store answered (U7)

Measured 2026-10-03 on the live MinIO store with throwaway objects (probe, result and README:
`world/audit-reports/g-358-202/live-delete-probe/`; every key was removed by version and an
independent listing shows nothing left). Nothing was run against AWS: the collector targets the
MinIO store only (guard-7339).

| Question | Live answer |
|---|---|
| HEAD versus listing `last_modified` (8 objects) | HEAD is whole seconds, the listing's time truncated: `head - list` is in [-0.872, -0.016] s, never positive. `GC_MTIME_SLOP_S` (2 s) is conservative, not too small. |
| A same-bytes re-PUT (the freshen) | ETag unchanged (it is the md5 of the stored bytes, plain and gzip), a new version id, the old version noncurrent, `last_modified` restarts. The freshen does on the live store what U6 built it to do. A re-PUT within 2 s of the listed time is invisible to the re-check; an orphan the planner lists is at least 14 d old, so it cannot meet that. |
| A create-only PUT on a key that exists | 412, the answer the writer's 412 branch relies on. |
| `DeleteObject` with `IfMatch` (wrong, right, or the ETag listed before a freshen) and with `IfMatchLastModifiedTime` (a day off) | 204 every time, a delete marker written, the object gone: both headers are ignored. |
| `DeleteObject` by `VersionId` | Permitted for this principal. The OLD version of a freshened key: the key stays and the new version stays current. The only version: the key is gone, no marker, no version. The LATEST of two versions: the older version becomes current and the key reappears. |

Decisions (mine, from those answers; override if you disagree):

- The conditional-delete route is closed: the store ignores the headers, and a same-bytes re-PUT leaves the
  ETag unchanged, so even an honoured `IfMatch` could not see a freshen.
- The gap between the sweep's re-check and its delete call is closed exactly by deleting the LISTED versions
  by version id: a freshen after the listing is a version the sweep never listed, so it survives. Two
  obligations: delete every version the plan listed for the key (deleting only the latest brings the older
  one back) and read back that the key is absent. The listing's latest version id also gives the re-check an
  exact identity test (the HEAD's `VersionId` equals the listed one) in place of the timestamp slop. Built in
  U8 (next section).
- The price: a version delete leaves no delete marker and no noncurrent version, so the 7-day noncurrent
  window stops being a second recovery layer for swept segments and the archive (mandatory) is the only copy.
  In return no delete markers accumulate and no header the store may ignore is needed.
- Still open: a sweep between the writer's look at a young object and its head commit (margin-based), so the
  late `composite_gc_restore` stays owed. The U2e probe's residue (4 noncurrent versions behind 4 delete
  markers) expires on the successor's clock, rounded up to midnight UTC (rb-11687): list it again on or after
  2026-10-10T00:00Z.

## Version-targeted delete (U8)

U7 measured that the live store ignores conditional deletes and honours a delete by version id, so on a
versioned store the delete pass now names versions. `composite_gc_apply` takes the branch from the archive
GET: a response carrying a `VersionId` other than `'null'` is a versioned store. An unversioned store answers
with none; `'null'`, which S3 documents for a suspended bucket, is read the same way and was not measured. On
that branch, for each archived object:

1. After the archive copy is read back, list the object's chain with `_composite_object_versions`: one
   `list_object_versions` call per object, exact to the key (the prefix also matches longer keys), read to
   the end of its pagination, newest first; each entry is a version or a delete marker with its id, ETag,
   size and time. The chain goes into the receipt, and the receipt is written before the first delete, so a
   pass that dies half way still names what it meant to remove.
2. At the delete step HEAD the object (gone: kept, as before) and ask the pure `plan_version_delete` for a
   verdict. It keeps the orphan as `rewritten-since-listing` when the chain has no single non-marker latest
   or has an entry without an id, when the HEAD's `VersionId` is not the listed latest (a re-PUT since the
   chain was listed) and when the listed latest is later than the enumeration's listing by more than
   `GC_MTIME_SLOP_S` (a re-PUT before the chain was listed). It keeps it as `version-bytes-not-archived`
   when any listed version's ETag is not the archived object's. Otherwise it returns every listed entry's
   id: the noncurrent ones oldest first and the latest last, because deleting only the latest brings the
   older version back as current (U7), and ending on the latest means a failure part way leaves the object
   itself present.
3. Delete each id with `DeleteObject(VersionId=...)`. A version the store reports already gone (a
   not-found code or `NoSuchVersion`) is the state asked for; any other error stops the pass.
4. HEAD once more. Absent: deleted. Present under a listed id: `delete-not-effective` (the store accepted
   the call and removed nothing). Present under an id the chain never listed: a writer's re-PUT after the
   HEAD, which survives and is reported `rewritten-since-listing`.

An unversioned store keeps the key delete and its re-check by time, makes no version listing and records
an empty chain.

Decisions (mine; override if you disagree):

- Placement. The chain is listed in the apply pass, per archived object, and not inside
  `composite_gc_enumerate`, which my U7 note had planned. Enumerate's call shape is pinned by its tests (a
  head GET, the listing of the segment prefix, a head HEAD), and a second listing there would still leave a
  gap after the first that only the time check covers. Listing right after the archive GET puts it where the
  `VersionId` is known and costs a call only for an object the pass is about to delete.
- Each gap between the enumeration and the delete is covered by a different signal, so on a versioned
  store the sweep leaves none of its own: a re-PUT before the chain was listed shows as a later time, one
  between the chain and the HEAD as a different `VersionId`, one after the HEAD as a version that is not on
  the list and so survives. The unversioned control (the same trigger, a key delete) loses the re-PUT; both
  are tests. The writer's look-to-commit gap (U6, margin-based) is not the sweep's and stays.
- Nothing is deleted whose bytes were not archived. The archive holds one copy, the CURRENT version's
  bytes, taken by a plain GET. Reading a noncurrent version's content works on the MinIO store (guard-7452,
  measured 2026-09-27) and was denied on regional AWS (guard-2464, guard-2945), and the pass should not
  depend on which store it runs against, so a listed version whose ETag differs from the archived
  object's (a noncurrent copy written with the gzip flag the other way, say) keeps the whole name. It stays
  in the ledger and is reported on every pass until a person looks. Archiving every version where reads
  are permitted would remove that residue; it is not built, and I have not measured that any such version
  exists.

The price, stated: a version delete leaves no delete marker and no noncurrent version, so the archive
object is the only copy of what the pass removes (the receipt's `restore` text says so). In return no
marker accumulates and no header the store may ignore is relied on. Cost: one `list_object_versions` call
per orphan a versioned pass archives and one `delete_object` per version instead of one per object, at most
`GC_MAX_DELETE` (500) orphans a pass; the HEAD count is unchanged (one before the delete, one after). The
listing needs the ListBucketVersions permission, which a principal that lists objects can lack: the pass
then raises `OwnCloudPermissionError` before it deletes anything (copies already made stay in the archive
prefix).

Verified (fake S3 with a version model, moto with bucket versioning on, and both unversioned): the new
`test_owncloud_composite_gc_versions_g358202.py` (46 items, 3 skipped on moto because only the fake can age
an object, page at 2 or inject a truncation) pins every branch of the planner, the chain listing, the
receipt naming every version before the first delete, every listed version and marker deleted by id with the
latest last and nothing left, a failure on the last delete leaving the object present, the three late-PUT
gaps with their unversioned control, bytes the archive does not hold, a delete the store does not honour, a
version reported already gone, an empty chain, and an unversioned store making no version listing. The
GC-apply file now runs over four stores (193 items, none skipped). The own-cloud group, which includes all
eleven composite files (548 items, 15 skipped), is 1301 items with 0 failed and 0 errors. Mutation proofs
by throwaway copies (guard-6701; the five files involved are md5-identical before and after, and the control
run is 46 items and 0 red): 24 one-token mutants each turn at least one item red (latest first, only the
latest, either re-check removed, the HEAD's `VersionId` replaced by the listed one, the ETag check removed or
applied to markers, a marker as the latest or two latests accepted, a missing id accepted, a key delete on
the versioned branch, the version listing skipped, the branch chosen by whether the chain is empty, a
read-back that always says deleted, the delete loop reversed, every error swallowed, an already-gone version
not tolerated, not-effective unreported, a chain not exact to its key, first page only, a truncation
returned instead of raised, a listing denial not translated, an unversioned store listing versions, the
archived ETag not recorded). The first matrix had one survivor, the delete marker as
the latest: every case of the unreadable-chain test passed one head id, which the marker case also failed
on its own, so the chain check was masked. Each case now names its own latest, and the rerun is 24 of 24.
NOT verified: any run of the new branch against the live MinIO store (no environment is named in the
flag, and U7 measured the primitives, not this pass); AWS's answer to deleting a version id that does not
exist (moto answers `{}`, and the code reads NoSuchVersion as success and lets the read-back judge); another
principal's ListBucketVersions grant.

Still open: the list at the end of the U2e section stands (who runs the pass, the late restore, pruning
the archive). U8 adds one: an orphan kept as `version-bytes-not-archived` is reported on every pass and
nothing resolves it, so it needs an owner once a runner exists.

## What the live PUT stream and the live store answered (U9)

Measured 2026-10-03 from cc-08 (load average 4.2 to 5.7 during the probe, one live Body on the box besides the probes)
against the post-cutover MinIO at 100.76.251.73:9000 (bucket `zds-own-cloud-data`, versioning on), botocore 1.43.39.
The live key was only read; every write went to throwaway keys under a store directory that no writer, reader or planner
names, and each was removed by version (497 versions and markers removed, 0 left in a fresh listing, none left on the host;
the one residue under that directory is the U2e probe's `asp-probe` entry from 2026-10-02T23:10Z). Evidence, scripts and
result files: `world/audit-reports/g-358-202/live-head-and-join-probe/`.

**Finding 1: about half of the live PUT stream cannot be split, so a flag-on writer would PUT it whole.** 200 of the 20,815
retained versions of the live key (one per 104 PUTs, 2026-09-25T23:59Z to 2026-10-03T10:45Z, each read by VersionId) went
through `split` at the default span: 97 refused with "joined bytes do not match the head's md5" and 103 split, so 48.5%
refused (binomial 95% interval about 41% to 55%; 38% to 64% by day). In all 97 the cause is goal order: at least one
aspiration's list holds a goal after a string-larger id. asp-115 accounts for 75 of the 97 (a new goal `g-115-11939` is
appended after the string-last `g-115-9998`, and five-digit ids sort before 9xxx), asp-335 for 22, asp-326 and asp-374 for
14 each, asp-353 for 5, asp-363 for 4, five others for 1 to 3. Every list is a sorted prefix plus an appended tail of 1 to
15 goals, and the tail stays until a merge re-sorts it: of the newest 100 versions the first 71 split and the last 29, over
20 minutes (2026-10-03T10:26:22Z to 10:45:52Z), were refused. `aspirations.py` sorts no goal list; `_merge_goals` ends
with a sort on the id string, then the content, so a merge restores string order. D6 stated a
property of the instant it was measured (2026-10-02T14:35Z, a canonical moment), not of the file. `_store_put` logs
a refused input once and PUTs the whole object.

**Finding 2: what the writer PUTs per mutation, read off the real stream, and what the head costs.** 60 consecutive-version
pairs spread over the same history, goal lists sorted first (what an order exception in the head makes equivalent), K=250.
59 of 60 mutations change exactly one segment and one changes two (none changes zero). Changed segments cost 196,446 B
gzipped on average (median 164,584, p90 337,365, max 603,565). The stored head costs 262,144 B on every write. A composite
PUT is therefore 458,590 B on average (median 426,728, p90 599,509, max 865,709) against a whole PUT of 11,293,395 B:
24.6x by ratio of means, 26.5x median per mutation, 13.4x for the worst of the 60. Counting the segment alone the ratio is
57.5x, which matches D2's 50x; D2 does not add the head (inferred from the match: D2's derivation was not re-read). With the
head the expected reduction is 24.6x, above outcome 5's 10x bar (a mean under about 1,129,000 B), and the worst mutation clears it.
At the refusal rate of finding 1 the mean is 0.485 x 11.29 MB + 0.515 x 0.459 MB = 5.71 MB, a 2.0x reduction, and outcome 5
needs the refused fraction under 6.2%. D5's "the head is written only by aspiration-level mutations" does not hold under
U2d: the head carries every segment's md5 and is the commit point, so every write PUTs it. The head is 234,974 B plain today
(219,922 B on average over the sample; 57,321 B gzipped), of which 177,867 B is the shells' `archived_census` (D4 puts the
tombstones in the segments; the implemented split leaves them in the head). The head is 57% of the bytes of an average PUT.

**Finding 3 (the question U4 was to measure): on this versioned bucket the store inlines up to 16 KiB, not 128 KiB.** Plain
bodies of 1 to 512 KiB, each key written 6 times with the head's PUT shape (plain-md5 metadata), the per-key metadata file
(xl.meta) read on the MinIO host over ssh with a read-only `find` after 1, 3 and 6 versions. Bodies up to 16,384 B are
inlined; bodies of 17,408 B and above are not (a data part file, and an xl.meta of 443 B after one version). An inlined
key's xl.meta grows by its body plus about 1.5 KB per retained version (1 KiB: +1,548 B; 16 KiB: +16,908 B); a non-inlined
key's grows by 416 to 422 B per version whatever the body (17 KiB to 512 KiB read 443, 1,287 and 2,553 B at 1, 3 and 6
versions). PUT p50 was 4.1 to 8.9 ms from 1 to 256 KiB and 12.1 ms at 512 KiB. Decision: `HEAD_MIN_BYTES` stays at 256
KiB, 16x the measured threshold and 2x the unversioned default. The pad costs 27 KB per write today (234,974 B natural head
against 262,144 B) and nothing once the head exceeds 256 KiB, and the margin over 128 KiB matters only if the head shrinks.
A gzipped head (57 KB today) would not inline either, 3.5x the threshold. By arithmetic, not measurement: about 20,800
retained versions at about 420 B each make the head key's xl.meta about 8.7 MB, rewritten on the host at every head PUT,
as the legacy key's is today, so the layout does not change that cost. Not measured: whether a MinIO upgrade or its
storage-class setting moves the threshold, and sizes between 16,384 and 17,408 B.

**Finding 4: read-side latency is not the floor on K.** Real segment sizes at each K come from the live bytes split after
sorting: 175, 89, 58, 41 and 32 objects with the head against D2's 170, 87, 55, 39 and 30, and largest objects of 297,052,
599,925, 1,097,745, 1,113,391 and 2,110,415 B against D2's 291,409, 583,349, 1,061,949, 1,067,780 and 2,119,678, which is the
control for the sizes (today's file is a little larger). Random-byte objects of those exact sizes were PUT under the probe
prefix and joined the way `fetch_segment` reads (the head, then every segment in turn, 3 rounds): K=100 0.436 s, 250 0.217 s,
500 0.173 s, 1000 0.105 s, 2500 0.090 s. One GET took p50 2.1 to 2.7 ms, p95 3.9 to 6.6 ms, max 14.5 ms. A steady-state
refresh (HEAD of the head, GET of the head, GET of one segment) took p50 5.5 to 7.3 ms with heads of 16 to 256 KiB. Limits:
the objects had just been written, so the host's page cache is warm; one reader box; sequential reads; no gunzip, md5 or
join CPU in the figures. K=250 stays: what binds K is the PUT tail. With the head, a PUT that hits the largest segment is
11.29 MB / (262,144 + 599,925) = 13.1x at K=250 and 8.3x at K=500, below the bar.

**Finding 5 (side observation, not explained): a GET of a retained version failed botocore's response checksum twice in
about 240 reads made by four worker processes** ("Expected checksum ... did not match calculated checksum ..."; the retry
succeeded). It did not reproduce in 1,020 reads of throwaway objects (11 MB and 100 KB, sequential and with 4 and 8 threads),
48 reads of four historic versions with 4 threads, 240 reads of distinct historic versions with 4 threads, or the 300
sequential reads behind finding 1. No script handles it by name (zero hits for `FlexibleChecksumError` over `core/scripts`
and `world/scripts`, and none in the knowledge tree), and the backend's client sets retries and leaves response checksum
validation at botocore's default.

**What followed (planned in U9, built in U10, next section).** U10: carry the order exception in the head so that every live list splits (per
aspiration the ids after the longest ascending prefix, in file order; join reproduces any list byte for byte, segment bytes
do not change because a segment is the sorted bucket); the acceptance is the same 200-version sample with the new `split`
refusing none. The module docstring's THE FORMAT paragraph ("0 adjacent inversions in 4,540 goals") and the
`HEAD_MIN_BYTES` comment are corrected in that unit. A head diet is a later candidate: on the same 60 pairs a gzipped head
with no pad gives 45.1x and the natural plain head with no pad 27.1x, against 24.6x today, and neither is needed for the bar.

**Not verified in U9.** That the refusal rate stays near 50% under a flag-on writer (the versions were written by the
whole-object writer; merge frequency may differ). That finding 1's order is the only refusal cause once it is handled (it
was the only cause in 97 of 97). The per-mutation figures use sorted lists, so they are the figures after U10, not before.

## The order exception in the head (U10)

Built and measured 2026-10-03 on cc-08. Code: `core/scripts/_owncloud_composite.py` (`_order_tail`, `_in_head_order`, and the
`tails` handling in `split` and `join`). Tests: `test_owncloud_composite_g358202.py` (51 tests became 76) and
`test_owncloud_composite_write_g358202.py` (40 became 44). Evidence and scripts: `world/audit-reports/g-358-202/live-head-and-join-probe/`
(the U10 files are listed in its README).

**What changed.**
- `split` records `tails[<aspiration id>]` for every aspiration whose goal list is not in plain string order of id: the ids after
  the longest strictly ascending prefix, in file order. The key is absent when no list needs one, so the head of a sorted file is
  byte for byte what it was before (a test rebuilds the old head from the manifest and compares).
- `join` sorts the aspiration's goals by id, then appends the tail goals in the recorded order. A segment is still the sorted
  bucket, so a tail costs bytes in the head and no segment: a test appends one goal out of order (one segment PUT), then sorts it
  in (no segment PUT and `tails` gone).
- `join` raises IntegrityError for a tail that is not a list of distinct strings, names an id no segment holds, or sits under an
  aspiration the head does not hold or one that has no goals. The md5 of the joined bytes is still the last check and still
  refuses a tail that was dropped or reordered.
- `plan_refresh` splits a reader's local file to find segments to reuse. A local file with a tail now splits, so the reader reuses
  its segments where it used to GET every one.
- What `split` still refuses (each has a test): a serialization other than the writer's (compact separators; raw non-ASCII), no
  trailing newline, a blank line, a duplicate goal or aspiration id, a null `goals`, a goal without an id, an aspiration id
  unusable as a segment key. D6's "order-free" is true of the head plus its order exception, not of sorting alone.

**Acceptance on the live key** (2026-10-03T11:33Z to 11:39Z; `split` on the raw bytes of each version, none canonicalised first,
with the sampler U9 used):
- U9's stride sample redrawn: 200 of 20,878 retained versions (2026-09-26T01:25Z to 2026-10-03T11:32Z), 200 split, 0 refused, 0
  read errors. U9 drew 97 refusals in its 200.
- The newest 100 versions (10:23Z to 11:35Z): 100 split, 0 refused. U9: the last 29 of its newest 100 were refused.
- The control that can fail: the 125 distinct versions U9 found refused (97 + 29, one in both), fetched again by version id: 125
  split and `join` returned each input byte for byte. In every one the tails agree with U9's diagnosis, the same aspirations
  with the same tail lengths (0 differences). asp-115 carried a tail in 103 of the 125. A version carries 1 to 26 tail ids
  (median 2, mean 2.8) and at most 15 in one aspiration; its head is 212,916 to 235,043 B before the pad, so the stored head
  stays 262,144 B and the tails cost no stored bytes while the head is under 256 KiB.

**Tests.**
- Against the pre-patch module (a throwaway copy), 24 of the 27 new or converted unit tests fail. The 3 that pass on both pin what
  must not change: the head of a sorted file, and the refusal of compact and of unescaped serializations.
- Mutation matrix on a throwaway copy of `core/scripts`, `mutation-partition-proof.sh`: 16 single-line mutants over 7 declared cases
  (tail recorded, tail replayed in the recorded order, non-tail goals sorted, tail minimal and absent when sorted, each tamper
  check, the md5 backstop, the write path). All 16 killed, 0 unproven cases, restore ok. One mutant first read "no change" because
  my sed repeated a word; the corrected sed was killed. Kills were attributed with `--junit-xml` for the six tamper checks and the
  write path: the list check by `not-a-list` (without it a tail given as an object of valid ids is accepted, the md5 passing), the
  string check by `not-ids`, the distinct check by `id-twice`, the check that a tail id exists in a segment by
  `goal-no-segment-holds` (without it a raw KeyError escapes instead of IntegrityError), the check that a tails key names an
  aspiration with goals by `unknown-aspiration` and `aspiration-without-goals` (without it both are accepted silently), the
  object check by `tails-not-object`, and the write path by the new end-to-end test in all four parametrizations. The tamper
  cases assert their own message, because the md5 refuses every one of them anyway and the exception type alone cannot tell a
  deleted check from a live one.
- Regression group, `test_owncloud_*.py` (40 files, `-m "not daemon_integration"`, backend pinned local by the conftest): 1,237
  tests, 1,215 passed, 16 skipped, 6 failed, 140 s. The 6 are in `test_owncloud_integration.py` and fail with `403 Forbidden` on
  `HeadObject`; the same 6 fail with the pre-patch module in the throwaway copy. The cause is outside this change: the `seam`
  fixture sets fake credentials and a moto bucket but does not clear `STORAGE_S3_ENDPOINT_URL`, which `owncloud_backend.py` reads
  in `from_env()` (line 769), so on a box that sets it the client talks to the live MinIO, which refuses the fake credentials. With the
  variable removed from the environment the file passes 7 of 7. This is tracked as g-115-10069 (pending since 2026-09-16);
  the same one-variable control was recorded there on 2026-10-02, and this unit added the pre-patch-module control.

**What a pre-U10 reader does with a tailed head** (the pre-patch module loaded beside the new one on a small tailed fixture): its
`join` raises IntegrityError "joined bytes do not match the head's md5", and its `split` of the same file raises NotSplittable,
which is U9's refusal. A pre-U10 reader therefore fails loudly and never returns wrong bytes. A head with `tails` exists only
after a flag-on writer has run, and the flag names no environment, so none exists today. The staged plan's item 4 already puts
reader-capable code on every box and downstream Mind first; U10 is part of that code, so the writer flag must not name an
environment until U10 is on every box that reads the key.

**Not verified in U10** (its first item, the refusal rate under a flag-on writer, is answered in U11, the next section: 0 of 237 composite
writes refused). That the refusal rate stays 0 under a flag-on writer (every version read was written by the whole-object
writer). Head growth under a pathological order: a tail costs about 16 B per id (measured as the slope of the head's size over 15 and 100 tail ids) plus about 110 B
the first time a head carries the key, so today's 1 to 26 ids over at most 5 aspirations cost under 1 KB; a list that is wholly
out of order puts every id of that aspiration in the head (about 48 KB for 3,000 goals, by arithmetic). Nothing caps it and
nothing produces it today. The FULL suite, which is owed before g-358-202 closes. Why the integration fixture never cleared the
endpoint variable (seven test files name `STORAGE_S3_ENDPOINT_URL`; this fixture and the conftest do not).

**Open after U10.** The full suite; the pre-flip gates (the reducer's merge of worker refs so the cutover tool reads SAFE, and the
measurement gauge at the flip); the post-flip GC runner and the archive pruning rule; outcome 5's fresh 24 h listing; the U2e
residue listing on or after 2026-10-10T00:00Z; and the optional head diet.

## What the live stream did through the real writer (U11)

Run 2026-10-03T12:12Z to 12:27Z on cc-08 against the live MinIO (endpoint 100.76.251.73:9000, bucket `zds-own-cloud-data`). No code
changed in this unit. Harness, results and a README block: `world/audit-reports/g-358-202/live-head-and-join-probe/` (the `u11-` files).

**What was run.** `OwnCloudBackend.write_bytes`, the real `_put` and `_store_put`, with `OWNCLOUD_COMPOSITE_STORES` naming one
throwaway environment id (`g358202-probe-u11-<run>`) and no other, over its own local world directory, its own `RUNTIME_DIR` (so the
sync-manifest baseline stamp was not the live one) and the live S3 client. The source is consecutive retained versions of the live key,
fetched by VersionId and written oldest first: the first write seeds the key and every later one is a real mutation. A botocore hook
counted every call and refused any mutating call outside the probe prefix and any read outside it and the one live key. After each
write the key was read back with `read_authoritative_bytes` and compared byte for byte with the source version, and the local file with
the same bytes. Each run ended by removing every probe object by version id, then listing the prefix and the host's data directory.

**Gzip on, the production state** (`OWNCLOUD_GZIP_STORES=ayoai-mind` in `.claude/settings.json`; the store is on the codec's
allowlist). The newest 201 retained versions when the run began (09:41:07Z to 12:11:03Z, 200 mutations in 2 h 30 min, 1,921 a day):
- 201 writes, 201 readbacks equal to the source, 201 local files equal, 0 whole-object fallbacks, 0 retries, 0 error codes.
- A mutation PUT 2 objects in 197 cases (the head and one changed segment) and 3 objects in 3 (two segments); none changed zero. Bytes
  PUT per mutation: mean 408,015 (min 277,675; median 357,963; p90 539,897; max 862,246). The head is 262,144 of them; a changed
  segment averages 145,871 (median 95,819; max 600,102). The seed write PUT 89 objects, 11,960,708 B.
- The same 200 mutations as today's writer sent them (the stored size of each source version): 2,337,938,108 B against 81,602,942 B
  composite, **28.65x** by ratio of sums; per mutation min 13.56x, median 32.7x, max 42.16x. U9 predicted 24.6x, 26.5x median and
  13.4x worst from 60 pairs spread over a week; the newest window's changed segments are smaller (145,871 B against U9's 196,446 B)
  and the head is 64% of a PUT (U9: 57%).
- 44 of the 201 writes carried a tail in the head (60 ids in all, at most 3 in one head): the live stream exercises the order exception.
- Cross-check against the store: it holds 201 head versions (52,690,944 B) and 291 segment versions (40,872,706 B), 492 objects and
  93,563,650 B, equal to the hook's count of successful PUTs. The host holds 93,807,030 B in 768 files under the prefix (0.26% above),
  and none after cleanup.
- Outcome 5's arithmetic, a projection from this replay and not the fresh listing the outcome asks for: at the window's 1,921 a day the
  head plus segments are 783,776,977 B, 22.4x below the baseline 17,577,753,534 B, and the head key alone 503,566,249 B, 34.9x; at
  U9's 1,516 a day 618,550,285 B (28.4x) and 397,410,304 B (44.2x). Today's writer at 1,921 a day PUTs 22.5 GB, so the 28.65x
  like-for-like figure is the one that does not move with the rate.

**Branches the replay did not enter, and the exercises that did** (guard-2574: a live run proves only the branches its sample entered).
Counting the replay and the exercises, 237 composite writes, none refused.
- Gzip off (the newest 21 versions at 12:26Z; run `gzoff2`): 21 writes, 21 readbacks equal, 0 fallbacks; mean 517,528 B per mutation
  (the changed segment is plain: mean 255,384 B) against 32.6 MB plain for today's writer with the flag off, 63.0x. A first attempt
  aborted after its seed because my own byte-count control compared plain bytes with the gzipped source's size; its cleanup removed the
  89 objects it had made and the listing showed none left. The control was corrected before the run reported here.
- Rollback (the previous version written back, A, B, A; run `gzonex`): the changed segment already exists, so its PUT answered 412 and
  `_freshen_segment` made one HEAD; the segment is young, so no re-PUT. The write stored only the head, 262,144 B, and the readback
  equals the source.
- A write by a process holding no head and no fence over an existing key (the first write after a restart): the handler registry
  dispatches by basename, so `_put`'s empty-fence branch went to `_merge_reconcile_put`, which read the remote whole (89 GETs,
  12,024,170 B), merged and PUT the head only (the merged result's one changed segment already existed: 412). The readback equals the
  file the writer left, so the round trip of the merged bytes is exact. It does not equal the source version, and the cause is not the
  composite layer: run offline on the same two source versions, the handler's result differs from the newer input only in the order of
  one aspiration's goals (asp-326: the same goals, the same line length; the result is in string order of id and the source, which the
  writer had appended to, is not), and `handler(x, x)` is not `x` for either version. A sorted list is what the composite layer stores
  with no tail, as the U10 tests pin.
- Not entered anywhere: a segment old enough for the forced re-PUT (13 days; the unit tests drive it with a clock, and no live object
  can be that old), a 412 on the head from a concurrent writer, GC, the daemon's lock and read-modify-write path above `write_bytes`,
  the daemon's refresh reader (`plan_refresh` reusing local segments; the replay read with `read_authoritative_bytes`), and the LAN
  object cache (unset in the probe).

**What the composite write costs in time, and why gzip on is slower.** Median write: composite with gzip on 1.513 s (n=200; min 1.423,
max 1.780), composite with gzip off 0.569 s (n=20), today's writer on the same probe key 1.102 s (n=10, `composite=off`). A full cold
read through the composite reader takes 0.540 s (median; 89 GETs, 12.0 MB) against 0.134 s for the whole object. Timed alone on one
32.6 MB version: gzip of the whole body 0.951 s (min 0.939, max 0.972), `plan_write` with a held head 0.415 s, gzip of the changed
segment 0.007 s. The on/off difference of the composite write, 0.944 s, equals the whole-body gzip to 0.01 s. In `_put`,
`_body_kwargs` encodes the whole body before `_store_put` runs, and `_store_put` drops that encoding (its `hkw` excludes Body,
ContentEncoding and Metadata) when it takes the composite plan: the encode is computed and thrown away on every composite write with
the flag on. So in the production state the composite writer is 0.41 s slower per write than today's although it PUTs 28.65x fewer
bytes, and encoding only when a whole-object PUT is what goes out would leave the cost the gzip-off run shows, 0.569 s. Not measured:
whether 0.41 s moves lock hold time.

**Not verified in U11.** That the daemon's own paths (lock, refresh) behave the same: only `write_bytes` and
`read_authoritative_bytes` were exercised. Refusal rate and 412 rate under concurrent writers. The fresh 24 h listing of outcome 5,
which needs the flag on production. One 2.5 hour window stands for the stream. The gzip-off run took newer versions than the gzip-on
run, so their per-mutation figures are not paired.

**Open after U11.** Make the encode lazy (done in U12, the next section; a unit of its own: code, tests, the pre-apply consultation); the full suite; the pre-flip
gates (the reducer's merge of worker refs so the cutover tool reads SAFE, and the measurement gauge at the flip); the post-flip GC
runner and the archive pruning rule; outcome 5's fresh listing; the U2e residue listing on or after 2026-10-10T00:00Z; and the
optional head diet.

## The whole-body encode made lazy (U12)

Patched and tested 2026-10-03 on cc-08, then measured against the live MinIO from 14:15:57Z to 14:18:37Z with the U11 harness unchanged
(`u11-replay.py`; its run ids keep the `u11-` prefix because its local cleanup guard matches it). Evidence and a README block:
`world/audit-reports/g-358-202/live-head-and-join-probe/` (the `u12-` files).

**The change.** `core/scripts/owncloud_backend.py`, 16 lines added and 10 removed, comments and docstrings included. `_put` and
`_merge_reconcile_put` no longer call `_body_kwargs`; `_store_put` calls it at the two places a whole-object PUT leaves it: the store is
not composite for this environment (flag off, another environment, a path off the allowlist), and the plan is `None` (a store under
`MIN_RAW_BYTES`, or one `split` refuses). The composite plan never calls it. Each segment is still encoded by `_codec_put_kwargs` on its
own, and the head is plain. The whole-object request is the same request as before: `{**kw, **encoded}` carries the Body,
ContentEncoding and Metadata that the eager `kw` carried. The contract of the `kw` handed to `_store_put` changed from "the whole-object
PUT, Body included" to "the PUT begun, no Body". A caller that still passes a Body gets the encoded one on a whole PUT, and `hkw` drops
Body, ContentEncoding and Metadata from the head as it did.

**Tests.** Three functions, 14 cases, added to the write-path file (44 tests, now 58). A spy on `owncloud_backend._codec_put_kwargs`
records the length of every body the writer hands the codec and calls through. A composite write must encode exactly its segments, never
a body as long as the store (`test_a_composite_write_never_gzips_the_whole_store`, and the composite case of
`test_the_merge_reconcile_site_gzips_the_whole_store_only_when_it_goes_whole`). A write that goes whole must encode the store once and
land a gzip object with the plaintext md5 in its metadata that reads back equal (`test_a_whole_object_put_gzips_the_store_exactly_once`:
flag unset, flag naming another environment, under the size floor, a layout the split refuses; and the whole case of the merge-reconcile
test). On the pre-patch module 4 of the 14 fail (the four composite cases: two tests, each in both S3 flavors, with the extra body of
1,667,542 B in the spy's list) and 10 pass: the ten whole-object cases pin what the patch must not change.

**Mutation matrix** (guard-6701; a throwaway copy of `core/scripts`, 7 mutants, one changed line each, run by
`mutation-partition-proof.sh`, attribution by `mutation-proof-test.sh --junit-xml` one mutant per call): 7 of 7 killed, 7 of 7 cases
proven, 0 survivors, restore byte-verified, and the copy's module equal to the patched one afterwards. Every one of the 14 test cases is
killed by at least two mutants, and the mutants split along the branch lines: an eager encode in `_put` (m01) reddens 10 cases (the
composite case in both flavors and all eight whole-object ones, since each then encodes twice); an eager encode in the composite branch (m02) reddens
the 4 composite cases only; one in `_merge_reconcile_put` (m03) the 4 merge-reconcile cases; a whole PUT that drops the encode or
encodes twice reddens the flag-off cases (m04, m06: 6 each, the 4 flag cases and the 2 merge-whole cases) or the plan-is-`None` cases
(m05, m07: 4 each, under the size floor and layout refused) and nothing else.
The first run reported 7 survivors of 7, and that was the harness, not the tests. The copy lives under the repository, so pytest resolved its
rootdir upward (the repository's `pytest.ini` and root `conftest.py` apply to it) and imported `owncloud_backend` from the shared tree,
the patched module, instead of the mutated copy. A probe test that printed `owncloud_backend.__file__` under the same invocation showed
`/opt/ayoai-mind/core/scripts/owncloud_backend.py`; with `--rootdir` and `--confcutdir` both pinned to the copy, both modules load from
it (which of the two flags matters was not isolated). The first report is kept as `u12-mutation-report-run1-wrongtree.json`.

**Regression group.** `core/scripts/tests/test_owncloud_*.py`, 41 files: 1,251 tests (U10 ran 1,237; the difference is the 14 new
cases), 0 failures, 0 errors, 16 skipped (junit). Run with `STORAGE_S3_ENDPOINT_URL` removed from the environment, so the six
integration tests that U10 listed as red under that leak (g-115-10069) ran against the moto fixture; the run with the variable set was
not repeated in this unit.

**What the live replay measured** (the newest 21 retained versions at each start, 20 mutations a run; same harness, same probe
isolation and cleanup as U11; every write read back equal; box load average 7 to 10 on 20 cores):

| run | load | mutations | write p50 | p90 | min to max |
|---|---|---|---|---|---|
| composite, gzip on (R1) | 7.11 | 20 | 0.566 s | 0.652 | 0.529 to 0.692 |
| composite, gzip on (R1b) | 8.97 | 20 | 0.601 s | 0.645 | 0.519 to 0.663 |
| composite, gzip off | 10.35 | 20 | 0.570 s | 0.608 | 0.521 to 0.664 |
| whole-object writer, gzip on (R2) | 9.77 | 10 | 1.178 s | 1.309 | 1.123 to 1.316 |
| whole-object writer, gzip on (R2b) | 9.19 | 10 | 1.154 s | 1.175 | 1.092 to 1.189 |
| U11, composite, gzip on, before the patch | 4.71 | 200 | 1.513 s | 1.599 | 1.423 to 1.780 |

Pooled over R1 and R1b, composite with gzip on writes in a median 0.579 s (n=40, p90 0.652); the whole-object writer takes 1.154 s
(n=20) in the same session, so the composite write is 1.99x as fast and 0.575 s shorter (before the patch it was 0.411 s longer). The
gzip-on minus gzip-off gap is 0.009 s (U11: 0.944 s). The drop from U11's median is 0.934 s, against 0.951 s measured in U11 for gzip of
the whole body alone. Every composite run read back 21 of 21 equal with 0 whole-object PUTs and 0 segment 412s; each ended with 129
probe versions removed and 0 left, and the final sweep found 0 versions, 0 delete markers and 0 current keys under any `g358202-probe-`
prefix, none on the host, none locally, and the live key present. The bytes are not this unit's subject and did not move by
construction (the encode only stopped being computed twice): per mutation 454,076 and 467,091 B on average, 25.9x and 25.2x below the
whole-object PUT on these windows (U11: 28.65x over 200; U9 predicted 24.6x). One mutation per window (source version 84670bd3, in
all three composite windows) changed no segment: one head PUT of 262,144 B and no segment HEAD; which head field it changed was not
examined. The seed write over an empty key costs 2.34 s and 2.52 s with gzip on (89 objects, the head and 88 segments, 12.05 MB) against
1.22 s for the whole-object seed; it is paid once per key, by the first flagged write.

**Not verified in U12.** Whether the daemon's lock hold time falls by the same 0.93 s (only `write_bytes` was timed, and no daemon ran
with the flag on). Refusal and 412 rates under concurrent writers. The full suite (owed before the goal closes). The live replay
entered the branches U11's replay entered (no forced re-PUT of an old segment, no 412 on the head), and U11's rollback and cold-process
exercises were not repeated.

**Open after U12.** The full suite as its own unit; the pre-flip gates: `store-cutover-check.sh --store composite` read UNSAFE at
2026-10-03T13:20:13Z (`origin_main_does_not_call_the_seam_symbols`: only alpha attested, derived from its worker ref at 797f0298a;
bravo, echo, foxtrot and zeta unattested, `seam_not_ancestor`), so the reducer's merge of worker refs comes first, and the measurement
gauge at the flip; the post-flip GC runner and the archive pruning rule; outcome 5's fresh listing; the U2e residue listing on or after
2026-10-10T00:00Z; and the optional head diet.

## The full suite at the U12 head (U13)

Run 2026-10-03 on cc-08 from 15:38:38Z to 16:46:11Z (67.6 min) at HEAD a360f8f888, which carries U1 to U12 and the g-115-10980 commits
(ancestry checked; origin/main 8ae77392f5 carries none of them). Worker Body, main repo, live daemon not restarted,
`STORAGE_BACKEND=local`, runner-default 4 chunks, 1,696 files, composite flag off, HEAD equal at launch and at exit. Evidence, a README and
the run record: `world/audit-reports/g-358-202/u13-full-suite/`; the dated entry headed 2026-10-03T15:38Z is in
`core/config/run-full-suite-baselines.md`.

**Result.** `TOTAL: 28187 passed, 33 failed, 0 errors`, `VERDICT: GENUINE`; invisible half 81 of 84 files, domain half 121 of 122
units. 19 files carry the 33 reds; triage: 3 environmental, 13 genuine-owned, 3 genuine-VERIFY, 0 unowned. None of the 19 is a
composite-writer or own-cloud test; all 11 `test_owncloud_composite_*_g358202.py` files ran in chunk 02, which failed 2 tests in 2 other
files. The three VERIFY files were opened: `test_completed_not_committed_scoped_probe` (g-115-6350 names the test),
`test_iteration_close_quality_flag_carry` (g-115-9181, the same 9 reds as before), and `test_daemon_import_surface` (named by no goal;
U8 had already relayed it: `knowledge_projection` is missing from the `mind-api-code-changed.sh` pathspec since c621d7cac1, and the relay
is still queued). The pathspec does cover the composite module: its second entry is the glob `core/scripts/_*.py`.

**The moto gap, measured.** The chunked run uses the system python, which has no moto. 18 test files import it, three of them composite
(`gc_apply`, `gc_versions`, `read`); the other eight composite files do not, and ran in full. Same 18 files, same HEAD: 169 passed and
145 skipped under the system python (pytest 7.4.4), 519 passed and 4 skipped under an isolated venv (moto 5.2.3, pytest 9.1.1), so the
chunked run established 350 fewer cases than the venv did. Those 350 rest on the venv run (15:31:55Z, before the launch), not on the
suite. All 7 `test_owncloud_integration` tests pass under the venv with the endpoint variable unset; the six reds the 2026-10-01 entry
assigned to g-115-10069 did not appear in the chunked run because that file skips at module level on this box.

**What it does not establish.** The suite ran with the flag off, so it shows the flag-off path unchanged and says nothing about the
flag-on path (U4b's fixtures and U11's replay are that coverage). No A/B was run on the 19 files against the tree before this goal's first
commit; the claim that they are not this goal's rests on their owners and on the identified causes. The `daemon_integration` subset is
excluded by design. The system pytest ignores `faulthandler_exit_on_timeout` (g-115-6801), so a hung test would not have aborted at
600 s; none hung.

**Side effects.** The two WM cadence slots and the iteration checkpoint were not touched. The suite restamped team-state
`shared_cadences.last_fresh_eyes_tree_review` at 15:50:54 in chunk 01. The writer is `test_fresh_eyes_record_tick_unknown_flag.py` (run
solo, it moved the key; g-115-6065 names it). The stamp was not a real fire (count 16042 against 16037 at the real 14:56:53 fire, in a
25-goal window), and the pre-launch value was restored and read back byte-identical.

**Open after U13.** The pre-flip gates: the reducer's merge of worker refs, so that `store-cutover-check.sh --store composite` reads
SAFE (it read UNSAFE at 2026-10-03T13:20:13Z, `origin_main_does_not_call_the_seam_symbols`, and origin/main still carries none of U11,
U12 or the g-115-10980 commits), and the measurement gauge at the flip; the post-flip GC runner design and the archive pruning rule;
outcome 5's fresh listing (needs the flag on production); the U2e residue listing on or after 2026-10-10T00:00Z; and the optional head
diet. The goal is not closed: the flip has not happened.

## The scheduled GC pass (U14)

*Superseded in part by "The tick and the flip checklist (U15)" below: the runner is no longer unscheduled, the live lifecycle config has
been read, and the "Open after U14" paragraph at the end of this section is replaced by that section's checklist, and "Anomaly routing (U16)" is the caller that posts. What follows is the
record as of U14.*

U2e built the delete pass, U6 and U8 hardened it, and nothing called it. `core/scripts/composite_gc_runner.py` (new) is the caller: one
`run_pass` takes a lease, reads a cadence stamp, enumerates or applies, keeps the first-seen ledger, gives every recent archive run its
late restore, writes the stamp and returns one result dict. The backend gained three methods (`composite_gc_runs`,
`composite_gc_state_get`, `composite_gc_state_put`) and `_owncloud_composite.py` the key builder `gc_state_key`, the run-id inverse
`gc_run_time`, `GC_STATE_DIR` and `GC_STATE_DOCS`. It is dark: nothing schedules the runner, no flag was set anywhere, and no S3 call
from this code was made against the production store. It answers the first two bullets of the U2e list (who runs the pass, how often,
where the ledger lives, the lock; the late restore). The pruning bullet stands.

**Decisions.** Values marked "choice" are mine, not measurements.
- *Own process, never the daemon.* `mind_api/state/daemon.log` on cc-08 records eight stop/start pairs on 2026-10-03 up to 18:28 UTC
  (00:41, 03:26, 08:10, 10:23, 11:47, 14:25, 17:20, 18:28; gaps 68 min to 4 h 44 min, mean 2.5 h). The one at 17:20 cut this goal's
  claim write off mid-request (below). A pass hosted in the daemon would be killed that often. Every one of the eight falls 0 to 11 s
  after a HEAD move in the git reflog (five after a `merge ... Fast-forward`, three after this goal's own U6, U8 and U10 commits); the
  hook that restarts the daemon was not read, so that is an association and not a mechanism (table:
  `world/audit-reports/g-358-202/u14-gc-runner/restart/restart-vs-head-moves.txt`).
- *Who and when.* Any caller on any box; exclusivity is a lease, not a role. `acquire_lock` on `<store>.gc.lock` is a conditional put
  (rb-9975), tried once (`timeout=0`), TTL 1800 s (choice); a caller that does not get it returns `lease-held`. The cadence test and the
  action are both fleet-wide, so the stamp lives in the object store and N callers cost N small reads and one pass (guard-2585; the
  cold-snapshot tick is the precedent). `INTERVAL_S` is 4 h (choice): orphans arrive at about 1,540 to 1,950 a day (1,516 mutations a
  day in U9 to 1,921 in U11, at 1.015 changed segments each, U11's 203 over 200), the cap is 500 a pass (`GC_MAX_DELETE`), so six passes
  a day delete up to 3,000 against that arrival, a margin of 1.54x to 1.95x. A stamp in the future is treated as due and overwritten,
  because honouring it would stop collection until the clock caught up.
- *Where the state lives.* `<env>/_composite-gc-archive/_state/<store>/{state,ledger}.json`, beside the runs and outside the governed
  roots, so the sync layer never mirrors it. The ledger holds one first sighting per unlisted name, so it is bounded by the standing
  orphan set (about 14 days of arrivals, 21,600 to 27,300 names by that rate; an estimate). **Unreadable is not empty:** a stamp or
  ledger that cannot be read, or is the wrong shape, stops the pass with no write, because a caller that took it for empty would
  overwrite 14 days of sightings with a fresh start. An entry that is not a finite number in (0, now] is dropped and its grace restarts,
  since a bogus zero would date a long-lived object as old on the day it first became an orphan.
- *Dry by default (guard-1301, guard-3925).* A pass observes: it enumerates, records sightings and reports `would_delete`. Deleting
  needs `--apply` AND the backend's own flag (`OWNCLOUD_COMPOSITE_GC`); either alone deletes nothing. The runner computes no licence: it
  asks `composite_gc_apply` and reads `gc-not-enabled` or `grace-below-floor` as "observe instead". Its own flag test (either flag names
  the environment) only decides whether to touch the store, so it can cause fewer calls and never a delete.
- *Late restore, stateless.* The runs to visit are listed from the archive, not remembered, so a pass that died before recording its own
  run still gets its sweep. A run is visited while its age is under `TRUST_WINDOW_S` (24 h, choice: the gap a late head commit can land
  in is seconds to minutes, U6) plus one interval, so the last visit falls after the window closes. It needs neither flag: recovery must
  not depend on the switch that enables deletion. A run directory with no receipt deleted nothing (the receipt precedes the first delete)
  and is skipped silently. An unreadable ledger does not stop it either.
- *Retries and noise.* A writer commit during the listing abandons that attempt; the pass is attempted up to 3 times, 2 s apart (choice),
  and then reads `busy`, as does a legacy whole file at the key (`not-yet-composite`); neither is an anomaly. `post` is None unless
  something was deleted or an anomaly needs a human line, and the runner never posts: a caller that posts owns the channel and the
  de-duplication.

**Verified.** The runner over a real `OwnCloudBackend` on four stores (an in-memory fake and moto, each plain and bucket-versioned),
with the writer producing real heads and segments: 54 test functions, 171 passed and 2 skipped under the moto venv (moto 5.2.3), 101
passed and 72 skipped under the system python (no moto), 0 failed. The default path runs with every parameter at its default, and each
absence-shaped claim (nothing deleted, no S3 call, ledger bytes unchanged) is paired with a control that can fail: the same state with
both licences deletes. Mutation proof (guard-6701): six throwaway copies of `core/scripts` with the rootdir and confcutdir pinned and
each module's `__file__` read back from inside the copy, a green control in every tree before the mutants and one after (171 passed
each), then 80 single-site mutants (64 in the runner, 11 in the backend, 5 in the composite module). The first pass of 73 left one
survivor, `ledger-repair-not-persisted`: the end-to-end case put its invalid entry on the orphan, so the plan's own change forced the
write and masked the repair term. A case with the orphan's real sighting already stored isolates it, and the survivor exposed the gaps
behind seven more mutants. The final run on the final files killed 80 of 80. Fast tier `run-scoped-suite.sh`: 58 of 1,697 files
(3.42%), 0 unmapped, `PASS_WITH_SKIPS`. It qualified 23 files as having "ran 0 tests"; the same interpreter runs 386 tests in 12 of
them (only 11 are module-level skips), because its discriminator reads `could not import 'moto'` from an `importorskip` inside a
fixture as a module-level skip. I ran the 22 files other than this one under the venv (714 passed, 16 skipped), and the whole
own-cloud group with this file (42 files): 1,406 passed, 18 skipped, 0 failed.

**The claim that straddled a restart.** `aspirations-claim.sh` for this goal integrated 40 origin commits first (the git reflog has
HEAD fast-forwarding to 2fc9eb9f89 at 17:20:32); the daemon restarted at 17:20:35, three seconds later (that the post-merge restart
hook did it is inferred from the timing, not read), and the write returned `WRITE OUTCOME UNKNOWN` (curl exit 52). Before sending
again I read whether it had landed: a daemon query for goals claimed by this session returned `[]`, the pre-restart pid (297564) no
longer ran, a direct read of the store of record gave 32,903,438 B and 25 records with none unparseable and no claim keys on the
goal, and the local mirror and the access log showed none either. I then re-sent once, as a stated choice against the wrapper's "next
iteration" line, and it landed. `executed_by_sid` alone is not proof of a claim: it was stale from earlier claims by the same
session, and my first probe filtered on the word "claim" and missed it.

**Not established.** A pass at the delete cap has not been timed, so the 30-minute lease is a margin on an unmeasured duration. The rate
of a writer commit during a listing is unmeasured (3 attempts is a choice). A name that flips between referenced and unreferenced across
two passes keeps its old first sighting (`plan_gc` documents this residual). The archive prefix's live lifecycle config is unread
(guard-1301 asks for it before any environment is named for deletion). Nothing ran against production, so the runner's behaviour on the
live store, including how long the ledger document takes to read at its real size, is unmeasured. No full suite was run this unit
(`owncloud_backend.py` is a storage-backend file; the fast tier and the own-cloud group were), and the full run is owed at closure.

**Open after U14.** Scheduling the runner: the next unit wires a tick (a `.sh` wrapper, per the Python-invocation rule) and writes the
flip checklist. Before any environment is named in `OWNCLOUD_COMPOSITE_GC`: at least one observe pass read back (`state.json`,
`ledger.json`), `would_delete` set against the arrival estimate, and the archive prefix's lifecycle config read. The pre-flip gates (the
reducer's merge of worker refs so `store-cutover-check.sh --store composite` reads SAFE, and the measurement gauge at the flip); the
archive pruning rule (it must skip `_state/` and owes its own archive-before-delete); outcome 5's fresh listing (needs the flag on
production); the U2e residue listing on or after 2026-10-10T00:00Z; and the optional head diet. The goal is not closed: the flip has not
happened.

## The tick and the flip checklist (U15)

U14 built the runner and left it dark. U15a schedules it, observe-only, and this section fixes the order in which the flip may happen. It
supersedes U14's "It is dark" sentence and its "Open after U14" paragraph; those stay above as the record of what was known then. Every
reading below was taken on cc-08 between 2026-10-03T23:45Z and 2026-10-04T00:10Z unless it names another source. "Predicted" marks arithmetic on
measured inputs, never a measurement; what was not run is listed under "Not established".

**What landed (U15a, commit 709dad3066 on the carried worker ref).** `core/scripts/composite-gc-tick.sh`; a `composite_gc_tick` block in
`core/config/aspirations.yaml` (`enabled: true`, `interval_minutes: 30`); one call in `iteration-close.sh` productivity-check beside the
eviction tick; `test_composite_gc_tick_g358202.py` (45 tests, 44 of 44 mutants killed). It has the eviction tick's shape (a fail-safe config
probe, a box-local stamp that only throttles the spawn, a per-box lock directory, a backgrounded pass, one log) with one deliberate
difference: it starts the runner with no argument at all and sets no flag, so it can never pass `--apply`. The fleet's cadence authority
is the runner's `state.json` (4 h), not the box-local stamp.

**Where it runs decides which box needs a flag.** Only the reducer's productivity-check calls the tick (a worker's loop skips that phase),
so the tick starts only when the reducer's tree holds it. Measured: `core/scripts/composite-gc-tick.sh` and
`core/scripts/composite_gc_runner.py` are not in `origin/main` (`git cat-file -e` rc 128 for both, rc 0 for `_owncloud_composite.py` as the
control); they are on `refs/workers/alpha/7d31b51a-9ae4-447e-98e3-7923190bc4bf` (remote tip 6f28c3e39c). And `run_pass` returns `inactive`
with no S3 call unless THIS box's own environment names the deployment in `OWNCLOUD_COMPOSITE_STORES` or `OWNCLOUD_COMPOSITE_GC`. So the
reducer's box must carry a flag: if only other boxes carry one, they write composite heads while the one box that observes and sweeps logs
`inactive` every 30 minutes. Flags are set the way the gzip flag is, as a key in the `env` object of `.claude/settings.json`, and a process
reads it at its next start. Measured: the running daemon and this session's shell both carry `OWNCLOUD_GZIP_STORES=ayoai-mind`, and neither
carries an `OWNCLOUD_COMPOSITE_*` variable. The runner inherits the environment of the shell that runs `iteration-close.sh`, not the
daemon's.

**The order of the two flags (this corrects a docstring).** U2e's `should_gc` docstring said the delete flag must be named before the
writer's, so that a writer never runs with no collector. That was written when the delete flag was the only way a collector could exist.
Three measured facts set the order now. The delete flag (`should_gc`) is read in exactly two places outside tests, the gate inside
`composite_gc_apply` and the runner's activity gate, and the writer's freshen (`_freshen_segment`) is not one of them, so the writer behaves
the same with it unnamed. Nothing is collectable until an orphan is `GC_GRACE_S` (14 d) old and a delete pass refuses a shorter grace, so a
collector armed at the writer's first minute collects nothing that one armed at day 14 would not. And an observe pass runs on the writer
flag alone. Naming the delete flag early would remove one of the two switches that arm deletion (the other is `--apply` in the tick's
command line) 14 days before it could matter. So the writer flag goes first and the delete flag last, in the commit that adds `--apply`.
The docstring says so now.

**What each trace proves.** An observe pass leaves four traces. They prove different things, and two of them are easy to over-read.

| Trace | Written by | Proves | Does not prove |
|---|---|---|---|
| `core/logs/composite-gc-tick.log`, on the reducer's box | the tick: a header (time, rc, agent, seconds the pass took), the last 5 lines the runner printed and, since U16, one `composite-gc route:` line per posting decision | the tick fired, and the runner's JSON line: `verdict`, `counts`, `would_delete`, `attempts`, `elapsed_s`, `anomalies` | that the two documents reached the store: the runner exits 1 on any anomaly (`state-write-failed` and `ledger-write-failed` among them), so rc 0 with a verdict is the claim |
| `state.json` | `_locked_pass`, after every pass that gets past the cadence check, whatever its verdict | `last_pass`, `last_verdict` and `last_counts` of the newest pass | anything for `inactive`, `lease-held`, `lease-unavailable`, `not-due`, `state-unreadable` and `not-own-cloud`: none of them writes it, so its absence means no pass completed, not that one failed |
| `ledger.json` | `_collect`, only when the ledger changed, was repaired or was never stored | the standing orphan set, one first sighting per unlisted name: after a pass that was not refused, `entries` equals `counts.orphans` | the time of the last pass: `updated` is the last CHANGE |
| `_composite-gc-archive/<run>/` | an apply pass only | what a delete pass removed, with its receipt | nothing in observe: expect no run directory (U2e's probe residue sits there as noncurrent versions) |

Verdicts: `inactive` (no flag names this box's environment; no S3 call), `not-due` (a pass ran within the interval; `next_due_in_s` says
when), `lease-held` (another caller holds the fleet lease), `not-yet-composite` and `busy` (routine: the key is still a whole file, or the
writer committed during all 3 listings), `observed` (a complete observe pass), `applied` (a delete pass). Rc 1 with a non-empty `anomalies`
goes with `error:`, `refused:`, `ledger-unreadable`, `state-unreadable` and `lease-unavailable`; rc 2 is `setup-failed`.

The two read-backs, as run:

```
# the tick's own record (on the reducer's box; a worker never runs the tick, so the file is absent there)
tail -n 8 core/logs/composite-gc-tick.log

# the two documents, from the store of record (any box; read-only)
STORAGE_BACKEND=own-cloud py -3 - <<'PY'
import sys, json, time
sys.path.insert(0, "core/scripts")
from pathlib import Path
import _paths, composite_gc_runner as R
from storage_backend import get_backend
be = get_backend()
path = Path(_paths.WORLD_DIR) / Path(R.STORE_REL).name
iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
bad = 0
for doc in ("state", "ledger"):
    try:
        d = be.composite_gc_state_get(path, doc)
    except Exception as exc:
        bad = 3
        print(doc, "UNREADABLE (not the same as absent):", type(exc).__name__, str(exc)[:120])
        continue
    if d is None:
        print(doc, "absent (never written)")
    elif doc == "state":
        print("state present:", json.dumps({k: d.get(k) for k in ("last_pass", "last_verdict", "last_counts")}, sort_keys=True))
    else:
        led = d.get("ledger")
        if not isinstance(led, dict):
            bad = 3
            print("ledger present but NOT A LEDGER (the runner stops on it as ledger-unreadable): keys=%s" % sorted(d))
            continue
        first = sorted(led.values())
        print("ledger present: entries=%d updated=%s oldest_first_seen=%s" % (len(led), d.get("updated"), iso(first[0]) if first else None))
sys.exit(bad)
PY
```

On 2026-10-03 the log is absent on cc-08 (rc 1, as it must be on a worker) and the documents read `state absent (never written)` and
`ledger absent (never written)`, rc 0: no pass has ever completed against the live store. The control, the same code over a path that is
not a composite store, prints `UNREADABLE (not the same as absent): CompositeError ...` for both, rc 3, so the script cannot read an
unreadable store as an empty one. The present-document branch has no live specimen yet. It was run over the documents `run_pass` itself
wrote to the test double (verdict `observed`, `ledger` `entries=5`). The first live observe pass is the first live specimen; read it as a
specimen and compare its field names with these before trusting a later read.

**How long the observe window is, and what to expect in it.** A delete pass needs a grace of at least `GC_GRACE_S` = 14 d, and an
orphan's clock is the later of its first sighting and its own `last_modified`; no orphan is older than the writer (the live segment
directory held 0 keys while the writer was off, U2e). So with T0 the time the first box writes a composite head, `deletable` is 0 until
about T0 + 14 d, and until then an observe pass can be judged on the ledger against arrivals alone. Three 24 h readings of the head key's
PUT count exist: 1,516 (2026-09-21, the goal's baseline), 2,895 (2026-10-02, the churn alarm, g-358-228) and 2,315 (2026-10-03, item 4).
Orphans arrive at about 1.015 per mutation (U11: 203 changed segments over 200 mutations), so 1,540 to 2,938 a day, 2,350 at the latest
reading (98 an hour, about 390 after the first 4 h pass). A pass deletes at most `GC_MAX_DELETE` = 500 and runs every 4 h, so the cap
allows 3,000 a day: 1.28x the latest reading and 1.02x the peak one. U14's margin of 1.54x to 1.95x was drawn on 1,540 to 1,950 a day and
did not contain the peak. The standing set 14 days in is 21,600 to 41,100 names (3.1 to 6.0 GB at U11's mean changed segment of 145,871 B;
U14 said 21,600 to 27,300), and 32,900 names and 4.8 GB at the latest reading; each further day adds about 2,350 names and 0.34 GB. The
cap and the interval are choices (U14); the arming commit re-derives them from the peak, not the mean.

**The checklist, in order.** Items 1 to 4 are reads and can be repeated at any time. Item 5 is the first act and needs items 1 and 4.
Item 6 follows 5. Item 7 follows 6 and is one commit.

1. **The readers are attested.**
   ```
   bash core/scripts/store-cutover-check.sh --store composite     # rc 0 SAFE, 2 UNSAFE, 3 error; fails closed
   ```
   On 2026-10-03: rc 2, UNSAFE, reason `origin_main_does_not_call_the_seam_symbols`. Alpha is attested through its worker-ref lane only;
   bravo, echo, foxtrot and zeta read `seam_not_ancestor`; `seam_symbols.missing` names `core/scripts/worker_stall.py` and
   `mind_api/src/endpoints/aspirations_write.py` (seam commit 0913fcd2c). It clears when the reducer has merged the worker ref that carries
   U3 onward and each agent has made an iteration commit after the merge. The tick, the runner and the writer ride the same merge, so
   this one gate holds the whole list. Pass: rc 0. **Re-read at U22 (2026-10-04T15:15:35Z): rc 0, SAFE.** All five live agents attested by
   derivation (alpha b3601ef26, bravo ff5b834da, echo 5d5fa93ab, foxtrot 722010c34, zeta 92a0ffeb3; ages 0.0 to 0.1 d), `unattested` and
   `stale` empty, `seam_symbols.ok` complete with `missing` empty, the local box (zc-02) carries the seam. The U20/U21 cause cleared
   exactly the way it was predicted to: the merge landed and the three unattested agents each made an iteration commit after pulling it.
   **Re-read at U30 (2026-10-05T10:08Z): rc 0, SAFE again, now with the per-Body lane complete** — per_body 18 of 18 proven,
   `all_proven` true, `enumeration_complete` true, `distinct_bases` 6; the UNSAFE `per_body_reader_unproven` of 2026-10-04 (12 of 18 at
   20:51Z, 14 of 18 at 21:35Z) cleared when the boxes pulled the merge and their ticks published `main_base`. Item 5's writer flag is
   committed on an alpha worker ref ("The writer flag (U30)"); the rollout read-backs are per box.
2. **The tick's code is in the reducer's tree.**
   ```
   git fetch -q origin; git cat-file -e origin/main:core/scripts/composite-gc-tick.sh; echo "tick rc=$?"     # 0 present, 128 absent
   ```
   On 2026-10-03 absent (the runner too; the control, `_owncloud_composite.py`, present). It follows from item 1 and is listed because
   the tick log of item 6 does not exist without it.
3. **The lifecycle configuration is read, by a principal that can read it.** The fleet principal cannot: `get_bucket_lifecycle_configuration`
   answered `AccessDenied` (403) from cc-08, and U2e recorded it unreadable on the older store. Archive-before-delete step 2 treats
   unreadable as absent, so the read comes from the storage host, as root, through the documented route (`ssh rack`, not `ssh zakbox1`:
   `world/conventions/aws-exit-cutover-runbook.md`, "Reaching the host's shell from a container"):
   ```
   ssh -o BatchMode=yes rack '[ "$(hostname)" = zakbox1 ] || { echo "wrong host: $(hostname)" >&2; exit 9; }; mc ilm rule ls zakbox1/zds-own-cloud-data --json'
   ```
   It reads and changes nothing, and nothing in this checklist adds, edits or removes a rule: a rule change that expires anything is its
   own archive-before-delete question. The host assertion trips on a wrong host (control: expecting another hostname printed
   `wrong host: zakbox1`, rc 9). On 2026-10-03: `updatedAt` 2026-09-28T06:42:33Z, five rules, all enabled: a bucket-wide
   noncurrent-version expiry of 7 days, and a noncurrent-version expiry of 1 day under four key prefixes (`ayoai-mind/world/team-state/`,
   `ayoai-mind/meta/spark-questions.jsonl`, `ayoai-mind/world/audit-reports/fleet-sweep-stamp.json`, `ayoai-mind/world/pipeline-meta.json`).
   No rule carries `Expiration` or a transition, so no retention clock reaches the archive's current-version copies. This replaces U2e's
   "by the provisioning intent and not by a read of the live config". Pass: no rule acts on current versions; item 7 re-reads and compares
   `updatedAt`.
4. **The gauge is wired in the same change as the flag (rb-8270).** It is: recurring goal g-358-18 (interval 64 h, last occurrence
   2026-10-02T20:17:09, 11 occurrences) runs `s3-store-churn.sh` and then the churn alarm on one live listing, so nothing is to be built.
   A 64 h cadence cannot date a flip, so the flip owes two manual readings, one before the first box carries the flag and one 24 h after
   the last box restarted with it:
   ```
   source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/s3-store-churn.sh" --window-hours 24 --top 8
   ```
   It takes about a minute; `--reuse-pages` re-aggregates the pages it fetched; rc 4, or a `control=` other than PASS, means the run is
   INVALID. Reading on 2026-10-03 (window ending about 23:51Z, `control=PASS`, 8 pages for 8 prefixes) and again at 2026-10-04T00:03Z (this
   exact command, rc 0, 45 s): the head key took 2,315 and then 2,318 versions, 25.4 GiB both times, in 24 h. It holds 21,973 versions and
   232.4 GiB in all (34.2% of the bucket's 679.0 GiB), then 19,644 and 209.6 GiB (32.8% of 638.4 GiB) twelve minutes later: the drop and the
   shift of its oldest retained version from 2026-09-25 to 2026-09-26 read as a lifecycle sweep between the two reads. The retained volume
   (outcome 6's input; segmentation does not reclaim it) is therefore a moving figure, and the 24 h rate is not. A third manual reading
   landed at U22 (2026-10-04, window ending about 15:15Z, this exact command, both controls PASS): the head key took 2,110 versions and
   23.2 GiB (24,910,572,559 B) in 24 h, and holds 20,911 versions and 223.6 GiB (34.0% of the 658.3 GiB bucket) in all. Outcome 5's bar is a fixed 1,757,775,353 B (a tenth of the 17,577,753,534 B baseline) in a fresh
   24 h window. Predicted at U11's mean of 408,015 B per mutation: 944,554,725 B at 2,315 mutations and 1,181,203,425 B at the 2,895 peak;
   the bar is reached at 4,308 mutations a day. Pass: the post-flip window is under the bar, and it is read beside the pre-flip reading
   from this item's first manual run, because the rate moves 1.9x between days. The window opens after item 5's pass, not at the commit:
   a window that contains a process without the flag measures the mixed fleet, and about 70 whole-object writes a day fail it at the
   designed arrival (U21, "The writer flag over a mixed fleet").
5. **The writer flag**, on every box including the reducer's, only after item 1 reads SAFE: add `"OWNCLOUD_COMPOSITE_STORES": "ayoai-mind"`
   to the `env` object in `.claude/settings.json`, beside `OWNCLOUD_GZIP_STORES`. Each process reads it at its next start, so the rollout
   is per box and takes days (rb-8270: readers attested, flip landed and rollout complete are three different states). Read it on each
   box, names only, because the full environment holds credentials:
   ```
   tr '\0' '\n' < /proc/$(cat mind_api/state/daemon.pid)/environ | grep -E '^(OWNCLOUD_GZIP_STORES|OWNCLOUD_COMPOSITE_STORES|OWNCLOUD_COMPOSITE_GC|ENVIRONMENT_ID)='
   env | grep -E '^(OWNCLOUD_GZIP_STORES|OWNCLOUD_COMPOSITE_STORES|OWNCLOUD_COMPOSITE_GC|ENVIRONMENT_ID)='     # the shell the tick runs in
   ```
   The gzip line is the positive control: a read that prints it and not the composite line means the composite flag is not set. On cc-08
   (daemon read 2026-10-03, shell read 2026-10-04T00:02Z) both commands print `ENVIRONMENT_ID=ayoai-mind` and `OWNCLOUD_GZIP_STORES=ayoai-mind` and no composite line. Pass: the
   composite line is present in the daemon and in the shell of every box, the reducer's included. A commit that touches only
   `.claude/settings.json` restarts no daemon (`mind-api-code-changed.sh` names no settings path): each daemon takes the flag at its next
   spawn for any other reason, and the reducer's iteration-close shell at its session's next launch (U21; on cc-07 the daemon's starts
   were 1.6 h apart at the median and 16.1 h at most over nine days, which is one box).
6. **Observe passes, read back.** Once the flag has reached the reducer's box and one productivity-check has run (a pass starts within
   about 30 minutes, then every 4 h), read the log and the documents with the two read-backs above. Pass, at the first read and at
   T0 + 1 d: (a) `verdict` `observed`, or `not-yet-composite` until the first composite head exists, with rc 0 and no anomalies; (b)
   `state.last_pass` within one interval of the read; (c) `ledger.entries` equals `counts.orphans` and grows by roughly the arrival rate
   above (a factor of 2 either way is a finding before it is a number); (d) `unknown` 0; (e) `elapsed_s` recorded. `deletable` stays 0
   until T0 + 14 d, and the read at T0 + 14 d is the dry run of the first real batch (`deletable` is the smaller of 500 and `aged`).
   (U26: read back at a probe, a to e passed.)
7. **Before `--apply`**: all of these, then one commit.
   - U16, the anomaly routing, is in the reducer's tree with the tick (item 2). The rehearsal this bullet used to owe has run once, from cc-08
     on 2026-10-04: the real `board-post.sh` and its daemon carried the tick's post (`msg-20261004-024627-alpha-142`) and it was read back by id
     ("The live post rehearsal (U18)"). Still owed: the same run from the reducer's box once its tree holds the tick, because that one ran from
     a worker Body's shell on cc-08 and the reducer starts the tick backgrounded from `iteration-close.sh`. The procedure and the stand-in
     runner are in that section; the U16 section has the routing itself ("Anomaly routing (U16)").
   - The wall-clock bound on the runner has landed (U17): `timeout -k 30 1500` in `run_pass`, 270 s under `LEASE_TTL_S` (1,800 s), which the runner
     never renews. Read in U24 against a timed pass at the real cap on a store holding 40,149 orphans (a throwaway store on the live MinIO): the
     apply pass took 19.57 s, 1.3% of the bound, and an observe pass 3.25 s ("The runner timed on the live store (U24)"). Still owed: the same
     reading from the first real observe passes' `elapsed_s` (item 6e), because the real store's contention and the real ledger were not in that
     run. A pass that nears the bound means the bound or the store's size is wrong, and the post says so. See "The bound on the runner (U17)".
   - The cap and the interval re-derived from the peak arrival, with a fresh item 4 reading. U22 re-derived (PREDICTED): the fresh reading
     is 2,110 versions / 23.2 GiB, under the 2,895 peak, so the current choice (500 / 4 h, 3,000 names a day) keeps 1.02x the peak and 1.40x
     this reading. The levers for a wider margin, derived not chosen: 500 / 2 h or 1,000 / 4 h (6,000 a day, 2.04x the peak). The arming
     commit makes the choice.
   - The lifecycle config re-read (item 3), `updatedAt` still 2026-09-28T06:42:33Z, or every rule read again.
   - At least one read at T0 + 14 d or later showing the first non-zero `deletable`.
   - The commit itself: it names `ayoai-mind` in `OWNCLOUD_COMPOSITE_GC` (the `env` object in `.claude/settings.json`), adds `--apply` to
     the one runner line of `composite-gc-tick.sh`, and changes `test_the_runner_is_started_with_no_argument_at_all` in
     `core/scripts/tests/test_composite_gc_tick_g358202.py`. That static pin turns red the moment `--apply` is added, so the edit cannot land
     without the test changing in the same commit; naming the flag alone leaves it green and deletes nothing. To disarm, drop either one:
     each alone deletes nothing, and `composite_gc_tick.enabled: false` stops the tick entirely.
   - The deadline: arm within a few passes of T0 + 14 d. Each further day adds about 2,350 names and 0.34 GB to the standing set.

**What the flip will trip (predicted, not observed).** The churn alarm files when a key takes more than 500 versions AND more than
100 MiB in 24 h, or more than 2 GiB. After the flip the head key still takes one PUT per mutation at the stored head size of 262,144 B
(U10, U11), so 2,315 mutations are 579 MiB (379 MiB at the low reading, 723 MiB at the peak): the versions limb keeps firing and the bytes
limb stops. The alarm's dedup holds only while an Investigate for the key is open, and g-358-228 is completed. Decide before the flip
whether the alarm learns the composite head (it already holds date-rotated streams to a designed envelope on the versions limb) or an open
Investigate stays as the tracker. Decided in U20: the alarm learns the head, and this prediction was replayed there.

**Still open, updated at U30 (2026-10-05).** The archive prune executor
(it must skip `_state/` and `_pruned/`, owes its own archive-before-delete and lands dark; "The archive pruning rule (U28)"); item 5's
per-box flag read-backs (the flag is committed on an alpha worker ref; "The writer flag (U30)"); item 6's observe passes, which start
once the flag has reached the reducer's box and one productivity-check has run; item 7's arming commit; outcome 5's fresh listing, whose
window opens after the LAST box restarts with the flag; the U2e residue listing on or after 2026-10-10T00:00Z; and the optional head diet.
The goal is not closed: the flip is committed but has reached no box's running process yet.

**Not established.** A delete pass at the cap was timed in U24 on a throwaway store (19.57 s at 500, 3.25 s to observe 40,149 orphans), not on
the real queue: the real runner has still not run against the real composite head, so the first real observe pass is the first live specimen of
both documents there and of how long the ledger takes to read at its real size. Removing the writer flag after it was on has not been exercised against the live
store; the read side does not depend on the flag (`reads_composite` is not env-flagged), so a box with the flag removed still reads a
composite head. When the reducer merges the carried ref is the reducer's own decision (g-306-284 carried it at occurrence 299); this
section only names the gate that reads it. The arrival range rests on three 24 h readings in 12 days, and the stored head size (262,144 B)
is U10's and U11's, measured on the replay and not on a live composite head. The older store's lifecycle config is still unreadable (the
live store has been the MinIO since 2026-09-14).

## Anomaly routing (U16)

U15a's tick logged what the runner printed and posted nothing. The runner never posts: its `post` field is the human line for a delete or an
anomaly and is `None` otherwise, and "a caller that posts owns the channel and the de-duplication" (U14). Until a caller did, an anomaly during a
delete pass reached only `core/logs/composite-gc-tick.log` on one box. U16 is that caller: after a pass, `composite-gc-tick.sh` reads the last line
the runner printed that opens a JSON object (the last line of all until U19) and its exit code, and posts once per condition. The runner is unchanged. Every reading below was taken on cc-08 on
2026-10-04 unless it names another source.

**What the tick does with the runner's result line and exit code.**

| What the tick reads | What it does |
|---|---|
| a result whose `post` has severity `anomaly` | posts the `post` body to `coordination` as an `escalation`, tags `composite-gc,anomaly`, unless the same condition was posted from this box within 24 h |
| a result whose `post` has severity `deleted` | posts it as a `finding`, tags `composite-gc,deleted`, under the same 24 h rule |
| no parseable result line (any exit code, empty output included) | an anomaly of class `no-result-line`: posts the exit code and the last three output lines, each cut to 300 characters |
| a parseable result with no `post` and an exit code other than 0 (the runner's own `setup-failed` line is one) | an anomaly of class `rc-without-post`: posts the exit code, the verdict and the last three lines |
| a result with no `post`, exit code 0, verdict `observed` or `applied` | a completed clean pass: ends the episode, deleting the state file if there is one |
| a result with no `post`, exit code 0, any other verdict (`inactive`, `not-due`, `lease-held`, `busy`, `not-yet-composite`, `not-own-cloud`) | nothing: such a pass says nothing about whether a condition went away |

**Why the exit code is read.** A runner that dies before it can describe itself (an import error, a missing file, the `setup-failed` line, which
carries no `post`) would route nothing under a router that only forwarded `post`, and that is the failure the post exists for. The same branch
covers a later runner that exits 1 without saying why. The real runner showed it while the tests were written: started in a tree that lacks its
backend modules it printed `setup-failed` with rc 2, and the tick posted it.

**The condition's key.** `kind:verdict:classes`, kept as the first 12 hex digits of its sha1. The verdict and every anomaly line are reduced to a
lead phrase: lower-cased, each run of digits replaced by `#`, cut at the first colon or opening parenthesis; the classes are the sorted,
de-duplicated lead phrases. So `kept 3 object(s): gone-since-listing` and `kept 17 object(s): rewritten-since-listing` are one condition, as are
the verdicts `error: KeyError` and `error: ValueError`, in any order. What follows a lead phrase is a measurement, an id or an exception type, and
a key built from it never matches the next pass (rb-2954). A different verdict, or one more or one fewer class, is a different condition and
posts. The runner's source holds 16 anomaly sites; a test reads them with `ast` on every run, so a new site is picked up, and runs each one
twice with different counts and ids, checking that the second is not posted. Fifteen have a lead phrase free of variable text. The sixteenth, `%d ledger entr%s dropped as invalid; the grace of
each starts over`, reads `entry` for one and `entries` for several, so that condition posts twice around a count of one; the test pins it as the
known exception, which means fixing the runner's wording forces the exception out.

**The 24 h window, and what ends an episode.** The state file `core/logs/.composite-gc-tick-post` (git-ignored, one per box) holds the key's hash,
and its mtime is the time of the last post. The same key inside 24 h (`REPOST_S`, a choice: a standing condition reaches the channel once a day)
is logged as deduplicated and not posted; a state dated in the future is due, as the cadence stamp is. A pass ends the episode only when it
completed and found nothing: verdict `observed` or `applied`, exit code 0, no `post`. The other verdicts prove nothing about the condition, and
the runner answers `not-due` on most ticks (its own cadence is 4 h and the tick fires every 30 minutes), so clearing on them would repost a
standing condition at every real pass. A delete post follows the same rule: while deletes continue, the first pass of a day is announced and the
later ones are not, so the post says a delete happened and is not the record of each; the record of a run is its receipt in the archive.

**When the key is written.** Only after the poster returned exit code 0 and printed a message id of the form `msg-YYYYMMDD-HHMMSS-<name>`
(guard-5382, guard-5403: a board post can fail by exiting 0 with plausible output). Anything else logs `post FAILED (rc=..., message id '...')`,
records nothing, and the next pass retries. The poster is killed after 120 s (`COMPOSITE_GC_TICK_POST_TIMEOUT_S` shortens it in tests), so a wedged
board daemon cannot leave one waiting process per pass.

**Order inside a pass.** Runner, log lines, stamp, lock release, then routing. The stamp and the lock are settled before the poster runs, so a slow
poster holds neither, and a failure inside the routing step cannot leave a lock for the 2 h reclaim.

**What the post is, and what it does to others.** An announcement. Nothing is filed, and the footer of every post says so and tells the reader
to file or claim an Investigate. I decided on a post and not a filed goal because a goal filed from a tick needs an aspiration to file into
(`escalation-target.sh` exists for that) and a de-duplication keyed on the open goal, and that is a larger change than the gap needs today. A
board post is an event ("it was announced") and reads as handled for ever, while the condition is state (rb-10420: a channel scan asks whether
something was announced, a health sweep asks whether the contract is met); the 24 h repost carries the state to the channel for as long as the
condition lasts, and a clean pass ends it. If a post sits unanswered for a day, the next step is the goal route. The type and channel are the ones
machine-authored infrastructure posts already use: `worker_execute.py` posts `coordination --type escalation` for a carrier wedge, and the worker
loop posts the same for a merge wedge. Two effects on other readers, both read 2026-10-04: `generation_phase_gate.py` counts a post as demand only
when it is addressed to the agent (a `requires_action_by` or bare agent tag, a reply to its own post, or a first line addressed to it by name),
and this post carries the tags `composite-gc,anomaly` and addresses nobody, so it defers no agent's generation; and `aspirations-all-blocked`
Step B0 reads `escalation` posts from the last 12 h and takes a goal id from the tags, which this post, like the carrier-wedge one, does not
have (see "Not established").

**Verified.**
- The tick's tests went from 45 to 106 and all 106 pass on the final script (22.8 s, system python, `STORAGE_BACKEND=local`, S3 endpoint unset).
  The new ones run the runner's real final line through the tick: `runner_line()` builds it with the runner's own `_new_result`, `build_post` and
  `summary`, so the contract under test is the real one and not a copy.
- 89 mutants of the tick, its config block and the board convention were each killed by a real test failure (pytest rc 1), with the control green
  before and after (105 passed; the one test that runs the real runner is deselected in the copies, which lack the backend modules, and passes in
  the real tree). 83 mutated scripts still parse under `bash -n`, so none died of a syntax error; the other 6 mutate the config block or
  `board.md`. 44 of the 89 are killed by exactly one test, the thinnest margin. One phrase of the footer ("the two read-backs") was corrected
  after the run; no test asserts it and no mutant edits it, and the 106 tests were re-run on the final script. A first run was discarded as
  invalid: its output path was relative, pytest's pinned rootdir did not exist, every mutant read rc 4 with no failures parsed, and the control
  line was what showed it.
- The real runner under the production call shape (`bash core/scripts/composite-gc-tick.sh`, the real config and the real runner), at
  2026-10-04T01:14:59Z with both composite flags unset: verdict `inactive`, rc 0, `post: null`, no `composite-gc route:` line, no state file, the
  stand-in poster not called, the stamp written and the lock released. Three seams differed from production (the state directory, the poster, and
  `COMPOSITE_GC_TICK_SYNC=1` to run inline).
- Wider checks on the final tree, 2026-10-04T01:20Z to 01:24Z: the own-cloud and composite test group (43 files) under the moto venv with
  `STORAGE_BACKEND=local` reads 1,512 passed, 18 skipped, 0 failed (the reading before U16 was 1,451 passed and 18 skipped, and the difference is
  the 61 new tick tests); the scoped tier reads PASS with the tick's test file selected; the Python-CLI-fallback audit is clean over 682 scripts.

**Not established.**
- The live post path had not run when U16 landed. It has since run once, from cc-08, in "The live post rehearsal (U18)". The U16 tests still use
  a stand-in poster that follows the board script's contract (rc 0 and a `msg-YYYYMMDD-HHMMSS-<name>` id on stdout); what the rehearsal did not
  cover is listed under that section's own "Not established".
- What Step B0 does with an `escalation` that has no goal id was not run. Its text extracts a goal id from the tags and files an Investigate when
  there is none open for it; it is an LLM-run step, and the carrier-wedge post it would also read has no goal id either.
- The synthetic anomalies post the last three output lines as printed, each cut to 300 characters and not scrubbed. The runner's own anomaly text
  carries exception type names and provider error codes only (`_why`), but a traceback's lines are the traceback's own and can name a path or an
  endpoint.
- The state file is per box, so a reducer that moves boxes repeats a post at most once per episode per move. The 24 h window has no measured basis.
- U16 is on the carried worker ref with the tick, so the reducer's tree does not hold it until the merge (item 2).

## The bound on the runner (U17)

U15a's tick started the runner with no time limit, and nothing renews the runner's lease. `run_pass` takes it with
`be.acquire_lock(lock, timeout=0, stale_seconds=LEASE_TTL_S)`, where `LEASE_TTL_S` is 1,800 s (the runner's own comment calls it "my value, not a
measurement"), and never extends or re-checks it. The lease is a row in the backend's lock table whose `ttl` is the holder's `now + stale_seconds`, and a contender
takes it when that `ttl` is below the contender's own `now`. So a pass still running 1,800 s after it began can run beside a second holder, and its
`release_lock` then finds the row someone else's; the backend's own comment on that conditional failure reads "positive proof a peer stole the lock
while we were inside the critical section" (g-115-8536). The box-local tick lock does not cover it: it reclaims only after 7,200 s. U17 puts the
bound in the tick and leaves the runner unchanged: the one line that starts it is now
`timeout -k "$KILL_GRACE_S" "$RUNNER_MAX_S" python3 "$RUNNER" 2>&1`. Every reading below was taken on cc-08 on 2026-10-04 unless it names another source.

**The numbers.**

| Quantity | Value | Where it comes from |
|---|---|---|
| lease, `composite_gc_runner.LEASE_TTL_S` | 1,800 s | the runner's own estimate, not a measurement |
| bound, `RUNNER_MAX_S` | 1,500 s | a choice; test seam `COMPOSITE_GC_TICK_RUNNER_MAX_S` |
| kill grace, `KILL_GRACE_S` | 30 s | a choice; test seam `COMPOSITE_GC_TICK_KILL_GRACE_S` |
| time between the bound's TERM and the earliest takeover | 300 s, and 270 s after the KILL | 1,800 - 1,500, and 1,800 - 1,500 - 30 |
| the runner's start-up, before the lease's clock starts | 0.246, 0.256 and 0.269 s | three runs of the real runner with both composite flags unset (the `inactive` path, which builds the backend and takes no lock) |
| one stalled object-store or lock-table operation | 120 s at most | 3 attempts x (10 s connect + 30 s read), the backend's own figure; both clients carry the config (`owncloud_backend.py`); `MIND_S3_CONNECT_TIMEOUT` and `MIND_S3_READ_TIMEOUT` override it |

**Why the bound is the earlier clock.** `timeout` starts counting when it starts the runner. The lease's clock starts later, at the `now` that
`acquire_lock` takes before its put, after Python's start-up and the backend's construction, so the bound is the earlier anchor and fires first
(rb-9564 asks which clock a duration-derived bound is anchored to; this one is anchored to the runner's own start, which is no later than the lease's). The value is a choice. The ceiling is
the lease less the grace and an allowance, and nothing measured sets a number inside it. The allowance covers the gap between the two clocks and
the skew between the holder's clock and a contender's, which the `ttl < now` comparison crosses. A test pins the defaults, parsed from the tick:
`RUNNER_MAX_S + KILL_GRACE_S <= LEASE_TTL_S - 120`, where 120 s is one stalled operation, so the lease cannot be lowered or the bound raised past it
without that test changing. There is no config key for it: the `composite_gc_tick` block holds `enabled` and `interval_minutes`, and a test pins
exactly those two.

**Why under the lease and not over the callee's budget.** rb-10253 says a bound should exceed the callee's own declared budget, because a repeated
stop at your own bound is evidence about the bound. The runner declares no budget for a pass, only the lease, and the lease is an exclusivity limit
the runner cannot extend, not an estimate of how long a pass takes (its comment: "a pass at the cap is estimated at minutes"). A bound over it would
leave the stale-break the bound exists to prevent. The inner bounds sit far below: guard-7289 asks the outer bound to exceed the inner one by the
parent's own work, and 1,500 s against a worst-case single operation of 120 s leaves 1,380 s for the runner's own. If real passes turn out to need longer than 1,500 s, the fix is
a renewing lease in the runner, not a higher bound.

**What the bound stops and what it does not.** One stalled operation ends in 120 s at most, and the runner turns it into a failed verdict of its own
(`error: <type>`, `state-unreadable`, `ledger-unreadable`, all in its `_FAILED_VERDICTS`), long before 1,500 s. The read timeout is the gap between
bytes and not a total-transfer deadline (the backend's own comment, "documented urllib3/botocore semantics, not measured here"), so a pass made of
many calls that each succeed slowly, or one that hangs outside a bounded call, is what the 1,500 s bound stops.

**Exit codes, and what the tick blames on the bound.** `timeout` answers 124 when it stopped the command at the bound, and 137 (128 + 9) when the
command ignored the TERM and the KILL after the grace was needed. The runner's own exit codes are 0, 1 and 2, so 124 is `timeout`'s. A bare 137
is also what any other SIGKILL reads (the OOM killer), so the tick reads 137 as the bound's only when the pass took at least `RUNNER_MAX_S`
seconds, measured by the tick itself with `date +%s` before and after the runner. A 137 sooner than that posts as an ordinary anomaly (`no-result-line`,
or `rc-without-post` when a result line came first) and does not name the bound; a test pins the first of them. A clock step during the pass moves that reading either way: backward fails toward the ordinary post,
forward could name the bound for an earlier SIGKILL, and either is still an anomaly post that carries the exit code. The `>=` at exactly the bound's second cannot be tested, since it would need a kill landing in a
named wall-clock second, and is not claimed.

**What the post says, and its key.** A stop is its own condition, class `wall-clock-bound`: it keys differently from a crash, so a crash after a stop
posts (a test pins that order). Without a result line a stop keys to `anomaly:none:wall-clock-bound` and a crash to
`anomaly:none:no-result-line`. When the runner had printed a clean line and then hung, the key carries the runner's own verdict (`observed`)
beside the class. The body names the bound and the exit code and says what is unknown (guard-3378: the counter lives inside the process that
died, so it cannot report its own death, and a missing summary is not evidence the pass failed): re-read the stored state and the newest archive
run before acting, do not re-run a delete pass on the strength of the missing line, and read a repeated stop at this bound as a statement about the
bound or the store's size and not about a hung runner (rb-10253). The log header now ends `elapsed=<n>s`, so a pass that took 1,400 s and one
that took 3 s are no longer the same line.

**What a stop leaves behind** (read from the constants and the code; no real runner has been stopped). The lock-table row stays, because a killed
process cannot release it, and the next contender takes it once the row's `ttl` has passed. The tick touches its stamp when the runner returns,
whatever the code, so the next tick starts at least 30 minutes after the stop, and by then the row is at least 1,500 + 1,800 s old. A pass stopped
before the last step of `_locked_pass` has not written `last_pass_at`, so the runner's own cadence does not hold the next pass off, and a store whose
pass never fits in the bound is retried at every tick and posts one stop a day. The tick never passes `--apply`, so a stopped pass has deleted
nothing. A stop in the middle of a delete is the death U14 designed for: the receipt is written before the first delete, and the late restore
visits runs from the archive on a later pass (see "The scheduled GC pass (U14)").

**What I decided, and why** (override if you disagree).
- The bound lives in the tick, not in the runner. A cooperative deadline inside the runner would have to be threaded through the backend's apply path
  (storage-backend files, and the full suite at closure), where a hard kill from outside matches the crash safety that is already designed and
  keeps the runner unchanged, as U16 did.
- TERM first and KILL 30 s later, so a runner that handles the signal can exit by itself and one that ignores it cannot outlive the grace.
- A stop is its own anomaly class and not a `no-result-line`: the remedy differs (re-read, do not re-run, and look at the bound) from a crash's (read the
  traceback).
- 137 is blamed on the bound only once the bound's own time has passed.

**Verified.**
- The tick's tests went from 106 to 112, and all pass on the final script (30.6 s, system python, `STORAGE_BACKEND=local`, S3 endpoint unset).
  Six are new: the defaults pin above, and five that run a stand-in runner. Under a 1 s bound it is held in a gate it never gets (rc 124, `elapsed` at least 1, one `escalation` naming the bound and
  `(rc=124)`, the stamp written and the lock gone, the run finished under 15 s although the stand-in would have waited 30); ignoring the TERM under a
  1 s grace (rc 137, posted as the bound's); printing a clean `observed` line and then sleeping (still stopped, and the post carries
  `composite GC anomaly: observed` and the bound); the same stop twice (one post and a logged duplicate, and a crash after it posts again). Under
  the default bound it kills itself with SIGKILL at once (rc 137 sooner than the bound, posted as `no result line` without the bound's name). Two older tests changed with the
  line they pin: the static pin that exactly one line starts the runner now expects the `timeout -k` form, and the header test expects `elapsed=<n>s`.
- 107 mutants of the tick, its config block, its call site and the board convention were each killed by a real test failure (pytest rc 1), with the
  control green before and after (111 passed; the one test that runs the real runner is deselected in the copies, which lack the backend modules, and
  passes in the real tree). 101 mutated scripts still parse under `bash -n`, so none died of a syntax error; the other 6 mutate the config block or
  `board.md`. 52 of the 107 are killed by exactly one test, the thinnest margin. 89 are U16's, with four anchors re-pointed at the U17 text, and 18 are
  new: dropping `timeout`, dropping `-k`, swapping the bound and the grace, three defaults (1,800, 1,700 and a 600 s grace), the 124 and the 137 tests
  of the exit code, the elapsed guard, the class name, the body's arguments and its summary sentence, the header's seconds, the start time, the two
  values the router is given, and the two seam names. Withholding the bound from the router fails 62 tests, because the router now requires it and
  its decision step fails. The run was valid at the first attempt: the output path was absolute, the control line was read before the table, and
  each anchor was first checked to occur once in the real tree. Not covered: `>=` against `>` at exactly the bound's second, and a bound of 0.
- The real runner under the production call shape (the real config and the real runner, the stand-in poster and a scratch state directory), at
  2026-10-04T01:54:43Z with both composite flags unset: header `rc=0 agent=alpha elapsed=0s`, verdict `inactive`, rc 0, `post: null`, no
  `composite-gc route:` line, the poster not called, the stamp written. `elapsed=0s` is a whole-second reading, consistent with the 0.24 to 0.27 s start-up readings.
- The seams at their edges, 2026-10-04T02:14Z: `timeout 0 sleep 2` returned 0 after 2 s and `timeout 1 sleep 2` returned 124 after 1 s, so a bound of 0 is
  no bound. A bound of `abc` made the real tick log `rc=125` and `timeout: invalid time interval`, and the router's decision step then failed on the
  non-number (`composite-gc route: the decision step failed; nothing posted`), so a bad value shows in the log and reaches no board. Only the tick and
  its tests name the two seams; `.claude/settings.json` and `.claude/settings.local.json` do not.
- Wider checks on the final tree, 2026-10-04T02:16Z to 02:21Z: the scoped tier selected by the tick and its test file reads PASS (1 of 1,700 test files
  selected, green); the own-cloud and composite test group (43 files) under the moto venv with `STORAGE_BACKEND=local` reads 1,518 passed, 18 skipped,
  0 failed (the reading before U17 was 1,512 passed and 18 skipped, and the difference is the 6 new tick tests); the Python-CLI-fallback audit is
  clean over 682 scripts.

**Not established.**
- The real runner has not been stopped by the bound and has not been timed at the cap. The tests stop a stand-in that waits in a gate, so what they
  prove is the tick's handling (the stop, the exit codes, the post, the stamp, the lock). How long a real observe or apply pass takes is the first
  observe passes' `elapsed_s` (item 6e), so 1,500 s is as untested against a real pass as the 1,800 s lease is. A first pass near 1,500 s says the bound
  is wrong.
- What a stop leaves behind is read from the constants and the code. No stopped pass has been followed by a real next pass, and the lock-table row's
  expiry has not been read after a kill.
- The gap between the bound's clock and the lease's clock was measured on the `inactive` path only. The skew between the boxes' clocks was not
  measured; the 270 s after the KILL is the allowance for both, a choice.
- `timeout` is GNU coreutils 9.4 on cc-08 (`type -t timeout` reads `file`, in the shell and inside `bash -c`). `iteration-close.sh` already relies on the
  same bare `timeout N` (at two call sites, for example), and this tick adds `-k`. Where a bare `timeout` resolves to Windows `timeout.exe` the runner line would not start
  (rb-1470). cc-04's `timeout` was not read.
- The runner could trap SIGTERM, print a `terminated` result and release its lease, which would turn a stop from an unknown into a known and free the
  lock at once. It is not built: it touches the runner's pass and its tests, and nothing yet shows a stop happening.
- A runner that prints a result carrying a `post` and is then stopped is announced by that `post` alone, because the router takes the runner's own post
  first; the stop shows only as `rc=124` and `elapsed=` in the log header. `main()` prints its one summary line after the pass, so this is a hang
  during exit. It has not been observed.
- The two seams are read as given and nothing validates them: a 0, or a value past the lease, defeats the bound. The header calls them test seams.
- U17, like U16, is on the carried worker ref, so the reducer's tree does not hold it until the merge (item 2).

## The live post rehearsal (U18)

U16's tests post through a stand-in poster, so until U18 the real `board-post.sh`, its daemon and the `coordination` channel had not carried a
post from the tick. U18 ran the rehearsal that item 7 names, on cc-08 (`uname -r` 6.8.0-142-generic) on 2026-10-04, and changed no code: the run
showed nothing to fix. Every reading below is from that run.

**The call shape.** The tick ran inline, with the real config (`composite_gc_tick.enabled: true`), an empty scratch state directory, a stand-in
runner and no poster seam, so the poster was the real `core/scripts/board-post.sh`:

    REHEARSAL_PROJECT_ROOT=<this tree> COMPOSITE_GC_TICK_SYNC=1 COMPOSITE_GC_TICK_STATE_DIR=<an empty scratch directory> \
      COMPOSITE_GC_TICK_RUNNER=<the stand-in> bash core/scripts/composite-gc-tick.sh

The stand-in builds its one result line with the runner's own `_new_result`, `build_post` and `summary`, so the line has the runner's shape. It
touches no store and takes no lock. `_new_result` is the runner's private helper: if its shape changes, build the dict by hand to match.

```python
import json, os, sys, time
sys.path.insert(0, os.path.join(os.environ["REHEARSAL_PROJECT_ROOT"], "core", "scripts"))
import composite_gc_runner as R

res = R._new_result(False, time.time())
res["verdict"] = "REHEARSAL, a test of the tick's post path"
res["anomalies"].append("REHEARSAL of the composite-GC tick's live post path: nothing is wrong with the store or the runner, and nothing "
                        "needs to be filed or claimed. It overrides the last line of the footer below. Posted by <who runs this>, for <the goal>")
res["elapsed_s"] = 0.0
res["post"] = R.build_post(res)
print(json.dumps(R.summary(res), sort_keys=True))
```

**The readings** (UTC, from the log headers and the board's own timestamps).
- Control, 02:46:12, with a recording stand-in in place of the poster: it was called with `--channel coordination --type escalation --tags
  composite-gc,anomaly` and a 914-byte body on stdin with no `@` in it (rb-2590: `board-post.sh` hangs on @mention tokens). The log header read
  `rc=0 agent=alpha elapsed=0s` and the state file `.composite-gc-tick-post` held the key `f516665a46e9`.
- Live, 02:46:27: the tick returned 0 and logged `composite-gc route: posted anomaly to coordination as msg-20261004-024627-alpha-142
  key=f516665a46e9`, and the state file held the same key.
- Read back by id before the second run, with `board-read.sh --channel coordination --last 8 --json` and no `--mark-read`: author `alpha`, channel
  `coordination`, type `escalation`, tags `composite-gc` and `anomaly`, `reply_to` null, the Body's session id. The stored text is 913 bytes. The
  control's stdin was 914, and the live body is the same text by construction (it holds no timestamp). The two are equal once trailing whitespace is trimmed.
- De-duplication, 02:46:49: the scratch stamp removed and the state file kept, the tick ran again and logged `composite-gc route: deduplicated anomaly
  key=f516665a46e9 ... the next post for it is due 86400 s after the last one`. The log holds one `posted` line in all, and
  `board-read.sh --channel coordination --tag composite-gc --last 20 --json` straight afterwards returned one message, the rehearsal's.
- Reaction check, 02:49:20, 2 min 53 s after the post: the last 40 messages on `coordination` (01:59:39 to 02:48:48) include the post, so the scan can
  see it. None has `reply_to` equal to its id and none names the id or the word REHEARSAL. The four messages after it belong to other work (a claim, a
  status, a completion and a production config-change escalation). `aspirations-query.sh --title-contains` returned an empty list for "composite GC
  anomaly" and for "REHEARSAL", and it finds g-358-202 for "Fix the write path".

**What it shows.** The real `board-post.sh` and its daemon carried the tick's post: the tick passed the body on stdin and the three flags, the script
returned rc 0 and the message id on stdout, the tick wrote its de-duplication key, and the channel holds one `escalation` with the intended tags. The
same condition did not post a second time against the real state file.

**What I decided** (override if you disagree).
- The label sits in the verdict, so the subject line reads `composite GC anomaly on <store>: REHEARSAL, ...`, and again in the anomaly line. The footer is fixed text that tells a reader
  whoever acts on a post files or claims an Investigate, so the body says it overrides that line.
- One post only. The second run is the de-duplication test, and it could not add one.
- No change to the tick or its tests: the run showed no defect.

**Not established.**
- The reducer's box. The run was a worker Body's shell on cc-08, as `alpha`. The reducer runs on cc-04 and starts the tick backgrounded from
  `iteration-close.sh`; its daemon and its environment were not exercised. The same run from there is owed once its tree holds the
  tick (item 2).
- The backgrounded form. `COMPOSITE_GC_TICK_SYNC=1` ran `run_pass` inline. Production runs `( run_pass ) >>"$LOG" 2>&1 </dev/null &`, the same function
  with the same redirections, so a difference is not expected, and none was measured.
- The runner's own post. The stand-in built the line with the runner's functions; the real runner has not yet produced an anomaly on a live store.
- Whether anyone acts on a post like this. Under three minutes passed before the reaction check, which says nothing about an anomaly left on the
  channel for hours.
- The rehearsal message stays on the channel with its label; nothing was done to withdraw it.

## The full suite at a pinned tip (U19)

The reducer's notes on this goal's carried ref (g-306-284, occurrences 287 to 300) say it can merge the ref and chooses not to: the ref touches the storage
backend (`owncloud_backend.py`, `_owncloud_composite.py`, `aspirations_write.py`), the scoped tier cannot certify that, and what would change the choice is
"a full-suite window, or the owner's own full-suite result on g-358-202". Earlier units listed the full suite as owed at closure, which made the wait
circular: the reducer waits for a result the owner defers until the merge. U19 runs it now, at a tip that is frozen and named by its SHA, so the result says
exactly what it covers. Every reading below was taken on cc-08 on 2026-10-04 unless it names another source. The run record is
`world/audit-reports/g-358-202/u19-full-suite/`; the dated entry is in `core/config/run-full-suite-baselines.md`.

**What was run.** `run-full-suite.sh` at `44f29bf0b5`, which is origin/main `e777c42907` plus the carried commits (39 non-merge commits, 89 with the merges that
integrated origin/main). They touch 34 files (`git diff --name-only e777c42907 44f29bf0b5`; the list is `carried-34.txt` in the run record). `STORAGE_BACKEND=local`
pinned, 1,703 files in 4 fresh processes, system python 3.12.3 with pytest 7.4.4 and no moto. Launched 03:12:31; the wrapper's last write was 04:22:40 (70 min); the
chunks took 11.0, 8.1, 11.2 and 22.4 minutes. HEAD and `git status` were read at every poll and never moved, no commit, merge or tracked-file edit was made in the
window, the tree lock was held by this body, and the box's live daemon (pid 3366516) was not restarted. The load average at launch was 13.2 on 20 cores.
`OWNCLOUD_COMPOSITE_STORES` and `OWNCLOUD_COMPOSITE_GC` were unset, so the suite measured the flag-off tree.

| Half | Result |
|---|---|
| chunked pytest, 1,703 files | `VERDICT: GENUINE`; 28,629 passed, 35 failed, 0 errors (chunks 6,559 / 10, 6,866 / 14, 8,060 / 2, 7,144 / 9) |
| invisible suites, 84 files | 82 passed, 2 failed (`test-g3-worker-store-rails.sh`, `test_aspirations_claim_source_flag.sh`); both pass solo, three runs each |
| domain half, 124 units | 123 passed, 1 skipped, 0 failed (4,103 pytest tests in 8 batches) |
| the 20 moto-importing files, under a venv with moto 5.2.3 | 696 passed, 6 skipped (structural), 0 failed; under system python the same files read 270 passed, 218 skipped |

**The 35 chunked reds, against U13.** U13 (`a360f8f888`, 28,187 passed, 33 failed) is the earlier run with the same method. Compared by node id
(`compare-vs-u13.out`): 32 reds are in both runs, 3 are new, 1 is gone. None of the 20 files that hold a red is one of the 34 carried files.

| | Node | Cause | From this range? |
|---|---|---|---|
| new | `test_check_stderr_json_merge::test_repo_core_scripts_clean` | the tick's router parsed the last line of output that has stderr merged into it (next paragraph) | yes, U16 |
| new | `test_runtime_store_rbguard::test_guard_set_field_erase_blanks_the_rule_of_a_retired_record` | `erase_not_local`, from a test-order effect (the paragraph after) | no |
| new | `test_runtime_store_rbguard::test_guard_set_field_erase_refuses_a_record_that_is_not_retired` | the same | no |
| gone | `test_daemon_import_surface::test_every_loaded_module_is_in_the_pathspec` | red in U13's run, not red in this one; 13 passed solo | |

The 32 shared reds carry U13's disposition: the run's ownership block names an owner or candidate goal for each file (`suite-run.log`), and U13's `--triage` found
three files environmental (green solo) and the rest owned or verified. `--triage` was not run again.

**The one red this range introduced.** `check-stderr-json-merge.py` flags a `2>&1` capture whose variable reaches a JSON parser with no per-line or `raw_decode`
extraction. U16's router fed `out` (the runner's output with stderr merged in, which is how a crash's traceback reaches the post) to `json.loads(lines[-1])`. A warning
printed after the summary (an interpreter-exit message, a library's `ResourceWarning`, any bracket-led notice) would then be the last line, the parse would fail, and a
clean pass would post a `no-result-line` anomaly, once a day per box at most (the 24 h key). The detector run over each committed version of the tick reads clean at U15a
(`709dad3066`) and flagged at U16 (`ac995601c3`) and U17 (`1fcefde350`), so U16 introduced it and the two later units carried it. Now the router takes the last line that
opens a JSON object, which is guard-659's own alternative to dropping the merge, and the detector reads the tick as exempt for per-line extraction.

**The two erase reds are not from this range.** Both fail with `{"error": "erase_not_local"}` from `_backend_is_local()` (`mind_api/src/endpoints/store.py:587`), which reads
`isinstance(get_backend(), LocalBackend)`. The tests came with commit `f3d3cf1bba` (g-335-1726 u3b, 2026-10-03 14:27), which is on origin/main, is not in the carried range
and is not in U13's tree. They pass solo (the 11 `erase` tests of that file, rc 0) and paired with each of 22 earlier chunk-03 files that touch the backend or its environment, so the
cause is order, and the order is found. A tracing plugin run over chunk 03's own first 110 files and then the two erase tests (the real order, 237 s) reproduces the two reds and
nothing else (2 failed, 2,004 passed, 2 skipped), and logs two events. `mind_api.src.endpoints.store` first enters `sys.modules` during
`test_stop_hook_in_flight_integration.py::test_genuine_close_clears_in_flight_through_the_real_chain`. Later `storage_backend.LocalBackend` becomes a new class during
`test_tree_match_prefetch_wiring.py::test_local_backend_prefetch_is_a_real_no_op`, which calls `importlib.reload(storage_backend)`. `store.py` keeps the class it bound at import, so a
`LocalBackend` instance is no longer an instance of it for the rest of the process. The control removes only the reload file from the same prefix: 2,000 passed, 2 skipped, 0 failed, with the
same early import and no change of class. A stand-in for the first event (a plugin that imports `store.py` at collection) gives the same split in three runs: red with the reload test
before the erase tests, green without it, and green when the import comes after the reload (`store.py` then binds the new class). The carried range has one `mind_api/` file,
`endpoints/aspirations_write.py`, and neither erase file is near it.

**The invisible half's two reds.** Both were red in U13's run and green solo then; they read the same way now (`solo/shell/`: `VERDICT: PASS` three times, `Summary: 6 passed, 0 failed`
three times). The in-suite cause was not identified, as in U13. The two other invisible or domain reds U13 had (`test_aspirations_update_goal_source_value.sh`,
`test_promotion_day_smoke_leg.sh`) did not recur.

**A side effect, restored.** `test_fresh_eyes_record_tick_unknown_flag.py` writes through the daemon into the live team-state store (its own docstring says so, and U13 measured
it). The run restamped `shared_cadences.last_fresh_eyes_tree_review` to 03:25:28 / 16076 and its `__inflight_claim` to 03:25:31. Both were restored from the pre-launch snapshot with
`team-state-update.sh` and the read-back of `shared_cadences` is byte-identical (582 B) to the snapshot. The working-memory cadence slots and the iteration checkpoint read equal. The
coordination board shows 36 posts inside the window, none from a test (read back, 160 posts decoded, 0 bytes left over).

**What I decided, and why** (override if you disagree).
- The router keeps the stderr merge and reads the last line that opens a JSON object, rather than splitting the streams. The merge is how a crash's traceback reaches the
  post's `last output` lines, and guard-659 names a tolerant parser as its alternative to a stdout-only capture. Splitting the streams would change the router's input and the post's evidence.
- The result is the last object-opening line and nothing is scanned back past it. A runner whose last object line is garbage did not print a summary this pass, and reading an earlier
  object as its result would hide that. A crash after a clean summary still posts, because the exit code is read beside the line (`rc-without-post`).
- The `isinstance(cand, dict)` guard went. After the opener test a successful parse can only be an object, so the guard had become unreachable code of this change's own making.
- No second full run at the final tip. The commit that adds this section changes the tick, its test file, this document and the baselines ledger. The tick and its tests are covered by the runs under
  Verified; the document and the ledger are prose that no test reads for content (the two test files that name them pin a docstring and a merge-driver map). A run at today's final tip would be stale
  against the reducer's merge in the same way U13's was against the tip that kept advancing, so the reducer's own run on the merged tree is the second tree.
- The two erase reds are not fixed here. Two small fixes suggest themselves (compare by class name in `_backend_is_local`, or restore the module in the reload test); neither was tried, and choosing belongs to
  the owner of g-335-1726. The relay goes through the spark lane.

**What this certifies, and what it does not** (guard-5631).
- Certified: the chunked, invisible and domain halves ran on the tree `44f29bf0b5`, and every red is accounted for. 32 are U13's by node id and none is in a carried test file; 2 come from a peer's new
  tests meeting an old reload; 1 was this range's and is fixed in this unit's commit.
- Not certified: green on HEAD (the framework half has 35 failures; U13's had 33); the final tip, which adds this unit's commit; the flag-on tree; the reducer's box; anything that landed after 03:12:31.
  origin/main has moved since: it read `23aa209d3e` at about 04:52, 19 commits that are not in the tested tree. They change 34 files, none of them one of the 34 carried files and none a storage-backend or
  runner file by name (`origin-since-tested.txt`).

**Verified.**
- The detector over the fixed tick: `flagged: []`, with the reason `per-line JSON extraction`. It flagged one reference in the tick as committed at U16 and at U17; U18 changed no code.
- The tick's tests went from 112 to 117 (four new, one run over two warnings), all pass on the final script with the lint test file: 123 passed in 31 s, system python, `STORAGE_BACKEND=local`.
  The new tests run a stand-in runner that writes to stderr after its stdout is flushed: a warning or a bracketed notice after a clean result still ends the episode; the same after an anomalous
  result leaves the runner's own post unchanged; a traceback after a clean result still posts as `rc-without-post`; a last object line that does not parse is no result.
- Mutation proof (throwaway copies of the files under test, `--rootdir` and `--confcutdir` pinned to each copy, the test module's own file paths printed from inside it, an absolute output path,
  the basetemps deleted after): 114 of 114 mutants killed, each by a real pytest failure (rc 1), 108 mutated scripts parse under `bash -n`, 51 killed by exactly one test, green controls (116 passed, one
  test deselected) before and after. Seven are new, all on the choice of the result line: the last line again, the guard on the wrong list, an array opener, an opener widened to brackets, the
  second-to-last object, a filter that only gates the parse of the last line, and a scan back past garbage. The older mutant that reads the first line was re-anchored to the new line.
- The scoped tier over the working set: `VERDICT: PASS`, one test file selected of 1,703 (the tick's own). The lint test file, the two files that name this document or the ledger, and the
  tick's tests were then run together: 311 passed in 78 s. The scoped tier does not select the lint test for a tick-only change (`fix/scoped-selection-for-a-tick-only-change.json` in the run record):
  the scanner reads every top-level script and names none, and the tier has no always-run or whole-tree list, so that class shows only in a full run or in an explicit run like this one.

**Not established.**
- The suite was not run at the final tip. The claim stands for `44f29bf0b5` only, and the fix is covered by the tests named above.
- The cause of the two invisible reds is unknown, as in U13. `--triage` was not re-run, so the 32 shared reds have no fresh solo reading except the ones named here.
- The trace covers chunk 03's first 110 files only. Whether anything after them can also leave a stale `LocalBackend` was not searched, and the fix for the pair was not applied.
- A suite with `OWNCLOUD_COMPOSITE_STORES` set was not run; flag-on coverage is U4b's fixtures and U11's live replay.
- Nothing was run on the reducer's box, and the reducer's own full run on the merged tree has not happened.

## What the churn alarm does to the head key (U20)

The flip checklist (U15) left one item to decide before the writer flag is set: whether the standing per-key churn alarm learns the composite head. It does. The evidence is
in `audit-reports/g-358-202/u20-churn-alarm/` under the world store (`README.md` indexes it); the code is `world/scripts/s3_churn_alarm.py` and its test file.

**What the unchanged alarm does at the designed normal (replayed, not observed).** The flag is off and the flip has not happened, so every reading here is a synthetic window pushed through the
real `find_breaches`, `breach_goal` and `gates.goal_duplication.evaluate`. 2,315 head PUTs of 262,144 B (U10, U11) are 606,863,360 B, 578.8 MiB. The versions limb files on that window,
`2315 versions > 500 (at 606,863,360 summed bytes)`, and the 100 MiB co-condition does not hold it back. The 2,895 peak and both head-PUT storm windows file the same way. The duplication gate
refuses that filing without an override: `pending_queue` matches the completed g-358-228 (`origin_signal_completed`) and three structural overlaps (g-358-18, g-374-77, g-374-80). `s3-churn-alarm.sh`
retries once with a canned override, and the same evaluation with that justification returns `would_block: false`. So a completed twin does not hold the alarm back: after the flip it re-files at the
designed normal, paced only by the script's own ledger window. g-358-228's close note predicted that re-arm (guard-5703) on the premise that the gate refuses only a second pending goal; the outcome
agrees and the mechanism differs. Scope: `evaluate` ran in-process with its two writers stubbed, and the daemon path and the script itself were not run.

**What I decided, and why** (override if you disagree).
- The alarm learns the head, as a pure narrowing for the registered head keys. On such a key the versions limb additionally requires the mean PUT to exceed 4 x the head floor, 1,048,576 B. The bytes limb
  stays at 2 GiB and every other key keeps both standard limbs. The alternative, an open Investigate as the tracker, is a goal whose only job is to be re-opened; an alarm that cannot go quiet is dead (guard-6508).
- The mean, not the count. The head key's PUT count is the fleet's mutation rate, which the writer cannot change; the mean PUT is what the writer controls (guard-6906). A whole-object writer puts about
  11.8 MB in a PUT and the composite head 262,144 B, a 45x gap that no mutation rate closes.
- The registered keys and the floor are read by AST from `_owncloud_composite.py` (`ALLOWLIST`, and `HEAD_MIN_BYTES = 256 * 1024` by constant folding) and never re-typed (guard-1960). A key or floor that
  cannot be read leaves the standard limbs in force, the loud side (guard-1977).
- The verdict JSON gains `composite_head`: the keys, the floor, the mean limit, and one reading per head key whether or not it breached, so the quiet band stays visible (guard-6508).
- The factor 4 is a choice, not a measurement. It lets the head grow fourfold before the limb re-engages.
- The alarm's retry, its ledger window and every other key are as they were, and nothing was filed.

**Backtest** (guard-5528: the real defect replayed under the new rule).

| window | PUTs | bytes | mean PUT | before | after |
|---|---|---|---|---|---|
| designed normal | 2,315 | 606,863,360 | 262,144 | versions limb | quiet |
| 2026-10-02 peak count | 2,895 | 758,906,880 | 262,144 | versions limb | quiet |
| head-PUT storm 1.5x | 3,500 | 917,504,000 | 262,144 | versions limb | quiet |
| head-PUT storm 3.5x | 8,192 | 2,147,483,648 | 262,144 | versions limb | quiet (the bytes limb is strict at exactly 2 GiB) |
| 5% whole-object PUTs | 2,315 | 1,932,972,461 | 834,977 | versions limb | quiet |
| 10% whole-object PUTs | 2,315 | 3,270,877,369 | 1,412,906 | both limbs | both limbs |
| all whole-object PUTs | 2,315 | 27,307,293,205 | 11,795,807 | both limbs | both limbs |

The three readings the head key actually breached on (g-358-191's alarm, 1,470 versions and 17,069,571,018 B; g-358-202's baseline, 1,516 and 17,577,753,534 B; g-358-228, 2,895 and 34,524,728,612 B) file on
both limbs under the rule (`test_the_whole_object_writer_still_alerts_on_both_limbs`). At 2,315 PUTs a day the limits are crossed at these shares of whole-object PUTs: 4.3% for outcome 5's bar
(1,757,775,353 B; counted on the head key's bytes alone, which U21 corrected to 3.1% with the segments), 5.8% for the 2 GiB bytes limb, 6.8% for the mean limit.

**Where the rule stops seeing.** A writer regression that rewrites 3.1% to 5.8% of PUTs whole (U20 wrote 4.3% to 5.8%, counting the head key's bytes alone; U21 corrected the lower edge) puts the day over outcome 5's bar while the alarm stays quiet; the fresh 24 h listing (item 4) measures that, and
the alarm is the order-of-magnitude catcher. A head-PUT storm stays quiet up to 8,192 PUTs a day, 3.5x the designed count, and then the bytes limb files. I did not add a head-specific bytes limit: it needs the
arrival rate as a declared constant, and the arrival range rests on three 24 h readings.

**Verified.**
- Mutation proof on a copy of the module: 27 mutants, 26 killed by real pytest failures (rc 1), green controls before and after. The survivor (the CLI passing no head to `find_breaches`) is equivalent, because
  `find_breaches` resolves the head with the same function and root when none is passed. The first run left four survivors, three of them real test gaps, closed by two new tests and a non-head page in the CLI test.
- The alarm's tests went from 29 to 43. With its consumers (`test_s3_stock_check.py`, `test_s3_store_churn.py`, `test_s3_churn_closure.py`) that is 104 passed, and the two shell tests pass 24 of 24 and 23 of 23.
  `STORAGE_BACKEND=local`, system python.
- The flip checklist's first gate still reads UNSAFE: `store-cutover-check.sh --store composite` names echo, foxtrot and zeta as unattested (`seam_not_ancestor`) with alpha and bravo attested. Neither flag name
  appears in `.claude/settings.json`.

**Not established.**
- The alarm has not seen a live composite head; the first live reading is item 4's.
- 262,144 B is U10's and U11's measurement on the replay. A head above 1,048,576 B puts the versions limb back in force; that is intended and not exercised live.
- The domain suite (`$WORLD_PATH/scripts/tests`) was not run. The unit changes one script and its test file, and the goal is not closing; the closer owes that run.

## The writer flag over a mixed fleet (U21)

The flag is read from the process environment (`should_composite` calls `_codec.env_enabled`, which reads `os.environ` at call time) and each process gets its environment when it starts, so the flag never flips for the fleet at once: for a while a process with it and a process without it write the same key. Until U21 the record said only that removing the flag after it was on "has not been exercised against the live store" and that item 5's rollout "takes days". U21 asks three things: which processes can PUT the head key and what decides each one's flag; what each combination does to the store; and what a mixed fleet costs against outcome 5.

**Who can PUT the head key (read from the code; one box measured).**

| lane | what runs | where its flag comes from | when it changes |
|---|---|---|---|
| daemon | one `mind_api.src` per box: every goal-queue endpoint, `_merge_reconcile_put` and the sync sweep's `merge_put` | its spawn environment; `daemon_overlay_settings_env` (g-358-183, `_daemon_env_scrub.sh`) fills each committed `.claude/settings.json` env key the spawning shell lacks and never overrides one it has, an empty value included | its next spawn |
| reducer's productivity-check | `iteration-close.sh:4753` runs `aspirations-evict-tick.sh` (24 h stamp, per box), then `aspirations-evict-completed.py`, then `locked_modify_jsonl`, then `get_backend()`: its own in-process `OwnCloudBackend` | the environment of the shell that runs `iteration-close.sh`; a Claude Code session's tool calls carry the settings env block as it stood when the session launched | that session's next launch |
| maintenance CLIs | `aspirations-compact-completed.py` and `aspirations-move-goals.py`, also through `locked_modify_jsonl`; no scheduled caller found | the invoking shell | each invocation |

`composite_gc_runner.py` deletes orphan segments and writes its state documents; it never writes the head. The grep that found these ranked at least 40 non-test files naming the store beside a write helper. I read the callers of the three above and no others, so the table is a lower bound on lanes, not a count.

**What triggers the daemon's next spawn.** The stale-code respawn (`_runtime.sh`, `rt_check_staleness`) fires when `mind-api-code-changed.sh` says the daemon's code surface changed between the code it loaded and HEAD: `mind_api/src`, `core/scripts/_*.py` and a named list of modules. The word "settings" appears nowhere in that script (0 matches), so a commit that touches only `.claude/settings.json` restarts no daemon by itself; the flag arrives at the next spawn for any other reason. On this box (cc-07) the spawn log holds 65 starts between 2026-09-25T15:43 and 2026-10-04T06:15 with gaps of median 1.64 h, p90 9.0 h and maximum 16.1 h: one box, nine days, every cause of a start counted. The previous settings-env flip (g-358-231, 2026-10-02) states the same outcome in its message: "Mixed state is the steady state: un-pulled boxes keep bumping the index in-request."

**What each combination does.** New tests, `core/scripts/tests/test_owncloud_composite_rollback_g358202.py`: two or three backends (own mirror, own fences, own cached head) over one store, each with its own flag state around each call.
- A process without the flag over a head writes ONE whole object under the head's ETag and sends no segment (`_store_put`, first branch). Every reader tolerates the layout it leaves (`reads_composite` is not env-flagged).
- A flagged process over what that left does not trust its cached head: `held[0] == kw.get("IfMatch")` fails because the fence moved, so `plan_write(None, body)` attempts every segment `IfNoneMatch`, each that answers 412 gets one freshen HEAD, and the head commits under the whole object's ETag.
- Alternating writers lose no edit, commit once each, and every reader, a flagless one included, reads the same joined bytes. A stale-fence writer of either kind takes the merge path across the layout change and keeps both edits. A collecting pass that removed every segment between the two writes leaves no dangling head, on the refresh path or the merge path. A removed flag followed by a restart reverts the layout with its first write, and the next restart with the flag set re-seeds. The sync sweep's `merge_put` from a process without the flag over a head reverts it and keeps both edits.
- **The line that carries this is the fence comparison.** Mutant M1, which trusts the cached head without comparing it to the fence, fails two of the new tests, and the failure is the worst one the layout has: `IntegrityError: composite head unreadable after 3 joins: <segment key>`, a head naming a segment the store does not hold, which no reader can join. With the comparison in place the writer plans only against a head its own fence still names (and U2e's pass collects only what the current head does not name).

**Measured live** (`u21-live-rehearsal.py`, run 3 of 3; a throwaway environment id, the live store's own current content, 11.8 MB stored in 87 segments; cleanup by VersionId, 103 versions removed and none left; all 9 readbacks equal, no warning). "Kept" is what the version listing shows the store kept.

| write | kept | versions | segment PUTs / 412 / HEADs | secs |
|---|---|---|---|---|
| ON, migration (whole to head) | 12,100,693 B | 88 | 87 / 0 / 0 | 2.38 |
| ON, steady (two writes) | 301,342 and 371,994 B | 2 each | 1 / 0 / 0 | 0.55, 0.53 |
| OFF over a head (four writes) | 11,801,354 to 11,801,370 B | 1 | 0 / 0 / 0 | 1.14 to 1.34 |
| ON over what OFF left, a re-seed (two writes) | 296,874 and 477,647 B | 3 each | 87 / 85 / 85 | 2.21, 2.35 |

An OFF write keeps 35.1x an ON steady write (mean 336,668 B) and 28.9x U11's mean per mutation (408,015 B). A re-seed keeps about what a steady write keeps, because the 85 rejected PUTs keep nothing, but it costs 86 extra segment PUT attempts, 85 HEADs and 2.28 s against 0.54 s. The refresh after a layout change differs too: an ON process after an OFF write GETs the whole object (11,801,354 B); an OFF process after an ON write GETs 88 objects (12.1 MB) cold and 2 or 3 objects (0.27 and 0.39 MB) warm. Runs 1 and 2 (8 and 9 steps) repeat the steady writes byte for byte and the whole writes within 1.7 kB. Run 1's last step was labelled "over a whole object" and was over a head; run 2 relabelled it and added a genuine second OFF write.

**What it costs against outcome 5.** The bar is 1,757,775,353 B in 24 h. The share of PUTs that may be whole-object before it fails, with ON writes at U11's mean:

| arrival | PUTs | bar per PUT | share | OFF writes a day |
|---|---|---|---|---|
| low reading | 1,470 | 1,195,766 B | 6.91% | 102 |
| baseline reading | 1,516 | 1,159,482 B | 6.60% | 100 |
| designed normal | 2,315 | 759,298 B | 3.08% | 71 |
| peak | 2,895 | 607,176 B | 1.75% | 51 |

**This corrects U20.** U20's 4.3% (and its "4.3% to 5.8%") was computed on the head key's bytes alone, 262,144 B per ON write. The bar counts the segments too, so at the designed normal it is crossed at 3.1% (3.7% at this run's steady mean) and at the peak at 1.75%, while the alarm's 2 GiB bytes limb is crossed at 5.77% and 4.16% and its mean limit at 6.82%. The band where outcome 5 fails and the alarm stays quiet is 3.1% to 5.8% at the designed normal and 1.75% to 4.2% at the peak. A single process without the flag that does more than about 70 mutations a day is enough; the reducer's evict tick, at most one write a day per box, is not.

**What I decided.**
- No framework code change. The tests found no defect in the mixed-fleet paths, and the comparison they depend on already exists and is now pinned by a test that fails without it.
- Item 4's window opens after item 5's pass, not at the commit: a window that contains a process without the flag measures the mixed fleet. Items 4 and 5 now say so.
- The residue can be read from the alarm without a new tool: with `composite_head.mean_put` as m, the whole-object share is (m - 262,144) / (11,801,366 - 262,144). It does not say which box.
- An option not taken, for the goal's owner: make the layout sticky, so that once the store is a head any process writes composite and the flag only decides whether the first head is created. A mixed fleet would then cost nothing. The price is that unsetting the flag no longer reverts the store, so the rollback lever becomes a separate revert tool. Relayed, not built.

**Not established.**
- Which boxes carry the flag. Only cc-07 was read (daemon pid 3198322, pid file 06:15, and its shell, 2026-10-04T07:13Z): both print `ENVIRONMENT_ID`, `OWNCLOUD_GZIP_STORES` and `STORAGE_BACKEND` and no composite line, with the gzip line as the positive control. The reducer's box was not read.
- The daemon's lock, read-modify-write and refresh path with the flag on, and any lane beyond the three read: the tests and the rehearsal call `write_bytes`, `read_bytes` and `merge_put` directly.
- A collecting pass against the live store. The dangling-head case is shown in the test double only, and the live store holds no collected segment.
- Whether the bodies of the 85 rejected segment PUTs cross the network: the hook records the declared body size, and the kept bytes come from the version listing.
- The 71-to-102 figures assume an 11.8 MB whole object, and the live object's size changes daily (it grew 1.6 kB between runs 1 and 3, minutes apart).

**Verified.**
- The new file: 8 tests x 2 stores (the in-memory double and moto 5.2.2) x gzip off and on = 32 passed in 17.3 s, `STORAGE_BACKEND=local`.
- Mutation proof on a copy of `owncloud_backend.py`: 6 mutants, 6 killed, green controls before and after (32 passed, 15.9 s and 17.9 s).
- The live rehearsal, three runs, each cleaned up with no version left. Evidence: `audit-reports/g-358-202/u21-mixed-writers/` under the world store.

## The pre-flip readings (U22)

The record's open list after U21 held three pre-flip readings: gate 1 (UNSAFE at U20's 06:20Z read), a fresh item-4 churn listing (the last was 2026-10-04T00:03Z), and the cap and interval re-derived from the peak against a fresh reading. All three are reads; nothing flips. This unit did them on zc-02 at 15:15Z.

**Gate 1 re-read: SAFE.** `store-cutover-check.sh --store composite`, rc 0 at 2026-10-04T15:15:35Z. Every live agent attested by derivation (alpha b3601ef26, bravo ff5b834da, echo 5d5fa93ab, foxtrot 722010c34, zeta 92a0ffeb3; ages 0.0 to 0.1 d), `unattested` and `stale` empty, `seam_symbols.ok` complete with `missing` empty, the local box carries the seam. The U20/U21 cause — echo, foxtrot and zeta reading `seam_not_ancestor` — cleared exactly as item 1 predicted it would: the merge landed in origin/main and the three unattested agents each made an iteration commit after pulling it. Item 5 (the writer flag per box) is unblocked. Item 2 read alongside: `composite-gc-tick.sh` and `composite_gc_runner.py` both present in origin/main (rc 0).

**The fresh item-4 reading, both controls PASS.** Structural: 8 pages for 8 prefixes. Key-based: the control suffix `world/aspirations.jsonl` present in the raw enumeration, `control_ok: true` in the JSON twin.

| head key, 24 h window | 2026-09-21 | 2026-10-02 (peak, g-358-228) | 2026-10-03 | 2026-10-04T00:03Z | **U22, 15:15Z** |
|---|---|---|---|---|---|
| versions / 24 h | 1,516 | **2,895** | 2,315 | 2,318 | **2,110** |
| bytes / 24 h | — | — | 25.4 GiB | 25.4 GiB | **23.2 GiB** |
| population (ALL-V) | — | — | 21,973 | 19,644 | **20,911** |
| retained bytes | — | — | 232.4 GiB | 209.6 GiB | **223.6 GiB** (34.0% of 658.3 GiB) |

The rate is the windowed column; the population is the whole enumeration. Never summed or trended across the bar (guard-3978).

**The cap and interval, re-derived from the peak (PREDICTED).** The fresh reading (2,110) is under the peak (2,895), so the peak the derivation hangs on is unchanged. Orphan arrival 1.015 per mutation (U11): 2,938 a day at the peak, 2,142 at this reading. The current choice, 500 per pass at 4 h (3,000 a day), is **1.02x the peak and 1.40x this reading** (1.95x the low). Standing set 14 days in: 41,130 names (~5.9 GB) at the peak, 29,990 (~4.4 GB) at this reading, at U11's 145,871 B mean changed segment. The arming commit re-derives from the peak, not the mean, and makes the choice; the levers for a wider margin, derived here and not chosen: 500 / 2 h or 1,000 / 4 h (both 6,000 a day, 2.04x the peak — and a cap-pass at 1,000 has not been timed against its 1,500 s bound, which the U17 reading still owes). The churn alarm's version limb (>500/24 h) and bytes limb (>2 GiB/24 h) both still fire on the head key at this reading.

**Not established.** A window at any other hour of the day: the rate moves 1.9x between days and the peak of the series still stands at 2,895. Nothing in this unit flipped a flag: neither flag name is in `.claude/settings.json` on zc-02, and the GC delete flag and `--apply` remain as they were.

**Verified.** Both listings exit 0 with both positive controls PASS; the JSON twin agrees with the human listing on every figure. Evidence: `audit-reports/g-358-202/u22-preflip-readings/` under the world store (README indexes the gate read, the two listings and the derivation).

## Reader attestation (U4a)

The writer flip waits for reader-capable code on every box, and `store-cutover-check.py` is
the tool that derives that proof per agent. `STORES["composite"]` registers this store: seam
0913fcd2c5 (U3, the last reader commit), flag `OWNCLOUD_COMPOSITE_STORES` (reporting only, and
unset), consumers the backend, the composite module, the sync layer's `.composite` exclusion
and the two raw readers of the queue key. It must read SAFE before any environment is named in
that flag. On 2026-10-03 (cc-08) it read UNSAFE with `origin_main_does_not_call_the_seam_symbols`,
because U3 (worker_stall.py and aspirations_write.py) is still on worker refs and not on
origin/main; bravo, echo, foxtrot and zeta read `seam_not_ancestor`, and alpha was proven from
its worker ref. That is the right reading for a seam that has not reached main. It clears when
the reducer merges U3 and each agent then commits an iteration.

Registering it exposed a regression U3 caused in the gzip entry: aspirations_write now calls
`decode_whole` instead of `decode_response`, so at HEAD that file matched no gzip symbol and the
fleet veto would have read UNSAFE for gzip once U3 merged. A `decode_whole` spec scoped to that
file fixes it. The general check, for any unit that swaps a call in a file some entry lists: run
`_symbol_report` for every store at HEAD, because the fleet veto reads origin/main and the swap is
not there yet.

owncloud-store-enumerate.py is recorded as a NON-consumer. Its loops list and copy every object
under a prefix with no key excluded by name, so heads and segments travel together. Its limit is
the prefix: one narrower than the environment root misses `_composite-gc-archive/`.

Still ahead of any flip, in an order that is mine and not a measurement: SAFE from this tool; the
head key's xl.meta size measured on the host (rb-12204) to choose HEAD_MIN_BYTES; the GC runner,
its ledger home and the late restore, and the archive pruning rule; the 412-window writer fix;
outcome 4's consumer fixtures; and, at the flip itself, the measurement gauge wired in the same
change (rb-8270).

## Per-box reader attestation (U23)

Gate 1 read SAFE on 2026-10-04 and it cannot answer the question item 5 asks. Its proofs are keyed by agent (the newest commit
touching `agents/<agent>/`, then the first live worker ref that proves), and its local-box veto covers only the box that runs it.
An agent runs a Body on many boxes. rb-8276 is the incident behind the per-box rule: an observer session on a box 357 commits
behind, its daemon 15 h old, failed every governed-store write the moment a peer wrote gzip. For this store the failure is
quieter. A pre-seam reader reads a composite head as an empty queue (H3), so a Body that has not pulled the seam answers a clean
wrong answer after the flip.

Measured before any change (evidence set `u23-per-body-reader-proof/`, 17:59Z, cc-08): 19 live Bodies from fresh heartbeat
carriers (a floor, `fleet-live-bodies.sh`), each judged by its own worker ref under the gate's predicate. 7 prove, 8 have no worker
ref, 4 have a ref whose tip predates the seam. A worker ref is pushed when a Body changes Mind-repo files and a reducer pushes to
origin/main instead, so no ref is the usual state and a ref goes stale between pushes. "First proving ref wins" therefore proves an
agent through one of its Bodies, and a stale or absent ref is missing evidence, not a missing reader.

What U23 builds. Each Body's heartbeat carrier (`session/body-heartbeat-<SID>.json`, written by `heartbeat-tick.sh`) now carries
`main_base`: the newest origin/main commit its checkout contains, `git merge-base HEAD origin/main`. HEAD itself is no use
fleet-wide, since a worker's HEAD is a local merge no other box can resolve; the base is a pushed commit every box can fetch.
`store-cutover-check.py` gains a per-Body lane, opt-in per store through `per_body_carriers`. Only `composite` sets it, so every
other cutover keeps its verdict and its report shape. The lane enumerates carriers from the store of record
(`worker_stall.enumerate_carriers`) and applies the roster lanes' own predicate, `_prove_commit` (seam ancestry, consumer routing,
recency), to the `main_base` of every live Body. A Body is live when its carrier is under 6 h old (the gate's existing
`BODY_LIVENESS_MAX_AGE_HOURS`) and its `body_state` is not closed. All must prove. The verdict overrides a SAFE and never rescues
an UNSAFE.

It fails closed, each with its own reason: `carrier_enumeration_incomplete` (a mirror read, or the listing failed),
`no_live_bodies`, `no_main_base` (a Body whose tick predates the field, or whose checkout cannot name an origin/main base),
`carrier_unreadable` and `unparseable_ts` (freshness unknown, so counted live), and whatever the predicate says per Body
(`seam_not_ancestor`, `main_base_unreadable`, ...). `unproven[]` names agent, host, sid prefix and age.

First live reading (18:14Z): 142 carriers scanned, 123 stale, 1 closed, 18 live, 0 proven, all 18 `no_main_base`. Gate 1 reads
UNSAFE `per_body_reader_unproven`. At 18:20Z, after this Body's tick published `main_base` 3902d864bd, the store read back 1 of 18
proven: writer, store, enumerator and predicate work on live data. Gate 1 stays UNSAFE until each box pulls `heartbeat-tick.sh`
and ticks once. That is the point: a box that has not pulled the tick cannot show that it pulled the seam.

What the lane does not see, so a SAFE from it is read with these in hand:
- a Body whose carrier is stale or frozen in the store (rb-12538), and any box that runs a daemon with no Body. The lane is a floor,
  like `fleet-live-bodies.sh`, not a census;
- the daemon's loaded code. `main_base` is the checkout. A daemon older than its checkout is restarted by that box's next wrapper
  call (`_runtime.sh` runs `mind-api-code-changed.sh`) on a box with a live loop; a loop-less box does not (rb-9811);
- uncommitted edits to a consumer file.
Item 5's own read-back, the composite flag in the environment of every daemon and shell after the flip, is unchanged and still
required: this lane shows the reader is present, not that the flag arrived.

Considered and not built. A decode probe in the tick (the capability itself, rb-8701) would add a Python spawn to the tick's path;
the predicate used here is the one already accepted for every other store, and the probe stays available if `no_main_base` residue
proves it necessary. A worker-ref push on every pass multiplies git traffic by the live Bodies. A hand stamp per box (`--attest`)
is the chore g-115-6243 starved on.

Pinned: `test_store_cutover_per_body_g358202.py` (21 tests: the pure core case by case, `_prove_main_base`, and the `cmd_check`
wiring with a positive control and an opt-out control) and two carrier tests in `test_body_heartbeat_writer.py` (the real tick
against a real git root, with its output fed through the real reader). 12 single-site mutants through `mutation-proof-test.sh`,
each killed by a named test (`mutants.txt`). Scoped suite: PASS_WITH_SKIPS over 34 files, one file skipped at module level on
this box (`test_owncloud_backend.py`, dependency absent).

Still ahead of item 5: roll the tick to every box and read Gate 1 SAFE with `per_body.bodies_proven == bodies_live`. The rest of
the checklist is as before.

## The runner timed on the live store (U24)

Item 7 asked for the 1,500 s bound (U17) to be read against a timed pass, and U17 recorded that the real runner had never run against the
live store. U24 ran it there, on a throwaway store, so nothing the fleet reads or writes was touched. No code changed. The harness, the run
records and a README are in `u24-timed-gc-pass/` under this goal's audit reports in the world store.

**How.** A throwaway environment id (`g358202-probe-u24-<run>`) under the live MinIO bucket (hostname cc-08, `uname -r` 6.8.0-142-generic,
2026-10-04T18:49Z to 18:54Z), seeded with the live queue object's own content (stored 11,873,672 B, plain 33,094,793 B). The real writer, with
the flag naming only the probe id, made 150 edits, each leaving one segment object unreferenced (149 orphans). 40,000 synthetic orphans (8,192 B
each, valid names with the content's md5 in them, written by 8 threads in 82.7 s) were added so the segment directory, the ledger and the
enumeration are as large as they will be after the flip: 14 d of arrival is about 32,900 names at the designed normal (2,350 a day) and 41,132 at
the peak (2,938). The real `composite_gc_runner.run_pass` then ran on the real `OwnCloudBackend`: two observe passes, apply passes at caps 25, 50, 75
and 500 (the pass clock moved past the 14 d grace; the objects are minutes old), and an observe pass 25 h later, inside the late restore's 28 h
horizon, so that step ran over the four runs. Every S3 call went through a hook that refuses a mutating call outside the probe prefix and a read
outside it and the one live key, and a second hook refuses any lock-table call outside the probe prefix. Cleanup was by VersionId under the probe
prefix: 40,408 versions removed, none left, lock row absent; an independent listing under every `g358202-probe-u24-` prefix reads 0 versions,
0 markers and 0 keys (positive control: the live key's prefix lists 5).

| pass | wall s | CPU s | S3 calls | S3 s | deleted |
|---|---|---|---|---|---|
| observe, 40,149 orphans, no ledger yet | 3.25 | 2.35 | 48 | 2.79 | 0 |
| observe again | 3.02 | 2.26 | 47 | 2.76 | 0 |
| apply, cap 25 | 3.82 | 2.54 | 230 | 3.45 | 25 |
| apply, cap 50 | 4.52 | 3.05 | 407 | 4.05 | 50 |
| apply, cap 75 | 4.62 | 3.13 | 585 | 4.12 | 75 |
| apply, cap 500 (the real cap) | 19.57 | 7.85 | 3,570 | 18.73 | 500 |
| observe 25 h later (late restore over 4 runs) | 3.05 | 2.22 | 54 | 2.76 | 0 |

**Readings.**
- The bound holds with a wide margin. The pass at the real cap took 19.57 s, 1.3% of 1,500 s (76.6x) and 1.1% of the 1,800 s lease; an observe
  pass took 3.25 s. The four apply points fit 0.0337 s a name plus 2.65 s, which gives 19.5 s at 500 (measured 19.57 s). The 2.65 s is the
  enumeration: 42 `ListObjectsV2` pages at 64.6 ms mean, 2.3 s of CPU to plan over 40,237 names, and the ledger read (3,039,616 B for 40,149
  entries, 75.7 B each). PREDICTED by extrapolating past the measured range: 36 s at a cap of 1,000 and 171 s at 5,000, so a pass at U22's
  lever of 1,000 should take about 36 s; it has not been run.
- Per name at the real cap: 7.14 S3 calls. `DeleteObject` 2.5 ms mean. `ListObjectVersions` 15.3 ms mean at cap 500 against 2.2 to 2.8 ms at caps
  25 to 75, and it is 7.6 s of that pass's 18.7 s in S3. The synthetic keys sit at the end of a 40k-key namespace and are 8 KB where the real
  ones are 123 KB, and the run does not separate the two causes.
- The archive path ran end to end on the live bucket (guard-1301, guard-1305): every deleted name has its archive copy, read by HEAD at the key
  `gc_archive_key` names (25 of 25, 50 of 50, 75 of 75, 500 of 500), one receipt per run, and no deleted name has a version or a delete marker
  left. No pass put anything back. No anomaly, no warning, and `unknown` 0 on every pass.
- Item 6's five read-backs held on a real observe pass: verdict `observed` with no anomaly; the state document's `last_pass_at` equals the pass
  clock; ledger entries 40,149 equal `counts.orphans` 40,149; `unknown` 0; `elapsed_s` 3.254 recorded. The ledger is rewritten only when it
  changes: the first pass wrote 2.96 MB, the second wrote 295 B (the state document) and read the ledger back.
- The 150 real-writer orphans that were deleted averaged 123,378 B (18,506,670 B). At 2,350 to 2,938 names a day their archive copies add
  about 290 to 363 MB a day and the ledger rewrite about 18 MB (6 passes of 3.0 MB), on keys other than the head key. PREDICTED, arithmetic on
  this run's mean; outside outcome 5's key-level bar.

**What this settles for item 7.** The bound was read against a timed pass at the cap on a store at steady-state size: it holds, and nothing in
the run argues for changing it in either direction. Time is not what limits the cap. The design's own reason for 500 (a pass stays bounded and its
enumeration reviewable) is, and the arming commit makes that choice.

**Not established.**
- The runner has still not run against the real composite head or the real ledger (the flag is unset). The first real observe pass is the first
  specimen with the real store's contention and segment fan-out, and its `elapsed_s` is item 6e's reading.
- A pass stopped by `timeout` (exit 124 or 137, the lock row after a kill) is still unexercised: every pass here finished.
- `composite-gc-tick.sh` was not run. The times are the runner's; the tick adds a clock pair, the log line and the post router.
- One process only, so `lease-held` was not met. The fleet was live on the same MinIO during the run, so the latencies include its ordinary
  traffic and no second GC pass.
- Every orphan here has one version. A real orphan can carry two after a freshen (U7), which lengthens the per-name version chain by one.
- 40,149 names is derived (14 d at the peak), and the arrival range rests on three 24 h readings (U22).
- The same run from the reducer's box (item 7, first bullet) is still owed.

## What the retained versions cost (U25)

Outcome 6, dispositioned 2026-10-04 (U25; evidence `audit-reports/g-358-202/u25-retained-version-disposition/`).

**Decision: acceptable, no new noncurrent-expiry policy.** The lifecycle config was re-read through the documented route (five
rules; `updatedAt` 2026-09-28T06:42:33Z, unchanged from item 3): a bucket-wide 7-day noncurrent-version expiry covers the head key,
and none of the four 1-day prefixes matches it. Measured at 19:14Z: the head key retains 21,264 versions and 244,253,354,209 B
(227.5 GiB, 34.0% of the bucket's bytes, oldest 8.0 d). PREDICTED after the flip, at the head's stored 262,144 B a version: 3.96 to
5.65 GiB (5.19 GiB at today's count), 40x to 57x lower. The retained COUNT does not change (one head PUT per mutation), so xl.meta
stays about 8.9 MB (arithmetic), the cost the legacy key carries today.

**Two conditions, because the 7-day rule does not reach them:** the orphan standing set (0.34 GB a day, 4.76 GB at the 14 d grace)
needs the collector armed within a few passes of T0 + 14 d (item 7), and the GC archive (290 to 363 MB a day at a retention-immune
prefix) needs the archive pruning rule.

**Lever not taken:** a head-specific 1-day noncurrent rule (precedent: the four hot prefixes) would hold 2,029 to 2,895 retained
versions (7.3x to 10.5x fewer, xl.meta 0.85 to 1.22 MB) at the price of the head's 7-day undo window, and an expiry change is its own
archive-before-delete question. Relayed, not filed.

**Check:** at flip + 9 d, `s3-store-churn.sh --window-hours 24 --top 8 --json` with `control_ok` true; PASS when the head key's retained
bytes are at most 1.5 x `versions_total` x 262,144 B; FAIL above 3x that, or with an oldest retained version older than 9 d.

## The real tick, end to end (U26)

Run 2026-10-04T20:26:57Z on cc-07 (evidence `audit-reports/g-358-202/u26-real-tick-end-to-end/`). U14 and U24 drove `run_pass` in-process and U16 and U18 drove the tick's router with a stand-in
runner. This run started `bash core/scripts/composite-gc-tick.sh` itself, which started the real runner on the real backend, against a throwaway
composite store under the live bucket (seeded by the real writer from a synthetic 5.9 MB object, so no goal text was read; a guard refused any call
outside the probe prefix and its controls were observed refusing). The tick's state directory, the poster and the runner slot were stand-ins; the
tick script, the runner file, the backend, the config block and the call shape were not.

**Measured (all MEASURED, none predicted).** An observe pass over 29 listed objects cost 8 S3 calls (3 GET, 2 list, 1 HEAD, 2 PUT: the two documents) and 2 lock calls,
`elapsed_s` 0.123. A second full observe pass over an unchanged orphan set rewrote `state.json` and not `ledger.json` (`updated` unchanged, 7 S3 calls). One new orphan grew the ledger by
one entry, rewrote it and kept the oldest first sighting. `not-due` cost 1 GET and 2 lock calls and wrote nothing; `lease-held` cost 1 lock call and wrote nothing, both with the tick's rc 0.
A ledger that is not a ledger gave `ledger-unreadable`, runner and tick rc 1; the runner wrote the state and did not overwrite the ledger, and the tick handed the runner's own `post` to the
poster as an `escalation` and recorded key `44b654a09674`. Item 6 (a) to (e) held at the probe: verdict `observed` with rc 0 and no anomaly, `last_pass` within the interval, `counts.orphans`
11 equal to the ledger's entries, the read-back's entries and an independent listing count, `unknown` 0, `elapsed_s` recorded. A second tick at once starts no runner (the stamp).

**One change to the read-back ("The two read-backs, as run").** It printed `ledger present: entries=0 updated=None` and exited 0 for a document that is not a ledger, the same shape as an
empty ledger bar `updated=None`. In the ledger branch, `led = d.get("ledger") or {}` is now:

```
        led = d.get("ledger")
        if not isinstance(led, dict):
            bad = 3
            print("ledger present but NOT A LEDGER (the runner stops on it as ledger-unreadable): keys=%s" % sorted(d))
            continue
```

Run offline against in-memory documents (no store call): a junk ledger gives rc 0 before and rc 3 with that line after; a valid empty ledger and a populated ledger print byte-identical
output before and after.

**Not established.** The run from the reducer's box with the real flag and environment id (item 7's first bullet, still owed); item 6's read of the REAL documents; the router's de-duplication
and its episode end with the real runner (U16's tests and U18's rehearsal cover both with a stand-in); the wall-clock bound firing; any delete path (`--apply` was never passed and the delete
flag was never set).

## The archive pruning rule (U28)

Decided 2026-10-04 and planned in code, not executed: nothing removes an archive run yet, no environment is named and the flag is unset. The planner is
`plan_archive_prune` in `_owncloud_composite.py`. It is pure: it reads a listing, receipts and a set of names it is handed, and removes nothing.
`test_owncloud_composite_gc_prune_g358202.py` pins it with 70 cases that need no S3, and each of 24 single-site mutants of the planner was killed on an isolated copy
(the live files were byte-identical before and after). Evidence: `audit-reports/g-358-202/u28-archive-pruning-rule/`.

**Why a rule is owed.** The archive holds the ONLY copy of what a versioned delete removed (the receipt says so: "no noncurrent copy remains, so the archive object is the only
copy", U8), and it grows by what the collector removes: 290 to 363 MB a day at the predicted rate (U25), so 8.7 to 10.9 GB a month unpruned (30 times that rate, arithmetic).
It is not a condition of arming, because nothing breaks while it grows. It is what makes the archive bounded.

**The rule.** A run directory of the archive is removed when ALL of these hold; every other run stays, with a named reason:
1. Its name is a run id (`gc_run_time`). The directory also holds `_state/` (the scheduled pass's two documents per store) and, once an executor exists, `_pruned/`
   (the tombstones, below). Both are reserved (`GC_ARCHIVE_RESERVED`): neither is a candidate and neither is reported. Any other name is reported (`unknown`) and left
   alone. A pruner that shares a parent directory with another subsystem's files excludes them by what they ARE, never by what it happens to list (rb-1219).
2. It is at least `GC_PRUNE_AFTER_S` old: 14 days, the same as `GC_GRACE_S`, and never shorter (a shorter window is refused, a longer one honored). My choice, not a
   measurement. The reasoning: an orphan waited 14 d unreferenced before its delete, so it stays recoverable as long after; once the runner's trust window
   (`TRUST_WINDOW_S`, 24 h) has passed the archive can only be wanted to undo a defect in the sweeper itself, and a head that names a missing object fails every read of
   it loudly (`join`), so such a defect shows at the next read and not a month later. At the U25 rate the archive then holds 4.1 to 5.1 GB (14 times the daily rate,
   arithmetic).
3. Its receipt is the format-1 archive receipt of THIS run (kind, format and run id all read), for the store the pass covers, with status `done` and every object a
   record. Otherwise it stays as `no-receipt`, `receipt-not-recognized`, `other-store`, `status-not-done` (a run that stopped half way holds the only copy of what it did
   delete) or `receipt-malformed`. A run directory with no receipt deleted nothing (`composite_gc_apply` writes the receipt before its first delete), but nothing in the
   planner proves that, so it is kept and shown to a person.
4. None of its objects is a name the current head names while the store lacks it: those are exactly the objects `composite_gc_restore` could still take from the
   archive. The check is on KEY IDENTITY against the live head and store, an axis independent of the age that chose the candidates (guard-6388: a coverage check that
   re-uses the selecting predicate is circular and cannot fail).
5. It is within the cap, `GC_PRUNE_MAX_RUNS` = 12 runs a pass, oldest first. A delete pass every 4 h adds at most 6 runs a day (U22's 500 / 4 h), so 12 is twice a day's
   growth: one prune pass a day keeps pace and one per tick drains a backlog. At the default `GC_MAX_DELETE` (500; the tick starts the runner with no argument, so the default applies) a run holds at most 500 objects, so a pass is bounded at 6,000 deletes.
   The rest stay as `over-cap`.

**What the plan refuses.** It removes nothing when `now` is not a finite number, when the window is not a finite number or is under the floor, and when `needed` (the names
the head names and the store lacks) is None, because without it nothing shows that the archive is not what a restore is waiting for.

**What reaches an archive object today.** Item 3 of the flip checklist (U15) read the bucket's lifecycle config on 2026-10-03 and U25 re-read it on 2026-10-04T19:15Z
(`updatedAt` 2026-09-28T06:42:33Z both times): one bucket-wide 7-day noncurrent-version expiry and four 1-day noncurrent expiries under keys that continue `world/` or
`meta/` after the environment root; no rule carries `Expiration` or a transition. An archive key is the environment root, then `_composite-gc-archive/` (`gc_archive_key`),
so it matches none of the four prefixes, and nothing expires a current version. That is why the archive only grows until a rule removes runs. It is a reading of the MinIO
bucket (the live store since 2026-09-14); the older store's config is still unreadable.

**What the executor owes (delivered: see "The archive prune executor, verified (U31)" below).** The plan says a run MAY go, not that its objects are still there and still what
the receipt says. The executor is a deletion and runs the archive-before-delete protocol in its own right:
- INPUTS: one delimiter listing of the archive directory (`composite_gc_runs` already does it and keeps only run ids); the receipts of the runs at least 14 d old (a
  younger run needs none); `needed` = the names the head names minus the store's listing, from the head and listing one delete pass already reads.
- ENUMERATE each run's prefix. Every key must be `RECEIPT.json` or an `archive_key` its receipt names, at the receipt's size; a foreign key keeps the run. A name the receipt
  lists and the prefix lacks is already gone and is fine (a pass that died half way finishes).
- THE RECOVERY LAYER (rb-2859: 'versioned' is not 'archived'). The delete is a plain delete of the current version (never by version id), which on a versioned bucket
  leaves a delete marker and the object as a noncurrent version until the bucket's noncurrent window ends (7 d, from the config above), so that window is the undo. The
  fleet principal cannot read the lifecycle config (U15, item 3: AccessDenied), and the delete pass already decides 'versioned' from the `VersionId` on the archive GET
  and not from the bucket's config, so the executor cannot do the config reading itself at run time. Its unit must decide between two things: (a) a reading of the config
  by the storage host's root at arming (the route U15 item 3 and item 7 already use), recorded with its `updatedAt` in the arming commit, plus the per-pass control below; and
  (b) a cold copy of every run before it goes, which defeats the pruning. Recommendation: (a). A pass whose control fails removes nothing.
- A POSITIVE CONTROL BEFORE THE BATCH (guard-1301 asks for archive, delete and restore end to end; rb-9122: it earns its keep by being able to change the plan): write one sentinel
  object under a throwaway key beside the archive (never inside a run), delete it the way the batch will, and read its versions back: a `VersionId` on the PUT, the delete marker and
  one noncurrent version at the sentinel's size must all be there. Then restore it from that noncurrent version and compare its md5 with the original's. Any miss and the pass stops.
- Recompute `needed` from a fresh head read before each run is removed, delete by single calls, and read each one back absent.
- TOMBSTONE, then release the original: write the receipt with `status: pruned`, the time, the pass's counts and the object list kept, to `_pruned/<run id>/RECEIPT.json`, and only
  then delete `<run id>/RECEIPT.json`. A run directory in a listing is then by definition not yet pruned, so a pass reads the receipts of the runs inside its window and never
  of history. A pass that dies between the two leaves a run whose receipt still reads `done` and whose objects are gone; the next pass finishes it.
- BLAST RADIUS: the archive is read by `composite_gc_restore` (names excluded by rule 4), by the runner's late-restore sweep (a run is visited while its age is under
  `TRUST_WINDOW_S` plus one interval, 28 h at a 4 h interval, so never a run old enough to go here) and by a person. `composite_gc_runs` keeps only names `gc_run_time` accepts, so
  `_state` and `_pruned` never reach the sweep. The archive sits beside `world/`, `meta/` and `agents/`, the only roots the sync layer pulls (the comment on `GC_ARCHIVE_DIR`), so
  no mirror holds a copy to push back: the resurrection in the `owncloud-legacy-prefix-prune` node needs a local copy under a synced root, and the archive has none.
- LAND IT DARK (guard-1301): observe only first, the control on a throwaway archive prefix, then its own switch. A GC flag licenses deleting orphans, not deleting their archive,
  as the writer flag does not license deletion.

**Check.** Two readings of the archive prefix's bytes from a current-version listing with `_state/` and `_pruned/` excluded, taken 7 d apart and both after the first prune pass
has had 14 d to settle: PASS when they differ by under 15%; FAIL when the later is 1.5x the earlier or more; between the two, read again 7 d later (rb-4897: a running cadence is
not evidence that it drains). The bucket's own byte count lags the listing by the 7-day noncurrent window, because the plain delete leaves each object as a noncurrent version.

**Not established.** The executor. The lifecycle config as it stands on the day the executor runs (the reading above is of 2026-10-04T19:15Z), and how the executor learns it without
a principal that can read it (its unit decides). No store was read this unit, so no live listing of the archive prefix backs any statement here. Every moto-backed test of the composite
code on this box: moto is not installed here, so 10 of the 15 test files the fast tier selected were skipped at module level, and the other five, the planner's among them, ran green. The
planner has read no real receipt: the shape it reads is the one `composite_gc_apply` builds (`owncloud_backend.py`, the `receipt = {"kind": "composite-gc-archive", "format": 1` block
and the closing `receipt.update(status=...)`), read from the code and not from a run, because the collector has never run against production. The 14 d window and the cap of 12 are choices.

## The writer flag (U30)

Committed on an alpha worker ref 2026-10-05 (zc-07): `OWNCLOUD_COMPOSITE_STORES=ayoai-mind` added to the `env` object of
`.claude/settings.json`, beside `OWNCLOUD_GZIP_STORES`. The flag reaches a box when the reducer merges the ref and that box pulls it, and
a process takes it at its next start, so the rollout is per box and takes days (U21: a commit that touches only settings.json restarts no
daemon). The per-box rollout is the rest of item 5; this box carries the flag locally from the commit.

**Why it is landed now.** Item 1's gate has read `UNSAFE per_body_reader_unproven` since U23 (12 of 18 live Bodies proven at 20:51Z, 14 of 18 at
21:35Z). Re-read at 2026-10-05T10:08Z it reads **rc 0, SAFE**: per_body 18 of 18 proven, `all_proven` true, `enumeration_complete` true,
`distinct_bases` 6, 142 carriers scanned, 123 stale skipped, 1 closed skipped, local box (zc-07) seam present, all five agents attested by
derivation with commits of 2026-10-05. U23's lane filled exactly as its prediction said it would ("per_body.bodies_proven should climb toward
bodies_live over the next merge-and-pull cycle"): the merge of U23's ref landed, the boxes pulled, and their ticks published `main_base`. Item 2
was re-checked for this flip: `composite-gc-tick.sh` and `composite_gc_runner.py` are both present on origin/main (both `git cat-file -e`
exits 0). Item 3's lifecycle read stands as U25's (2026-10-04T19:15:08Z; the re-read is owed at item 7, not before the flip).

**The pre-flip reading (item 4, owed "before the first box carries the flag").** `s3-store-churn.sh --window-hours 24 --top 8` at
2026-10-05T10:2xZ: rc 0, `control=PASS`, structural control PASS (8 non-empty pages for 8 prefixes). The head key took **2,201 versions,
24.1 GiB** in the 24 h window; the whole enumeration holds 19,892 versions and 215.2 GiB for the key (33.8% of the 635.9 GiB bucket; current
108,061 objects / 52.6 GiB, noncurrent 161,124 / 583.3 GiB, delete markers 45,596; oldest retained head version 2026-09-27). This is the
companion reading outcome 5's bar is read against (1,757,775,353 B in a fresh 24 h window): the window opens after the LAST box restarts with
the flag, because a window containing a process without the flag measures the mixed fleet and about 70 whole-object writes a day fail it (U21).
At the head's stored size, the bytes limb is far from the bar and the versions limb (>500/24 h) will keep firing (U20's decision stands).

**Not established.** The rollout itself: no box has restarted with the flag yet as of this commit, so item 5's pass (the composite line in the
daemon and the shell of every box, the reducer's included) and item 6's observe passes are future readings, and the per-box read-backs are
per-box evidence read with item 5's names-only commands. The churn figure is one reading; the 24 h rate moves 1.9x between days (U25's caveat),
so outcome 5's window is read beside this one, not against a trend. The arming commit (item 7) is still gated on item 6's first real read-backs
and the T0 + 14 d reading, unchanged.

**Check.** Per-box read-back of the flag (item 5's commands, the gzip line the positive control); item 6's read at the first read and T0 + 1 d
and T0 + 14 d; outcome 5's fresh listing after the last box restarts with the flag.

**State at 2026-10-05T21:2xZ (alpha, cc-04), and a corrected premise.** The flag did not stay on a worker ref: the reducer merged it as M1 (4afe5df478a0, 11:21Z) and it reached origin/main at 21:01Z while item 1 read UNSAFE (per_body 17 of 19 proven; unproven alpha 64d80ba1 and bravo 1d2e91a2 on DESKTOP-O91DLK2, `no_main_base`). "A process takes it at its next start, so the rollout takes days" did not hold for a box whose daemon respawns on HEAD moves: `daemon_overlay_settings_env` (g-358-183) fills a respawned daemon from the working tree's committed settings.json, so this box's daemon carried the flag from its first respawn after the merge, with composite heads in the store from 11:37:07Z (83 of the newest 1,000 versions of the goal-queue object by 21:01:09Z, none a small non-head version). Commit 4b18c2f80c sets the value to the empty string (off), and this box's daemon was respawned with it off at 21:16:25Z. Item 5 is therefore OPEN again: set `ayoai-mind` back only after item 1 reads SAFE, re-read in the same command as the commit. Record: rb-12875, msg-20261005-212330-alpha-606, and the g-358-202 progress_note marker `m1-flag-armed-disarmed-alpha-20261005`.

## Outcome 3 under design (b): no segment handler, and the seam is pinned (U3b)

Outcome 3 reads "merge handler registered for the segment set". Under design (b) there
is no segment set for a handler to receive: the local mirror is one joined legacy file,
and the handler registered for the legacy key (`merge_handler_for` resolves it to
`merge_aspirations`) only ever receives whole joined bytes. H1 cannot occur there: the
displaced id is `max(all ids)+1` over every range, and `split` files each goal under the
range its own id names. Measured 2026-10-03 on a fixture whose range 1 already holds
`g-1-250`: two distinct goals racing for `g-1-249` merge, joined, to a loser at
`g-1-251` (range 1, ids unique); the same merge over range 0 alone assigns `g-1-250`, an
id range 1 already owns. That second result is the H1 collision, and it is why no
handler may see a segment.

guard-6907 (classify the name the writer mints, not the live file) was applied to the
segment name. `merge_handler_for` returns None for the `.composite/...` key and for its
basename, so by the lookup it reads as class (b). The prior question decides it carries
none of class (b)'s risk (governed-store-write-classes.md, class (c)): the sync layer
never carries these objects (`.composite` is in `_EXCLUDE_DIRS`; the walk prunes it and
the pull sweep skips the key), and the writer PUTs each one create-only
(`IfNoneMatch="*"`, `IfMatch` unset; a name is its md5, so an existing name already
holds the same bytes). The only fenced write is the head's conditional PUT, whose 412
reconcile resolves the legacy key to class (a).

Pinned, each proof run with `mutation-proof-test.sh` (red under the sabotage named,
green after restore, targets byte-identical to HEAD afterwards):

- `test_owncloud_composite_g358202.py::test_a_displaced_goal_is_placed_past_every_range_the_merge_can_see`
  is red when `SEGMENT_SPAN` goes 250 to 251 (`assert 'g-1-250' in ['g-1-251']`).
- `test_owncloud_composite_g358202.py::test_no_merge_handler_resolves_for_a_composite_segment`
  is red when a handler is returned for a `.composite` path.
- `test_owncloud_composite_sync_exclusion_g358202.py`: both
  `test_the_segment_key_is_under_the_excluded_directory` and
  `test_pull_sweep_never_pulls_a_segment_object` are red when `.composite` leaves
  `_EXCLUDE_DIRS`; the sweep test ALONE is red when the sweep's directory filter is
  disabled and the constant kept, which the constant check cannot see.

Create-only segment PUTs and the fenced head are pinned already in
`test_owncloud_composite_write_g358202.py` (`IfNoneMatch == "*"` on every segment,
`IfMatch` on the head), against a fake S3 everywhere and against moto where it imports.
On cc-08's system python the moto variants skip (17 in that file, 15 in the read file)
and the fake-S3 variants pass, so on 2026-10-03 the eight composite test files plus
the eager-pull and cutover-registry files were also run under a `--system-site-packages`
venv with moto 5.2.2: 470 passed, 0 skipped. If design (a) or any per-segment merge is
ever chosen, the literal requirement revives: the handler tripwire goes red, and H1's
registration test (every output id of a segment merge names that segment, at most 4
displacements per range) must exist before a handler is registered.

## Outcome 4 under design (b): consumers exercised against a segment fixture (U4b)

Under design (b) no consumer opens a segment: the local file is one joined legacy file, so a consumer can only
receive the wrong BYTES (a head, a short join, a stale mirror) or read a REMOTE attribute whose meaning the layout
changed (object version, size, a directory listing). Outcome 4 is therefore answered by a census of how each consumer
acquires bytes or attributes, and by exercising each acquisition class on a store that the real writer wrote and a
second box read through the real reader. The census is by regex and by reading, not a call graph.

Census (2026-10-03, cc-08, HEAD 1ab47af7e6, non-test code): 172 .py and .sh files name the queue file (219 counting
md and yaml); 56 files carry a raw-S3 or remote-read signature (get_object, an SDK client import, s3api, aws s3, decode_response,
backend-cat, efs-ssh); 30 of those also name `aspirations`. Read one by one:

- The seam: owncloud_backend.py, _owncloud_codec.py, _owncloud_composite.py, store-cutover-check.py.
- Raw readers of the store: worker_stall.py and aspirations_write.py (both on decode_whole since U3), and the LAN cache
  endpoint (mind_api cache_object.py), which serves the head's decoded plaintext. `_refresh` joins AFTER the cache
  fetch (the `_cache_fetch` then `_composite_whole` pair), pinned by test_a_lan_cache_hit_for_the_head_is_still_joined.
- Readers of a remote attribute: _idle_cache_common.py (a memo keyed on stat().version, else read_authoritative_bytes),
  aspirations_write.py `_head_etag_fast_path` (version against the fence), the sync layer (`_content_matches`, which
  prefers plain_md5), backend-cat.sh (`cat` is read_authoritative_bytes; `head` compares plain_md5 and prints the HEAD
  object's size). Pinned by test_stat_names_the_head_and_moves_when_a_non_newest_segment_changes.
- Listing readers: _utilization_store.store_paths (a date-shaped name regex, so `.composite` is ignored), the sync
  sweeps (the segment directory is excluded; pinned in U3b and again here), backend-cat.sh `list` (shows it, harmless).
  list_dir of the world directory returns ['.composite', <the file>], which test_listings_show_the_segment_directory_...
  pins as a characterization, not as a promise.
- Not readers of the store: cold-snapshot-tick.py (a marker key), history_vacuum_archive.py (a graveyard copy of history
  files), cold_snapshot.py and _transplant_pack.py (the local mirror; an SDK name appears in install text), peer_queue_read.py
  (reads no bytes), provision_aws.py, v2.0.0-n4-aws-resource-names.sh (an env-prefix `aws s3 sync`, which carries the head
  and `.composite/` together), the efs-ssh mentions (another store), and the files that name backend-cat or S3 only in
  prose or a regex (closure_evidence.py STORE_CLAIM_RE, wrapper-surface.py, obligation-audit.py, goal-selector.py, ...).
  Every other file of the 172 reads the local mirror, which is whatever `_refresh` materialized.

The named consumers run on box B's mirror, read through the real reader from a store box A wrote through the real writer
(core/scripts/tests/test_owncloud_composite_consumers_g358202.py, 5 tests, each over an in-memory S3 and moto, 10 runs):
the daemon's id lookup, and the real aspirations-query.sh pointed at that daemon, return goals held in the token 0, 1 and 2
segments (g-358-300 is in an OLD segment of three); the daemon's compact read feeds precheck-eval's zombie sweep; and the
goal selector's own refresh_aspiration_caches, read_jsonl and collect_candidates run on the result. The fixture is built so
each verdict depends on a non-newest segment: g-7-5 is the only unfinished goal of asp-7 and sits in token 0 of 2, so the
sweep reads asp-7 as live until box A completes it (one segment and the head, asserted) and as an all-terminal zombie
after, while the selector's candidate set loses it on box B with no manual refresh. A guard in every test asserts the
stored object is a head over the six expected segments, so a fixture that fell back to a whole-object PUT cannot pass.

Mutation proofs (guard-6701: a one-token mutant written beside the live file, loaded as a throwaway module, unlinked in a
finally; owncloud_backend.py and goal-selector.py md5-identical before and after): no mutation, 0 of 10 red; `_refresh`
keeps the head unjoined, 6 red (the lookup, the flow and the cache pin, all consumer-level); the steady-state segment PUTs
skipped, 10 of 10 red; stat() drops plain_md5, 2 red; the selector's refresh made a no-op, 2 red (the flow test, on its
"after" half). The group of ten composite and neighbor test files runs 478 passed, 0 failed, 0 skipped under a moto venv.

Disposition. Outcome 4 is met under design (b) for every acquisition class above. What it does not show is that each of
the 172 files was run, and the census that sorts them is a regex plus reading. The forward guard that would make the claim
hold for a file added tomorrow is the registry-wide invariant test for STORES entries (relayed as sq-013 after U4a, not
filed here). The goal owners should confirm this reading of outcome 4, as the fork section above already asks.

## The archive prune executor, verified (U31)

Verified 2026-10-05. The code and a 116-case test file were already in the tree; this unit measured them under moto, found three pins missing and added them, and ran the
executor's control and its read-only enumeration once each on the live store. Evidence: `audit-reports/g-358-202/u31-prune-executor-verified/`.

**What was found.** The executor U28 listed as owed has been in the tree since commit 8230d2afee (2026-10-05T06:38Z). That commit is a `chore(worker-stop)` commit: a worker Body
that was stopped mid-unit had its uncommitted work swept into it, so no section, note or evidence recorded it, and its code comments call it U30, a number the writer-flag unit took
later. The NOT CLOSED lists of the two units after it (router, writer flag) therefore still said the executor was owed. This unit found it by listing the test directory, not by
reading this record. Nothing calls it: a search of `core`, `mind_api` and `.claude` finds the definitions and their documentation only, and `OWNCLOUD_COMPOSITE_GC_PRUNE` is set
in `.claude/settings.json`, in that box's agent `local-paths.conf` or in its environment.

**What it is.** `OwnCloudBackend` in `owncloud_backend.py`, with the pure parts in `_owncloud_composite.py`:
- `composite_gc_prune_enumerate(path, now)`: read-only, no flag. It lists the archive's top-level names, reads the receipt of every run at least 14 d old, reads the head and the
  segment directory for `needed` (the head is read again after the listing: a head that moved is not an answer), plans with `plan_archive_prune`, and for each planned run lists its
  prefix and scopes the removal by the receipt (`plan_run_removal`): the receipt's `archive_key`s, each at the receipt's size. A key outside the run's `objects/`, a key the receipt
  does not name, a missing receipt or a size that differs keeps the whole run, with the reason.
- `composite_gc_prune_control(now)`: no flag. It PUTs a sentinel under `_state/_prune-control/<token>` (the response must carry a real `VersionId`), reads it back, plain-deletes it,
  requires the key absent with exactly one delete marker (the newest entry) and exactly one noncurrent version, the PUT's, at the sentinel's size, restores from that version and
  compares its md5, puts the restored bytes back and reads them, then removes every version it made. A key that already has any entry is refused before the PUT. Any miss stops the pass.
- `composite_gc_prune_apply(path, now)`: inert (`prune-not-enabled`, no S3 call) unless `OWNCLOUD_COMPOSITE_GC_PRUNE` names this environment and the store is on the allowlist; the flag
  is its own, never implied by `OWNCLOUD_COMPOSITE_GC` or the writer flag. It enumerates (a refusal stops it), runs the control (a miss removes nothing), then goes run by run, oldest
  first, re-reading the head's `needed`, the run's receipt and its listing and re-planning that run (guard-5952). Per run the order is the protocol: HEAD each object (size equals the
  receipt's), plain delete (never by version id), read it back absent; then the tombstone `_pruned/<run id>/RECEIPT.json` (status `pruned`, the objects kept, `removed` as key to version
  id, `gone`, the control's evidence, the restore text), read back equal; and only then the run's own RECEIPT.json, deleted and read back absent. A pass that dies anywhere leaves that
  receipt reading `done`; the next pass finishes the run and keeps the version ids an earlier tombstone recorded.

**Measured.** All under moto 5.2.3 and pytest 9.1.1, installed with `pip install --target` into the session's scratch directory (nothing system-wide changed); `STORAGE_BACKEND=local`.
- The planner file and the executor file together: 186 passed (70 and 116) in 16 s. Every moto-backed test the earlier units had to skip now runs.
- The whole own-cloud and composite family (47 files, `test_owncloud_*` and `test_composite_*`): the first run read 1735 passed, 6 failed, 18 skipped. All six failures are in
  `test_owncloud_integration.py` and none touches the executor. The cause is the shell's `STORAGE_S3_ENDPOINT_URL`, which that file's fixture does not clear (it sets credentials and
  the bucket only): with the variable unset the file passes 7 of 7, and with it pointed at a dead local port the same test fails with `EndpointConnectionError` naming that address, so
  the client built from the variable is not intercepted by moto. Against the real endpoint the calls carried the fixture's fake credentials and its bucket name (`zds-data`, not the
  store's) and came back 403. With the variable unset the family reads 1741 passed, 18 skipped, 0 failed; after the three pins below, 1745 passed, 18 skipped, 0 failed (264 s, HEAD
  unchanged throughout). All 18 skips are ones the tests declare for a property of a double (moto stamps `last_modified` at PUT so an object cannot be aged, the in-memory double pages at 2) or for Windows.
- A mutation matrix over the executor, 53 single-site mutants on isolated copies of the two modules (the live files were byte-identical before and after): flag ignored, each of the
  control's checks neutralised, the size check, delete by version id, the receipt deleted before the tombstone or before the objects, the re-plan skipped, a stop turned into a continue,
  the receipt scope widened to the directory, a run prefix widened to the archive, the prune flag aliased to the orphan flag, and so on. 50 died when first run; three survived and
  were pinned: the control's read-back right after its PUT, a second noncurrent version in the chain, and a clock or window that is not a number reading receipts. All 53 now die, each
  with its red tests named. Isolation needed care: a copy under the repo root picked up the repo-root `conftest.py` and `pytest.ini`, and the first matrix read
  every mutant as surviving; a module that raises at import left the file green (116 passed) until pytest was given `--confcutdir`, `--rootdir` and `-c` for the copy, after which it
  turned the file red. That probe (a mutant that raises at import) is part of the matrix.
- On the live store (2026-10-05T14:34:08Z), the control once, with its planned key written before its first PUT: ok, 0.043 s. The PUT carried a real `VersionId`; the plain delete left
  one delete marker and one noncurrent version at the sentinel's 81 bytes; a read by version id matched the md5; the restored copy read back; and the cleanup by version id was
  permitted and complete. An independent listing in a separate process found 0 versions, 0 delete markers and 0 current objects under the key and under `_prune-control/`.
- On the live store, the read-only enumeration (0.24 s): the archive prefix holds two top-level names, `_state` and `probe-g358202-20261002T231009Z`, the U2e probe's directory, which is
  not a run id and so is reported `unknown` and left alone. The plan refuses with `needed-unknown: head-not-composite`, because the store is still the legacy layout. No archive run exists.

**Review (by reading, and by the matrix above).** Read against U28's list item by item (inputs, enumeration scoped by the receipt, the recovery layer, the control before the batch, `needed` recomputed per run, single plain
deletes read back absent, tombstone before the receipt, land dark), the code does what the list says, and reading found no defect. The limits that stay, none fixed here (mine; override if you disagree):
- A pass that dies after deleting some objects and before writing any tombstone leaves their version ids unrecorded: the finishing pass lists those keys under `gone` without ids. The
  objects stay recoverable from the bucket's own version listing for the noncurrent window; only the map in the tombstone is short. Closing it means writing a tombstone before the
  deletes, which changes the order U28 fixed.
- An object that vanishes between a run's listing and its HEAD is in neither `removed` nor `gone` (the tombstone's `objects` still names it). Audit completeness only.
- The caller's clock is trusted. Nothing compares `now` with the store's own time, and the planner takes `now` as an argument so tests can age runs. A wrong clock could prune a run
  younger than 14 d; the noncurrent window still holds what a plain delete removed. The caller that is written later should pass the store's time or check the skew.

**Still owed (executor side).** The arming-time reading of the bucket's lifecycle configuration by the storage host, with its `updatedAt`, recorded in the arming commit (U28's
recommendation (a); the control proves the undo exists now, not how long it lasts); the control run again at arming, because U31's run is a specimen and not the arming reading; a
caller, which observes first (the enumeration against a live archive, once the collector has produced a real run) and has its own cadence and lease, one pass a day keeping pace; the first
real receipt read by the planner; and U28's check, two readings of the archive prefix 7 d apart. This section sits here and not after U28's because the writer-flag unit's unmerged edit
inserts there.

## Staged plan

1. Names, key function and the segment merge handler, registered and dark (outcome
   3). Acceptance: the H1 assertion, tombstone no-resurrection, no head-field
   invention, strict name regex that excludes `aspirations-archive.jsonl` and
   `aspirations-meta.json`, handler non-None for every derived name (guard-6907).
   (U3b, 2026-10-03: the handler clause is answered under design (b) in "Outcome 3 under
   design (b)" above; the other clauses of this item were not re-read.)
2. Pure split and join with the D6 round trip as a test over a corpus fixture.
3. Integration layer (the fork below). 4. Writer behind a default-OFF flag, a
   `store-cutover-check.sh` entry, migration and rollback; reader-capable code on
   every box and downstream Mind first. 5. Flip, then a fresh 24 h listing for
   outcome 4. 6. Retained versions (outcome 6) after reading the bucket's own
   lifecycle config (U25: dispositioned).

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

Lock contention: a naive grep of the
contention message is contaminated, 15 hits in one corpus dump that merely quotes
it, so count events in lane logs, not strings. The head PUT rate is every write (U2d; U9). The router's de-duplication and episode end with the real runner (U26). The archive prune executor against a live archive, and the lifecycle config on the day it runs (U28, U31): U31 ran the executor's control and its enumeration on the live store, not a pass, and no archive run exists to prune.
