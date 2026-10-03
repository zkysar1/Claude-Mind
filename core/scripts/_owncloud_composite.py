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
merge handler emits and the order the live file is in: 0 adjacent inversions in 4,540
goals; NUMERIC order differs, and counting inversions in it is how an earlier draft
reported 81 phantom ones).

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
rule instead of keeping copies.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple

import _owncloud_codec as _codec

FORMAT = "composite-v1"
SEGMENT_SPAN = 250
SEGMENT_DIR = ".composite"  # beside the head object; in owncloud_sync._EXCLUDE_DIRS, so never mirrored
READ_ATTEMPTS = 3
HEAD_MIN_BYTES = 256 * 1024  # the stored head is at least this: above the 128 KiB inline threshold with margin (U4 measures the real xl.meta)
MIN_RAW_BYTES = 4 * 1024 * 1024  # a store under this goes whole: its PUT is already cheap, and the head floor alone would cost more
FLAG_ENV = "OWNCLOUD_COMPOSITE_STORES"
ALLOWLIST = ("world/aspirations.jsonl",)  # the measured churn leader (): one explicit file
_HEAD_PREFIX = ('{"composite": "%s"' % FORMAT).encode("ascii")
_SAFE_ASP = re.compile(r"^[A-Za-z0-9_.-]+$")
_NUM_TAIL = re.compile(r"-(\d+)$")


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
            for g in goals:
                gid = g.get("id") if isinstance(g, dict) else None
                if not isinstance(gid, str):
                    raise NotSplittable("%s: a goal has no string id" % asp)
                bucket = buckets.setdefault("%s/%s" % (asp, goal_token(gid, span)), {})
                if gid in bucket:
                    raise NotSplittable("%s: duplicate goal id %s" % (asp, gid))
                bucket[gid] = g
        shells.append(obj)
    segments: Dict[str, bytes] = {}
    manifest: Dict[str, dict] = {}
    for key in sorted(buckets):
        body = "".join(dumps(g) + "\n" for _gid, g in sorted(buckets[key].items())).encode("ascii")
        segments[key] = body
        manifest[key] = {"md5": _md5(body), "bytes": len(body), "goals": len(buckets[key])}
    doc = {"composite": FORMAT, "span": span, "joined_md5": _md5(raw), "joined_bytes": len(raw),
           "aspirations": shells, "segments": manifest}
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
            goals.sort(key=lambda g: g["id"])
            obj["goals"] = goals
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


def plan_write(old_head: Optional[bytes], raw: bytes, span: int = SEGMENT_SPAN) -> Put:
    """What one write PUTs: the new head plus only the segment objects the old head does not
    already name. Raises NotSplittable, and the caller then PUTs the whole object."""
    new = split(raw, span)
    held = set()
    if old_head and is_head(old_head):
        held = {segment_object_name(k, m["md5"]) for k, m in _parse_head(old_head)["segments"].items()}
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


def should_composite(rel: str, env_id: Optional[str], env: Optional[dict] = None,
                     allowlist=ALLOWLIST) -> bool:
    """Writer gate: the deployment's `env_id` is named by OWNCLOUD_COMPOSITE_STORES AND `rel`
    (the backend's env-scoped logical path) is on the allowlist. Never true by default, and
    independent of the gzip flag: readers attested for one layout are not attested for the other."""
    return _codec.env_enabled(env_id, env, FLAG_ENV) and _codec.rel_allowlisted(rel, allowlist)
