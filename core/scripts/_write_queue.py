"""In-process per-path FIFO write queue — same-path writers wait in line.

g-328-28 (fleet write-path BRD, P1): under multi-agent load, N same-machine
callers hitting one file raced the cross-process lock — losers spun in the
retry/backoff loop (g-328-20) and eventually surfaced a raw
``TimeoutError("Could not acquire lock: ...")`` to the caller (2026-07-04
incident, 5 agents live). Since the daemon-only architecture funnels all
framework writes through ONE daemon per box (ThreadingHTTPServer — each
request is a thread in one process), same-path contention is almost always
same-PROCESS contention. This module serializes those threads in ARRIVAL
ORDER ahead of the lock/CAS layer:

  - different paths stay fully parallel (one queue per normalized path)
  - same-path callers wait their turn (strict FIFO) instead of hammering
    the lock file with jittered retries
  - overload fails LOUD with a DISTINCT, actionable backpressure error
    (``WriteQueueFullError`` / ``WriteQueueTimeoutError``) — never the raw
    lock error — mapped to HTTP 429 by the daemon (server.py)
  - contention is MEASURED (``metrics_snapshot()``, exposed at
    GET /v1/admin/write-queue) — the conflict-rate signal that would
    justify a remote-lock-table conditional-write escalation if contention
    stays high after the g-328-27 team-state sharding

Wiring: ``_fileops.acquire_lock`` enters the queue before calling the
backend lock; ``_fileops.release_lock`` releases the turn after unlinking
the lock file. The turn spans the WHOLE hold (acquire → release), so the
next waiter starts only when the file lock is actually free — that is what
makes it a line rather than a faster stampede.

Residual raw-lock contention remains possible from OTHER processes on the
same box (direct CLI invocations bypassing the daemon, a second daemon).
Those still see the backend's own timeout — by design: cross-process
contention is a different, rarer failure with a different fix.

Semantics notes:
  - Nested same-path acquire from one thread would self-deadlock the queue;
    it also cannot work today (the backend lock would time out against
    itself), so no live code path does it. Not supported.
  - A holder that dies without release wedges neither the file lock (the
    backend stale-break clears it) nor the queue: waiters steal the turn
    after ``hold_stale`` seconds (mirrors the stale-break, counted in
    metrics as ``turns_stolen``).
  - Single-threaded CLI processes pass through uncontended: one Condition
    acquire on an empty queue, microseconds.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

# --- bounds (env-overridable; read once at import) ---------------------------
# Max WAITERS behind the current holder before new arrivals are refused
# loudly. 16 same-path writers queued inside one daemon is pathological —
# better to shed load with a distinct error than build unbounded latency.
MAX_QUEUE_DEPTH = int(os.environ.get("MIND_WRITE_QUEUE_DEPTH", "16") or 16)
# Max seconds a waiter stands in line before failing loud. Sized above the
# worst observed legitimate hold (~22s client-visible during the 2026-07-04
# burst) and well under RT_CURL_TIMEOUT (90s) so the caller gets the
# distinct backpressure error, not a transport timeout.
WAIT_TIMEOUT_S = float(os.environ.get("MIND_WRITE_QUEUE_WAIT_S", "30") or 30)


class WriteQueueBackpressure(RuntimeError):
    """Base class — distinct from the raw lock TimeoutError by design."""


class WriteQueueFullError(WriteQueueBackpressure):
    """Queue depth bound hit: the write was REFUSED before waiting."""


class WriteQueueTimeoutError(WriteQueueBackpressure):
    """Stood in line past the wait bound: the write did NOT run."""


class _PathQueue:
    __slots__ = ("cond", "waiters", "holder", "holder_since",
                 "enqueued", "contended", "timeouts", "rejected_full",
                 "turns_stolen", "max_waiters_seen", "total_wait_ms",
                 "max_wait_ms")

    def __init__(self):
        self.cond = threading.Condition()
        self.waiters = deque()   # arrival-ordered waiter tokens
        self.holder = None       # token of the current turn holder
        self.holder_since = 0.0  # monotonic timestamp of turn grant
        # metrics
        self.enqueued = 0
        self.contended = 0
        self.timeouts = 0
        self.rejected_full = 0
        self.turns_stolen = 0
        self.max_waiters_seen = 0
        self.total_wait_ms = 0.0
        self.max_wait_ms = 0.0


_REGISTRY: dict = {}
_REGISTRY_LOCK = threading.Lock()


def _normalize(path) -> str:
    """One queue per file regardless of path spelling (case-insensitive
    filesystems included — Windows is a primary deployment target)."""
    return os.path.normcase(os.path.abspath(str(path)))


def _queue_for(key: str) -> _PathQueue:
    with _REGISTRY_LOCK:
        q = _REGISTRY.get(key)
        if q is None:
            q = _REGISTRY[key] = _PathQueue()
        return q


def acquire_turn(path, *, max_depth: int = None, wait_timeout: float = None,
                 hold_stale: float = 30.0) -> None:
    """Stand in line for `path`. Returns holding the turn; raises
    WriteQueueFullError / WriteQueueTimeoutError on backpressure."""
    max_depth = MAX_QUEUE_DEPTH if max_depth is None else max_depth
    wait_timeout = WAIT_TIMEOUT_S if wait_timeout is None else wait_timeout
    key = _normalize(path)
    q = _queue_for(key)
    token = object()
    started = time.monotonic()
    with q.cond:
        q.enqueued += 1
        if len(q.waiters) >= max_depth:
            q.rejected_full += 1
            _telemetry("queue_full", key, waiters=len(q.waiters))
            raise WriteQueueFullError(
                f"write-queue full for {key}: {len(q.waiters)} waiters "
                f"(bound {max_depth}) — load-shedding; retry after backoff "
                f"or investigate the slow holder (g-328-28)")
        busy = q.holder is not None or bool(q.waiters)
        if busy:
            q.contended += 1
        q.waiters.append(token)
        q.max_waiters_seen = max(q.max_waiters_seen, len(q.waiters))
        deadline = started + wait_timeout
        while not (q.holder is None and q.waiters[0] is token):
            now = time.monotonic()
            # Stale-holder escape — mirrors the backend lock's stale-break.
            if q.holder is not None and now - q.holder_since > hold_stale:
                q.holder = None
                q.turns_stolen += 1
                _telemetry("turn_stolen", key)
                q.cond.notify_all()
                continue
            remaining = deadline - now
            if remaining <= 0:
                q.waiters.remove(token)
                q.timeouts += 1
                q.cond.notify_all()
                _telemetry("queue_timeout", key,
                           waited_s=round(now - started, 3),
                           waiters=len(q.waiters))
                raise WriteQueueTimeoutError(
                    f"write-queue wait exceeded {wait_timeout}s for {key} "
                    f"({len(q.waiters)} still queued) — the write did NOT "
                    f"run; safe to retry after backoff (g-328-28)")
            # Bounded wait tick so the stale-holder check re-runs even if a
            # notify is lost to a crashed holder.
            q.cond.wait(min(remaining, 1.0))
        q.waiters.popleft()
        q.holder = token
        q.holder_since = time.monotonic()
        if busy:
            waited_ms = (time.monotonic() - started) * 1000.0
            q.total_wait_ms += waited_ms
            q.max_wait_ms = max(q.max_wait_ms, waited_ms)


def release_turn(path) -> None:
    """Release the turn for `path`. Keyed by path (not thread) to mirror
    file-lock semantics — release_lock just unlinks. Releasing a turn you
    don't hold simply advances the line (never raises)."""
    key = _normalize(path)
    with _REGISTRY_LOCK:
        q = _REGISTRY.get(key)
    if q is None:
        return
    with q.cond:
        q.holder = None
        q.cond.notify_all()


def metrics_snapshot() -> dict:
    """Aggregate + per-path contention counters. conflict_rate =
    contended/enqueued — THE signal for deciding whether contention
    survived sharding (g-328-27) well enough to justify escalation."""
    with _REGISTRY_LOCK:
        items = list(_REGISTRY.items())
    per_path = []
    tot = {"enqueued": 0, "contended": 0, "timeouts": 0,
           "rejected_full": 0, "turns_stolen": 0}
    for key, q in items:
        with q.cond:
            row = {
                "path": key,
                "enqueued": q.enqueued,
                "contended": q.contended,
                "timeouts": q.timeouts,
                "rejected_full": q.rejected_full,
                "turns_stolen": q.turns_stolen,
                "max_waiters_seen": q.max_waiters_seen,
                "avg_wait_ms": round(q.total_wait_ms / q.contended, 1)
                               if q.contended else 0.0,
                "max_wait_ms": round(q.max_wait_ms, 1),
                "waiting_now": len(q.waiters),
            }
        for k in tot:
            tot[k] += row[k]
        per_path.append(row)
    per_path.sort(key=lambda r: (r["contended"], r["enqueued"]), reverse=True)
    tot["conflict_rate"] = (round(tot["contended"] / tot["enqueued"], 4)
                            if tot["enqueued"] else 0.0)
    tot["paths_tracked"] = len(per_path)
    return {"global": tot, "top_paths": per_path[:20],
            "bounds": {"max_queue_depth": MAX_QUEUE_DEPTH,
                       "wait_timeout_s": WAIT_TIMEOUT_S}}


def _telemetry(kind: str, key: str, **fields) -> None:
    """Best-effort JSONL line for backpressure EVENTS (failures only — the
    rates live in metrics_snapshot). meta/write-queue-telemetry.jsonl is
    machine-local (owncloud_sync excludes it, like its sibling
    file-contention-telemetry.jsonl). Never raises."""
    try:
        from _paths import META_DIR  # lazy — avoids import cycle with _fileops
        if META_DIR is None:
            return
        record = {"timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                  "agent": os.environ.get("MIND_AGENT", "unknown"),
                  "kind": kind, "path": key}
        record.update(fields)
        with open(Path(META_DIR) / "write-queue-telemetry.jsonl", "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        return
