"""Productivity snapshot telemetry — the per-iteration composite-score store.

One record per iteration close, written by `productivity-stop-gate.sh`'s G4
block. Storage: `{WORLD_DIR}/productivity-snapshots.jsonl` (legacy, append-only)
plus `productivity-snapshots-YYYY-MM-DD.jsonl` date segments once
PRODUCTIVITY_SNAPSHOTS_SEGMENTED is on. `store_name()` is the one writer rule;
`snapshot_paths()` the one reader rule.

WHY SEGMENTS (g-358-49, sibling of the gate-firings cutover g-358-08). The
legacy file is a single unrotated object that only grows, and S3 has no append
primitive: every append PUTs the WHOLE object. Measured 2026-09-04 on this box —
7,417,337 B / 13,549 records, 662 appends/24h fleet-wide, so ~4.72 GB/24h of
transfer for ~250 KB/24h of actual new content, which was 86.9% of the August
S3 bill. The cost is quadratic in the file's life, so it worsens on its own.
Daily segments bound each PUT to the current day's segment (~365 KB at this
rate) instead of the whole history.

This module exists so the writer's filename and the readers' matcher cannot
drift: they are two halves of one contract, and the failure when they drift is
SILENT — the writer keeps producing files the readers do not recognise, so
consumers read a short window and report it as the full retention window.
`coordination_merge.merge_handler_for` imports `_SEGMENT_RE` from here for the
same reason, never re-typing the pattern.
"""

import datetime as _dt
import os as _os
import re as _re
import sys as _sys
from pathlib import Path as _Path

# Single source of truth for WORLD_DIR — same resolver every other script uses.
from _paths import WORLD_DIR

# EXACT segment shape, not a `productivity-snapshots-*` prefix glob. Matching the
# precise date form means any future sibling that merely shares the stem (an
# archive, a spool, a per-box shard) is excluded BY CONSTRUCTION rather than by
# an enumerated denylist someone must remember to extend. Same reasoning as
# _gate_log._SEGMENT_RE, which was written after a spool file collided with a
# looser pattern.
_SEGMENT_RE = _re.compile(r"^productivity-snapshots-\d{4}-\d{2}-\d{2}\.jsonl$")


def segment_name(day=None):
    """Basename of the date segment covering `day` (default: today).

    Defined HERE, immediately beside `_SEGMENT_RE`, rather than in the writer —
    see the module docstring for why the two halves must share one definition.

    Dates are UTC wall clock (TZ=UTC fleet-wide), matching the `ts` field the
    consumers window on.
    """
    day = day or _dt.datetime.now().date()
    return f"productivity-snapshots-{day.isoformat()}.jsonl"


# Writer flag. Per-box on purpose: a box flips it only after the fleet's readers
# understand segments. Set fleet-wide from .claude/settings.json `env`, so a box
# cannot acquire the flag without also having pulled the reader code that ships
# in the same commit.
SEGMENTED_ENV = "PRODUCTIVITY_SNAPSHOTS_SEGMENTED"
LEGACY_STORE_NAME = "productivity-snapshots.jsonl"


def segmented_enabled():
    """True when this box writes new snapshots to today's date segment."""
    return _os.environ.get(SEGMENTED_ENV, "").strip().lower() in ("1", "true", "yes")


def store_name(day=None):
    """Basename the writer appends to: today's segment when the flag is on, the
    legacy file otherwise. Readers accept both (snapshot_paths)."""
    return segment_name(day) if segmented_enabled() else LEGACY_STORE_NAME


def snapshot_paths(world_dir=None):
    """Ordered paths comprising the snapshot store, oldest-first.

    Legacy file first (it holds every record written before the cutover), then
    date segments in lexical == chronological order. Consumers that concatenate
    these see one continuous history across the cutover with no special-casing.
    """
    # Guard the module constant, not just the parameter: WORLD_DIR is None
    # whenever paths are unresolved, so the no-arg call — the shape the
    # docstring invites — would raise TypeError from _Path(None).
    resolved = world_dir if world_dir is not None else WORLD_DIR
    if resolved is None:
        # Say so on stderr. Returning [] silently would make "paths unresolved"
        # indistinguishable from "store is genuinely empty", and a consumer
        # reading zero snapshots concludes productivity was never recorded —
        # which the health ledger would then publish as a real zero.
        print("[_productivity_snapshots] WORLD_DIR unresolved — snapshot store "
              "not enumerable; returning no paths", file=_sys.stderr)
        return []
    base = _Path(resolved)
    paths = []
    legacy = base / LEGACY_STORE_NAME
    if legacy.is_file():
        paths.append(legacy)
    for seg in sorted(base.glob("productivity-snapshots-*.jsonl")):
        if _SEGMENT_RE.match(seg.name) and seg.is_file():
            paths.append(seg)
    return paths
