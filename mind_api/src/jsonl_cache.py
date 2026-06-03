"""mtime-based cache for JSONL reads.

Files below `SMALL_FILE_BYTES` skip the cache — direct read is already
microsecond-scale and the cache machinery is pure overhead. For bigger files
(aspirations.jsonl is ~1.7 MB), the cache amortises parse cost across calls.

Cache invariant: cache hit ⇒ file mtime unchanged since the cached read.
On every request we stat the file. If mtime changed, we reload. Concurrent
reloads are serialised per file via a per-file lock; only the first thread
reads from disk, the rest pick up the freshly-parsed list.

The cache does NOT track writes by other processes outside the daemon —
the fallback-to-direct-python path can write while the daemon is reading.
The stat check catches that on the next request.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple


SMALL_FILE_BYTES = 10 * 1024  # below this, skip the cache entirely


class _Entry:
    __slots__ = ("mtime_ns", "size", "data", "lock")

    def __init__(self) -> None:
        self.mtime_ns: int = -1
        self.size: int = -1
        self.data: List[Dict[str, Any]] = []
        self.lock = threading.Lock()


class JsonlCache:
    """Per-file mtime-keyed cache. Thread-safe.

    Designed for read-heavy workloads. The daemon's writes invalidate the
    cache implicitly by updating the file mtime; the next read sees the new
    mtime and reloads.
    """

    def __init__(self) -> None:
        self._entries: Dict[Path, _Entry] = {}
        self._table_lock = threading.Lock()

    def get(self, path: Path) -> List[Dict[str, Any]]:
        """Return a parsed JSONL list for `path`, reloading if mtime changed.

        WARNING: the returned list and its dicts are the SHARED cache copy.
        Do not mutate them (no .append, .sort, dict[k]=v). Wrap in list(...)
        or copy.deepcopy first if you need to modify, or use a list
        comprehension to filter/transform into a new list.

        Returns [] if the file is missing. Skips the cache for small files.
        Parse errors on individual lines are tolerated (log to stderr, drop
        the line) so a single partial-write doesn't kill the read — matches
        aspirations.py's read_jsonl_with_recovery semantics, just without the
        history snapshot recovery (that lives in _fileops.py and is invoked
        on the fallback path).
        """
        # s5 (lodestar own-cloud): materialize from the active backend BEFORE
        # the stat, so the mtime-keyed cache invariant holds under own-cloud.
        # ensure_local() does a TTL-throttled HeadObject and downloads a changed
        # S3 object into the local cache, bumping its mtime so the reload logic
        # below fires (correcting the doc's H3 "no change needed" — nothing else
        # pulls the object in before this stat). Identity/no-op on the default
        # LocalBackend (zero added I/O); function-local import so module-load
        # order can't break it. Covers BOTH the small-file direct read and the
        # cached read since it precedes the size branch.
        from storage_backend import get_backend
        get_backend().ensure_local(path)
        try:
            st = path.stat()
        except FileNotFoundError:
            return []

        if st.st_size < SMALL_FILE_BYTES:
            return self._read_uncached(path)

        with self._table_lock:
            entry = self._entries.get(path)
            if entry is None:
                entry = _Entry()
                self._entries[path] = entry

        # Per-file lock — concurrent reads of the same file serialise, but
        # reads of different files don't block each other.
        with entry.lock:
            if entry.mtime_ns == st.st_mtime_ns and entry.size == st.st_size:
                return entry.data
            entry.data = self._read_uncached(path)
            entry.mtime_ns = st.st_mtime_ns
            entry.size = st.st_size
            return entry.data

    def invalidate(self, path: Path) -> None:
        """Force the next read of `path` to reload from disk."""
        with self._table_lock:
            entry = self._entries.get(path)
        if entry is None:
            return
        with entry.lock:
            entry.mtime_ns = -1
            entry.size = -1

    def clear(self) -> None:
        with self._table_lock:
            self._entries.clear()

    @staticmethod
    def _read_uncached(path: Path) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for lineno, raw in enumerate(f, 1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        items.append(json.loads(raw))
                    except json.JSONDecodeError as e:
                        print(
                            f"[jsonl_cache] {path}:{lineno} parse error: {e}",
                            file=sys.stderr,
                        )
        except FileNotFoundError:
            return []
        return items


_GLOBAL_CACHE = JsonlCache()


def cache() -> JsonlCache:
    """Module-singleton cache. Endpoints use this for read paths."""
    return _GLOBAL_CACHE
