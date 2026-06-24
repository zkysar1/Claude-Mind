"""Pluggable storage backend seam (remote-storage cutover — step s1).

The single low-level I/O + locking + record-store abstraction that ``_fileops``
and the daemon endpoints will route through once the cloud cutover lands. This
module is ADDITIVE in s1: nothing imports it yet. The later steps wire it in:

  - s2: ``_fileops`` low-level ops (``acquire_lock``, ``_atomic_write_with_fallback``,
    raw ``open``) are replaced by calls to the active backend. The transient
    duplication between this file and ``_fileops`` collapses at that point.
  - s3: ``OwnCloudBackend`` (a remote object store for whole-file stores + a
    remote record store for the per-goal aspirations table + a remote lock
    table) and the public-commons HTTP backend seam are added.

Design (see ``mind_api/docs/lodestar-own-cloud-architecture.md`` s0 decisions):

  * The backend takes ABSOLUTE filesystem paths (``str | os.PathLike``) — the
    same path type ``_fileops`` uses today — so the s2 wiring is a drop-in.
    Cloud backends map an absolute path to ``(base_dir, relative key)``
    internally via the same ``resolve_base_dir`` logic ``_fileops`` already has.
  * ``read_text``/``read_bytes`` return content; ``ensure_local`` returns a real
    local ``Path`` (needed by ``retrieve.py``'s module-global swap and the mtime
    caches). Cloud backends implement ``ensure_local`` by materialising the
    object to a local cache file. ``read_*`` carries ``force_fresh`` so a read
    INSIDE a write-lock can bypass a cloud read cache (the "lock acquisition
    implies cache invalidation" invariant that closes the lost-update window).
  * The record-level JSONL methods (``read_jsonl``/``write_jsonl``/
    ``append_jsonl_record``/``modify_jsonl``) are the bridge between whole-file
    backends (Local, a remote object store) and per-record backends (a remote
    record store for the hot aspirations store). ``LocalBackend`` implements
    them via whole-file I/O,
    byte-identical to ``_fileops`` today: ``json.dumps(item, ensure_ascii=True)
    + "\\n"`` per record, written through the same text-mode ``open`` so OS
    newline handling matches exactly.

``LocalBackend`` reimplements the low-level ops self-contained (it does NOT
import ``_fileops``) to avoid an import cycle — ``_fileops`` imports THIS module
in s2. The retry/fallback numbers mirror ``_fileops._atomic_write_with_fallback``
(g-285-03) and the lock algorithm mirrors ``_fileops.acquire_lock``; both are
kept in sync until s2 collapses the duplication. History snapshots, changelog
append, surrogate validation, and JSONL post-write recovery remain orchestration
in ``_fileops`` — the backend is the raw storage layer beneath them.
"""
from __future__ import annotations

import contextvars
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Union, runtime_checkable

PathLike = Union[str, os.PathLike]


# ---------------------------------------------------------------------------
# Multi-tenant customer dimension (T-b/T-c, 1)
# ---------------------------------------------------------------------------
# The unit of isolation/billing is the CUSTOMER (the daemon's ctx.tenant); an
# env-id is a world WITHIN a customer. OwnCloudBackend prepends a leading
# ``<customer>/`` segment to every storage key so one shared bucket/table
# isolates many customers by key-prefix (+ IAM prefix-conditions, owner-gated).
#
# customer == "default" (the single-tenant baseline this deployment runs today)
# yields NO customer segment, so keys are BYTE-IDENTICAL to the legacy
# env-id-only scheme — the live data needs no move (back-compat invariant).
#
# Lives HERE (the -free abstract seam), not in owncloud_backend, so the
# daemon (server.py) can set/reset it per request WITHOUT importing the cloud
# backend — server.py must stay importable on a LocalBackend-only host. Held in
# a ``contextvars.ContextVar`` (NOT a singleton attr) so concurrent requests for
# distinct customers each observe their own value with zero key bleed; contextvars
# propagate across both threads and asyncio tasks. LocalBackend ignores it (one
# local tree); only OwnCloudBackend consults it.
_DEFAULT_CUSTOMER = "default"
_current_customer: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "ayoai_storage_customer", default=_DEFAULT_CUSTOMER)


def set_customer(customer: Optional[str]) -> "contextvars.Token":
    """Set the active customer for the current context (the daemon request
    handler). Returns a token for :func:`reset_customer` in a ``finally``. A
    falsy/blank value resets to the default (single-tenant) baseline. A value
    containing ``/`` is rejected — it would corrupt key-prefix segmentation and
    could escape the customer's IAM-conditioned prefix."""
    c = (customer or _DEFAULT_CUSTOMER).strip().strip("/")
    if not c:
        c = _DEFAULT_CUSTOMER
    if "/" in c:
        raise ValueError(f"customer {customer!r} must not contain '/'")
    return _current_customer.set(c)


def reset_customer(token: "contextvars.Token") -> None:
    """Restore the customer the context held before the matching set_customer."""
    _current_customer.reset(token)


def current_customer() -> str:
    """The customer active in the current context (the default baseline if unset
    — i.e. every CLI invocation and every single-tenant request)."""
    return _current_customer.get()


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

class FileStat:
    """Backend-neutral file metadata.

    ``version`` is the optimistic-concurrency token: ``str(st_mtime_ns)`` for
    the local backend, an object version token (ETag) for a remote backend. ``mtime_ns`` is 0 for
    non-filesystem backends (callers that special-case mtime must tolerate 0).
    """

    __slots__ = ("version", "size", "mtime_ns")

    def __init__(self, version: str, size: int, mtime_ns: int = 0):
        self.version = version
        self.size = size
        self.mtime_ns = mtime_ns

    def __repr__(self) -> str:
        return (f"FileStat(version={self.version!r}, size={self.size}, "
                f"mtime_ns={self.mtime_ns})")


class WriteResult:
    """Result of a write. ``version`` is the post-write concurrency token
    (``str(st_mtime_ns)`` locally). ``fallback_used`` is True when the atomic
    ``os.replace`` path was exhausted and the in-place rewrite fired.
    ``retry_count`` / ``wall_clock_ms`` / ``error_class`` / ``error_msg`` carry
    the contention data ``_fileops`` records as telemetry — it no longer owns
    the retry loop, so the backend reports what happened for it to log."""

    __slots__ = ("version", "fallback_used", "retry_count", "wall_clock_ms",
                 "error_class", "error_msg")

    def __init__(self, version: str = "", fallback_used: bool = False,
                 retry_count: int = 0, wall_clock_ms: int = 0,
                 error_class: Optional[str] = None, error_msg: str = ""):
        self.version = version
        self.fallback_used = fallback_used
        self.retry_count = retry_count
        self.wall_clock_ms = wall_clock_ms
        self.error_class = error_class
        self.error_msg = error_msg

    def __repr__(self) -> str:
        return (f"WriteResult(version={self.version!r}, "
                f"fallback_used={self.fallback_used}, "
                f"retry_count={self.retry_count})")


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class StorageBackend(Protocol):
    """Every backend resolves an absolute filesystem path to its own coordinate
    space. ``name`` identifies the backend for diagnostics and telemetry."""

    name: str

    #: The exception type a write may raise when an optimistic-concurrency
    #: fence (e.g. an S3 If-Match) rejects the PUT because the object moved
    #: since the in-lock read. Callers catch this to re-run the
    #: read-modify-write. Backends that cannot conflict (LocalBackend) set it
    #: to an empty tuple so ``except backend.conflict_error`` matches nothing.
    conflict_error: "type[BaseException] | tuple[type[BaseException], ...]"

    # --- reads -------------------------------------------------------------
    def read_bytes(self, path: PathLike, *, force_fresh: bool = False) -> bytes: ...
    def read_text(self, path: PathLike, encoding: str = "utf-8",
                  *, force_fresh: bool = False) -> str: ...
    def exists(self, path: PathLike) -> bool: ...
    def stat(self, path: PathLike) -> Optional[FileStat]: ...
    def list_dir(self, path: PathLike) -> List[str]: ...
    def ensure_local(self, path: PathLike) -> Path: ...
    # Force the local cache current with the remote source-of-truth WITHOUT
    # reading the content (a true no-op for LocalBackend — the local file IS
    # the truth). Call this after acquiring a lock and before a raw in-lock
    # read, so a read-modify-write starts from the latest remote state. For a
    # remote-only file it materializes the local cache, so the subsequent
    # raw read sees it.
    def refresh(self, path: PathLike) -> None: ...

    # --- writes ------------------------------------------------------------
    def write_text(self, path: PathLike, content: str,
                   encoding: str = "utf-8") -> WriteResult: ...
    def write_bytes(self, path: PathLike, content: bytes) -> WriteResult: ...
    def atomic_write(self, target: PathLike, write_to_handle,
                     *, max_retries: int = 10) -> WriteResult: ...

    # --- locking (operates on the LITERAL lock-file path the caller passes) -
    def acquire_lock(self, lock_path: PathLike, timeout: int = 10,
                     stale_seconds: int = 30) -> None: ...
    def release_lock(self, lock_path: PathLike) -> None: ...

    # --- record-level JSONL (whole-file locally, per-record remotely) ---
    def read_jsonl(self, path: PathLike) -> List[dict]: ...
    def write_jsonl(self, path: PathLike, items: List[dict]) -> WriteResult: ...
    def append_jsonl_record(self, path: PathLike, record: dict) -> WriteResult: ...
    def modify_jsonl(self, path: PathLike,
                     modifier_fn: Callable[[List[dict]], Optional[List[dict]]],
                     *, initial: Optional[List[dict]] = None) -> List[dict]: ...


# ---------------------------------------------------------------------------
# Local filesystem backend
# ---------------------------------------------------------------------------

class LocalBackend:
    """Storage on the local filesystem (disk or a cloud-synced folder). The default backend
    and the only one needed for the 100%-local user — no account, no cloud
    calls. Byte-compatible with ``_fileops`` so s2 can route ``_fileops``
    through it without churning any on-disk file."""

    name = "local"

    #: No optimistic-concurrency fence — a single-filesystem write cannot 412 —
    #: so the conflict type is the empty tuple: ``except backend.conflict_error``
    #: in _fileops' RMW retry then matches nothing and the retry wrapper is a
    #: transparent single pass (zero added I/O on the default local path).
    #: See StorageBackend.conflict_error and OwnCloudBackend.conflict_error.
    conflict_error: tuple = ()

    # --- locking: operates on the LITERAL lock-file path the caller passes,
    #     byte-for-byte identical to the legacy _fileops.acquire_lock /
    #     release_lock it replaces. The lock_path = resource.with_suffix(".lock")
    #     derivation stays in _fileops's callers. A cloud backend maps the same
    #     lock_path onto a remote lock-table key. ----------------------------
    def acquire_lock(self, lock_path: PathLike, timeout: int = 10,
                     stale_seconds: int = 30) -> None:
        lock_path = Path(lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()
        while True:
            try:
                # O_CREAT | O_EXCL is atomic create-if-absent. Never replace
                # with exists()+write (TOCTOU race).
                fd = os.open(str(lock_path),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("utf-8"))
                os.close(fd)
                return
            except (FileExistsError, PermissionError):
                # POSIX surfaces a held lock as FileExistsError; Windows can
                # surface ERROR_SHARING_VIOLATION as PermissionError. Treat
                # both as "held, retry".
                try:
                    if time.time() - lock_path.stat().st_mtime > stale_seconds:
                        lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() - start > timeout:
                    raise TimeoutError(f"Could not acquire lock: {lock_path}")
                time.sleep(0.1)

    def release_lock(self, lock_path: PathLike) -> None:
        Path(lock_path).unlink(missing_ok=True)

    # --- reads -------------------------------------------------------------
    def read_bytes(self, path: PathLike, *, force_fresh: bool = False) -> bytes:
        return Path(path).read_bytes()

    def read_text(self, path: PathLike, encoding: str = "utf-8",
                  *, force_fresh: bool = False) -> str:
        return Path(path).read_text(encoding=encoding)

    def exists(self, path: PathLike) -> bool:
        return Path(path).exists()

    def stat(self, path: PathLike) -> Optional[FileStat]:
        try:
            st = Path(path).stat()
        except FileNotFoundError:
            return None
        return FileStat(version=str(st.st_mtime_ns), size=st.st_size,
                        mtime_ns=st.st_mtime_ns)

    def list_dir(self, path: PathLike) -> List[str]:
        p = Path(path)
        if not p.exists():
            return []
        return sorted(os.listdir(p))

    def ensure_local(self, path: PathLike) -> Path:
        # The local backend's files already ARE local — identity.
        return Path(path)

    def refresh(self, path: PathLike) -> None:
        # No-op: the local file IS the source of truth, so there is nothing to
        # pull. Importantly this reads NOTHING — callers insert refresh() before
        # an in-lock read, and on the local (default) path that must add zero I/O.
        return None

    # --- atomic write: byte-for-byte identical to the legacy
    #     _fileops._atomic_write_with_fallback it replaces (tmp-write +
    #     os.replace with the  retry schedule + jitter + stderr
    #     diagnostics + in-place fallback). Telemetry recording and the
    #     post-write JSONL canary stay in _fileops orchestration; this method
    #     returns the contention DATA (retry_count / wall_clock_ms /
    #     fallback_used / error_*) for _fileops to log. write_to_handle is
    #     called once for the tmp write and again on the fallback rewrite, so
    #     it must be idempotent. Does NOT mkdir the parent — the caller does
    #     (matches _fileops; a missing parent must surface, not be silently
    #     created). ----------------------------------------------------------
    @staticmethod
    def _version(target: Path) -> str:
        try:
            return str(target.stat().st_mtime_ns)
        except OSError:
            return ""

    def _atomic_write(self, target: PathLike, write_to_handle, *,
                      binary: bool = False, encoding: str = "utf-8",
                      max_retries: int = 10) -> WriteResult:
        target = Path(target)
        # Deterministic tmp name — single-writer is guaranteed by the caller's
        # lock, so two writers cannot collide here.
        tmp = Path(str(target) + ".tmp")
        mode = "wb" if binary else "w"
        open_kw = {} if binary else {"encoding": encoding}
        try:
            with open(tmp, mode, **open_kw) as f:
                write_to_handle(f)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

        last_err = None
        retry_start_ms = time.monotonic() * 1000.0
        for attempt in range(max_retries):
            try:
                os.replace(str(tmp), str(target))
                return WriteResult(
                    version=self._version(target),
                    fallback_used=False,
                    retry_count=attempt,
                    wall_clock_ms=int(time.monotonic() * 1000.0 - retry_start_ms),
                    error_class=(type(last_err).__name__ if last_err else None),
                    error_msg=(str(last_err) if last_err else ""),
                )
            except (PermissionError, OSError) as e:
                last_err = e
                if attempt == max_retries - 1:
                    break
                wait = min(0.05 * (2 ** attempt) + random.uniform(0, 0.1), 5.0)
                print(f"_atomic_write retry {attempt + 1}/{max_retries}: {e} "
                      f"(waiting {wait:.2f}s) target={target.name}",
                      file=sys.stderr)
                time.sleep(wait)

        # Retries exhausted — in-place truncate-rewrite. A cloud-synced folder's
        # reparse point tolerates write-through but can refuse rename; that is
        # the whole reason this fallback exists. Crash-atomicity is sacrificed;
        # the caller's lock + the post-write JSONL canary in _fileops cover it.
        print(f"_atomic_write FALLBACK to in-place rewrite after "
              f"{max_retries} replace attempts: {last_err} target={target.name}",
              file=sys.stderr)
        try:
            with open(target, mode, **open_kw) as live:
                write_to_handle(live)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return WriteResult(
            version=self._version(target),
            fallback_used=True,
            retry_count=max_retries,
            wall_clock_ms=int(time.monotonic() * 1000.0 - retry_start_ms),
            error_class=(type(last_err).__name__ if last_err else None),
            error_msg=str(last_err),
        )

    # --- writes ------------------------------------------------------------
    def atomic_write(self, target: PathLike, write_to_handle, *,
                     max_retries: int = 10) -> WriteResult:
        """Text-mode durable write — the canonical op _fileops routes through.
        ``write_to_handle(handle)`` writes the FULL content. The caller is
        responsible for the parent dir and (where needed) the lock."""
        return self._atomic_write(target, write_to_handle, binary=False,
                                  max_retries=max_retries)

    def write_text(self, path: PathLike, content: str,
                   encoding: str = "utf-8") -> WriteResult:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return self._atomic_write(
            p, lambda h: h.write(content), binary=False, encoding=encoding)

    def write_bytes(self, path: PathLike, content: bytes) -> WriteResult:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return self._atomic_write(p, lambda h: h.write(content), binary=True)

    # --- record-level JSONL (whole-file; byte-identical to _fileops) --------
    def read_jsonl(self, path: PathLike) -> List[dict]:
        """Parse a JSONL file into records. Blank lines skipped. This is the
        common-case read; severe-corruption recovery (.history restore) remains
        in ``_fileops.read_jsonl_with_recovery`` and is layered above the
        backend, not duplicated here."""
        p = Path(path)
        if not p.exists():
            return []
        out: List[dict] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    @staticmethod
    def _jsonl_text(items: List[dict]) -> str:
        # Exact match for _fileops: ensure_ascii=True, newline-terminated.
        return "".join(json.dumps(it, ensure_ascii=True) + "\n" for it in items)

    def write_jsonl(self, path: PathLike, items: List[dict]) -> WriteResult:
        return self.write_text(path, self._jsonl_text(items))

    def append_jsonl_record(self, path: PathLike, record: dict) -> WriteResult:
        # No retry loop — mirrors _fileops.locked_append_jsonl (retrying an
        # append risks a duplicate record). Caller holds the lock.
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
        st = p.stat()
        return WriteResult(version=str(st.st_mtime_ns), fallback_used=False)

    def modify_jsonl(self, path: PathLike,
                     modifier_fn: Callable[[List[dict]], Optional[List[dict]]],
                     *, initial: Optional[List[dict]] = None) -> List[dict]:
        """Whole-file read-modify-write. The CALLER is responsible for holding
        the lock across this call (same contract as ``_fileops`` orchestration);
        the backend does not re-acquire it. ``modifier_fn`` may mutate in place
        and return the list, build a new list, or return None (== the input)."""
        p = Path(path)
        items = self.read_jsonl(p) if p.exists() else (
            list(initial) if initial is not None else [])
        result = modifier_fn(items)
        if result is None:
            result = items
        self.write_jsonl(p, result)
        return result


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_ACTIVE_BACKEND: Optional[StorageBackend] = None


def get_backend() -> StorageBackend:
    """Return the process-wide active storage backend.

    Selected by ``STORAGE_BACKEND`` (``local`` default). ``own-cloud``
    routes to the own-cloud object/record backend (s3). Unknown values raise —
    no silent fallback to local (that would recreate the split-brain the cutover
    exists to remove).
    """
    global _ACTIVE_BACKEND
    if _ACTIVE_BACKEND is None:
        kind = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
        if kind in ("", "local", "local-files"):
            _ACTIVE_BACKEND = LocalBackend()
        elif kind == "own-cloud":
            # Lazy import: keeps this module domain-free and avoids forcing the
            # cloud SDK onto 100%-local users. owncloud_backend lives in
            # core/scripts (Layer-1) so this import does not cross the boundary.
            from owncloud_backend import OwnCloudBackend
            _ACTIVE_BACKEND = OwnCloudBackend.from_env()
        else:
            raise NotImplementedError(
                f"STORAGE_BACKEND={kind!r} is not available yet — "
                "'local' (s1) and 'own-cloud' (s3) are implemented. The "
                "lodestar-hosted commons backend lands later."
            )
    return _ACTIVE_BACKEND


def reset_backend_for_tests() -> None:
    """Clear the cached backend (test isolation only)."""
    global _ACTIVE_BACKEND
    _ACTIVE_BACKEND = None
