"""Composite layout for a whole-file-rewritten JSONL store: one head plus keyed segments ().

WHY THIS EXISTS. The world goal queue is one JSONL file that every goal mutation rewrites
whole, so the object store receives the full file (tens of MB) per change. A composite
layout stores a small HEAD and, per (aspiration, goal-id range), one SEGMENT of goals; a
mutation then PUTs the head plus the one segment it touched. Measured on the live file
(2026-10-02): 223,975 B head, 89 segments at span 250, 505,289 B per mutation at the median
segment (64.5x smaller than the whole file) and 1,818,070 B at the largest (17.9x).

THE SEAM. The LOCAL file stays one legacy file. Only the remote layout changes, so every
local reader is untouched, and the merge handler still receives WHOLE joined bytes (it is
never run over a segment, so a goal that moves across a range edge cannot be misfiled).

THE FORMAT. The head is one JSON line: the format tag, the span, the md5 and size of the
JOINED legacy bytes, the aspiration records with `goals` kept in place and EMPTY, and a
manifest of segment key -> md5/bytes/goal count. A segment is the JSONL of its goals. The
join orders each aspiration's goals by plain STRING order of the goal id (the order the
merge handler emits; NUMERIC order differs, and counting inversions in it is how an earlier
draft reported 81 phantom ones) and then appends the goals the head lists under `tails`, in
the order it records. A live list is a sorted prefix plus a tail whenever a goal was added
since the last merge, because `aspirations.py` appends and only the merge sorts: 97 of 200
versions of the live key sampled across 2026-09-25 to 2026-10-03 carried a tail of 1 to 15
goals (U9), and a file whose lists are all sorted was one instant's snapshot. `split` writes
the ids after the longest ascending prefix as the head's `tails` (the key is absent when
there are none, so the head of a sorted file is unchanged), so any order is reproduced byte
for byte. A segment is the sorted bucket whatever the order, so a tail costs head bytes and
no segment.

IMMUTABLE SEGMENTS. A segment is stored under a content-addressed object name
(`segment_object_name`: key plus md5) and is never overwritten, so the head PUT (a
compare-and-swap on the head's ETag) is the ONLY commit point: a writer that dies after
some segment PUTs leaves orphans, never a head whose manifest disagrees with a stored
segment, and a reader always sees the exact objects its head names. In-place segments
under a head-only fence cannot give that: a crash between the two PUTs leaves the old
head over new segment bytes. The price is orphan objects. A content-addressed segment never
goes noncurrent, so no lifecycle rule expires it; the writer's grace-window garbage collection
deletes the names the current head no longer lists (g-358-202 outcome 6, condition C1).

THE STORED HEAD (g-358-202 U2d, outcome 6 condition C2). The head is stored PLAIN, never gzipped, and
`pad_head` blank-pads it to HEAD_MIN_BYTES: the object store inlines a small object into its per-key
metadata file beside every retained version, so a head that fell under the inline threshold would be
rewritten with all of them on each PUT (rb-12204). A store under MIN_RAW_BYTES is not worth the layout
and goes whole, as does one that is NotSplittable.

ORPHAN GC IS PLANNED HERE AND PERFORMED ELSEWHERE (g-358-202 U2e). `plan_gc` is pure: given the segment
directory's listing, the head just read and a first-seen ledger, it names the segment objects the head no
longer lists that have stayed unlisted for a whole grace window, and refuses (deletes nothing) whenever the
listing and the head cannot be trusted to agree. It deletes nothing itself. The read-only pass that feeds it is
`OwnCloudBackend.composite_gc_enumerate`; the pass that deletes from its plan is `composite_gc_apply`, behind its
own default-OFF flag (`should_gc`, OWNCLOUD_COMPOSITE_GC) and a grace floor (`gc_grace_ok`). It archives each object
to an S3 prefix outside the governed roots (`gc_archive_key`) and reads the copy back BEFORE deleting it, writes a
receipt there before the first delete, re-reads the head before each batch of single deletes, and afterwards puts back
from the archive any deleted object the head now names (`composite_gc_restore`, which also serves a later sweep):
the writer treats a 412 on a segment PUT as 'already there', so a head can re-reference an object this pass is
about to delete.

THE ARCHIVE'S PRUNING IS PLANNED HERE TOO (g-358-202 U28). The archive holds the only copy of what a versioned delete
removed (U8) and grows by what the collector removes, so a rule owns its end. `plan_archive_prune` is pure: it names the
archive runs whose retention has passed (GC_PRUNE_AFTER_S, never shorter), whose receipt reads `done` for this store, and
none of whose objects the head still names while the store lacks them. Only a name that is a run id is ever a candidate;
`_state` and `_pruned` (a pruned run's tombstone) are skipped and any other name is reported and left alone. It
removes nothing.

THE INVARIANT. `split` proves `join(split(x)) == x` before returning, and refuses
(`NotSplittable`) anything it cannot reproduce byte-for-byte; the caller then PUTs the
whole object instead. `join` verifies every segment against the manifest and the result
against the head's md5 (`IntegrityError`), so a missing or stale segment fails loudly.

READ SIDE ALWAYS ON, WRITE SIDE FLAG-GATED (g-358-202 U2c; the gzip codec's order, readers first).
`read_whole` joins any composite head of an allowlisted store (`reads_composite`: the static
allowlist, NOT the env flag): a box whose flag is unset must still read a head that a flagged box
wrote, and an old-code reader would read a head as the whole file. `should_composite` is the WRITER
gate (default OFF, scoped to the environment-ids named in OWNCLOUD_COMPOSITE_STORES, exactly as the
gzip codec's flag is); the backend's PUT seam (OwnCloudBackend._store_put, U2d) consults it. No
environment may be named until its readers are attested.

NO I/O HERE. `read_whole` takes its two I/O steps (fetch a segment object, re-read the head) as
callables, so the backend and the raw-S3 readers share one join, one bounded retry and one fence
rule instead of keeping copies. `decode_whole` is the raw-S3 readers' one entry to it (g-358-202 U3):
the codec's decode (it drains the response body and does nothing else) and, for a head only, the
backend's own join.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import math
import re
import time
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple

import _owncloud_codec as _codec

FORMAT = "composite-v1"
SEGMENT_SPAN = 250
SEGMENT_DIR = ".composite"  # beside the head object; in owncloud_sync._EXCLUDE_DIRS, so never mirrored
READ_ATTEMPTS = 3
HEAD_MIN_BYTES = 256 * 1024  # the stored head is at least this. U9 measured the versioned MinIO inlining up to 16,384 B (17,408 B is not), so this is 16x the threshold and 2x the unversioned 128 KiB default; it costs 27 KB per write while the head is 235 KB, and nothing once the head is larger
MIN_RAW_BYTES = 4 * 1024 * 1024  # a store under this goes whole: its PUT is already cheap, and the head floor alone would cost more
FLAG_ENV = "OWNCLOUD_COMPOSITE_STORES"
ALLOWLIST = ("world/aspirations.jsonl",)  # the measured churn leader (): one explicit file
GC_GRACE_S = 14 * 24 * 3600  # my choice, not a measurement: at least the head key's noncurrent window (7 d on MinIO, 14 d on AWS, guard-6837), so an orphan outlives every retained head version that could name it
GC_MAX_DELETE = 500  # my value, not a measurement: objects one pass may delete, so a pass stays bounded and its enumeration reviewable
GC_FLAG_ENV = "OWNCLOUD_COMPOSITE_GC"  # the environment ids whose orphans a DELETE pass may collect: its own flag, never implied by FLAG_ENV (a writer flag does not license deletion)
GC_DELETE_BATCH = 50  # my value, not a measurement: objects deleted between two re-reads of the head
GC_ARCHIVE_DIR = "_composite-gc-archive"  # under the env prefix beside the governed roots (world, meta, agents), not inside one: the sync layer pulls only those roots, so it never mirrors an archive
FRESHEN_MARGIN_S = 24 * 3600  # my choice, not a measurement: a writer re-PUTs a segment it finds present once it is within this much of collectable, so the margin only has to exceed the time between a write's first segment PUT and its head commit; a segment is re-PUT at most once per GC_GRACE_S - FRESHEN_MARGIN_S however many writes find it
GC_MTIME_SLOP_S = 2.0  # a listing reports an object's last_modified to the millisecond and a HEAD to the second, so one unchanged object can read up to a second apart between them
_HEAD_PREFIX = ('{"composite": "%s"' % FORMAT).encode("ascii")
_SAFE_ASP = re.compile(r"^[A-Za-z0-9_.-]+$")
_NUM_TAIL = re.compile(r"-(\d+)$")
_OBJECT_NAME = re.compile(r"^[A-Za-z0-9_.-]+/(?:[0-9]+|x)\.[0-9a-f]{32}\.jsonl$")  # what `segment_object_name` writes for a key `split` makes


class CompositeError(Exception):
    pass


class NotSplittable(CompositeError):
    """The input cannot take the composite layout; the caller PUTs the whole object."""


class IntegrityError(CompositeError):
    """The head and its segments disagree, or the bytes are not a composite head."""


class SegmentMissing(CompositeError):
    """No object exists under a segment name the head lists."""


class Split(NamedTuple):
    head: bytes
    segments: Dict[str, bytes]
    manifest: Dict[str, dict]


def dumps(obj) -> str:
    # The writer's canonical form: the backend's JSONL text helper is json.dumps(it, ensure_ascii=True).
    return json.dumps(obj, ensure_ascii=True)


def is_head(data: bytes) -> bool:
    return data.startswith(_HEAD_PREFIX)


def goal_token(goal_id: str, span: int = SEGMENT_SPAN) -> str:
    m = _NUM_TAIL.search(goal_id)
    return str(int(m.group(1)) // span) if m else "x"


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _order_tail(ids: List[str]) -> List[str]:
    """The ids after the longest strictly ascending prefix of `ids`, in file order: what sorting cannot recover.
    Empty for a list in plain string order, which is every list a merge has just written."""
    for k in range(1, len(ids)):
        if ids[k - 1] > ids[k]:
            return ids[k:]
    return []


def _in_head_order(asp: Any, goals: List[dict], tail: Any) -> List[dict]:
    """`goals` in the file's order: sorted by id, then the goals the head lists as a tail, in the order it records."""
    if not tail:
        return sorted(goals, key=lambda g: g["id"])
    if not isinstance(tail, list) or any(not isinstance(i, str) for i in tail) or len(set(tail)) != len(tail):
        raise IntegrityError("%s: the head's tail is not a list of distinct ids" % asp)
    by_id = {g["id"]: g for g in goals}
    for gid in tail:
        if gid not in by_id:
            raise IntegrityError("%s: the head's tail names %s, which no segment holds" % (asp, gid))
    tailed = set(tail)
    return sorted((g for g in goals if g["id"] not in tailed), key=lambda g: g["id"]) + [by_id[i] for i in tail]


def split(raw: bytes, span: int = SEGMENT_SPAN) -> Split:
    if is_head(raw):
        raise NotSplittable("input is already a composite head")
    if raw and not raw.endswith(b"\n"):
        raise NotSplittable("no trailing newline")
    try:
        lines = raw.decode("utf-8").split("\n")[:-1]
    except UnicodeDecodeError as exc:
        raise NotSplittable("not UTF-8: %s" % exc)
    shells: List[dict] = []
    buckets: Dict[str, Dict[str, dict]] = {}
    tails: Dict[str, List[str]] = {}
    seen = set()
    for n, line in enumerate(lines, 1):
        try:
            obj = json.loads(line)
        except ValueError:
            raise NotSplittable("line %d is not JSON" % n)
        if not isinstance(obj, dict):
            raise NotSplittable("line %d is not an object" % n)
        if "goals" in obj:
            asp = obj.get("id")
            if not isinstance(asp, str) or not _SAFE_ASP.match(asp):
                raise NotSplittable("line %d: aspiration id unusable as a segment key" % n)
            if asp in seen:
                raise NotSplittable("duplicate aspiration id %s" % asp)
            seen.add(asp)
            if not isinstance(obj["goals"], list):
                raise NotSplittable("%s: goals is not a list" % asp)
            goals, obj["goals"] = obj["goals"], []
            ids: List[str] = []
            for g in goals:
                gid = g.get("id") if isinstance(g, dict) else None
                if not isinstance(gid, str):
                    raise NotSplittable("%s: a goal has no string id" % asp)
                bucket = buckets.setdefault("%s/%s" % (asp, goal_token(gid, span)), {})
                if gid in bucket:
                    raise NotSplittable("%s: duplicate goal id %s" % (asp, gid))
                bucket[gid] = g
                ids.append(gid)
            tail = _order_tail(ids)
            if tail:
                tails[asp] = tail
        shells.append(obj)
    segments: Dict[str, bytes] = {}
    manifest: Dict[str, dict] = {}
    for key in sorted(buckets):
        body = "".join(dumps(g) + "\n" for _gid, g in sorted(buckets[key].items())).encode("ascii")
        segments[key] = body
        manifest[key] = {"md5": _md5(body), "bytes": len(body), "goals": len(buckets[key])}
    doc = {"composite": FORMAT, "span": span, "joined_md5": _md5(raw), "joined_bytes": len(raw),
           "aspirations": shells, "segments": manifest}
    if tails:
        doc["tails"] = tails
    head = (dumps(doc) + "\n").encode("ascii")
    try:
        rebuilt = join(head, segments)
    except IntegrityError as exc:
        raise NotSplittable("round trip failed: %s" % exc)
    if rebuilt != raw:
        raise NotSplittable("round trip differs from the input")
    return Split(head, segments, manifest)


def _parse_head(head: bytes) -> dict:
    if not is_head(head):
        raise IntegrityError("not a composite head")
    try:
        doc = json.loads(head.decode("utf-8"))
    except ValueError as exc:
        raise IntegrityError("head is not JSON: %s" % exc)
    for field in ("aspirations", "segments", "joined_md5"):
        if field not in doc:
            raise IntegrityError("head has no %s" % field)
    return doc


def join(head: bytes, segments: Mapping[str, bytes]) -> bytes:
    doc = _parse_head(head)
    manifest = doc["segments"]
    tails = doc.get("tails", {})
    if not isinstance(tails, dict):
        raise IntegrityError("the head's tails is not an object")
    held = {shell.get("id") for shell in doc["aspirations"] if "goals" in shell}
    for asp in tails:
        if asp not in held:
            raise IntegrityError("the head lists a tail for %s, an aspiration it does not hold" % asp)
    keys_of: Dict[str, List[str]] = {}
    for key in manifest:
        keys_of.setdefault(key.split("/", 1)[0], []).append(key)
    out = []
    for shell in doc["aspirations"]:
        obj = dict(shell)
        if "goals" in obj:
            goals = []
            for key in keys_of.get(obj.get("id"), ()):
                body = segments.get(key)
                if body is None:
                    raise IntegrityError("segment %s is missing" % key)
                if _md5(body) != manifest[key]["md5"]:
                    raise IntegrityError("segment %s does not match the head's manifest" % key)
                goals.extend(json.loads(ln) for ln in body.decode("utf-8").split("\n")[:-1])
            obj["goals"] = _in_head_order(obj.get("id"), goals, tails.get(obj.get("id")))
        out.append(dumps(obj) + "\n")
    raw = "".join(out).encode("ascii")
    if _md5(raw) != doc["joined_md5"]:
        raise IntegrityError("joined bytes do not match the head's md5")
    return raw


class Put(NamedTuple):
    head: bytes
    segments: Dict[str, bytes]  # object name -> body, only objects the old head does not already name


class Refresh(NamedTuple):
    fetch: Dict[str, str]    # logical key -> object name to GET
    reuse: Dict[str, bytes]  # logical key -> bytes the local file already carries


def segment_object_name(key: str, md5: str) -> str:
    return "%s.%s.jsonl" % (key, md5)


def head_object_names(head: bytes) -> frozenset:
    """The segment object names a composite `head` lists: the objects the layout needs. Raises IntegrityError for
    a body that is not a head; a manifest of the wrong shape raises KeyError, TypeError or AttributeError, which
    a caller reading an untrusted head catches with it."""
    return frozenset(segment_object_name(k, m["md5"]) for k, m in _parse_head(head)["segments"].items())


def plan_write(old_head: Optional[bytes], raw: bytes, span: int = SEGMENT_SPAN) -> Put:
    """What one write PUTs: the new head plus only the segment objects the old head does not
    already name. Raises NotSplittable, and the caller then PUTs the whole object. An old head the
    layout cannot read names nothing a write may skip: every segment is PUT, create-only, and the new
    head replaces it. Defense in depth: the one caller holds only heads it wrote or the reader validated
    (`plan_refresh` rejects a manifest `head_object_names` rejects), so no path reaches this today."""
    new = split(raw, span)
    try:
        held = head_object_names(old_head) if old_head and is_head(old_head) else frozenset()
    except (IntegrityError, KeyError, TypeError, AttributeError):
        held = frozenset()
    puts = {segment_object_name(k, m["md5"]): new.segments[k] for k, m in new.manifest.items()}
    return Put(new.head, {n: b for n, b in puts.items() if n not in held})


def pad_head(head: bytes, floor: Optional[int] = None) -> bytes:
    """The head as it is stored: blank-padded after its JSON to `floor` bytes (HEAD_MIN_BYTES by
    default), never shortened. The blanks are inert: `_parse_head` reads the head with json.loads."""
    floor = HEAD_MIN_BYTES if floor is None else floor
    if len(head) >= floor:
        return head
    return head.rstrip(b"\n") + b" " * (floor - len(head)) + b"\n"


def plan_refresh(local_raw: Optional[bytes], remote_head: bytes) -> Refresh:
    """What a reader must GET to turn its local file into the file `remote_head` describes:
    every segment whose md5 the local file does not already carry."""
    doc = _parse_head(remote_head)
    have: Dict[str, bytes] = {}
    if local_raw:
        try:
            have = split(local_raw, doc.get("span", SEGMENT_SPAN)).segments
        except NotSplittable:
            have = {}
    fetch: Dict[str, str] = {}
    reuse: Dict[str, bytes] = {}
    for key, meta in doc["segments"].items():
        body = have.get(key)
        if body is not None and _md5(body) == meta["md5"]:
            reuse[key] = body
        else:
            fetch[key] = segment_object_name(key, meta["md5"])
    return Refresh(fetch, reuse)


def segment_s3_key(head_key: str, object_name: str) -> str:
    """The S3 key of a segment object, for the store whose head object is at `head_key`: a dot
    directory beside the head, `<dir>/.composite/<head basename>/<object name>`. The directory is
    in the sync layer's _EXCLUDE_DIRS, so the pull sweep never mirrors a segment as a file."""
    head_dir, _, base = head_key.rpartition("/")
    return "/".join(([head_dir] if head_dir else []) + [SEGMENT_DIR, base, object_name])


def reads_composite(rel: str, allowlist=ALLOWLIST) -> bool:
    """Reader gate: `rel` (the backend's env-scoped logical path) is on the composite allowlist.
    Deliberately NOT env-flagged (module docstring): a flag read here would leave a box whose flag
    is unset reading a head that a flagged box wrote as if it were the file."""
    return _codec.rel_allowlisted(rel, allowlist)


def read_whole(head: bytes, etag: Any, fetch_segment: Callable[[str], bytes],
               reread_head: Callable[[], Tuple[bytes, Any]], *, local_raw: Optional[bytes] = None,
               attempts: int = READ_ATTEMPTS) -> Tuple[bytes, Any]:
    """Turn a composite `head` the caller already read (with its `etag`) into the whole legacy file.

    Returns (raw, etag); a body that is not a head is returned unchanged. `fetch_segment(name)`
    returns the DECODED bytes of the segment object `name` and raises SegmentMissing when it is
    absent; `reread_head()` returns a fresh (decoded body of the head key, etag). A segment the local
    file already carries (md5 equal to the head's manifest) is reused, never fetched.

    A missing or mismatching segment means the head moved on under the reader (a writer committed
    and its garbage collection removed the superseded names) or an object is corrupt. So the head is
    re-read before each retry, up to `attempts` joins, and IntegrityError follows: never a partial
    file. If a re-read finds the whole-object layout again (the writer was switched off), that whole
    file is returned as it is.

    THE RETURNED ETAG IS THE FENCE TOKEN: it belongs to the head the bytes were joined from, so after
    a re-read it is the NEWER head's. Bytes older than their token would let a conditional write over
    a newer head succeed (a lost update); bytes newer than their token only make that write fail and
    retry."""
    if not is_head(head):
        return head, etag
    failure: Exception = IntegrityError("no join attempted")
    for attempt in range(attempts):
        if attempt:
            head, etag = reread_head()
            if not is_head(head):
                return head, etag
        try:
            plan = plan_refresh(local_raw, head)
            fetched = {key: fetch_segment(name) for key, name in plan.fetch.items()}
            return join(head, {**plan.reuse, **fetched}), etag
        except (SegmentMissing, IntegrityError) as exc:
            failure = exc
    raise IntegrityError("composite head unreadable after %d joins: %s" % (attempts, failure))


class GcPlan(NamedTuple):
    delete: List[str]         # object names (relative to the store's segment directory), oldest orphan first
    ledger: Dict[str, float]  # name -> epoch first seen unreferenced, for every name still unreferenced
    refused: List[str]        # why this pass must delete nothing; empty when it may proceed
    unknown: List[str]        # listed names that are not a segment object name: never deleted, reported
    counts: Dict[str, int]


class GcEnumeration(NamedTuple):
    plan: GcPlan
    head_etag: Any          # ETag of the head `plan` was computed against; None when no head was read
    items: Dict[str, dict]  # for each name in plan.delete: size, last_modified (epoch), etag, first_seen


class GcApplied(NamedTuple):
    stopped: Optional[str]    # why the pass did not run to the end (a refusal, or where it was abandoned); None when it did
    plan: Optional[GcPlan]    # the plan the pass acted on; None when it stopped before planning
    ledger: Dict[str, float]  # the first-seen ledger for the caller to persist for the next pass
    run_id: Optional[str]     # names the archive run and its receipt; None when nothing was archived
    deleted: List[str]        # names deleted AND read back absent
    restored: List[str]       # deleted names the post-check found the head naming again, put back from the archive
    skipped: Dict[str, str]   # name -> why an object the plan named was kept


class VersionDelete(NamedTuple):
    order: List[str]        # version ids a delete pass removes for one orphan, in this order; empty when `keep` is set
    keep: Optional[str]     # why the orphan is kept instead ('rewritten-since-listing', 'version-bytes-not-archived'); None when it may go


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def plan_gc(listing, head: bytes, ledger: Mapping[str, float], now: float,
            grace_s: float = GC_GRACE_S, max_delete: int = GC_MAX_DELETE) -> GcPlan:
    """Which segment objects one garbage-collection pass may delete. PURE: no I/O, no clock.

    `listing` is (name, last_modified epoch, size) for every object under the store's segment directory,
    names relative to it; `head` is the head object as read for this pass; `ledger` maps a name to the epoch
    an earlier pass first saw it unreferenced; `now` is the pass's epoch.

    An object is deleted only when ALL hold: it is not named by `head`; its name is exactly what
    `segment_object_name` writes (anything else is never touched, only reported); and it has been unreferenced
    for `grace_s`. The clock is max(first sighting in the ledger, the object's own last_modified), so an old
    object first seen today waits a full window and a young one never rides an old ledger entry. A name that
    is referenced again, or gone from the listing, leaves the ledger, so a re-referenced name starts over.
    The oldest orphans go first, at most `max_delete`.

    A pass that cannot trust its inputs deletes NOTHING and leaves the ledger as it found it. It refuses when
    the current object is not a composite head (an orphan is only defined against a head), when the head cannot
    be read, when the head names a segment the listing does not show (a truncated listing, or a head newer than
    the listing: the orphan set is then unreliable), when the head names no segments yet objects exist, and
    when `grace_s` is negative or not a number. A last_modified that is not a finite number counts as 'now' on
    every pass, so that object is never old enough to delete, and it is counted in `undated` so a pass whose
    orphans never age is visible rather than silent.

    WHAT THE GRACE DOES AND DOES NOT COVER. It protects a writer that has PUT its segments and not yet
    committed its head, and it keeps an orphan alive past every retained head version that could name it
    (point-in-time recovery). It does not close two windows, both for the delete pass to handle: a head that
    commits after the pass's last re-read of it and re-references an orphan the writer found present (the writer
    re-PUTs such an object once `needs_freshen`, and the delete pass skips one `moved_since_listing`, so what is
    left is the gap between that last check and the delete call, which `composite_gc_restore` repairs), and a
    name that flips referenced and unreferenced between two passes, which then keeps its old clock."""
    seen: Dict[str, Tuple[float, int]] = {}
    undated = set()
    for name, modified, size in listing:
        if not _finite(modified):
            undated.add(name)
            modified = now
        if name not in seen or modified > seen[name][0]:
            seen[name] = (modified, size)
    refused: List[str] = []
    referenced = set()
    if not _finite(grace_s) or grace_s < 0:
        refused.append("grace-invalid")
    if not is_head(head):
        refused.append("not-a-head")
    else:
        try:
            referenced = head_object_names(head)
        except (IntegrityError, KeyError, TypeError, AttributeError) as exc:
            refused.append("head-unreadable: %s" % exc)
    if not refused:
        unlisted = referenced - seen.keys()
        if unlisted:
            refused.append("head-names-unlisted-segments: %d" % len(unlisted))
        if seen and not referenced:
            refused.append("head-names-no-segments")
    if refused:
        return GcPlan([], dict(ledger), refused, [], {"listed": len(seen)})
    unknown: List[str] = []
    orphans: Dict[str, Tuple[float, int]] = {}  # name -> (the clock's start, size)
    kept: Dict[str, float] = {}
    for name in sorted(seen):
        if name in referenced:
            continue
        if not _OBJECT_NAME.match(name):
            unknown.append(name)
            continue
        modified, size = seen[name]
        first = ledger.get(name)
        if not _finite(first):
            first = now
        kept[name] = first
        orphans[name] = (max(first, modified), size)
    aged = sorted((n for n, (since, _size) in orphans.items() if now - since >= grace_s),
                  key=lambda n: (orphans[n][0], n))
    delete = aged[:max(0, max_delete)]
    counts = {"listed": len(seen), "referenced": len(referenced), "unknown": len(unknown),
              "orphans": len(orphans), "orphan_bytes": sum(s for _t, s in orphans.values()),
              "undated": len(undated & orphans.keys()), "aged": len(aged), "deletable": len(delete),
              "deletable_bytes": sum(orphans[n][1] for n in delete)}
    return GcPlan(delete, kept, [], unknown, counts)


def needs_freshen(last_modified, now: float, grace_s: float = GC_GRACE_S, margin_s: float = FRESHEN_MARGIN_S) -> bool:
    """Should a writer that found the segment object last modified at `last_modified` already present PUT it again?
    True once it is within `margin_s` of the age the delete pass collects at (`grace_s`). PURE. An undated object is
    never collected (`plan_gc` counts it as 'now'), so it is never freshened."""
    return _finite(last_modified) and now - last_modified >= grace_s - margin_s


def moved_since_listing(listed, current) -> bool:
    """Has an object been re-written since a listing reported its last_modified as `listed`, judged from the
    `current` one a HEAD returned just before the delete? True when `current` is later by more than GC_MTIME_SLOP_S,
    and also when either value is not a finite number: the re-check fails closed, as the planner never deletes an
    undated object. PURE."""
    return not _finite(listed) or not _finite(current) or current > listed + GC_MTIME_SLOP_S


def plan_version_delete(versions, listed_last_modified, head_version_id, archived_etag) -> VersionDelete:
    """What a delete pass may remove from a VERSIONED store for one orphan, judged from the chain of versions and delete
    markers it listed (g-358-202 U8). PURE: no I/O, no clock.

    A key delete on a versioned store only hides the object behind a delete marker, and it names the KEY, so it also
    hides a version a writer put after the listing (the freshen of an old segment). Deleting the versions the listing
    named, by id, closes that gap exactly: a version the listing never saw is not on the list, so it survives.

    `versions` is the chain as listed, newest first: one dict per version or delete marker with `version_id`,
    `is_latest`, `marker`, `etag` (None on a marker) and `last_modified` (epoch). `listed_last_modified` is the object's
    last_modified in the listing the plan was made from, `head_version_id` the VersionId a HEAD returned just before the
    delete, `archived_etag` the ETag of the bytes the pass archived.

    The orphan is KEPT as 'rewritten-since-listing' when the chain has no single latest version to check against (or a
    version without an id), when that version is later than the plan's listing by more than the slop (a freshen between
    the two listings), and when the HEAD's VersionId is not the listed latest (a freshen since). It is kept as
    'version-bytes-not-archived' when any listed version is not byte-identical to the archived one (its ETag differs):
    the archive holds the current version only, so deleting another would destroy bytes that have no copy. An
    unreadable value keeps the orphan, as `moved_since_listing` does.

    Otherwise `order` is EVERY listed entry's version id, the noncurrent ones first (oldest first) and the latest last:
    deleting only the latest brings the older version back as current (measured live, U7), and ending on the latest means
    a failure part way leaves the object itself present."""
    if any(not v.get("version_id") for v in versions):
        return VersionDelete([], "rewritten-since-listing")
    latest = [v for v in versions if v.get("is_latest")]
    if len(latest) != 1 or latest[0].get("marker"):
        return VersionDelete([], "rewritten-since-listing")
    top = latest[0]
    if head_version_id != top["version_id"] or moved_since_listing(listed_last_modified, top.get("last_modified")):
        return VersionDelete([], "rewritten-since-listing")
    if any(not v.get("marker") and v.get("etag") != archived_etag for v in versions):
        return VersionDelete([], "version-bytes-not-archived")
    return VersionDelete([v["version_id"] for v in reversed(versions) if v is not top] + [top["version_id"]], None)


def decode_whole(be, key: str, obj) -> bytes:
    """The WHOLE legacy bytes of one `get_object` response, for a reader that bypasses the backend's
    mirror (g-358-202 U3): the codec's decode and, only when the decoded body is a composite head,
    the backend's join (`be.join_composite`, which applies the reader gate and the bounded retry).
    Any other body comes back as the codec decoded it and `be` is never touched, so a reader of a
    plain store behaves exactly as it did before the layout.

    WHY A RAW READER NEEDS IT. Under the layout the object at the store's key is a small head, and
    parsed as the whole file it carries no goal and no claim. Callers keep their own broad `except`
    around this call: a join that cannot complete (IntegrityError, a segment that stays missing) is
    the 'authoritative read unavailable' they already handle, and never a partial file."""
    body = _codec.decode_response(obj, key=key)
    if not is_head(body):
        return body
    return be.join_composite(key, body, obj.get("ETag"))


def should_composite(rel: str, env_id: Optional[str], env: Optional[dict] = None,
                     allowlist=ALLOWLIST) -> bool:
    """Writer gate: the deployment's `env_id` is named by OWNCLOUD_COMPOSITE_STORES AND `rel`
    (the backend's env-scoped logical path) is on the allowlist. Never true by default, and
    independent of the gzip flag: readers attested for one layout are not attested for the other."""
    return _codec.env_enabled(env_id, env, FLAG_ENV) and _codec.rel_allowlisted(rel, allowlist)


def should_gc(rel: str, env_id: Optional[str], env: Optional[dict] = None, allowlist=ALLOWLIST) -> bool:
    """Delete-pass gate: the deployment's `env_id` is named by OWNCLOUD_COMPOSITE_GC AND `rel` is on the
    allowlist. Never true by default, and independent of the writer flag in BOTH directions: naming an
    environment for the writer does not license deleting, and the box that sweeps needs only this one. It is
    named LAST, not first (U15; this sentence said the opposite until then): an observe pass runs on either
    flag, nothing is collectable until an orphan is GC_GRACE_S old, and the tick passes no --apply, so this
    flag is the second of the two switches that arm deletion. Order and reasons: core/config/rationale/
    aspirations-store-segmentation.md, "The tick and the flip checklist (U15)"."""
    return _codec.env_enabled(env_id, env, GC_FLAG_ENV) and _codec.rel_allowlisted(rel, allowlist)


def gc_grace_ok(grace_s) -> bool:
    """A delete pass may use a grace window only when it is at least GC_GRACE_S. The planner takes any
    non-negative window so a dry pass can preview a shorter one; DELETING under a shorter one could remove a
    segment that a retained head version still names."""
    return _finite(grace_s) and grace_s >= GC_GRACE_S


def gc_run_id(now: float, names) -> str:
    """Names one delete pass: its UTC start second and a digest of the exact set it will delete, so an archive,
    its receipt and the delete list can be matched to one run and to nothing else (guard-5276)."""
    return "%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)),
                      _md5("\n".join(sorted(names)).encode("utf-8"))[:8])


def gc_archive_key(env_root: str, run_id: str, rel: str, name: str) -> str:
    """The S3 key an orphan is archived to: `<env root>_composite-gc-archive/<run id>/objects/<store>/<name>`.
    `env_root` is the customer and environment prefix with its trailing slash."""
    return "%s%s/%s/objects/%s/%s" % (env_root, GC_ARCHIVE_DIR, run_id, rel, name)


def gc_receipt_key(env_root: str, run_id: str) -> str:
    """The archive run's receipt: a top-level RECEIPT.json of the run, never inside the store it describes."""
    return "%s%s/%s/RECEIPT.json" % (env_root, GC_ARCHIVE_DIR, run_id)


GC_STATE_DIR = "_state"  # under GC_ARCHIVE_DIR, beside the run directories: the scheduled pass's two documents per store ( U14). A run id starts with a digit, so the names never collide; anything that lists the archive to prune it must skip this one
GC_STATE_DOCS = ("state", "ledger")  # 'state': the pass's cadence stamp and the recent runs awaiting their late restore; 'ledger': the first-seen ledger `plan_gc` takes and returns


def gc_state_key(env_root: str, rel: str, doc: str) -> str:
    """The S3 key of one scheduled-pass document of the store `rel`: `<env root>_composite-gc-archive/_state/<store>/<doc>.json`.
    `doc` is one of GC_STATE_DOCS; anything else is a ValueError, never a key."""
    if doc not in GC_STATE_DOCS:
        raise ValueError("unknown composite GC state document %r (expected one of %s)" % (doc, ", ".join(GC_STATE_DOCS)))
    return "%s%s/%s/%s/%s.json" % (env_root, GC_ARCHIVE_DIR, GC_STATE_DIR, rel, doc)


_RUN_ID = re.compile(r"^(\d{8}T\d{6}Z)-[0-9a-f]{8}$")  # what `gc_run_id` writes


def gc_run_time(run_id: str) -> Optional[float]:
    """The epoch a run id names (the UTC second `gc_run_id` stamped), or None for anything that is not a run id: the `_state`
    directory, a stray name. The scheduled pass lists the archive and keeps the runs this puts inside its trust window."""
    m = _RUN_ID.match(run_id)
    return float(calendar.timegm(time.strptime(m.group(1), "%Y%m%dT%H%M%SZ"))) if m else None


GC_PRUNED_DIR = "_pruned"  # under GC_ARCHIVE_DIR, beside the run directories and GC_STATE_DIR: where a pruned run's receipt is kept as its tombstone ( U28). It starts with an underscore, so it never collides with a run id, and a lister of the archive skips it exactly as it skips GC_STATE_DIR
GC_ARCHIVE_RESERVED = (GC_STATE_DIR, GC_PRUNED_DIR)  # the top-level names under GC_ARCHIVE_DIR that are not run directories: never candidates, never reported


GC_PRUNE_AFTER_S = 14 * 24 * 3600  # my choice, not a measurement: how long an archive run stays after its delete pass, and the shortest window a pass may use. It equals GC_GRACE_S, so an orphan stays recoverable as long after its delete as it waited for it. A head that names a missing object fails every read of it loudly (`join`), so a defect shows at the next read, not a month later
GC_PRUNE_MAX_RUNS = 12  # my value, not a measurement: archive runs one prune pass may remove, so a pass stays bounded and its enumeration reviewable (a run holds at most GC_MAX_DELETE objects). A delete pass every 4 h adds at most 6 runs a day, so 12 is twice a day's growth: one prune pass a day keeps pace and one per tick drains a backlog


class PrunePlan(NamedTuple):
    prune: List[str]       # run ids a prune pass may remove, oldest first
    kept: Dict[str, str]   # run id -> why it stays: every run id the listing showed that `prune` lacks
    refused: List[str]     # why this pass must remove nothing; empty when it may proceed
    unknown: List[str]     # listed names that are neither a run id nor in GC_ARCHIVE_RESERVED: never touched, reported
    counts: Dict[str, int]
    items: Dict[str, dict]  # for each run id in `prune`: objects, bytes, age_s, as its receipt describes the run


def plan_archive_prune(listing, receipts: Mapping[str, Any], needed, now: float, store: str,
                       prune_after_s: float = GC_PRUNE_AFTER_S, max_runs: int = GC_PRUNE_MAX_RUNS) -> PrunePlan:
    """Which archive runs one prune pass may remove. PURE: no I/O, no clock.

    `listing` is every top-level name under the archive directory (the run directories, GC_STATE_DIR, GC_PRUNED_DIR);
    `receipts` maps a run id to its parsed RECEIPT.json, or to None where it is missing or unreadable (the caller
    reads the receipts of runs at least `prune_after_s` old; a younger run is kept without one); `needed` is the
    set of segment names the current head names and the store lacks, the only objects `composite_gc_restore` could
    still take from an archive, or None when the caller could not read it; `store` is the store whose runs this
    pass may remove.

    A run is removed only when ALL hold: its name is a run id (`gc_run_time`: the GC_ARCHIVE_RESERVED names are
    skipped and any other name is reported in `unknown` and left alone, so a pruner that shares the archive directory
    with the scheduled pass's documents and its own tombstones never touches them); it is at least `prune_after_s`
    old; its receipt is a format-1 archive
    receipt for this run and this store whose status is `done` and whose objects are all records; and none of its
    objects is in `needed`. Every other run stays, with the reason in `kept`: 'inside-retention', 'no-receipt' (a run
    directory without a receipt deleted nothing, but nothing here proves it, so a person looks),
    'receipt-not-recognized', 'other-store', 'status-not-done' (a run that stopped half way holds the
    only copy of what it did delete), 'receipt-malformed', 'head-needs-archived-object', 'over-cap'. The oldest runs
    go first, at most `max_runs`.

    A pass that cannot trust its inputs removes NOTHING. It refuses when `now` is not a finite number, when
    `prune_after_s` is not a finite number or is under GC_PRUNE_AFTER_S (the window is a floor, so a shorter one cannot
    even be previewed here), and when `needed` is None (without it nothing shows that the archive is not what a restore
    is waiting for).

    WHAT THIS DOES NOT COVER. The plan says a run MAY be removed, not that its objects are still there and still what
    the receipt says. That is the delete pass's own re-check against the listing, each object's size and md5, and the
    recovery layer a versioned bucket gives a delete: none of them is read here. The rule and its reasons:
    core/config/rationale/aspirations-store-segmentation.md, "The archive pruning rule (U28)"."""
    refused: List[str] = []
    if not _finite(now):
        refused.append("now-invalid")
    if not _finite(prune_after_s) or prune_after_s < GC_PRUNE_AFTER_S:
        refused.append("retention-below-floor")
    if needed is None:
        refused.append("needed-unknown")
    names = sorted(set(listing))
    runs = [n for n in names if gc_run_time(n) is not None]
    unknown = [n for n in names if n not in GC_ARCHIVE_RESERVED and gc_run_time(n) is None]
    counts = {"listed": len(names), "runs": len(runs), "reserved": sum(n in GC_ARCHIVE_RESERVED for n in names),
              "unknown": len(unknown)}
    if refused:
        return PrunePlan([], {}, refused, unknown, counts, {})
    wanted = set(needed)
    kept: Dict[str, str] = {}
    eligible: List[Tuple[float, str, int, int]] = []  # (run time, run id, objects, bytes)
    for run_id in runs:
        when = gc_run_time(run_id)
        receipt = receipts.get(run_id)
        if now - when < prune_after_s:
            kept[run_id] = "inside-retention"
        elif not isinstance(receipt, dict):
            kept[run_id] = "no-receipt"
        elif (receipt.get("kind"), receipt.get("format"), receipt.get("run_id")) != ("composite-gc-archive", 1, run_id):
            kept[run_id] = "receipt-not-recognized"
        elif receipt.get("store") != store:
            kept[run_id] = "other-store"
        elif receipt.get("status") != "done":
            kept[run_id] = "status-not-done"
        elif not isinstance(receipt.get("objects"), dict) or not all(isinstance(r, dict) for r in receipt["objects"].values()):
            kept[run_id] = "receipt-malformed"
        elif wanted.intersection(receipt["objects"]):
            kept[run_id] = "head-needs-archived-object"
        else:
            objects = receipt["objects"]
            eligible.append((when, run_id, len(objects), sum(r["size"] for r in objects.values() if _finite(r.get("size")))))
    eligible.sort()
    take = eligible[:max(0, max_runs)]
    for _when, run_id, _n, _b in eligible[len(take):]:
        kept[run_id] = "over-cap"
    counts.update(eligible=len(eligible), prunable=len(take), prunable_objects=sum(n for _w, _r, n, _b in take),
                  prunable_bytes=sum(b for _w, _r, _n, b in take))
    return PrunePlan([run_id for _w, run_id, _n, _b in take], kept, [], unknown, counts,
                     {run_id: {"objects": n, "bytes": b, "age_s": int(now - when)} for when, run_id, n, b in take})


GC_PRUNE_FLAG_ENV = "OWNCLOUD_COMPOSITE_GC_PRUNE"  # the environment ids whose archive a prune pass may remove runs from ( U30): its own flag, never implied by GC_FLAG_ENV (a flag that licenses deleting orphans does not license deleting their archive, as the writer flag does not license deleting)
GC_PRUNE_CONTROL_DIR = "_prune-control"  # under GC_STATE_DIR: where the control a prune pass runs before its first removal writes its throwaway sentinel. A store path starts with world/, meta/ or agents/, so the name never collides with a store's own documents
_CONTROL_TOKEN = re.compile(r"[0-9a-f]{32}")  # matched with fullmatch: `$` would also pass a token followed by a newline


def should_prune_archive(rel: str, env_id: Optional[str], env: Optional[dict] = None, allowlist=ALLOWLIST) -> bool:
    """Archive-prune gate: the deployment's `env_id` is named by OWNCLOUD_COMPOSITE_GC_PRUNE AND `rel` is on the allowlist.
    Never true by default and independent of the other two flags in BOTH directions: the box that prunes needs only this
    one, and naming an environment for the writer or for orphan collection licenses no removal of an archive run. Read-only
    enumeration and the control need no flag at all (`composite_gc_prune_enumerate`, `composite_gc_prune_control`): the
    control is what has to run BEFORE this one is set (guard-1301)."""
    return _codec.env_enabled(env_id, env, GC_PRUNE_FLAG_ENV) and _codec.rel_allowlisted(rel, allowlist)


def gc_run_prefix(env_root: str, run_id: str) -> str:
    """The S3 prefix every key of one archive run sits under: `<env root>_composite-gc-archive/<run id>/`. A name that is not
    a run id is a ValueError, never a prefix, so a listing or a delete built from it cannot leave the run's directory."""
    if not isinstance(run_id, str) or gc_run_time(run_id) is None:
        raise ValueError("%r is not an archive run id" % (run_id,))
    return "%s%s/%s/" % (env_root, GC_ARCHIVE_DIR, run_id)


def gc_pruned_key(env_root: str, run_id: str) -> str:
    """A pruned run's tombstone: `<env root>_composite-gc-archive/_pruned/<run id>/RECEIPT.json`, beside the run directories
    (GC_PRUNED_DIR), so a run in a listing is by definition not yet pruned."""
    if not isinstance(run_id, str) or gc_run_time(run_id) is None:
        raise ValueError("%r is not an archive run id" % (run_id,))
    return "%s%s/%s/%s/RECEIPT.json" % (env_root, GC_ARCHIVE_DIR, GC_PRUNED_DIR, run_id)


def gc_prune_control_key(env_root: str, token: str) -> str:
    """The control's throwaway sentinel: `<env root>_composite-gc-archive/_state/_prune-control/<token>`, never inside a run.
    `token` is 32 lower-case hex characters; anything else is a ValueError, so a caller cannot aim the control at a key it
    did not make up."""
    if not isinstance(token, str) or not _CONTROL_TOKEN.fullmatch(token):
        raise ValueError("%r is not a prune control token" % (token,))
    return "%s%s/%s/%s/%s" % (env_root, GC_ARCHIVE_DIR, GC_STATE_DIR, GC_PRUNE_CONTROL_DIR, token)


class RunRemoval(NamedTuple):
    delete: List[str]      # archive keys of the run a prune pass may remove, sorted; the run's RECEIPT.json is not among them (it goes last, after the tombstone)
    gone: List[str]        # keys the receipt names and the run's prefix lacks: already removed, which is the state asked for
    keep: Optional[str]    # why the whole run stays; None when it may go


def plan_run_removal(run_id: str, receipt: Any, listing: Mapping[str, int], env_root: str) -> RunRemoval:
    """What a prune pass may remove from ONE run `plan_archive_prune` named, scoped by the receipt and never by the directory
    (guard-6946). PURE: no I/O. `listing` maps every key under the run's prefix (`gc_run_prefix`) to its size.

    A key goes only when the run's receipt names it as an archived object's `archive_key` AND the run's listing holds it at
    the receipt's size. The run STAYS, with the reason, when: the receipt's object records are not all `archive_key` and an
    integer `size` ('receipt-malformed'); a record names a key outside this run's `objects/` directory
    ('receipt-names-foreign-key': a receipt must never point a delete elsewhere); the run's RECEIPT.json is not in the
    listing ('receipt-not-listed': another pass took it); the listing holds a key that is neither the receipt nor one it
    names ('foreign-key-in-run': something else lives there and a person looks); or a listed object's size differs from the
    receipt's ('archive-size-differs': it is no longer what the receipt says it archived). A key the receipt names and the
    listing lacks is `gone`: an earlier pass that died half way already removed it, and finishing it is the point."""
    prefix = gc_run_prefix(env_root, run_id)
    receipt_key = gc_receipt_key(env_root, run_id)
    objects = receipt.get("objects") if isinstance(receipt, dict) else None
    if not isinstance(objects, dict):
        return RunRemoval([], [], "receipt-malformed")
    named: Dict[str, int] = {}
    for rec in objects.values():
        key = rec.get("archive_key") if isinstance(rec, dict) else None
        size = rec.get("size") if isinstance(rec, dict) else None
        if not isinstance(key, str) or not isinstance(size, int) or isinstance(size, bool):
            return RunRemoval([], [], "receipt-malformed")
        if not key.startswith(prefix + "objects/"):
            return RunRemoval([], [], "receipt-names-foreign-key")
        named[key] = size
    if receipt_key not in listing:
        return RunRemoval([], [], "receipt-not-listed")
    if any(k != receipt_key and k not in named for k in listing):
        return RunRemoval([], [], "foreign-key-in-run")
    if any(listing[k] != size for k, size in named.items() if k in listing):
        return RunRemoval([], [], "archive-size-differs")
    return RunRemoval(sorted(k for k in named if k in listing), sorted(k for k in named if k not in listing), None)


class PruneEnumeration(NamedTuple):
    plan: PrunePlan
    runs: Dict[str, RunRemoval]  # for each run id in plan.prune: what a pass may remove from it, or why that run stays
    needed_why: Optional[str]    # why the head's needed set could not be read ('head-missing', ...); None when it was


class PruneControl(NamedTuple):
    ok: bool
    failed: Optional[str]        # why the control stopped the pass; None when ok
    evidence: Dict[str, Any]     # the sentinel's key, size, md5 and the version ids the control saw; JSON-native, it goes into every tombstone of the pass


class PruneApplied(NamedTuple):
    stopped: Optional[str]       # why the pass did not run to the end; None when it did
    plan: Optional[PrunePlan]    # the plan the pass acted on; None when it stopped before planning
    control: Optional[PruneControl]
    pruned: List[str]            # run ids removed: objects gone and read back absent, tombstone written and read back, receipt removed and read back absent
    skipped: Dict[str, str]      # run id -> why a planned run was kept
