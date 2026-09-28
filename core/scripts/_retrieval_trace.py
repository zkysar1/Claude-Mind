"""Retrieval-trace telemetry — the one definition of what the store's files ARE.

One row per retrieve.py invocation, written by `retrieve._log_retrieval_trace`.
Storage: `{WORLD_DIR}/retrieval-trace.jsonl` (legacy, append-only) plus
`retrieval-trace-YYYY-MM-DD.jsonl` date segments once a segmented writer lands.
`segment_name()` is the filename a writer emits, `_SEGMENT_RE` / `segment_parent()`
what the merger and readers match, and `trace_paths()` the one reader rule.

WHY SEGMENTS (g-358-220). S3 has no append primitive, so every appended row
re-PUTs the WHOLE object: measured 2026-09-27 on the live store, 501 versions /
1,235,871,935 B per 24h for a ~2.4 MB (gzip) object. A daily segment bounds each
PUT to the current day's rows instead of the whole history.

WHY THIS MODULE EXISTS BEFORE THE WRITER. The writer's filename, the merger's
registration and the readers' matcher are three halves of one contract, and the
failure when they drift is SILENT: a segment with no merge handler write-freezes
under concurrent cross-box appends (guard-1055), and a reader still hardcoding
the legacy name sees a short window and reports it as the whole store. So the
merger (`coordination_merge.merge_handler_for`) and every reader import from
here, and the writer lands last (g-358-183 / g-358-121 ordering).

STDLIB ONLY, deliberately. `_productivity_snapshots` imports `_paths.WORLD_DIR`
at import time; this module must not, for two reasons: `coordination_merge`
imports it at module level from the backend hot path, and the trace's writer
runs inside the daemon, which swaps `retrieve.WORLD_DIR` per request (Decision
#58), so a module-level WORLD_DIR here would name the daemon's STARTUP world.
Callers therefore pass `world_dir` explicitly.
"""

import datetime as _dt
import os as _os
import re as _re
import sys as _sys
from pathlib import Path as _Path

LEGACY_STORE_NAME = "retrieval-trace.jsonl"

# The writer flag ( outcome 4). DEFAULT OFF: unset, the writer keeps
# appending to the legacy file byte-for-byte as before. Flip it only after every
# box runs the reader seam + merge branch above ( ordering) — the same
# lane-by-lane cutover GATE_FIRINGS_SEGMENTED took (_gate_log.segmented_enabled).
SEGMENTED_ENV = "RETRIEVAL_TRACE_SEGMENTED"

# EXACT segment shape, not a `retrieval-trace-*` prefix glob: a future sibling
# that merely shares the stem (an archive, a spool, a per-box shard) is excluded
# BY CONSTRUCTION rather than by a denylist someone must remember to extend.
# Same reasoning as _gate_log._SEGMENT_RE and _productivity_snapshots._SEGMENT_RE.
_SEGMENT_RE = _re.compile(r"^retrieval-trace-\d{4}-\d{2}-\d{2}\.jsonl$")


def segment_name(day=None):
    """Basename of the date segment covering `day` (default: today).

    Defined beside `_SEGMENT_RE` so the name a writer emits and the pattern
    readers match cannot drift apart. Dates are UTC wall clock (TZ=UTC
    fleet-wide), the same clock as each row's `ts`.
    """
    day = day or _dt.datetime.now().date()
    return f"retrieval-trace-{day.isoformat()}.jsonl"


def segmented_enabled():
    """True when this process writes new trace rows to today's date segment."""
    return _os.environ.get(SEGMENTED_ENV, "").strip().lower() in ("1", "true", "yes")


def store_name(day=None):
    """The one writer rule: the BASENAME a trace row is appended to.

    Today's segment when the flag is on, the legacy file otherwise. A basename
    only, so the caller composes it with its own world dir — retrieve.py must use
    its per-request-swappable `WORLD_DIR` (Decision #58), never a path from here.
    Readers accept both shapes through `trace_paths`.
    """
    return segment_name(day) if segmented_enabled() else LEGACY_STORE_NAME


def is_segment(name):
    """True when basename `name` is a retrieval-trace date segment."""
    return bool(_SEGMENT_RE.match(name or ""))


def segment_parent(name):
    """The legacy basename that segment `name` belongs to, or None.

    The merger's inheritance hook: `merge_handler_for` resolves a segment to
    whatever `_HANDLERS` registers for THIS name, so the segments follow any
    later change to the parent's registration instead of hardcoding a handler.
    The legacy file itself is not a segment and returns None.
    """
    return LEGACY_STORE_NAME if is_segment(name) else None


def trace_paths(world_dir):
    """Ordered paths comprising the trace store, oldest-first.

    The legacy file first (every row written before a cutover), then date
    segments in lexical == chronological order, so a consumer that concatenates
    them reads one continuous history with no special-casing.
    """
    if world_dir is None:
        # Say so. Returning [] silently would make "paths unresolved"
        # indistinguishable from "store is genuinely empty", and a consumer
        # reading zero rows would report a real-looking zero.
        print("[_retrieval_trace] world_dir unresolved — trace store not "
              "enumerable; returning no paths", file=_sys.stderr)
        return []
    base = _Path(world_dir)
    paths = []
    legacy = base / LEGACY_STORE_NAME
    if legacy.is_file():
        paths.append(legacy)
    for seg in sorted(base.glob("retrieval-trace-*.jsonl")):
        if is_segment(seg.name) and seg.is_file():
            paths.append(seg)
    return paths
