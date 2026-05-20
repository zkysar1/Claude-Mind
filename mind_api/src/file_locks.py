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
from _fileops import acquire_lock, release_lock  # noqa: E402


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
