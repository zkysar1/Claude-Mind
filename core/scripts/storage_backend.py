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
# Multi-tenant customer dimension (T-b/T-c, )
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
# Lives HERE (the cloud-SDK-free abstract seam), not in owncloud_backend, so the
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
    # Pure read of the STORE's current content, straight to memory — never
    # mutates the local mirror (no download-into-cache, no fence/cache-stamp
    # updates) and never falls back to local bytes when local and store have
    # both diverged. This is the diagnostic-read primitive: read_* with
    # force_fresh routes through the cache-refresh path, which on a remote
    # backend WRITES the fetched object into the local cache (the rb-3128
    # read-side clobber) and in the both-diverged no_clobber state returns
    # the LOCAL content. Raises FileNotFoundError when absent in the store.
    def read_authoritative_bytes(self, path: PathLike) -> bytes: ...
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

    def read_authoritative_bytes(self, path: PathLike) -> bytes:
        # The local file IS the store — a plain read is already authoritative
        # and mutation-free.
        return Path(path).read_bytes()

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


# Registry key (in core/config/environments/<env-id>.yaml) -> env var.
#
# MUST stay identical to mind_api/src/__main__.py::_REGISTRY_KEY_TO_ENV. The
# daemon cannot import this module at its derivation point — core/scripts
# reaches sys.path only later, inside _start_owncloud_sync_thread (called well
# after _apply_environment_registry) — so the two copies are held in lockstep by
# test_storage_backend_registry_fallback.py::test_mapping_matches_daemon rather
# than by a shared import. Changing one without the other fails that test.
REGISTRY_KEY_TO_ENV = {
    "backend": "STORAGE_BACKEND",
    "bucket": "STORAGE_S3_BUCKET",
    "sessions_table": "STORAGE_DDB_SESSIONS_TABLE",
    "lock_table": "STORAGE_DDB_LOCK_TABLE",
    "region": "AWS_DEFAULT_REGION",
}


def _warn_registry_unresolved(env_id: str, reg_file: Path, why: str) -> None:
    """Announce a registry-derivation miss on stderr ().

    Fail-open stays fail-open — every caller still returns normally. What
    changes is that the miss is no longer SILENT. A config-presence failure
    here is indistinguishable in its effect from a deliberate
    ``STORAGE_BACKEND=local`` pin: both leave the var unset, and
    ``get_backend()``'s ``os.environ.get("STORAGE_BACKEND", "local")`` then
    selects LocalBackend with ``errors=0`` on every downstream surface. That
    is lane A of ``_bootstrap_env_defaults``' docstring, and it has burned
    this fleet twice — the cc-02 gate-firings franken-copy (a month of
    local-tail rot), and the 2026-07-26 flip where 28 of 49 daemon starts came
    up local-only inside one 12-minute restart storm, stranding ~8 encodings.

    Only reachable when ENVIRONMENT_ID is SET (the caller returns earlier in
    legacy N-var mode), so a purely local box never sees this line.
    """
    print(
        f"[storage-backend] WARNING: ENVIRONMENT_ID={env_id!r} is set but the "
        f"storage env could not be derived from {reg_file} ({why}). STORAGE_* "
        "was NOT filled from the registry; the backend now falls back to the "
        "ambient value, which is LocalBackend when STORAGE_BACKEND is unset. "
        "If this box is meant to be own-cloud, writes are landing LOCAL-ONLY.",
        file=sys.stderr,
    )


def _apply_registry_defaults(root: Path) -> None:
    """Derive storage env vars from the environment registry ().

    Closes a daemon-vs-CLI split that made the registry migration unsafe. The
    daemon derives STORAGE_* from ``ENVIRONMENT_ID`` +
    ``core/config/environments/<id>.yaml`` in ``_apply_environment_registry``;
    nothing on the bare-subprocess lane did. So a box configured the
    registry-native way — ENVIRONMENT_ID only, which is exactly what the
    daemon's own DEPRECATION warning instructs ("remove them from .env.local
    and keep ONLY ENVIRONMENT_ID") — got a correct daemon and a CLI that
    silently resolved to LocalBackend. That is lane A of
    ``_bootstrap_env_defaults``' docstring, reached by following the system's
    own migration advice.

    Observed on cc-02: g-115-2158 removed precisely the five registry-derived
    keys on 2026-07-14; every bare subprocess on that box read LocalBackend for
    11 days until the keys were restored by hand on 2026-07-25.

    ``setdefault`` throughout, so an explicit launch-env value still wins —
    same "explicit wins" contract as the ``.env.local`` pass and the daemon's
    own derivation, which keeps the guard-955 ``STORAGE_BACKEND=local``
    test-runner pin authoritative.

    FAIL-OPEN, unlike the daemon's fail-loud counterpart. ``get_backend()`` is
    reached from never-raises callers (``_gate_log.log``); raising here would
    convert a config gap into dropped records. A typo'd ENVIRONMENT_ID is
    already caught loudly at daemon startup, so nothing is silently swallowed
    that is not reported elsewhere.
    """
    env_id = os.environ.get("ENVIRONMENT_ID", "").strip()
    if not env_id:
        return  # legacy N-var mode — registry not in play
    reg_file = root / "core" / "config" / "environments" / f"{env_id}.yaml"
    try:
        if not reg_file.is_file():
            _warn_registry_unresolved(
                env_id, reg_file, "the registry file does not exist")
            return
        import yaml  # lazy — mirrors the daemon's local import
        data = yaml.safe_load(reg_file.read_text(encoding="utf-8")) or {}
    except Exception as e:
        # Absent/unreadable/malformed registry: the daemon raises here, we must
        # not. Guarded INSIDE the function rather than relying on the caller's
        # wrapper, so the fail-open contract holds for every future call site.
        _warn_registry_unresolved(
            env_id, reg_file, f"{type(e).__name__}: {e}")
        return
    if not isinstance(data, dict):
        # Third silent exit, and the one easiest to miss: this file PARSED
        # cleanly, so neither guard above fires — yet nothing is derived.
        _warn_registry_unresolved(
            env_id, reg_file,
            f"it parsed as {type(data).__name__}, not a YAML mapping")
        return
    for reg_key, env_key in REGISTRY_KEY_TO_ENV.items():
        val = data.get(reg_key)
        if val is None or str(val).strip() == "":
            continue  # registry omits this key (a local backend has no bucket)
        os.environ.setdefault(env_key, str(val).strip())
    if not os.environ.get("STORAGE_BACKEND", "").strip():
        # FOURTH silent path — and the only one that fires no `return` at all:
        # control falls off the end of the loop having matched zero keys, so an
        # exit-by-exit audit of this function does not see it. Two inputs reach
        # it, both PROBED: an EMPTY registry (safe_load -> None, which the
        # `or {}` above launders into a valid-looking empty mapping that clears
        # the isinstance guard), and a mapping that simply omits `backend`.
        # Either way STORAGE_BACKEND stays unset and get_backend() selects
        # LocalBackend — the exact class this instrumentation exists to kill.
        #
        # Deliberately a POST-CONDITION ("is the backend still unresolved?") and
        # NOT "did the registry carry a backend key?": when STORAGE_BACKEND was
        # already pinned explicitly — the guard-955 test-runner pin, or any
        # deliberate override — the setdefault above is a CORRECT no-op and this
        # must stay silent. Checking the registry's contents instead would
        # false-fire on every pinned run.
        _warn_registry_unresolved(
            env_id, reg_file, "it supplied no usable `backend` key")


def _bootstrap_env_defaults(root: Optional[Path] = None) -> None:
    """Best-effort env self-resolution for BARE subprocesses ().

    A hook- or shell-spawned Python that reaches ``get_backend()`` without the
    box's sourced environment degrades in one of two silent lanes:

      A. ``STORAGE_BACKEND`` absent  -> LocalBackend -> local-only appends that
         S3 never sees (the cc-02 gate-firings franken-copy: a month of
         local-tail rot behind the authoritative store).
      B. ``STORAGE_BACKEND=own-cloud`` ambient but bucket/roots/creds absent ->
         ``OwnCloudBackend.from_env()`` raises -> never-raises callers
         (``_gate_log.log``) swallow -> the record is DROPPED entirely.

    Both lanes are configuration-presence failures on a box whose canonical
    config already exists at ``PROJECT_ROOT/.env.local``. Fill the gaps from
    that file via ``os.environ.setdefault`` — EXPLICIT env always wins, so the
    guard-955 ``STORAGE_BACKEND=local`` test-runner pin and any deliberate
    override are untouched — then default the governed-root vars from
    ``_paths`` so ``_resolve_root_map()`` can build the world/meta map.

    Best-effort by design: every step is wrapped; a missing .env.local (fresh
    local-only clone) or an unresolvable ``_paths`` leaves env exactly as it
    was and ``get_backend()`` behaves as before. ``from_env()``'s fail-closed
    guards still apply to whatever env results — this fills gaps, it never
    weakens validation. Skipped under pytest (tests monkeypatch env and must
    not inherit production config) unless ``ENV_BOOTSTRAP_ALLOW_PYTEST`` is
    set. The pytest signal is ``pytest in sys.modules``, NOT just
    PYTEST_CURRENT_TEST: the env var is absent during COLLECTION, and a
    collection-time (module-import) ``get_backend()`` call that bootstrapped
    would side-load production bucket/creds into the suite process env — after
    which any test monkeypatching only ``STORAGE_BACKEND=own-cloud`` builds a
    REAL production backend instead of getting from_env()'s expected raise,
    and the cached instance poisons later tests (observed 2026-07-16: one
    leaked backend broke 3 unrelated tests in the full-suite run while the
    baseline was clean). ``root`` parameter is a test seam only.
    """
    if ("pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST")) \
            and not os.environ.get("ENV_BOOTSTRAP_ALLOW_PYTEST"):
        return
    try:
        import re
        env_local = (root or Path(__file__).resolve().parents[2]) / ".env.local"
        if env_local.exists():
            # Real shell/dotenv name class — an uppercase-only class SILENTLY
            # SKIPS a lowercase key rather than erroring, so the var never
            # reaches os.environ and the caller sees an unset value it can see
            # plainly in the file (). setdefault below means widening
            # can only ADD vars the file already declares, never clobber. Second
            # of three copies: see core/scripts/env.py and core/scripts/_paths.py.
            key_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
            for line in env_local.read_text(
                    encoding="utf-8", errors="replace").splitlines():
                m = key_re.match(line.strip())
                if not m:
                    continue
                k, v = m.group(1), m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                os.environ.setdefault(k, v)
    except Exception:
        pass
    try:
        # : fill any storage var .env.local did not supply from the
        # environment registry, so a registry-native box (ENVIRONMENT_ID only)
        # resolves the same backend here as it does in the daemon. Runs AFTER
        # the .env.local pass because that is where ENVIRONMENT_ID comes from.
        _apply_registry_defaults(root or Path(__file__).resolve().parents[2])
    except Exception:
        pass
    try:
        # Governed roots for _resolve_root_map (not in .env.local — resolved
        # per-agent by _paths from local-paths.conf). Lazy import: only when a
        # root var is actually missing, so pure-local processes never pay it.
        if not (os.environ.get("MIND_WORLD") or os.environ.get("WORLD_PATH")) \
                or not (os.environ.get("MIND_META") or os.environ.get("META_PATH")):
            from _paths import META_DIR, WORLD_DIR
            os.environ.setdefault("MIND_WORLD", str(WORLD_DIR))
            os.environ.setdefault("MIND_META", str(META_DIR))
    except Exception:
        pass


_SWALLOWED_BACKEND_ERRORS: set = set()


def note_swallowed_backend_error(op: str, path, exc: BaseException) -> None:
    """Announce a backend failure that a fail-open call site is about to swallow.

    THE SWALLOW IS CORRECT AND STAYS. Ten sites across ``core/scripts`` and
    ``mind_api/src`` wrap ``ensure_local``/``refresh`` in a bare
    ``except Exception: pass`` because the call is a best-effort materialize
    ahead of an ``exists()`` / ``is_file()`` / read gate — and every one of those
    gates guards a WRITE. Crashing there is worse than answering conservatively,
    so none of them may be converted to a raise.

    What was wrong is that the failure was RECORDED NOWHERE. Each of those
    idioms exists to fix one specific own-cloud bug: an S3-only ``world/config``
    overlay read as absent (the g-115-1279 config-404 class), a synced
    team-state re-created and clobbered on a fresh box, a tree-node body read as
    empty so the concept index builds degraded. When the backend is broken the
    ``except`` fires, the site degrades to exactly the local-only answer the
    idiom was written to prevent, and the restored bug is byte-indistinguishable
    from healthy operation. The fix that silenced the symptom is then the thing
    hiding its return.

    CALL-SITE SHAPE — the reporting call is itself wrapped::

        except Exception as e:
            try:  # report, never raise
                from storage_backend import note_swallowed_backend_error
                note_swallowed_backend_error("ensure_local", p, e)
            except Exception:
                pass

    The inner ``try`` is load-bearing, not ceremony. Every one of those sites
    imports ``get_backend`` INSIDE the guarded block, so on the benign "bare
    subprocess without daemon env" path the IMPORT is what failed and this
    helper is unbound too — an unguarded call would raise ``NameError`` and turn
    a fail-open site fail-closed, a strictly worse defect than the silence.
    It also keeps that benign path silent, which is correct: there is no backend
    to have failed.

    Deduplicated on ``(op, exception class)`` for the life of the process.
    ``tree_match.parse_front_matter`` is the per-node reader behind the concept
    index, so an unconditional line would emit one per tree node (~1246 on this
    deployment) and bury its own signal. The first occurrence names a concrete
    path; identical repeats are suppressed, and the suppression is announced so
    a reader never mistakes one line for one failure.

    NEVER RAISES — not on a malformed ``path``, not on an ``exc`` whose
    ``__str__`` throws. (g-306-218)
    """
    try:
        key = (op, type(exc).__name__)
        if key in _SWALLOWED_BACKEND_ERRORS:
            return
        _SWALLOWED_BACKEND_ERRORS.add(key)
        print(
            f"[storage-backend] WARNING: {op}({path}) failed and was SWALLOWED: "
            f"{type(exc).__name__}: {exc}. The caller now falls back to whatever "
            "the local filesystem already held, so a stale or absent local copy "
            "is being treated as the truth. Further identical "
            f"{op}/{type(exc).__name__} failures are suppressed this process.",
            file=sys.stderr,
        )
    except Exception:
        pass


def get_backend() -> StorageBackend:
    """Return the process-wide active storage backend.

    Selected by ``STORAGE_BACKEND`` (``local`` default). ``own-cloud``
    routes to the own-cloud object/record backend (s3). Unknown values raise —
    no silent fallback to local (that would recreate the split-brain the cutover
    exists to remove).
    """
    global _ACTIVE_BACKEND
    if _ACTIVE_BACKEND is None:
        _bootstrap_env_defaults()  # : bare-subprocess env self-heal
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
    # The swallow-diagnostic dedup is per-process, so without this a second test
    # case in the same process would see its warning suppressed by the first and
    # read as "no diagnostic emitted" ().
    _SWALLOWED_BACKEND_ERRORS.clear()
