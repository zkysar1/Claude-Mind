"""Per-file lock manager for the daemon's write path.

Two-layer locking:

  Layer 1 — `threading.Lock` (in-process, fast):
    One Lock per canonical absolute path. Two daemon threads writing to the
    same JSONL file serialise here without ever touching the filesystem.
    Cost: ~1 microsecond per acquire on contention.

  Layer 2 — `<path>.lock` file via _fileops.acquire_lock (cross-process):
    Atomic O_CREAT|O_EXCL on a sibling .lock file. The fallback-to-direct-
    python path uses this EXACT primitive, so daemon and fallback serialise
    correctly. Cost: ~1-10 ms per acquire on OneDrive.

CRITICAL: do not "simplify" by dropping the file lock. The threading.Lock
is an OPTIMISATION; the file lock is the SAFETY mechanism that lets the
daemon and fallback path coexist without corrupting JSONL.

Usage:
    with file_locks.locked(path):
        # path.with_suffix('.lock') is held; threading.Lock for path is held
        # do the read-modify-write
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "core" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Imported once. acquire_lock/release_lock take explicit paths — they do NOT
# reference _paths globals (WORLD_DIR/META_DIR), so importing _fileops here
# is daemon-safe despite _fileops' top-level `from _paths import WORLD_DIR`.
# _rmw_with_conflict_retry is the own-cloud optimistic-concurrency retry wrapper
# (#38) — it calls get_backend().conflict_error internally, so it stays a
# transparent single pass on LocalBackend.
from _fileops import (  # noqa: E402
    acquire_lock,
    release_lock,
    _rmw_with_conflict_retry,
)


class FileLockManager:
    """Manages per-path threading.Lock objects. Thread-safe."""

    def __init__(self) -> None:
        self._locks: Dict[str, threading.Lock] = {}
        self._table_lock = threading.Lock()

    def get(self, path: Path, key: Optional[str] = None) -> threading.Lock:
        """Return the threading.Lock for `path`, creating it if absent.

        Keyed by the canonical absolute path string — different aliases for
        the same file (relative paths, symlinks) MUST resolve to the same
        key, else two threads could write through different Lock objects.

        `key` lets a caller that has ALREADY resolved the path hand the key
        in rather than pay a second `Path.resolve()` — that syscall, not the
        dict work, is what a duplicate costs (measured: resolving twice per
        acquire added 0.0115ms, 18.4% of a local-backend acquire/release).
        It MUST be `str(Path(path).resolve())`; passing anything else splits
        one file across two Lock objects and silently removes the
        serialisation this class exists to provide.
        """
        key = str(Path(path).resolve()) if key is None else key
        # Fast path: hit-without-lock is safe because dict-get of an existing
        # key is atomic in CPython and we never delete entries.
        cached = self._locks.get(key)
        if cached is not None:
            return cached
        with self._table_lock:
            cached = self._locks.get(key)
            if cached is None:
                cached = threading.Lock()
                self._locks[key] = cached
            return cached


_GLOBAL_MANAGER = FileLockManager()


def manager() -> FileLockManager:
    return _GLOBAL_MANAGER


# ─── Write-path hold telemetry () ─────────────────────────────────
# WHY THIS EXISTS: `thread_lock.acquire()` in locked() below is UNBOUNDED. A
# holder that never releases parks every other writer in an indefinite futex
# wait, and NOTHING reported it — GET /v1/admin/health answered 200 in 0.2ms
# throughout a total box-wide WM-write freeze, so mind-api-start.sh's
# idempotent "alive and responsive → exit 0" fast path repaired nothing. The
# daemon was liveness-green and write-dead at the same instant, and the
# liveness probe is structurally incapable of telling the difference because
# it never touches the write path.
#
# These two dicts ARE that missing surface. They record what the lock layer
# already knows and previously threw away: when each hold began, and how many
# threads are blocked behind it.
#
# COST AND WHY IT IS LEGAL ON /health: reading this is process memory only —
# no filesystem, no lock acquisition, and NO STORE ROUND TRIP. That is what
# keeps it compatible with ready.py's standing rule ("DO NOT add store checks
# to /health") and leaves rt_ensure_running's hot path unchanged: it still
# makes exactly one request and that request still touches no store.
#
# NOT a second lock-ordering participant: _TELEMETRY_LOCK is only ever held
# for a few dict ops and is NEVER held across thread_lock.acquire(), the file
# lock, or the yield — so it cannot join a deadlock cycle.
_TELEMETRY_LOCK = threading.Lock()
_HOLDS: Dict[str, float] = {}     # lock key → monotonic time the hold began
_WAITING: Dict[str, int] = {}     # lock key → threads blocked on acquire

# A hold older than this is pathological BY CONSTRUCTION, not by guesswork:
# acquire_lock's own file-lock timeout is 10s (locked()'s `timeout` default)
# and _write_queue steals a turn after 30s (`hold_stale`), so no legitimate
# hold survives a minute. Env-overridable so a test can drive the threshold
# instead of sleeping past it.
WEDGE_SECONDS = float(os.environ.get("MIND_WRITE_PATH_WEDGE_S", "60") or 60)


def write_path_status(wedge_seconds: Optional[float] = None) -> dict:
    """Verdict on whether THIS process's write path is wedged.

    Pure in-process read (see the cost note above). `wedged` is the field a
    consumer branches on; the rest is diagnosis for whoever reads the wedge.

    `wedged` is False when no hold is in flight — an idle daemon is not a
    wedged one, and the fail-safe direction matters here: a false `wedged`
    would restart a daemon that is serving the whole box.
    """
    wedge_seconds = WEDGE_SECONDS if wedge_seconds is None else wedge_seconds
    now = time.monotonic()
    with _TELEMETRY_LOCK:
        holds = list(_HOLDS.items())
        blocked = sum(_WAITING.values())
    longest = 0.0
    longest_path = None
    for key, started in holds:
        age = now - started
        if age > longest:
            longest, longest_path = age, key
    return {
        "wedged": longest > wedge_seconds,
        "longest_hold_s": round(longest, 3),
        "longest_hold_path": longest_path,
        "holds_in_flight": len(holds),
        "blocked_writers": blocked,
        "wedge_threshold_s": wedge_seconds,
    }


@contextlib.contextmanager
def locked(path: Path, *, timeout: int = 10, stale_seconds: int = 30):
    """Acquire both the threading lock AND the file lock for `path`.

    The file lock is `<path>.lock` — the SAME location _fileops uses, so
    daemon writes and fallback-path writes serialise correctly. The
    threading lock is per-process; it short-circuits the file-lock dance
    when concurrent daemon threads target the same path.

    Lock ordering (acquire): thread → file. Release order (reverse):
    file → thread. Holding the thread lock while waiting on the file lock
    is safe because no other path involves both locks in opposite order
    (the file lock has no other in-process acquirers — only out-of-process
    fallback writers).
    """
    path = Path(path)
    # CRITICAL: must match _fileops' lock-path convention EXACTLY.
    # _fileops uses path.with_suffix(".lock") — REPLACES the suffix
    # (aspirations.jsonl → aspirations.lock). If the daemon used a
    # different lock path (e.g., aspirations.jsonl.lock), daemon writes
    # and fallback-direct-python writes would race on the same file.
    # Do not "simplify" this to .suffix + ".lock".
    lock_path = path.with_suffix(".lock")
    # Resolved ONCE and shared with the lock manager: the telemetry names the
    # file a reader would recognise (not whichever alias this caller passed),
    # and the manager keys on the identical string, so instrumenting the lock
    # costs no additional syscall.
    key = str(Path(path).resolve())
    thread_lock = _GLOBAL_MANAGER.get(path, key=key)

    with _TELEMETRY_LOCK:
        _WAITING[key] = _WAITING.get(key, 0) + 1
    try:
        thread_lock.acquire()
    finally:
        # Runs on BOTH paths (acquired, or interrupted while blocked) — a
        # waiter count that only decremented on success would drift upward
        # forever and eventually report a wedge that is not there.
        with _TELEMETRY_LOCK:
            remaining = _WAITING.get(key, 1) - 1
            if remaining > 0:
                _WAITING[key] = remaining
            else:
                _WAITING.pop(key, None)
    with _TELEMETRY_LOCK:
        _HOLDS[key] = time.monotonic()
    try:
        acquire_lock(lock_path, timeout=timeout, stale_seconds=stale_seconds)
        try:
            yield
        finally:
            release_lock(lock_path)
    finally:
        # Cleared BEFORE the lock is released: the window where the hold is
        # recorded must never outlast the window where it is actually held,
        # or an idle daemon reports a phantom hold that ages into a wedge.
        with _TELEMETRY_LOCK:
            _HOLDS.pop(key, None)
        thread_lock.release()


def locked_rmw(path: Path, cycle_fn, *, timeout: int = 10,
               stale_seconds: int = 30):
    """Hold the per-path lock and run a full refresh→read→modify→write cycle,
    retrying on the backend's optimistic-concurrency ConflictError (the
    own-cloud If-Match stale-lock-break race: the DDB lock that normally
    serialises cross-machine writes was force-broken by a crashed/reclaimed
    holder, so a second machine's PUT 412s against the etag this cycle read).

    `cycle_fn` is the handler's ENTIRE in-lock body — read, validate, mutate,
    write, changelog — and returns whatever the handler returns (typically a
    Response). It MUST re-read fresh on every call so each retry re-applies the
    modification on top of the peer's landed write; the daemon JSONL read
    helpers (store._read_jsonl, aspirations_write._read_jsonl) already begin
    with get_backend().refresh(path), satisfying this. A 412 means the PUT was
    rejected and NOTHING landed (see owncloud_backend._put), so re-running the
    cycle cannot double-apply.

    On LocalBackend `conflict_error` is the empty tuple, so the retry wrapper is
    a transparent single pass — byte-for-byte equivalent to a bare
    `with locked(path): return cycle_fn()` with zero added I/O. Drop-in
    replacement for that idiom on the daemon write path (#38)."""
    with locked(path, timeout=timeout, stale_seconds=stale_seconds):
        return _rmw_with_conflict_retry(path, cycle_fn)
