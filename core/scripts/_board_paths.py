"""_board_paths.py — the READER-SIDE PATH-LIST SEAM for world/board/<channel>.jsonl.

This is the board's analogue of `_gate_log.firings_paths()`, and it exists for
the same reason and under the same ORDERING CONSTRAINT, stated verbatim in
`_gate_log.py`: **the reader seam lands BEFORE any writer change**, because a
segmented writer silently starves every consumer that hardcodes the filename,
and "a false all-clear is the worst available failure direction". The board is
the fleet's own claim/coordination path, so the failure direction of getting it
wrong is an agent reading a partial board, finding no partner claim, and
double-executing (g-358-110).

NOTHING IN THIS MODULE FLIPS A WRITER BY DEFAULT. The writer rule (`write_name`,
g-358-183) lives here beside `segment_name` so every writer lane shares one
definition. It is OFF until `BOARD_SEGMENTED_CHANNELS` names a channel, and naming
one is a separate, later step, gated on every reader of that channel understanding
segments. A reader that already understands segments is safe on a box that never
writes one: `channel_paths()` simply returns the archive+live pair it returns today.

WHAT THIS GENERALISES. `mind_api/src/endpoints/board.py` (g-358-107) reads TWO
files with a KNOWN relationship: live + its one archive. Date segmentation
produces N files whose SET CHANGES OVER TIME, which is a different contract:
a reader must tolerate a segment DISAPPEARING between two reads, not merely
appearing. That is why `channel_paths()` re-enumerates on every call and
`read_paths()` treats a vanished path as data (`evicted`), not as an error.

THE STORE IS A ROLLING WINDOW, NOT APPEND-ONLY. Certified with a prefix test
against historical S3 versions (g-358-110 description): coordination.jsonl
diverges at byte 17 — inside the FIRST line — while the older object is LARGER,
confirmed head-eviction (7,281 of 8,945 lines evicted, 3,733 appended, surviving
order preserved). Two consequences this module encodes:
  * any design that saves a byte OFFSET or reads a tail by offset is invalid
    outright, because an offset taken before an eviction points into the wrong
    place. This module returns PATHS and parses whole files; it never seeks.
  * under segmentation, archival becomes "drop a whole old segment" — cheap, but
    the reader must survive it mid-read. See `read_paths()`.

WHY PATHS AND NOT RECORDS. Same reason `_gate_log.firings_paths()` gives: the
consumers have genuinely different parse/filter needs — the census in this
goal's outcome_note measured `--since` windows from 30m to 720h plus
`--unread-only` receipts, `--mark-read` receipt writes, `--type`, `--tag`,
`--last N`, `--author` and reply-to threading. A shared record-reader would mean
rewriting every one of those working parse loops. A path list leaves them
untouched and makes segmentation a change to this function alone.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import os as _os
import re as _re
import sys as _sys
from pathlib import Path as _Path

# --- The name contract -------------------------------------------------------
#
# Writer filename and reader matcher are the two halves of ONE contract, and the
# failure mode when they drift is silent: the writer keeps producing files the
# reader does not recognise, so consumers read a short window and report it as
# the full retention window. Defined together, here, so that drift is impossible
# rather than merely unlikely (the `_gate_log.segment_name` rationale).
#
# THE PATTERN IS STRICT DATE-SHAPED, NOT A LOOSE `<channel>-*.jsonl` GLOB, and on
# the board that is not a stylistic preference — a loose glob is WRONG against the
# directory as it exists. Measured (alpha, cc-04, uname -r 6.8.0-139-generic,
# 2026-09-17T02:0x) in $WORLD_PATH/board/, a loose `coordination-*.jsonl` admits:
#     coordination-archive.jsonl          46,440,540 B   the rotation destination
#     coordination-archive-archive.jsonl           0 B   a rotated-away archive
#     coordination-reads.jsonl               566,808 B   READ RECEIPTS, not posts
# and `findings-*.jsonl` / `decisions-*` / `general-*` / `reasoning-*` each admit
# their own `-reads.jsonl`. Admitting `-reads.jsonl` would inject receipt records
# into a message read as though they were board posts. The archive is handled
# EXPLICITLY below (it is part of the store, at a known position) — it must not
# arrive by accident through the segment matcher. Matching the exact segment shape
# excludes all three by construction rather than by an enumerated denylist that
# must be kept in sync with every future sibling file (`_gate_log._SEGMENT_RE`
# learned this after a loose glob admitted `gate-firings-spool.jsonl`).
_DATE_RE = r"\d{4}-\d{2}-\d{2}"


def segment_name(channel: str, day=None) -> str:
    """Basename of the date segment covering `day` (default: today, UTC).

    Dates are UTC wall clock (TZ=UTC fleet-wide, CLAUDE.md "Naming Rules"),
    matching the `timestamp` field every board consumer windows on.
    """
    day = day or _dt.datetime.now().date()
    return f"{channel}-{day.isoformat()}.jsonl"


def live_name(channel: str) -> str:
    """The legacy/base file every writer appends to today."""
    return f"{channel}.jsonl"


def archive_name(channel: str) -> str:
    """Rotation destination. Rotation MOVES posts here and deletes nothing."""
    return f"{channel}-archive.jsonl"


def _segment_re(channel: str):
    return _re.compile(r"^" + _re.escape(channel) + r"-" + _DATE_RE + r"\.jsonl$")


def is_segment(channel: str, name: str) -> bool:
    """True only for a real date segment of `channel`.

    Exposed so a writer-side or audit-side caller can ask the SAME question the
    reader asks, instead of re-deriving the pattern.
    """
    return bool(_segment_re(channel).match(name))


_ANY_SEGMENT_RE = _re.compile(r"^(?P<channel>.+)-" + _DATE_RE + r"\.jsonl$")


def segment_parent(name: str):
    """The channel `name` is a date segment OF, or None. Inverse of `is_segment`.

    `is_segment` answers "is this a segment of THIS channel" and so needs the
    channel up front. The channel ENUMERATOR has the opposite shape: it holds a
    filename off a bare glob and no candidate channel at all (g-358-154). Rather
    than let that caller hand-roll a second date pattern, this shares `_DATE_RE`
    and CONFIRMS through `is_segment`, so the pattern stays defined exactly once
    and the two directions cannot drift apart.

    NOT an archive test, deliberately: `archive_name()` is `<channel>-archive`
    and "archive" is not a date, so an archive returns None here and keeps its
    own row. Archive-file handling is a separate surface (g-358-129).
    """
    m = _ANY_SEGMENT_RE.match(name)
    if not m:
        return None
    channel = m.group("channel")
    return channel if is_segment(channel, name) else None


# --- The writer rule ---------------------------------------------------------
#
# ONE rule for which file a new post is appended to, imported by EVERY writer
# lane that posts to THIS world's board: the daemon endpoint
# (mind_api/src/endpoints/board_write.py::post) and the CLI
# (core/scripts/board.py::cmd_post). The gate-firings flip was half-effective for
# a day because its rule lived in one lane while a second lane hard-coded the
# legacy name (`_gate_log.store_name`, ). Defining the rule here, beside
# `segment_name`, keeps the lanes from drifting ().
#
# DEFAULT OFF, AND A CHANNEL LIST RATHER THAN A BOOLEAN. When unset, every writer
# appends to `<channel>.jsonl` exactly as before. A channel flips only when it is
# named, so a flip can be staged one channel at a time after that channel's
# readers are verified (the reader census differs per channel). A truthy value
# such as `1` names a channel called "1" and so flips NOTHING. That is the rule
# `OWNCLOUD_GZIP_STORES` settled on: legacy truthy values encode nothing.
#
# The lanes that write INTO ANOTHER world's board (peer_board_post.py,
# cross-world-post.sh) deliberately do NOT use this. That board's readers run
# that world's checkout, which may not understand segments, so those lanes stay
# on `live_name` (guard-1851).
SEGMENTED_ENV = "BOARD_SEGMENTED_CHANNELS"


def segmented_channels():
    """Channels whose writers in this world append to today's date segment."""
    raw = _os.environ.get(SEGMENTED_ENV, "")
    return frozenset(c.strip() for c in raw.split(",") if c.strip())


def write_name(channel: str, day=None) -> str:
    """Basename a new post to `channel` is appended to.

    That is the date segment for `day` (default: today, UTC) when the channel is
    named in `BOARD_SEGMENTED_CHANNELS`, and the live file otherwise. Readers
    accept both (`channel_paths`). A writer picks the name before it takes the
    file lock, so a post whose lock wait crosses midnight lands in the previous
    day's segment. That is harmless because no reader infers a record's date from
    its segment name: windows and `continuity_gaps` read the `timestamp` field.
    """
    if channel in segmented_channels():
        return segment_name(channel, day)
    return live_name(channel)


def group_by_channel(paths):
    """Group board files by the channel they BELONG to, folding date segments.

    Returns `{channel: [path, ...]}` with segments folded under their parent, so
    an enumerator emits ONE row per channel whose count is the channel's TRUE
    total. This is the FOLD half of g-358-154's decision (fold, not hide): hiding
    a segment removes the spurious row but leaves the parent's count understating
    the channel by exactly the hidden messages -- a visible wrong row traded for
    an invisible wrong number.

    It agrees with `channel_paths()` on what a channel IS. Pure grouping, no I/O:
    the two enumerators that call it (core/scripts/board.py::cmd_channels and
    mind_api/src/endpoints/board_write.py::channels) format differently but must
    not DISAGREE, so the shared piece is this function rather than two
    hand-synced copies of the rule.
    """
    groups = {}
    for p in paths:
        name = getattr(p, "name", None) or str(p)
        parent = segment_parent(name)
        if parent is None:
            parent = name[:-6] if name.endswith(".jsonl") else name
        groups.setdefault(parent, []).append(p)
    for key in groups:
        groups[key].sort(key=lambda q: getattr(q, "name", str(q)))
    return groups


# --- The path list -----------------------------------------------------------

def channel_paths(board_dir, channel: str, include_archive: bool = True):
    """Ordered paths comprising one board channel, OLDEST-FIRST.

    Order is `[<channel>-archive.jsonl] + [<channel>.jsonl] + [date segments]`:

      * the ARCHIVE is oldest because rotation moves the OLDEST posts out of the
        live file into it (measured on both rotated channels 2026-09-16: the
        coordination archive ended 2026-08-29 against a live file starting
        2026-09-07; findings ended 2026-08-14 against 2026-08-26);
      * the LIVE file is next, and is the "legacy base" in the `firings_paths`
        sense — the file every writer appends to until a segmented writer lands;
      * SEGMENTS follow in lexical (== chronological, ISO dates) order, because
        segmentation starts AFTER the live file stops growing.

    RE-ENUMERATED ON EVERY CALL, deliberately. Half the answer to eviction is
    simply never caching this list: a cached list names segments that may already
    be gone. The other half is `read_paths()`, which survives a path that
    disappears between this enumeration and the open.

    `include_archive=False` serves the cheap common read: a `--since` window that
    lies inside the live file must not stat, open or read the archive at all
    (the g-358-107 branch condition). Callers keep that decision; this function
    does not guess it.
    """
    if board_dir is None:
        # Say so on stderr. Returning [] silently would make "paths unresolved"
        # indistinguishable from "channel is genuinely empty", and a consumer
        # reading zero posts concludes no partner claim exists and
        # double-executes — the false all-clear this seam exists to prevent.
        # Matching `_gate_log.firings_paths` means matching its loudness too.
        print("[_board_paths] board_dir unresolved — channel not enumerable; "
              "returning no paths", file=_sys.stderr)
        return []
    base = _Path(board_dir)
    paths = []
    if include_archive:
        arch = base / archive_name(channel)
        if arch.is_file():
            paths.append(arch)
    live = base / live_name(channel)
    if live.is_file():
        paths.append(live)
    seg_re = _segment_re(channel)
    for seg in sorted(base.glob(f"{channel}-*.jsonl")):
        if seg_re.match(seg.name) and seg.is_file():
            paths.append(seg)
    return paths


# --- Reading, eviction-tolerantly -------------------------------------------

def read_paths(paths):
    """Parse an ordered path list into `(records, missing)`.

    `missing` is a list of `{"path", "reason", "error"}`, where `reason` is
    `"evicted"` (the path is GONE) or `"unreadable"` (the path is there and the
    open failed). BOTH shorten the window, and they are reported SEPARATELY on
    purpose: routine archival and a real fault must not look alike. Conflating
    them is itself a false all-clear — a permissions or I/O fault on a live
    segment would read as ordinary eviction and nobody would chase it.

    That distinction is not a matter of taste; it is the only thing the mutation
    control in `test_board_paths.py` can measure. `FileNotFoundError` is a
    SUBCLASS of `OSError`, so a version of this function whose two branches did
    the same thing is byte-for-byte indistinguishable from one with the eviction
    branch deleted — the arm "passes" against a broken copy and proves nothing.
    Caught by that control on its first run (g-358-110).

    EVICTION IS DATA, NOT AN ERROR. Under segmentation, archival becomes
    "drop a whole old segment", so a path enumerated microseconds ago can be
    gone by the time it is opened. Raising there would turn routine archival
    into a read outage on the fleet's coordination path. Instead the vanished
    path is reported so the caller can SAY the window is short rather than
    silently serving less than was asked for.

    EACH FILE IS PARSED SEPARATELY AND NEVER BYTE-CONCATENATED (guard-6846).
    Board JSONL dumps do not reliably end with a newline — measured on
    `board-read.sh --json`: 5,260 newlines for 5,261 records, last byte `}`.
    Concatenating N such files glues the last record of one onto the first of
    the next, producing one unparseable line per boundary; in the common
    `for line in fh: json.loads(line)` shape that raises `Extra data` and stops
    the scan there, so the reader silently loses everything after the FIRST
    boundary. Reading per-file makes that impossible by construction.

    A malformed line inside a file is skipped, matching
    `mind_api/src/endpoints/board.py::_read_archive_tail`: a single corrupt row
    must not take out the channel.
    """
    records = []
    missing = []
    for p in paths:
        p = _Path(p)
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                blob = fh.read()
        except FileNotFoundError as exc:
            # The segment was evicted between enumeration and open. Expected
            # under a rolling window; not a fault.
            missing.append({"path": str(p), "reason": "evicted", "error": str(exc)})
            continue
        except OSError as exc:
            # Present but unreadable — permissions, I/O, a truncated mount.
            # ALSO a short window, but a FAULT rather than routine archival.
            missing.append({"path": str(p), "reason": "unreadable", "error": str(exc)})
            continue
        for line in blob.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = _json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records, missing


def post_in_live_half(held_path, channel: str, post_id: str, held_items) -> bool:
    """True when `post_id` names a post in the channel's live half.

    The live half is the base file plus its date segments, never the archive.
    That is the same scope a writer's in-lock snapshot covered while the base
    file was the only live file.

    `held_path` is the file the writer is appending to, and `held_items` is the
    snapshot it already read under the lock. The snapshot is checked first, so
    the common case reads nothing extra. Only on a miss are the channel's OTHER
    live-half files read. When no segment exists that list is empty, so behaviour
    is unchanged until a segment exists.

    Without this, a segmented writer holds only TODAY's segment, and every reply
    to an older post would draw a false "reply_to not found" warning. The same
    false warning would hit a box that still writes the base file while a peer
    box writes segments.
    """
    if post_id in {r.get("id") for r in held_items if isinstance(r, dict)}:
        return True
    held = _Path(held_path)
    others = [p for p in channel_paths(held.parent, channel, include_archive=False)
              if p.name != held.name]
    records, _missing = read_paths(others)
    return any(r.get("id") == post_id for r in records)


# --- Discontinuity ----------------------------------------------------------

def continuity_gaps(records, ts_field: str = "timestamp"):
    """ISO days with ZERO records strictly inside the span the records cover.

    THE SEAM MUST REPORT DISCONTINUITY, NOT MERELY ENUMERATE PATHS. A footer that
    computes "window covered X .. Y" from oldest/newest asserts coverage ACROSS a
    hole: enumerating paths cannot detect a MISSING segment, and that is the same
    defect as eviction seen from the other side. Continuity is cheap — one
    day-bucket pass over the merged result — and it is the only check that can
    tell a clean hand-off from two stores that have diverged past each other
    (guard-6871: a zero live/archive intersection is AMBIGUOUS, not healthy).

    Live reading on this box after the g-358-117 archive-refresh fix landed
    (alpha, cc-04, 2026-09-17T02:0x): findings span 2026-08-15..2026-09-17 and
    coordination span 2026-09-02..2026-09-17 each return ZERO gap days — so this
    is a forward-looking guard for the N-segment case, not a live defect today.
    The 11-day findings / 9-day coordination holes measured at 00:5x the same
    morning were a STALE LOCAL ARCHIVE (guard-980), not missing data.

    A caller with no records gets `[]`: there is no span, so there is no hole.
    """
    days = set()
    for r in records:
        ts = r.get(ts_field) if isinstance(r, dict) else None
        if isinstance(ts, str) and len(ts) >= 10:
            days.add(ts[:10])
    if len(days) < 2:
        return []
    try:
        lo = _dt.date.fromisoformat(min(days))
        hi = _dt.date.fromisoformat(max(days))
    except ValueError:
        return []
    gaps = []
    d = lo + _dt.timedelta(days=1)
    while d < hi:
        if d.isoformat() not in days:
            gaps.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return gaps


def coverage_note(records, missing=None, ts_field: str = "timestamp") -> str:
    """One-line human coverage statement that is honest about holes.

    Returns "" when the read is continuous and nothing was missing, so a caller
    can append it unconditionally without adding noise to the healthy path.
    The two `missing` reasons are rendered SEPARATELY: "evicted" is routine
    archival, "unreadable" is a fault someone should chase.
    """
    parts = []
    gaps = continuity_gaps(records, ts_field=ts_field)
    if gaps:
        parts.append(f"DISCONTINUOUS — {len(gaps)} day(s) with zero records "
                     f"inside the covered span ({gaps[0]}"
                     + (f" .. {gaps[-1]}" if len(gaps) > 1 else "")
                     + "); window NOT fully covered")
    for reason, label in (("evicted", "evicted mid-read"),
                          ("unreadable", "UNREADABLE (fault, not archival)")):
        hit = [m for m in (missing or []) if m.get("reason") == reason]
        if hit:
            parts.append(f"{len(hit)} path(s) {label} "
                         f"({', '.join(_Path(m['path']).name for m in hit[:3])}"
                         + (", ..." if len(hit) > 3 else "")
                         + "); window NOT fully covered")
    return "; ".join(parts)
