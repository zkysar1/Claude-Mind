#!/usr/bin/env python3
"""_idle_cache_common.py -- shared idle-path cache-invalidation logic for the
two cycle-caches (dry-idle-cycle-cache.py + quiescence-cycle-cache.py).

Facet-1 of the g-115-3059 idle-path consolidation (story-a / g-115-3060).

The load-bearing helper is wake_timer_elapsed(): the SINGLE canonical
"has the stored baseline wake time arrived?" check. Both caches record an
`earliest_wake_at` baseline -- the soonest a queue goal becomes executable,
computed via _wake_timers.scan_queue when the sleep started -- and both must
MISS once it arrives. A FRESH rescan alone is not enough: _wake_timers'
future-only guard (g-115-3018) DROPS an ELAPSED recurring due-time from the
fresh scan, so a fresh-scan-only check silently sleeps through the now-due
goal. dry-idle-cycle-cache hit this as g-115-3033 (alpha msg-4248: the dry
cache reported "queue empty" while goal-selector returned g-001-04 executable
for ~10min); quiescence-cycle-cache was CONFIRMED to share the same latent gap
(g-115-3065, twin positive control) because its evaluate_cache checked ONLY the
fresh rescan and never the stored baseline that quiescence-gate already writes.

SHARED LOGIC, NOT SHARED STATE (g-115-3034): this module owns the elapsed
COMPARISON only. Each cache keeps its own cache file, its own writer, and its
own `within_s` imminent margin (dry-idle: MIN_FLOOR_S=60s; quiescence: its
SLEEP_CAP_S=600s horizon). Passing the margin in keeps the two sleep models
(dry honors the full exponential curve, quiescence caps every sleep at 600s)
independent while sharing one elapsed-check implementation.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# _parse_iso: single canonical ISO parser. _iter_goals / _goal_wake_time: the
# wake compute facet-2 reuses over authoritative-store records (shared LOGIC,
# not shared state -- ).
from _wake_timers import _parse_iso, _iter_goals, _goal_wake_time  # noqa: E402


def wake_timer_elapsed(earliest_wake_at, now, within_s=0):
    """True iff `earliest_wake_at` has arrived (elapsed) or is within `within_s`
    seconds of `now` -- i.e. a timer-gated goal may now be executable, so the
    caller MUST MISS the cache and run the full cycle.

    `earliest_wake_at` may be an ISO string (as stored in the cache / returned by
    scan_queue) or a datetime; None / unparseable -> False (no timer -> this lane
    forces no MISS, matching both caches' prior inline behavior).

    `within_s` is the imminent margin: 0 = elapsed-only; >0 also MISSes when the
    wake is that many seconds in the future. It is a per-CALLER parameter, not a
    module constant, because the two caches carry different horizons -- SHARED
    LOGIC, NOT shared state (g-115-3034)."""
    wake = earliest_wake_at if isinstance(earliest_wake_at, datetime) \
        else _parse_iso(earliest_wake_at)
    if wake is None:
        return False
    return (now + timedelta(seconds=within_s)) >= wake


def authoritative_earliest_wake_at(now, *, backend=None,
                                   world_dir=None, agent_dir=None):
    """The soonest queue wake time (ISO string) read from the AUTHORITATIVE
    store -- never the local mirror -- or None if no goal carries a pending timer.

    Facet-2 of the g-115-3059 idle-path consolidation (story-b / g-115-3062).

    WHY authoritative, not local: under own-cloud the local aspirations.jsonl
    mirror can lag the store, so a recurring goal due T+10s in the store may read
    T+300s (or absent) locally. A cache-invalidation (sleep) decision made from the
    local mirror can therefore sleep THROUGH now-due work -- g-115-3015; guard-1139
    'on own-cloud never make freshness decisions from local cache'. Each queue is
    read via backend.read_authoritative_bytes: an S3 get_object straight to memory
    on own-cloud (never mutates the local mirror, never falls back to local bytes),
    and a plain read on LocalBackend -- where the local file IS the store, so this
    is behavior-identical for the 100%-local user (the facet-2 gate is a redundant
    no-op there, matching the local fresh scan).

    SHARED LOGIC, not shared state (g-115-3034): only the SOURCE of the goal
    records differs from _wake_timers.scan_queue (authoritative store vs local
    mirror). The wake compute reuses _wake_timers._iter_goals + _goal_wake_time
    unchanged, so both paths agree on which timers count and how they are computed.

    Fail-open contract (mirrors _wake_timers.scan_queue + the idle path's 'never
    freeze asleep on error' direction, dry-idle-backoff tree node): a source ABSENT
    in the store (FileNotFoundError -- e.g. a fresh agent with no agent queue) is a
    skipped empty source, NOT an error. Any OTHER store failure (S3 unreachable,
    an undecodable object) PROPAGATES so the caller fails open to a MISS (full
    cycle), never a sleep -- guard-1139: do not commit to sleep on a local-only
    decision when the authoritative check could not run.

    backend / world_dir / agent_dir are test-only injection points; all default
    to the live resolution (the public call is authoritative_earliest_wake_at(now))."""
    if backend is None:
        from storage_backend import get_backend
        backend = get_backend()
    if world_dir is None or agent_dir is None:
        from _paths import WORLD_DIR, AGENT_DIR
        if world_dir is None:
            world_dir = WORLD_DIR
        if agent_dir is None:
            agent_dir = AGENT_DIR

    asps = []
    # Agent then world -- order is irrelevant (we take the min wake across both),
    # but mirrors _wake_timers.scan_queue's agent-then-world load order.
    for base in (agent_dir, world_dir):
        if base is None:
            continue
        path = Path(base) / "aspirations.jsonl"
        try:
            raw = backend.read_authoritative_bytes(path)
        except FileNotFoundError:
            continue  # absent in the store -> empty source (scan_queue parity)
        # Any OTHER exception (S3 error, decode failure) propagates -> caller MISS.
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) \
            else str(raw)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                asps.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue  # tolerate a corrupt line (scan_queue parity)

    earliest = None
    for g in _iter_goals(asps):
        w = _goal_wake_time(g, now)
        if w is not None and (earliest is None or w < earliest):
            earliest = w
    return earliest.isoformat(timespec="seconds") if earliest else None
