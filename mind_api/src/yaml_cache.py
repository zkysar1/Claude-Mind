"""mtime-based cache for YAML reads.

Mirrors jsonl_cache.py for YAML files. yaml.safe_load is the expensive
operation (~50-100ms on a 270KB tree). Caching it amortises parse cost
across calls.

Cache invariant: cache hit ⇒ file mtime AND size unchanged since the
cached read. On every request we stat the file. If mtime changed, we
reload. Concurrent reloads serialise per file via a per-file lock.

WARNING: get() returns the SHARED cache copy. Do not mutate the
returned dict/list — clone with copy.deepcopy first if you need to
modify. The cache does NOT track writes by other processes outside the
daemon; the fallback-to-direct-python path can write while the daemon
is reading. The stat check catches that on the next request.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError as e:  # pragma: no cover — install-time error
    raise RuntimeError("PyYAML required for yaml_cache") from e


# YAML parse cost grows nonlinearly with size; cache aggressively.
SMALL_FILE_BYTES = 4 * 1024


class _Entry:
    __slots__ = ("mtime_ns", "size", "data", "lock")

    def __init__(self) -> None:
        self.mtime_ns: int = -1
        self.size: int = -1
        self.data: Any = None
        self.lock = threading.Lock()


class YamlCache:
    """Per-file mtime-keyed cache. Thread-safe."""

    def __init__(self) -> None:
        self._entries: Dict[Path, _Entry] = {}
        self._table_lock = threading.Lock()

    def get(self, path: Path) -> Any:
        """Return parsed YAML for `path`, reloading if mtime changed.

        Returns None if the file is missing. Skips the cache for small
        files. See module docstring for the no-mutation contract.
        """
        # s5 (lodestar own-cloud): materialize from the active backend BEFORE
        # the stat (mirrors jsonl_cache.get). ensure_local() pulls a changed S3
        # object into the local cache and bumps its mtime so the reload below
        # fires; identity/no-op on LocalBackend. Function-local import for
        # module-load-order safety. Precedes the small-file branch, so it covers
        # the direct _load() path too.
        from storage_backend import get_backend
        get_backend().ensure_local(path)
        try:
            st = path.stat()
        except FileNotFoundError:
            return None

        if st.st_size < SMALL_FILE_BYTES:
            return self._load(path)

        with self._table_lock:
            entry = self._entries.get(path)
            if entry is None:
                entry = _Entry()
                self._entries[path] = entry

        with entry.lock:
            if entry.mtime_ns == st.st_mtime_ns and entry.size == st.st_size:
                return entry.data
            entry.data = self._load(path)
            entry.mtime_ns = st.st_mtime_ns
            entry.size = st.st_size
            return entry.data

    def invalidate(self, path: Path) -> None:
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
    def _load(path: Path) -> Any:
        # libyaml's C scanner when the wheel ships it, else the pure-Python one
        # — same SafeConstructor, identical objects. Same expression as
        # core/scripts/tree_match._yaml_loader (inlined: this module must not
        # depend on core/scripts import order). Measured 2026-09-03 on the live
        # 1.75 MB tree index: 6.87 s -> 0.82 s per (re)load, and the index is
        # reloaded on every box each time any agent's counting retrieval
        # rewrites it.
        loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader
        try:
            with path.open("r", encoding="utf-8") as f:
                return yaml.load(f, Loader=loader)
        except FileNotFoundError:
            return None


_GLOBAL_CACHE = YamlCache()


def cache() -> YamlCache:
    """Module-singleton cache. Endpoints use this for YAML reads."""
    return _GLOBAL_CACHE
