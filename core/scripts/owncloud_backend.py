# domain-leak-exempt: cloud backend — boto3 / S3 / DynamoDB client calls are
# functional infrastructure for the own-cloud storage tier (Lodestar cutover s3),
# not a domain leak. The abstract seam (storage_backend.py) stays domain-free;
# THIS is the concrete implementation the seam was built for. Lazily imported by
# storage_backend.get_backend() only when STORAGE_BACKEND=own-cloud, so
# 100%-local users never import boto3.
"""OwnCloudBackend — StorageBackend over S3 (whole-file stores) + DynamoDB
(cross-machine locks + agent-session coordination), for the Lodestar own-cloud
tier (cutover step s3).

Implements the concurrency design verified in
``mind_api/docs/lodestar-own-cloud-architecture.md`` (5 critic fixes + 3
adversary fixes), each mechanism unit-tested against moto in
``core/scripts/tests/test_owncloud_backend.py``:

  Fix #1  DDB lock liveness via the app-level ``ttl < :now`` ConditionExpression
          — NOT DynamoDB TTL deletion (which is garbage-collection only and lags
          up to ~48h). The TTL attribute exists purely for GC.
  Fix #2  ``read_*(force_fresh=True)`` inside a lock bypasses the local cache so
          a read-modify-write never starts from a stale cached value (lost-update).
  Fix #3  Every PUT made while holding a lock carries ``If-Match`` = the ETag
          observed at read time. A broken/expired-lock write whose object moved
          underneath it gets a 412 → ``ConflictError`` (the caller re-runs the RMW).
  Fix #4  Dual-runner prevention: conditional ``UpdateItem`` IDLE→RUNNING.
  Fix #5  agent-state / runner-token live in the DDB sessions table (SYNC tier).
  Fix B2  ``heartbeat_at`` + ``reclaim_if_stale`` lets another machine reclaim a
          crashed runner instead of it sitting RUNNING forever.
  Fix A2  ``If-Match`` rejection raises ``ConflictError`` rather than silently
          dropping the write; ``modifier_fn`` must be append-only / idempotent.

Path → S3 key: a root map of (absolute_local_root, logical_prefix) pairs maps an
absolute governed path to ``s3://<bucket>/<env-id>/<prefix>/<relpath-under-root>``
(D1). The three independent roots are WORLD_PATH→"world", META_PATH→"meta", and
PROJECT_ROOT/agents→"agents". The local file under each root IS the cache (D7:
WORLD_PATH/META_PATH are the local cache root in own-cloud mode), so the
mtime-keyed jsonl/yaml caches and retrieve.py work against it unchanged. A single
``cache_root`` (prefix="") is also accepted, for unit tests that model one
unified cache tree.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional, Union

import boto3
from botocore.config import Config as _BotoConfig
from botocore.exceptions import ClientError

from storage_backend import (
    FileStat, WriteResult,
    # Multi-tenant customer dimension (g-115-1601) — defined in the boto3-free
    # seam so the daemon (server.py) can set/reset without importing this cloud
    # backend; re-exported here so callers already importing owncloud_backend
    # (tests, CLI) reach them unchanged.
    _DEFAULT_CUSTOMER, current_customer, set_customer, reset_customer,
)

PathLike = Union[str, os.PathLike]

# S3/DDB error codes that mean "object/item absent" across boto3 surfaces.
_NOT_FOUND = {"404", "NoSuchKey", "NotFound", "ResourceNotFoundException"}
_PRECONDITION = {"PreconditionFailed", "412"}
_COND_FAILED = "ConditionalCheckFailedException"


class ConflictError(Exception):
    """An ``If-Match`` conditional PUT was rejected (the object changed since the
    in-lock read). The caller MUST re-run the whole read-modify-write; the
    ``modifier_fn`` must therefore be safe to re-apply (append-only / idempotent)."""


class RunnerHeld(Exception):
    """``acquire_runner`` found the agent already RUNNING (and its heartbeat is
    not stale). The caller becomes an observer or refuses — never a second runner."""


class RunnerClaim(NamedTuple):
    """One ``zds-sessions`` row projected for ownership resolution. The dynamic
    ``_owned_agents()`` resolver (design §3) consumes these by attribute
    (``c.agent`` / ``c.machine_id`` / ``c.agent_state`` / ``c.heartbeat_at``).
    ``heartbeat_at`` is epoch-seconds as ``int`` — 0 when the row was never
    heartbeated (a create-only IDLE row), so the resolver's ``now - heartbeat_at``
    staleness math needs no per-call coercion. ``machine_id`` is ``None`` for a
    never-claimed IDLE row."""
    agent: str
    machine_id: Optional[str]
    agent_state: str
    heartbeat_at: int


class OwnCloudBackend:
    """StorageBackend over S3 + DynamoDB. Constructed explicitly (tests) or via
    :meth:`from_env`. Boto3 clients are injectable for testing (moto)."""

    name = "own-cloud"

    #: The optimistic-concurrency exception this backend raises when an
    #: If-Match PUT is rejected (the object moved since the in-lock read).
    #: _fileops' locked RMW helpers catch THIS — via get_backend().conflict_error
    #: — to drive the G1 re-read->re-apply->re-PUT retry, with zero import
    #: coupling to this concrete module. See storage_backend.StorageBackend.
    conflict_error = ConflictError

    def __init__(self, *, env_id: str, bucket: str, lock_table: str,
                 sessions_table: str, cache_root: PathLike = None,
                 root_map=None,
                 cache_ttl: int = 30, machine_id: str = "unknown",
                 region: str = "us-east-2",
                 runner_stale_seconds: int = 900,
                 aws_access_key_id: str = None, aws_secret_access_key: str = None,
                 s3=None, ddb=None):
        self.env_id = env_id.strip("/")
        self.bucket = bucket
        self.lock_table = lock_table
        self.sessions_table = sessions_table
        # Root map: list of (absolute_local_root, logical_prefix) pairs. An
        # absolute governed path maps to <env-id>/<prefix>/<relpath-under-root>
        # (D1). The three independent external roots (WORLD_PATH->"world",
        # META_PATH->"meta", agents_root->"agents") are NOT nested, so the only
        # ordering that matters is defensive: longest root first. The single
        # ``cache_root`` form (prefix="") is back-compat for the unit tests that
        # model one unified cache tree.
        if root_map is not None:
            roots = [(Path(r), prefix.strip("/")) for r, prefix in root_map]
        elif cache_root is not None:
            roots = [(Path(cache_root), "")]
        else:
            raise ValueError(
                "OwnCloudBackend requires either cache_root or root_map")
        roots.sort(key=lambda rp: len(str(rp[0])), reverse=True)
        self._roots = roots
        self.cache_ttl = cache_ttl
        self.machine_id = machine_id
        self.runner_stale_seconds = runner_stale_seconds
        _cfg = _BotoConfig(retries={"max_attempts": 3, "mode": "standard"})
        # Dedicated scoped creds (the least-privilege Zak_first_test user) when
        # given — kept SEPARATE from the process-wide AWS_* keys, which on this
        # deployment are the root keys used for unrelated lambda access and must
        # NOT be reused for the daemon. When unset, fall back to the default
        # boto3 chain (env AWS_*, shared config, instance role).
        if (s3 is None or ddb is None) and aws_access_key_id and aws_secret_access_key:
            _cred_sess = boto3.Session(aws_access_key_id=aws_access_key_id,
                                       aws_secret_access_key=aws_secret_access_key,
                                       region_name=region)
            _mk = lambda svc: _cred_sess.client(svc, region_name=region, config=_cfg)
        else:
            _mk = lambda svc: boto3.client(svc, region_name=region, config=_cfg)
        self.s3 = s3 if s3 is not None else _mk("s3")
        self.ddb = ddb if ddb is not None else _mk("dynamodb")
        # ETag observed at the most recent read of each key — the If-Match fence
        # token (fix #3). Per-process; the DDB lock serializes RMW on a key, so
        # read-then-write within a held lock is sequential and this is race-free.
        self._etags: dict = {}
        # local-path -> monotonic time of the last HeadObject freshness check.
        self._cache_check: dict = {}

    # --- env wiring --------------------------------------------------------
    @classmethod
    def from_env(cls) -> "OwnCloudBackend":
        """Build from env vars. Required: STORAGE_S3_BUCKET, STORAGE_DDB_LOCK_TABLE,
        STORAGE_DDB_SESSIONS_TABLE, and at least one of MIND_WORLD/WORLD_PATH or
        MIND_META/META_PATH (so a governed path can resolve to a root). Also
        requires the scoped creds MIND_AWS_ACCESS_KEY_ID + MIND_AWS_SECRET_ACCESS_KEY
        UNLESS MIND_AWS_ALLOW_DEFAULT_CHAIN=1 is set (fail-closed — see below).

        The world/meta roots are read from the env vars that ``_paths``/``/start``
        already resolve and export — from_env CONSUMES that resolved output, it
        does NOT re-parse local-paths.conf (that conf->value chain is _paths'
        single responsibility, per .claude/rules/path-resolution.md). The
        daemon-context wiring (routing these through the per-request ctx.paths
        resolver instead of process env) is the s3-integration follow-up; the
        env form here is correct for CLI invocation and is fully test-controllable."""
        missing = [v for v in ("STORAGE_S3_BUCKET", "STORAGE_DDB_LOCK_TABLE",
                               "STORAGE_DDB_SESSIONS_TABLE")
                   if not os.environ.get(v)]
        if missing:
            raise RuntimeError(
                "OwnCloudBackend.from_env: missing required env var(s): "
                + ", ".join(missing))
        # Fail-closed credential resolution (security). On this deployment the
        # process-wide AWS_* keys are the ROOT keys reserved for unrelated lambda
        # access — NOT the scoped daemon role. The default boto3 chain would
        # resolve those, so an UNSET MIND_AWS_* must NOT silently fall back to it:
        # that would write to the cloud with over-privileged root creds, defeating
        # the whole least-privilege scoped-user isolation. Refuse, unless the
        # operator EXPLICITLY opts into the default chain — the legitimate
        # instance-role / ECS task-role case where no static keys exist and the
        # chain resolves a scoped role. communication-clarity.md rule 5: prefer
        # failing visibly over silently falling back to an inconsistent source.
        akid = os.environ.get("MIND_AWS_ACCESS_KEY_ID")
        asec = os.environ.get("MIND_AWS_SECRET_ACCESS_KEY")
        allow_default_chain = os.environ.get(
            "MIND_AWS_ALLOW_DEFAULT_CHAIN", "").strip().lower() in (
                "1", "true", "yes")
        if not (akid and asec) and not allow_default_chain:
            raise RuntimeError(
                "OwnCloudBackend.from_env: MIND_AWS_ACCESS_KEY_ID / "
                "MIND_AWS_SECRET_ACCESS_KEY are not set. Refusing to fall back to "
                "the default boto3 credential chain — on this deployment it "
                "resolves to the process-wide AWS_* keys (reserved for unrelated "
                "lambda access, NOT the scoped daemon role), so a silent fallback "
                "would use over-privileged credentials for cloud writes. Set "
                "MIND_AWS_* to the scoped least-privilege keys, or set "
                "MIND_AWS_ALLOW_DEFAULT_CHAIN=1 to explicitly opt into the default "
                "chain (instance-role / ECS task-role deployments with no static "
                "keys).")
        # G5 fail-closed: a UNIQUE per-machine id is required for the DDB lock to
        # be safe across machines. The lock holder is machine_id:pid:tid (see
        # _holder); two machines both defaulting to "unknown" can produce an
        # IDENTICAL holder (pid+tid can coincide across hosts), so machine B's
        # release_lock ConditionExpression "holder = :me" would MATCH and delete
        # machine A's LIVE lock -> false-release -> concurrent read-modify-write ->
        # data corruption. Refuse rather than run with that hazard (same
        # fail-visible posture as the creds guard above; communication-clarity.md
        # rule 5). One line in .env.local (MACHINE_ID=<hostname>) satisfies it
        # — and the machine-2 bring-up runbook sets it on every machine.
        machine_id = os.environ.get("MACHINE_ID", "").strip()
        if not machine_id or machine_id.lower() == "unknown":
            raise RuntimeError(
                "OwnCloudBackend.from_env: MACHINE_ID is not set (or is "
                "'unknown'). The own-cloud DDB lock holder is machine_id:pid:tid; "
                "two machines both defaulting to 'unknown' can have an identical "
                "holder (pid+tid can coincide across hosts) and false-release each "
                "other's locks -> concurrent read-modify-write -> data corruption. "
                "Set MACHINE_ID to a unique per-machine value (the hostname is "
                "a good default) in .env.local.")
        return cls(
            env_id=os.environ.get("ENVIRONMENT_ID", "ayoai-mind"),
            bucket=os.environ["STORAGE_S3_BUCKET"],
            lock_table=os.environ["STORAGE_DDB_LOCK_TABLE"],
            sessions_table=os.environ["STORAGE_DDB_SESSIONS_TABLE"],
            root_map=cls._resolve_root_map(),
            cache_ttl=int(os.environ.get("OWNCLOUD_CACHE_TTL", "30")),
            machine_id=machine_id,
            region=os.environ.get("AWS_DEFAULT_REGION", "us-east-2"),
            # Scoped least-privilege creds (Zak_first_test), separate from the
            # root AWS_* keys. Both-None is only reached when the operator set
            # MIND_AWS_ALLOW_DEFAULT_CHAIN=1 above -> __init__ default chain.
            aws_access_key_id=akid,
            aws_secret_access_key=asec,
        )

    @staticmethod
    def _resolve_root_map():
        """The three independent governed roots -> their logical prefixes.
        world/meta from env (MIND_* preferred, then *_PATH); agents-root is
        always PROJECT_ROOT/agents (derivable from this file's location, or
        overridable via AGENTS_ROOT for tests)."""
        world = os.environ.get("MIND_WORLD") or os.environ.get("WORLD_PATH")
        meta = os.environ.get("MIND_META") or os.environ.get("META_PATH")
        agents = (os.environ.get("AGENTS_ROOT")
                  or str(Path(__file__).resolve().parents[2] / "agents"))
        if not world and not meta:
            raise RuntimeError(
                "OwnCloudBackend.from_env: neither MIND_WORLD/WORLD_PATH nor "
                "MIND_META/META_PATH is set — cannot map a governed path to a root")
        root_map = []
        if world:
            root_map.append((Path(world), "world"))
        if meta:
            root_map.append((Path(meta), "meta"))
        root_map.append((Path(agents), "agents"))
        return root_map

    # --- key / path mapping ------------------------------------------------
    def _rel(self, path: PathLike) -> str:
        """Map an absolute governed path to its env-scoped logical path
        (e.g. <world-root>/reasoning-bank.jsonl -> "world/reasoning-bank.jsonl").
        Raises if the path is under NO configured root — the old p.name fallback
        aliased distinct locks (world/aspirations.lock and
        agents/<a>/aspirations.lock collapsed to the same DDB key), which would
        serialize or corrupt unrelated writes. A path under no root is a
        misconfiguration, not something to paper over."""
        p = Path(path)
        for root, prefix in self._roots:
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            return f"{prefix}/{rel}" if prefix else rel
        raise ValueError(
            f"{p} is not under any configured root "
            f"({[str(r) for r, _ in self._roots]}) — cannot derive an "
            "env-scoped S3/lock key")

    def _customer_prefix(self) -> str:
        """Leading ``<customer>/`` segment for the active context, or ``""`` for
        the "default" single-tenant baseline (⇒ byte-identical legacy keys). See
        the module-level customer-contextvar block. Read per CALL (not cached on
        the singleton) so a concurrent request for a different customer never
        bleeds into this one."""
        c = current_customer()
        return "" if c == _DEFAULT_CUSTOMER else f"{c}/"

    def _s3_key(self, path: PathLike) -> str:
        return f"{self._customer_prefix()}{self.env_id}/{self._rel(path)}"

    def _lock_key(self, lock_path: PathLike) -> str:
        # The DDB lock key is the customer+env-scoped logical path of the lock
        # file. Acquire and release derive it identically, so the .lock suffix is
        # harmless. The customer prefix isolates locks across tenants.
        return f"{self._customer_prefix()}{self.env_id}/{self._rel(lock_path)}"

    def _holder(self) -> str:
        return f"{self.machine_id}:{os.getpid()}:{threading.get_ident()}"

    def _local(self, path: PathLike) -> Path:
        return Path(path)

    def _machine_local(self, path: PathLike) -> bool:
        """True iff this path is machine-local per owncloud_sync's exclusion
        policy -- the SAME _EXCLUDE_DIRS directory-prune + _is_machine_local
        basename rules the periodic sync-walk applies (owncloud_sync L724 +
        L751). The per-operation backend MUST honor it too: otherwise a per-op
        write/refresh to an excluded path (e.g. jsonl_hygiene truncating
        world/presence/<agent>.jsonl via get_backend()._put under
        STORAGE_BACKEND=own-cloud) reaches S3 even though the walk prunes it,
        diverging from the LocalBackend writer (presence-tick.py) and leaving
        S3 lagging local for disposable per-agent telemetry (g-115-1654 /
        rb-2396). NOTE _is_machine_local does NOT itself test _EXCLUDE_DIRS
        (that is the walk's dirnames prune, not a per-file rule) -- so this
        checks BOTH the directory-segment exclusion AND the basename policy.
        Lazy import mirrors _overwrite_decision (L401): owncloud_sync is a peer
        module imported at call time to avoid an import cycle. Fail-open: a
        path under no configured root, or any owncloud_sync import error,
        returns False (treat as syncable -- the exact pre-fix behavior)."""
        try:
            p = Path(path)
            from owncloud_sync import _is_machine_local, _EXCLUDE_DIRS
            for root, prefix in self._roots:
                try:
                    rel = p.relative_to(root)
                except ValueError:
                    continue
                if any(seg in _EXCLUDE_DIRS for seg in rel.parts[:-1]):
                    return True
                return _is_machine_local(p.name, prefix,
                                         full_path=p, root_path=root)
        except Exception:
            return False
        return False

    # --- reads -------------------------------------------------------------
    def _refresh(self, path: PathLike, force_fresh: bool) -> Path:
        """Ensure the local cache file is current vs S3, returning its path. On a
        fresh-enough cache (within cache_ttl) and not force_fresh, skips the HEAD.
        Records the current ETag in self._etags (the fence token, fix #3)."""
        # g-115-1654: machine-local paths (_EXCLUDE_DIRS / _is_machine_local)
        # are never on S3 -- the local file IS the source of truth. Skip the S3
        # HEAD/GET entirely (mirrors LocalBackend.refresh's no-op), matching the
        # sync-walk's exclusion so the per-op read path shares one policy.
        if self._machine_local(path):
            return self._local(path)
        key = self._s3_key(path)
        local = self._local(path)
        now = time.monotonic()
        if not force_fresh and local.exists():
            last = self._cache_check.get(str(local), 0.0)
            if (now - last) < self.cache_ttl:
                return local  # cache fresh enough; trust it (reads outside locks)
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                self._cache_check[str(local)] = now
                self._etags.pop(key, None)
                return local  # absent remotely (local may also be absent)
            raise
        etag = head["ETag"]
        self._cache_check[str(local)] = now
        if local.exists() and self._etags.get(key) == etag:
            return local  # unchanged since our last download
        # --- No-clobber guard (g-115-1574 / rb-2096) -------------------------
        # self._etags (L151) is in-process and EMPTY after a daemon restart, so
        # the equality check above CANNOT stop the first post-restart refresh
        # from overwriting local with stale S3 -- even when local holds unpushed
        # writes a non-backend writer made during a backend-down window (the
        # g-115-1573 reasoning-bank.jsonl 2020->0 valid_from revert). Gate the
        # overwrite on the PERSISTENT sync-manifest baseline, symmetric to
        # owncloud_sync._pull_one (L781-805), so the read path and the sweep
        # path share ONE clobber-safety semantics.
        if local.exists():
            decision = self._overwrite_decision(path, local, etag)
            if decision == "identical":
                # local already byte-identical to S3; the empty post-restart
                # cache only made it look stale. Adopt the ETag as the fence
                # token and skip the needless re-download.
                self._etags[key] = etag
                return local
            if decision == "no_clobber":
                # local is authoritative (unpushed writes, or an uncomparable
                # multipart S3 ETag). Do NOT overwrite. Leave self._etags
                # untouched so a later _put keeps its existing post-restart push
                # behavior -- this guard is purely additive to the read path and
                # never alters the write path.
                return local
            # decision == "download" -> fall through to the pull below.
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        body = obj["Body"].read()
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(body)
        self._etags[key] = etag
        return local

    def _overwrite_decision(self, path: PathLike, local: Path, etag: str) -> str:
        """Classify whether _refresh may overwrite an EXISTING local file with
        the S3 object at ``etag``, mirroring owncloud_sync._pull_one (L781-805)
        so the read path shares the sweep path's no-clobber semantics
        (g-115-1574 / rb-2096). Returns one of:

          "identical"  local is byte-identical to S3 -> skip the download; the
                       caller adopts the ETag as the fence token.
          "no_clobber" local diverged from the PERSISTENT sync-manifest baseline
                       (unpushed local writes -> local is authoritative), OR the
                       S3 ETag is multipart (uncomparable) -> keep local, do NOT
                       download.
          "download"   safe to pull S3 over local: local == baseline and S3 moved
                       (a peer/other machine wrote), or there is no baseline
                       (S3-authoritative, matching _pull_one's no-baseline branch).

        Fail-open: an unreadable local, or unavailable owncloud_sync helpers,
        degrade to "download" (the pre-fix behavior for that single call) so a
        manifest/import hiccup never wedges a read. _load_manifest is itself
        fail-open (returns {} on any error -> no baseline -> "download")."""
        try:
            local_md5 = hashlib.md5(local.read_bytes()).hexdigest()
        except OSError:
            return "download"  # cannot preserve an unreadable local; S3 recovers
        # Lazy import: keep owncloud_sync (heavy) off the backend's import path
        # and reuse its manifest-format + ETag helpers as the single source of
        # truth (sys.modules-cached after first use; no import cycle because
        # owncloud_sync does not import this module at top level).
        try:
            from owncloud_sync import (_load_manifest, _manifest_entry,
                                       _etag_matches, _etag_is_multipart)
        except Exception:
            return "download"
        if _etag_matches(etag, local_md5):
            return "identical"
        try:
            _mtime, baseline_md5 = _manifest_entry(
                _load_manifest().get(self._rel(path)))
        except Exception:
            baseline_md5 = None
        if baseline_md5 is not None and local_md5 != baseline_md5:
            return "no_clobber"  # unpushed local writes -> local is authoritative
        if _etag_is_multipart(etag):
            return "no_clobber"  # uncomparable S3 ETag -> defer, never clobber
        return "download"

    def read_bytes(self, path: PathLike, *, force_fresh: bool = False) -> bytes:
        local = self._refresh(path, force_fresh)
        return local.read_bytes()  # FileNotFoundError if truly absent (matches local)

    def read_text(self, path: PathLike, encoding: str = "utf-8",
                  *, force_fresh: bool = False) -> str:
        local = self._refresh(path, force_fresh)
        return local.read_text(encoding=encoding)

    def exists(self, path: PathLike) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._s3_key(path))
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                return False
            raise

    def stat(self, path: PathLike) -> Optional[FileStat]:
        try:
            h = self.s3.head_object(Bucket=self.bucket, Key=self._s3_key(path))
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                return None
            raise
        # mtime_ns=0: S3 has no nanosecond mtime; callers that special-case mtime
        # must tolerate 0 (FileStat contract). version is the ETag.
        return FileStat(version=h["ETag"], size=int(h["ContentLength"]), mtime_ns=0)

    def list_dir(self, path: PathLike) -> List[str]:
        prefix = self._s3_key(path)
        if not prefix.endswith("/"):
            prefix += "/"
        expected = self._customer_prefix() + self.env_id + "/"
        assert prefix.startswith(expected), (
            f"list_dir prefix {prefix!r} escapes customer/env scope {expected!r} "
            "— IAM ListBucket is prefix-conditioned on it")
        names = set()
        token = None
        while True:
            kw = dict(Bucket=self.bucket, Prefix=prefix, Delimiter="/")
            if token:
                kw["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kw)
            for c in resp.get("Contents", []):
                names.add(c["Key"][len(prefix):].split("/")[0])
            for cp in resp.get("CommonPrefixes", []):
                names.add(cp["Prefix"][len(prefix):].rstrip("/").split("/")[0])
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break
        names.discard("")
        return sorted(names)

    def ensure_local(self, path: PathLike) -> Path:
        return self._refresh(path, force_fresh=False)

    def refresh(self, path: PathLike) -> None:
        # Pull the latest remote object into the local cache (fix #2): bypasses
        # the cache TTL (force_fresh) and records the current ETag as the
        # If-Match fence token. Materializes a remote-only file locally. Used by
        # _fileops before an in-lock raw read so a read-modify-write starts from
        # the latest remote state, not a stale local cache.
        self._refresh(path, force_fresh=True)

    # --- writes (with the If-Match fence) ----------------------------------
    def _put(self, path: PathLike, body: bytes) -> WriteResult:
        # g-115-1654: machine-local paths (_EXCLUDE_DIRS / _is_machine_local)
        # must NOT be pushed to S3 -- write the local file only, mirroring
        # LocalBackend, so a per-op write (e.g. jsonl_hygiene presence
        # truncation under own-cloud, reached via write_jsonl/append/mirror_put
        # -> _put) shares the LocalBackend writer's backend and S3 never lags
        # local for disposable per-agent telemetry (rb-2396). All writes funnel
        # through _put, so this single guard covers every write path.
        if self._machine_local(path):
            local = self._local(path)
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(body)
            return WriteResult(version=str(local.stat().st_mtime_ns),
                               fallback_used=False)
        key = self._s3_key(path)
        local = self._local(path)
        local.parent.mkdir(parents=True, exist_ok=True)
        kw = dict(Bucket=self.bucket, Key=key, Body=body)
        fence = self._etags.get(key)
        if fence is not None:
            kw["IfMatch"] = fence  # fix #3: only overwrite the version we read
        # G2 (machine-2 gate): the boto3 client carries
        # retries={"max_attempts": 3, "mode": "standard"} (see __init__), so a
        # transient 5xx / throttle / timeout on put_object is ALREADY retried
        # with exponential backoff + jitter at the client layer — a manual loop
        # here would only double-retry. The OTHER half of the §5 G2 concern (a
        # PUT that ultimately fails leaving the local cache ahead of S3, then
        # lost on the next restart when _refresh re-pulls the stale remote) is
        # closed by the write ORDER below: the local cache is written ONLY AFTER
        # put_object succeeds. A 412 (ConflictError) or an exhausted-retry
        # transient failure therefore leaves the local cache byte-identical to
        # the last good S3 version — no local-ahead divergence to lose. (This
        # reverses the prior "local first" order, which seeded exactly that
        # divergence; do NOT move the local write back above the PUT.)
        try:
            r = self.s3.put_object(**kw)
        except ClientError as e:
            if e.response["Error"]["Code"] in _PRECONDITION:
                # fix A2: surface, never silently drop. Caller re-runs the RMW
                # (G1 conflict-retry in _fileops' locked RMW helpers).
                raise ConflictError(
                    f"If-Match failed for {key}: remote changed since the in-lock "
                    "read; re-run the read-modify-write")
            raise
        # PUT succeeded — NOW make the local cache match what S3 holds.
        local.write_bytes(body)
        self._etags[key] = r["ETag"]
        self._cache_check[str(local)] = time.monotonic()
        return WriteResult(version=r["ETag"], fallback_used=False)

    def atomic_write(self, target: PathLike, write_to_handle,
                     *, max_retries: int = 10) -> WriteResult:
        buf = io.StringIO()
        write_to_handle(buf)
        return self._put(target, buf.getvalue().encode("utf-8"))

    def write_text(self, path: PathLike, content: str,
                   encoding: str = "utf-8") -> WriteResult:
        return self._put(path, content.encode(encoding))

    def write_bytes(self, path: PathLike, content: bytes) -> WriteResult:
        return self._put(path, content)

    def mirror_put(self, path: PathLike, content: bytes,
                   *, expected_version: Optional[str] = None) -> WriteResult:
        """Push LOCAL-authoritative bytes to S3 with an optional If-Match fence,
        WITHOUT downloading first — so a locally-newer file is never clobbered by
        the older remote copy. (``read_bytes(force_fresh=True)`` would download
        and overwrite local; that is exactly the wrong move for a local->S3 mirror
        of a file a raw write path persisted locally but never pushed — B15.)

        ``expected_version`` is the ETag from a prior ``stat()`` of the SAME key:
        the PUT is fenced on it (If-Match), so a concurrent backend write that
        moved the object underneath raises ``ConflictError`` and the caller skips
        (the next sweep reconciles). ``None`` => unconditional PUT (the object is
        absent on S3 / brand new). The byte content passed IS the local file's
        own bytes, so ``_put``'s local-cache rewrite is a harmless no-op-equivalent.

        Used by ``core/scripts/owncloud-sync.py`` (the governed-dir mirror sweep)
        and its PostToolUse single-file push. Not on the StorageBackend Protocol:
        it is an own-cloud-only reconciliation primitive; the sweep refuses to run
        under any other backend (no S3 to mirror to)."""
        key = self._s3_key(path)
        if expected_version is not None:
            self._etags[key] = expected_version  # fence on the version we observed
        else:
            self._etags.pop(key, None)            # new object — unconditional PUT
        return self._put(path, content)

    # --- record-level JSONL ------------------------------------------------
    def read_jsonl(self, path: PathLike) -> List[dict]:
        try:
            txt = self.read_text(path)
        except (FileNotFoundError, OSError):
            return []
        return [json.loads(ln) for ln in txt.splitlines() if ln.strip()]

    def _read_jsonl_fresh(self, path: PathLike) -> List[dict]:
        try:
            txt = self.read_text(path, force_fresh=True)  # fix #2
        except (FileNotFoundError, OSError):
            return []
        return [json.loads(ln) for ln in txt.splitlines() if ln.strip()]

    @staticmethod
    def _jsonl_text(items: List[dict]) -> str:
        return "".join(json.dumps(it, ensure_ascii=True) + "\n" for it in items)

    def write_jsonl(self, path: PathLike, items: List[dict]) -> WriteResult:
        return self._put(path, self._jsonl_text(items).encode("utf-8"))

    def append_jsonl_record(self, path: PathLike, record: dict) -> WriteResult:
        # No native append in S3 — read-modify-write. force_fresh so the fence
        # token is the CURRENT remote ETag (avoids a spurious ConflictError from a
        # stale cached read). Caller holds the lock.
        items = self._read_jsonl_fresh(path)
        items.append(record)
        return self.write_jsonl(path, items)

    def modify_jsonl(self, path: PathLike,
                     modifier_fn: Callable[[List[dict]], Optional[List[dict]]],
                     *, initial: Optional[List[dict]] = None) -> List[dict]:
        """Whole-file read-modify-write. The CALLER holds the lock (same contract
        as LocalBackend / _fileops). Reads force_fresh so the If-Match fence uses
        the current remote ETag. On ConflictError the caller re-runs this call."""
        items = self._read_jsonl_fresh(path)
        if not items and initial is not None:
            items = list(initial)
        result = modifier_fn(items)
        if result is None:
            result = items
        self.write_jsonl(path, result)
        return result

    # --- locking (DDB; liveness via the app-level ttl < :now condition) ----
    def acquire_lock(self, lock_path: PathLike, timeout: int = 10,
                     stale_seconds: int = 30) -> None:
        lock_key = self._lock_key(lock_path)
        holder = self._holder()
        start = time.time()
        while True:
            now = int(time.time())
            try:
                self.ddb.put_item(
                    TableName=self.lock_table,
                    Item={"lock_key": {"S": lock_key},
                          "holder": {"S": holder},
                          "acquired_at": {"N": str(now)},
                          "ttl": {"N": str(now + stale_seconds)}},
                    # fix #1: liveness is THIS condition, not DDB TTL deletion.
                    ConditionExpression="attribute_not_exists(lock_key) OR #t < :now",
                    ExpressionAttributeNames={"#t": "ttl"},
                    ExpressionAttributeValues={":now": {"N": str(now)}})
                return
            except ClientError as e:
                if e.response["Error"]["Code"] != _COND_FAILED:
                    raise
                if time.time() - start > timeout:
                    raise TimeoutError(f"Could not acquire lock: {lock_key}")
                time.sleep(0.1)

    def release_lock(self, lock_path: PathLike) -> None:
        lock_key = self._lock_key(lock_path)
        try:
            self.ddb.delete_item(
                TableName=self.lock_table,
                Key={"lock_key": {"S": lock_key}},
                ConditionExpression="holder = :me",
                ExpressionAttributeValues={":me": {"S": self._holder()}})
        except ClientError as e:
            if e.response["Error"]["Code"] != _COND_FAILED:
                raise
            # Someone else holds it now (we were stale-broken mid-work). No-op —
            # same forgiving semantics as LocalBackend.release_lock(missing).

    # --- agent-session coordination (SYNC-DDB tier; dual-runner + heartbeat)
    def _session_key(self, agent_name: str) -> str:
        return f"{self._customer_prefix()}{self.env_id}/{agent_name}"

    def acquire_runner(self, agent_name: str, token: str) -> bool:
        """Conditional IDLE→RUNNING (fix #4). Returns True on success; raises
        RunnerHeld if the agent is already RUNNING with a live heartbeat."""
        skey = self._session_key(agent_name)
        now = int(time.time())
        # Ensure the item exists in IDLE if absent (create-only).
        try:
            self.ddb.put_item(
                TableName=self.sessions_table,
                Item={"session_key": {"S": skey}, "agent_state": {"S": "IDLE"}},
                ConditionExpression="attribute_not_exists(session_key)")
        except ClientError as e:
            if e.response["Error"]["Code"] != _COND_FAILED:
                raise  # already exists is fine
        try:
            self.ddb.update_item(
                TableName=self.sessions_table,
                Key={"session_key": {"S": skey}},
                UpdateExpression=("SET agent_state = :run, runner_token = :tok, "
                                  "heartbeat_at = :hb, machine_id = :mid"),
                ConditionExpression="agent_state = :idle",
                ExpressionAttributeValues={":run": {"S": "RUNNING"},
                                           ":idle": {"S": "IDLE"},
                                           ":tok": {"S": token},
                                           ":hb": {"N": str(now)},
                                           ":mid": {"S": self.machine_id}})
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == _COND_FAILED:
                raise RunnerHeld(f"{agent_name} is already RUNNING")
            raise

    def heartbeat(self, agent_name: str, token: str) -> None:
        """Refresh heartbeat_at; conditional on still owning the runner_token, so
        a reclaimed runner cannot resurrect its heartbeat."""
        self.ddb.update_item(
            TableName=self.sessions_table,
            Key={"session_key": {"S": self._session_key(agent_name)}},
            UpdateExpression="SET heartbeat_at = :hb",
            ConditionExpression="runner_token = :tok",
            ExpressionAttributeValues={":hb": {"N": str(int(time.time()))},
                                       ":tok": {"S": token}})

    def reclaim_if_stale(self, agent_name: str) -> bool:
        """Fix B2: reclaim a crashed runner. Sets RUNNING→IDLE iff the heartbeat is
        older than runner_stale_seconds. Conditional, so a just-woken runner and a
        reclaiming machine cannot both win. Returns True iff reclaimed."""
        cutoff = int(time.time()) - self.runner_stale_seconds
        try:
            self.ddb.update_item(
                TableName=self.sessions_table,
                Key={"session_key": {"S": self._session_key(agent_name)}},
                UpdateExpression="SET agent_state = :idle",
                ConditionExpression="agent_state = :run AND heartbeat_at < :cut",
                ExpressionAttributeValues={":idle": {"S": "IDLE"},
                                           ":run": {"S": "RUNNING"},
                                           ":cut": {"N": str(cutoff)}})
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == _COND_FAILED:
                return False  # not RUNNING, or heartbeat still fresh
            raise

    def get_runner_state(self, agent_name: str) -> Optional[dict]:
        """Read the raw session item (diagnostics / observer decision)."""
        r = self.ddb.get_item(
            TableName=self.sessions_table,
            Key={"session_key": {"S": self._session_key(agent_name)}})
        item = r.get("Item")
        if not item:
            return None
        return {k: (v.get("S") if "S" in v else v.get("N")) for k, v in item.items()}

    def release_runner(self, agent_name: str, token: str) -> bool:
        """Clean RUNNING→IDLE release — the companion to :meth:`acquire_runner`,
        called at ``/stop`` AFTER the final S3 flush (design §4/§6). Transitions
        only if we STILL hold the claim (``runner_token`` matches AND state is
        RUNNING); that token condition is what distinguishes a clean self-release
        from :meth:`reclaim_if_stale` (a PEER breaking a crashed claim).

        Idempotent: on ConditionalCheckFailed (already reclaimed by a peer, or
        already IDLE, or the token is no longer ours) the row is already in the
        desired released state, so we treat it as released and return ``False``
        WITHOUT raising — ``/stop`` must never fail because its claim was already
        gone. Returns ``True`` iff THIS call performed the RUNNING→IDLE
        transition. IAM: an ``UpdateItem`` (state→IDLE), covered by the existing
        ``zds-sessions`` ``UpdateItem`` grant; the row persists at IDLE (NOT a
        ``DeleteItem``)."""
        try:
            self.ddb.update_item(
                TableName=self.sessions_table,
                Key={"session_key": {"S": self._session_key(agent_name)}},
                UpdateExpression="SET agent_state = :idle",
                ConditionExpression="agent_state = :run AND runner_token = :tok",
                ExpressionAttributeValues={":idle": {"S": "IDLE"},
                                           ":run": {"S": "RUNNING"},
                                           ":tok": {"S": token}})
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == _COND_FAILED:
                return False  # already reclaimed/idle — idempotent no-op release
            raise

    def list_runner_claims(self) -> List[RunnerClaim]:
        """Enumerate every runner claim under THIS env-id (one row per agent) for
        the dynamic ownership resolver (design §3). env-id-scoped Scan: the
        ``begins_with(session_key, :p)`` FilterExpression enforces the
        ``<env-id>/`` prefix discipline (mirrors :meth:`list_dir`'s IAM-prefix
        assert), and the code-side prefix recheck is defense-in-depth so a peer
        env's row can never leak into this machine's owned-set. A Scan (not a
        Query — ``session_key`` is the sole partition key, so prefix matching
        cannot go through KeyConditionExpression) is cheap here: the table holds
        one row per agent (≤ ~6 today). Returns a possibly-empty list of
        :class:`RunnerClaim`; rows of every state (IDLE and RUNNING) are returned
        — the §3 resolver does the machine_id / RUNNING / freshness filtering, not
        this primitive."""
        prefix = self._customer_prefix() + self.env_id + "/"
        claims: List[RunnerClaim] = []
        start_key = None
        while True:
            kw = dict(TableName=self.sessions_table,
                      FilterExpression="begins_with(session_key, :p)",
                      ExpressionAttributeValues={":p": {"S": prefix}})
            if start_key:
                kw["ExclusiveStartKey"] = start_key
            resp = self.ddb.scan(**kw)
            for item in resp.get("Items", []):
                skey = item.get("session_key", {}).get("S", "")
                if not skey.startswith(prefix):
                    continue  # defense-in-depth: never leak a peer env's claim
                hb_raw = item.get("heartbeat_at", {}).get("N")
                claims.append(RunnerClaim(
                    agent=skey[len(prefix):],
                    machine_id=item.get("machine_id", {}).get("S"),
                    agent_state=item.get("agent_state", {}).get("S", "IDLE"),
                    heartbeat_at=int(hb_raw) if hb_raw is not None else 0))
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                break
        return claims
