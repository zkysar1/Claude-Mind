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

# {(agent_etag, world_etag): (earliest_iso_or_None,)} -- the 1-tuple is what
# distinguishes "no memo" from "memo says None".
# The ETag short-circuit in authoritative_earliest_wake_at (). Held
# at module level so it survives across the two cycle-cache calls inside one
# iteration; cleared to a single entry on every write so a long-lived daemon
# cannot accumulate slots. Deliberately keyed on CONTENT identity only -- no
# session-scoped value goes anywhere near it (guard-2480).
_AUTH_WAKE_MEMO = {}


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

    # --- ETag short-circuit () --------------------------------
    # Each source below is a FULL unconditional S3 get_object straight to
    # memory. Measured 2026-08-26 on cc-05: world/aspirations.jsonl is
    # 19,882,815 B and agents/<a>/aspirations.jsonl 113,422 B, and BOTH
    # cycle-caches (dry-idle-cycle-cache.py:381, quiescence-cycle-cache.py:311)
    # call this every loop iteration on every agent on every box -- ~40 MB of
    # pure egress per iteration to compute one min() timestamp. That is the
    # dominant term in the 919 GB/wk re-pull  attributed to this
    # bucket (rb-9328: "the real lever is box-side caching -- conditional GETs
    # + longer read-through TTLs").
    #
    # The fix cannot be "read the mirror" -- guard-1139 / rb-2636: under
    # own-cloud the local aspirations.jsonl is non-authoritative, and that is
    # this whole function's reason to exist. But an ETag match is PROOF the
    # object is unchanged, not a freshness heuristic, so serving a memoized
    # answer behind one is authoritative in the same sense the GET is.
    # backend.stat() is already in the StorageBackend protocol
    # (storage_backend.py:199) and its `version` IS the S3 ETag
    # (owncloud_backend.py:1211) -- so this needs no new backend method, no
    # protocol change, and no update to the FakeBackends in the suite.
    versions = []
    stat_fn = getattr(backend, "stat", None)   # genuinely absent on some fakes
    for base in (agent_dir, world_dir):
        if base is None:
            continue
        if stat_fn is None:
            versions = None
            break
        src = Path(base) / "aspirations.jsonl"
        # stat() and read_authoritative_bytes DIVERGE on two path classes, and
        # in both the ETag would describe a different object than the bytes:
        #   * MACHINE-LOCAL -- read_authoritative_bytes short-circuits to a
        #     local read, while OwnCloudBackend.stat has no _machine_local
        #     branch and HEADs S3 (owncloud_backend.py:1211). The memo would
        #     then key on a remote ETag that local edits never move.
        #   * OUT-OF-ROOT -- read_authoritative_bytes catches the ValueError
        #     _s3_key raises and reads locally; stat does not catch it, so the
        #     probe would raise where today's code succeeds.
        # Neither is reachable for these two governed queues today, but this
        # helper is imported by both cycle-caches and the guard costs one
        # branch each. Declining the OPTIMISATION is always safe -- it falls
        # through to the unchanged read below and returns the same answer.
        # Note this is NOT a guard-160 synthesized default: nothing is
        # invented, and a genuine store failure still propagates untouched.
        # ONE try around BOTH probes: _machine_local is pure path
        # classification (owncloud_sync's _EXCLUDE_DIRS prune + basename
        # rules) and _s3_key resolves a path relative to the configured
        # root, so an out-of-root path can raise ValueError from either.
        # This call is NEW to this function, so an exception it raises would
        # be an error today's code never had -- it must not escape.
        try:
            is_machine_local = getattr(backend, "_machine_local", None)
            if callable(is_machine_local) and is_machine_local(src):
                versions = None
                break
            st = stat_fn(src)
        except ValueError:      # path outside the configured root
            versions = None
            break
        versions.append(getattr(st, "version", None) if st is not None else None)
    memo_key = tuple(versions) if versions is not None else None

    if memo_key is not None:
        memo = _AUTH_WAKE_MEMO.get(memo_key)
        if memo is not None:
            (earliest_iso,) = memo          # 1-tuple: distinguishes a memoized None
            # PROVABLY IDENTICAL to a fresh compute, and the proof is short
            # because _wake_timers._goal_wake_time ends in ONE global backstop:
            #     future = [c for c in candidates if c > now]
            #     return min(future) if future else None
            # so the answer is exactly R(t) = min{c in C : c > t}, and an ETag
            # match fixes C. As t advances candidates only DROP OUT, never
            # appear, therefore:
            #   * R(T) is None  -> {c > T} was empty, so {c > T'} is empty too.
            #   * R(T) > now    -> the minimiser is still in the set and nothing
            #                      smaller can have appeared, so R(T') == R(T).
            #   * R(T) <= now   -> the minimiser has elapsed and a fresh compute
            #                      would drop it. Fall through and recompute.
            # No staleness window and no tunable: the memo is either exactly
            # right or not used. (An earlier draft carried a third arm for a
            # candidate already past at compute time; that state is UNREACHABLE
            # through the global backstop above, and the test written to pin it
            # is what surfaced the misreading.)
            if earliest_iso is None:
                return None
            e = _parse_iso(earliest_iso)
            if e is None or e > now:
                return earliest_iso

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
    result = earliest.isoformat(timespec="seconds") if earliest else None
    if memo_key is not None:
        # SINGLE-SLOT by construction: keys are ETag tuples, so a dict would
        # grow without bound over a long-lived daemon's life. Only consecutive
        # calls can hit, which is exactly the win being harvested (the two
        # cycle-caches read the same two objects moments apart in one
        # iteration), so one slot loses nothing.
        _AUTH_WAKE_MEMO.clear()
        _AUTH_WAKE_MEMO[memo_key] = (result,)
    return result
