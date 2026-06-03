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
import sys
import threading
from pathlib import Path
from typing import Dict

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

    def get(self, path: Path) -> threading.Lock:
        """Return the threading.Lock for `path`, creating it if absent.

        Keyed by the canonical absolute path string — different aliases for
        the same file (relative paths, symlinks) MUST resolve to the same
        key, else two threads could write through different Lock objects.
        """
        key = str(Path(path).resolve())
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
    thread_lock = _GLOBAL_MANAGER.get(path)

    thread_lock.acquire()
    try:
        acquire_lock(lock_path, timeout=timeout, stale_seconds=stale_seconds)
        try:
            yield
        finally:
            release_lock(lock_path)
    finally:
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
