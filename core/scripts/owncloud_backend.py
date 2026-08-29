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
import logging
import os
import random
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional, Union

import boto3
from botocore.config import Config as _BotoConfig
from botocore.exceptions import ClientError, ParamValidationError

from storage_backend import (
    FileStat, WriteResult,
    # Multi-tenant customer dimension (g-115-1601) — defined in the boto3-free
    # seam so the daemon (server.py) can set/reset without importing this cloud
    # backend; re-exported here so callers already importing owncloud_backend
    # (tests, CLI) reach them unchanged.
    _DEFAULT_CUSTOMER, current_customer, set_customer, reset_customer,
)
# g-358-11: transport codec (gzip at rest, decoded into the local mirror). The
# READ side is always on and magic-byte authoritative — a plain object decodes
# to itself, so this is byte-identical to the pre-codec backend until a writer
# is flipped (OWNCLOUD_GZIP_STORES, allowlisted keys only). One implementation
# shared with every raw-boto3 caller; see _owncloud_codec's module docstring.
from _owncloud_codec import (
    decode_response as _codec_decode_response,
    head_plain_md5 as _codec_head_plain_md5,
    content_matches as _codec_content_matches,
    should_encode as _codec_should_encode,
    put_kwargs as _codec_put_kwargs,
)

# g-328-21: module logger for CAS (If-Match compare-and-swap) conflict telemetry.
# Emits the running 409/412 conflict rate when a coordination-store merge-reconcile
# recovers from (or exhausts retries on) a conflict — the durable, always-on
# measurement surface complementing the per-process cas_metrics() accessor.
_LOG = logging.getLogger(__name__)

PathLike = Union[str, os.PathLike]

# g-358-17: APPEND-MOSTLY PLAINTEXT stores eligible for the range-tail delta
# pull. Matched against the env-scoped logical path (`_rel`), prefix-wise.
#
# Scoped to an allowlist rather than tried everywhere because the probe is only
# a WIN where appends dominate: on an in-place-edited store (aspirations,
# reasoning-bank, guardrails, gate-firings — g-358-03 measured all four SHRINK)
# the md5 test fails and we pay one extra small range request before the full
# GET we would have done anyway. Correctness never depends on this list (the
# md5 equality below is an exact proof either way); cost does.
#
# These two are the stores no other g-358 lever can reach. gzip (g-358-11)
# cannot touch world/board/* — it must stay PLAINTEXT until Claude-Mind and
# ZDS-Mind carry the gzip reader, because peers write INTO our board with their
# own checkout's backend (BOARD_PATTERN_DEFERRED). Sharding (g-358-12) is the
# lever for aspirations, not for an append log. Encoded (gz) objects are
# excluded by construction: the tail of a gzip stream is not a suffix of the
# plaintext, so `_codec_head_plain_md5(head) is None` is a REQUIRED guard, not
# a nicety.
#
# g-115-5268 widened this from 3 entries to 6, completing "implement the range
# read for class A": that goal names FIVE class-A (append-only, byte-range
# sound) stores and only TWO of them -- the two board channels, via the
# "world/board/" prefix -- were reachable here. The three added below are the
# remaining named members. Delta computed per guard-2201 against ONE corpus
# snapshot (32,845 rel paths, both roots): OLD matched 20 paths, NEW matches 24,
# REMOVED set EMPTY, and the four newly-matched paths are exactly the intended
# files -- notably NOT `gate-firings-YYYY-MM-DD.jsonl` (date segments) nor
# `gate-firings.spool.jsonl` (machine-local), neither of which starts with a
# listed prefix.
#
# EVIDENCE for the three, measured rather than taken from the goal's class
# table -- that table is explicitly untrustworthy (the goal's own item (2):
# "3 of 5 prior class-A CANDIDATES were measured wrong"). For each, the newest
# 40 S3 versions carry ZERO shrink events, and the local mirror is byte-exact
# against head-object (1.00x). Remote sizes 5.32 MB / 4.19 MB / 1.27 MB.
# The window is the honest limit: 40 versions need not span a retention sweep,
# so this is consistent with append-only and does not PROVE it. It does not
# have to -- correctness lives in the md5 proof below, and a wrong guess here
# costs one small range GET before the full GET we would have done anyway.
#
# gate-firings.jsonl is DELIBERATELY NOT ADDED, and the reasoning is worth
# keeping because it looked like the biggest win on the bill. `_gate_log.py`
# calls it "(legacy, append-only)", which describes its WRITE IDIOM
# (locked_append_jsonl), not its size trajectory; the line above records that
# g-358-03 MEASURED it shrinking. A 40-version window showing no shrink cannot
# overturn a measurement whose event is a periodic retention sweep. Its cost
# case has also decayed: the goal cites 15.6% of GET egress from 2026-08-09
# when the object was ~40 MB, and head-object now reads 3.82 MB.
# g-115-7153 added the thermal store, the single biggest range-tail win on the
# bill: 7.411 GB/24h of version bytes across 686 versions, which is MORE than the
# entire pre-existing allowlist combined (5.59 GB/24h). It was missing for the
# reason guard-1969 names — a hand-maintained enumeration ages behind its
# population — not because anyone assessed and excluded it: it simply POSTDATES
# the class table.
#
# EVIDENCE IS THE DIRECT BYTE-PREFIX TEST, not the size-monotonicity proxy the
# three entries above rest on. Measured 2026-08-29 (alpha, cc-07) with
# s3:GetObjectVersion + Range: the OLDEST retained version (2,289 B,
# 2026-08-14T15:30:15Z) is an EXACT byte-prefix of the NEWEST (29,705,405 B,
# 2026-08-29T07:51:15Z) — md5 1810fe91630177a9e17410d4c1cc99dd on both sides —
# across 9,879 retained versions spanning 15 days with ZERO shrink events. That
# is a strictly stronger proof than the 40-version window above, and it closes
# the honest gap that window left ("40 versions need not span a retention sweep").
#
# NOTE for anyone re-deriving this: the goal recorded the reporting box as DENIED
# version-level object reads, which is why it could only offer size-monotonicity.
# That is FALSE on cc-07 as of 2026-08-29 — get-object --version-id --range
# returns rc=0. Probe the capability before assuming the proxy is all you have.
#
# world/script-evolution.jsonl is TESTED AND EXCLUDED — do not re-derive it.
# It is the cautionary case that justifies insisting on the direct test: ZERO
# shrink events across 3,496 versions / 15 days (so it PASSES size-monotonicity
# and looks exactly like the thermal store), yet the prefix test FAILS. Oldest
# version 9,534,318 B vs the newest's first 9,534,318 B: md5 e3e845d0… vs
# 0852774e…, 83,826 differing bytes. First divergence at 99.08% of the file, and
# the content names the cause — records are edited IN PLACE as
# "status": "awaiting_completion" -> "expired" with expired_at/expired_by ADDED
# to the existing object. An in-place edit that only ADDS fields grows the file
# monotonically, so it mimics an append-only store perfectly under the size proxy
# while being a lifecycle store. Correctness would still have held here (the md5
# equality below is exact either way); what it would have cost is a wasted range
# GET before the full GET on most pulls, since the rewritten region is the tail.
#
# The retrieval trace (2.995 GB/24h) is deliberately NOT considered here: it was
# already classified CLASS B by a prior DIRECT byte-prefix pass, and where that
# instrument disagrees with a size-monotonicity reading, it wins.
#
# MEASURED SAVING (cc-07, 2026-08-29, direct list-object-versions): thermal
# wrote 606 versions / 17.282 GB of version bytes in 24h while the content
# actually appended was 2.322 MB -- 7,443x. This EXCEEDS the 7.4 GB/24h in
# the goal headline because each write costs the FULL current size and the
# object grows monotonically, so the saving GROWS with the file. Zero shrink
# events across all 9,885 listed versions. Changes TRANSFER, not retention.
_RANGE_TAIL_STORES = (
    "world/board/",
    "world/changelog.jsonl",
    "meta/changelog.jsonl",
    "world/productivity-snapshots.jsonl",
    "world/goal-duplication-overrides.jsonl",
    "meta/trigger-firings.jsonl",
    "world/telemetry/zakpod1-thermal.jsonl",
)

# S3/DDB error codes that mean "object/item absent" across boto3 surfaces.
_NOT_FOUND = {"404", "NoSuchKey", "NotFound", "ResourceNotFoundException"}
_PRECONDITION = {"PreconditionFailed", "412"}
_COND_FAILED = "ConditionalCheckFailedException"

# g-328-20: error codes that mean "IAM/permission gap on a governed op" across
# boto3 surfaces (S3 -> AccessDenied; DDB -> AccessDeniedException; EC2-family ->
# UnauthorizedOperation). A governed op that hits one of these MUST fail loud
# (raise OwnCloudPermissionError, below), never fall through to a conservative
# no-op. The 2026-07-04 fleet-wedge (g-328-19): a missing dynamodb:Scan grant let
# list_runner_claims' Scan silently degrade to "owns no agent dirs" for days.
_ACCESS_DENIED = {
    "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
    "NotAuthorized",
}

# gap #5 (g-328-15): both-diverged coordination-store merge. _merge_reconcile_put
# GETs remote, merges with the outgoing local bytes via a commutative handler,
# and PUTs the result fenced on the remote ETag; if S3 moves mid-merge the fenced
# PUT 412s and we re-GET/re-merge. The loop is bounded AND converges because the
# handler is commutative (both machines compute identical merged bytes). See
# core/scripts/coordination_merge.py.
_MERGE_RECONCILE_CAP = 5


def _conflict_backoff(attempt: int) -> float:
    """Capped exponential backoff with FULL jitter between merge-reconcile CAS
    retries (g-328-21). Full jitter — a uniform draw over [0, capped-exponential]
    rather than exponential + a small additive jitter — because this loop is the
    CROSS-MACHINE CAS path: on a hot coordination store (team-state.yaml, written
    every iteration by every agent) multiple machines genuinely 412 in lockstep
    and re-merge together, the thundering-herd that rb-2639's >22min single-writer
    deadlock exemplifies. Full jitter decorrelates the retry wave far better than
    the additive form; the sibling caller-side retry in _fileops._conflict_backoff
    is already lock-serialized (lower contention) so it keeps the modest additive
    jitter. Cap 1.0s; attempt 0 => uniform[0, 0.05]."""
    return random.uniform(0.0, min(0.05 * (2 ** attempt), 1.0))


def _atomic_write_local(local: Path, body: bytes) -> None:
    """Atomically materialize `body` at `local` (same-dir tmp + os.replace).

    Every local-mirror write in this backend MUST route through here, never
    through bare Path.write_bytes — write_bytes opens with O_TRUNC, so a
    concurrent reader in the truncate-to-written window sees an EMPTY or
    PARTIAL file. That window is not hypothetical: it is the mechanism behind
    the g-115-6054 worker fork-WM wipe (a wm set reading the transiently-empty
    file triggered the g-115-748 empty-file self-heal, which rebuilt the LIVE
    working memory from template and destroyed every capture lane) and the
    g-115-3253 mid-run suite-log truncation/NUL class. os.replace is atomic on
    the same filesystem — readers see the old bytes or the new bytes, never
    the window. Same idiom as owncloud_sync._save_manifest.
    """
    local.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=local.name + ".", suffix=".tmp",
                                    dir=str(local.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, local)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _coordination_merge_handler(path):
    """Lazily resolve the commutative merge handler for a coordination store by
    basename, or None. The lazy import keeps coordination_merge (and its yaml
    import) off the backend's hot import path — it loads ONLY when a both-diverged
    write to a registered store actually needs reconciling (mirrors
    _overwrite_decision's lazy owncloud_sync import). Fail-open: any import error
    => None => the caller keeps the safe-freeze-on-conflict behavior."""
    try:
        from coordination_merge import merge_handler_for
        return merge_handler_for(path)
    except Exception:
        return None


# g-001-41: serialize _stamp_manifest_baseline's load+mutate+save. Per-path
# daemon locks do not cover the SHARED manifest, so two in-process backend
# threads stamping DIFFERENT paths could last-writer-wins-drop each other's
# entry. In-process only — the cross-PROCESS sweep collision is already accepted
# by contract (see the _stamp_manifest_baseline docstring). Restored g-115-2179.
_MANIFEST_STAMP_LOCK = threading.Lock()


class NoClaimError(Exception):
    """A write targeted an agent dir this machine does NOT hold the live runner
    claim for (g-115-8028). Structurally different from ConflictError: no retry
    and no refresh can ever succeed from here, because this box is permanently
    behind the claim-holder's advancing version. Raised only when ownership
    provenance is ``live-claims`` — never on the conservative empty-set
    fail-safe, where the box may in fact own the dir and merely failed to read
    the claim table."""


class ConflictError(Exception):
    """An ``If-Match`` conditional PUT was rejected (the object changed since the
    in-lock read). The caller MUST re-run the whole read-modify-write; the
    ``modifier_fn`` must therefore be safe to re-apply (append-only / idempotent)."""


class RunnerHeld(Exception):
    """``acquire_runner`` found the agent already RUNNING (and its heartbeat is
    not stale). The caller becomes an observer or refuses — never a second runner."""


class OwnCloudPermissionError(Exception):
    """A governed DDB/S3 op hit an IAM/permission gap (``AccessDenied`` & family —
    see ``_ACCESS_DENIED``). Raised by :func:`_reraise_access_denied` so a
    permission gap FAILS LOUD with a diagnosable message (the op + the underlying
    AWS error) instead of degrading to a conservative no-op. A DISTINCT type — not
    a bare ``ClientError`` — precisely so a fail-open caller's ``except Exception``
    can re-raise it rather than swallow a real permission gap as a transient error
    (the 2026-07-04 fleet-wedge root cause, g-328-19/g-328-20)."""


def _reraise_access_denied(e: ClientError, op: str) -> None:
    """If ``e`` is an IAM/permission gap (code in ``_ACCESS_DENIED``), raise a
    diagnosable :class:`OwnCloudPermissionError` naming the governed ``op`` and the
    underlying AWS error; otherwise return (the caller's own ``raise`` handles the
    non-permission case). Call this INSIDE a governed op's ``except ClientError``
    block BEFORE that block's own ``raise`` (or when wrapping a previously
    unguarded call), so an AccessDenied surfaces loudly (g-328-20) while every
    other error keeps its existing handling."""
    err = e.response.get("Error", {}) if getattr(e, "response", None) else {}
    if err.get("Code", "") in _ACCESS_DENIED:
        raise OwnCloudPermissionError(
            f"own-cloud governed op {op!r} hit an IAM/permission gap: "
            f"{err.get('Code')} — {err.get('Message', '')}. Fail-loud detection "
            f"(g-328-20), NOT a silent conservative degrade: check the IAM grant "
            f"for this op (e.g. dynamodb:Scan/Query/GetItem/UpdateItem on the "
            f"sessions/lock table, or s3:ListBucket/GetObject on the governed "
            f"prefix)."
        ) from e


def runner_token_fingerprint(token: Optional[str]) -> Optional[str]:
    """Non-reversible change-detection digest of a ``runner_token``.

    THE RAW TOKEN MUST NEVER LEAVE THIS PROCESS, AND THAT IS A SECURITY
    PROPERTY, NOT A STYLE CHOICE (g-306-224). ``runner_token`` is a BEARER
    CREDENTIAL: it is the ``ConditionExpression`` that authorises two mutations
    on someone else's claim — :meth:`OwnCloudBackend.heartbeat`
    (``runner_token = :tok``) and :meth:`OwnCloudBackend.release_runner`
    (``agent_state = :run AND runner_token = :tok``). ``release_runner``'s own
    docstring names the property exactly: "that token condition is what
    distinguishes a clean self-release from :meth:`reclaim_if_stale` (a PEER
    breaking a crashed claim)". So anything holding the token can (a) forge a
    heartbeat for another agent, which defeats ``reclaim_if_stale`` outright —
    a crashed runner would never look stale and could never be reclaimed — and
    (b) release a LIVE claim, forcing a healthy reducer to wind down mid-flight
    with its Bodies' work unmerged. Both are precisely the failures the lease
    exists to prevent, so publishing the token to close a liveness gap would
    defeat the mechanism it is meant to strengthen. (rb-3271 class: a read
    endpoint that returns a credential in its response body.) Independent
    corroboration that the framework already treats this name as sensitive:
    ``_transplant_pack.py`` carries ``runner-token`` in ``_LEAK_NAMES``.

    A consumer that only needs to notice CHANGE does not need the value. The
    fingerprint gives exactly that and nothing else: it is stable while the
    token is, it moves when the token is re-minted, and it is useless as a
    ``ConditionExpression`` value. Truncated SHA-256 over a UUID4 (122 bits of
    entropy) has no feasible preimage, and 64 bits of digest makes a collision
    — which would cost one MISSED wind-down, never a spurious one — negligible
    across a table holding one row per agent.

    Returns ``None`` for a missing/empty token (a never-claimed IDLE row), which
    consumers must read as "unknown", never as "unchanged"."""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class RunnerClaim(NamedTuple):
    """One ``zds-sessions`` row projected for ownership resolution. The dynamic
    ``_owned_agents()`` resolver (design §3) consumes these by attribute
    (``c.agent`` / ``c.machine_id`` / ``c.agent_state`` / ``c.heartbeat_at``).
    ``heartbeat_at`` is epoch-seconds as ``int`` — 0 when the row was never
    heartbeated (a create-only IDLE row), so the resolver's ``now - heartbeat_at``
    staleness math needs no per-call coercion. ``machine_id`` is ``None`` for a
    never-claimed IDLE row.

    ``runner_token_fp`` is the :func:`runner_token_fingerprint` digest, added for
    the worker reducer-liveness poll's same-box-restart detection (g-306-224).
    There is deliberately NO raw-token field on this tuple: the projection is the
    boundary the token must not cross, so making it unrepresentable here means a
    future caller cannot leak it by adding one line to a response dict. Defaulted
    so every existing positional construction stays valid."""
    agent: str
    machine_id: Optional[str]
    agent_state: str
    heartbeat_at: int
    runner_token_fp: Optional[str] = None


# Own-cloud writes use S3 PutObject(IfMatch=<etag>) compare-and-swap (fix #3 in
# _put), an IfMatch-on-PutObject feature that requires botocore >= 1.35. Older
# botocore (e.g. the 1.34.46 that Ubuntu apt ships) rejects the IfMatch param
# CLIENT-SIDE with ParamValidationError, before any network call. Both the init
# preflight and the _put runtime catch surface this ONE actionable message.
_IFMATCH_UPGRADE_MSG = (
    "own-cloud writes require botocore>=1.35 (PutObject IfMatch compare-and-swap). "
    "The installed botocore rejects the IfMatch parameter client-side, so every "
    "own-cloud write would silently fail while reads still look healthy. Run:\n"
    "    pip install -U 'botocore>=1.35' 'boto3>=1.35'\n"
    "then restart the daemon."
)


def _assert_ifmatch_supported() -> None:
    """Startup preflight: fail LOUD (once, at backend init) when the installed
    botocore is too old for PutObject IfMatch, instead of letting every write
    crash cryptically at runtime while reads look healthy (the zeta zakbox1
    bring-up incident). Fail-OPEN only when the botocore model cannot be
    introspected at all — the _put ParamValidationError catch is the runtime
    backstop; a botocore internals change must not brick a working backend."""
    try:
        import botocore.session
        model = botocore.session.get_session().get_service_model("s3")
        members = model.operation_model("PutObject").input_shape.members
    except Exception:
        return  # cannot introspect the model — defer to the _put runtime catch
    if "IfMatch" not in members:
        raise RuntimeError(_IFMATCH_UPGRADE_MSG)


# Cross-machine runner-lease staleness (design §5/§9, guard-594). A RUNNING
# claim whose heartbeat_at is older than this is treated as a CRASHED peer and
# becomes reclaimable (reclaim_if_stale). INVARIANT: this MUST exceed the LOCAL
# liveness threshold, runner_heartbeat.stale_minutes in core/config/
# aspirations.yaml (60 min) — a peer must never break a claim the owner's own
# machine still considers fresh. The DDB heartbeat advances on the SAME
# once-per-iteration heartbeat-tick.sh cadence as the local file mtime, and
# deep LLM iterations legitimately run 30-45+ min between ticks (the reason
# stale_minutes was bumped 30->60 on 2026-05-14, g-115-724). Calibrated
# 2026-07-07 after the bravo dual-runner incident: the original 900s (15 min)
# design placeholder let a /start on one machine stale-break the claim of a
# LIVE runner mid-iteration on another (22-min max-effort turn), producing the
# exact split-brain the lock exists to prevent. 3900 = 60 + 5 min margin,
# mirroring wedge_stale_minutes (65). Env override: OWNERSHIP_STALE_SECONDS
# (parsed in from_env; owncloud_sync._owned_agents honors the same env at
# call time and falls back to the live backend's value). Guarded by
# test_ownership_cutover.py::test_config_invariant_ddb_stale_exceeds_local_heartbeat_stale.
DEFAULT_RUNNER_STALE_SECONDS = 3900


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
    #: g-115-8028 twin of conflict_error — lets the daemon classify the
    #: structural no-claim case by TYPE without importing this module.
    no_claim_error = NoClaimError

    def __init__(self, *, env_id: str, bucket: str, lock_table: str,
                 sessions_table: str, cache_root: PathLike = None,
                 root_map=None,
                 cache_ttl: int = 30, machine_id: str = "unknown",
                 region: str = "us-east-2",
                 runner_stale_seconds: int = DEFAULT_RUNNER_STALE_SECONDS,
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
        # Explicit transport bounds (g-115-5853). Botocore's DEFAULTS are 60s
        # connect / 60s read, so with max_attempts=3 one operation composed to a
        # worst case of 3 x (60+60) = 360s. This was the ONLY unbounded surface
        # in the own-cloud path -- every other layer is already capped (shell
        # client 90s, python client 30s, file lock 10s) -- which is exactly why
        # it survived: anyone auditing for "is there a timeout?" finds one at
        # every layer they look at and concludes the path is bounded.
        #
        # WHY A LOWER read_timeout DOES NOT BREAK LARGE OBJECTS: botocore passes
        # read_timeout to urllib3 as the PER-SOCKET-READ timeout -- the maximum
        # gap BETWEEN bytes -- not a total-transfer deadline. A multi-MB store
        # streams continuously and never approaches it; what the bound actually
        # catches is a connection that has STOPPED delivering. So the real
        # trade-off is stall-detection latency, not object size. (Documented
        # urllib3/botocore semantics, not measured here.)
        #
        # Env-overridable so a box on a slow or high-latency link can raise them
        # without a code change. Defaults hold the per-operation worst case at
        # 3 x (10 + 30) = 120s, down from 360s.
        _conn_to = float(os.environ.get("MIND_S3_CONNECT_TIMEOUT", "10") or 10)
        _read_to = float(os.environ.get("MIND_S3_READ_TIMEOUT", "30") or 30)
        _cfg = _BotoConfig(retries={"max_attempts": 3, "mode": "standard"},
                           connect_timeout=_conn_to,
                           read_timeout=_read_to)
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
        # S3 keys whose LAST _refresh saw the both-diverged state (local holds
        # unpushed writes AND S3 moved -> "no_clobber"). _put consults this to
        # route a REGISTERED coordination store (reasoning-bank.jsonl,
        # team-state.yaml) to _merge_reconcile_put instead of freezing on a
        # stale fence or clobbering the peer on an empty one (gap #5, g-328-15).
        # Set only in _refresh's no_clobber branch; reset on every other verdict
        # so it always reflects the latest refresh — which, in an RMW cycle,
        # immediately precedes the _put that reads it under the same lock.
        self._diverged_keys: set = set()
        # g-328-21: CAS (If-Match compare-and-swap) conflict telemetry. Per-process
        # counters (reset on daemon restart); the durable cross-restart measurement
        # surface is the per-event _LOG line emitted from _merge_reconcile_put.
        #   _cas_writes             = fenced put_object attempts        (denominator)
        #   _cas_conflicts          = 412 PreconditionFailed events     (numerator)
        #   _cas_conflicts_resolved = merge-reconciles that recovered after >=1 conflict
        # cas_metrics() exposes the running 409/412 rate. Invariant: resolved <= conflicts.
        self._cas_writes = 0
        self._cas_conflicts = 0
        self._cas_conflicts_resolved = 0
        # Preflight: fail loud NOW if botocore is too old for PutObject IfMatch,
        # rather than letting every _put crash cryptically at runtime (reads
        # would still work, masking the break). See _assert_ifmatch_supported.
        _assert_ifmatch_supported()

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
        # OWNERSHIP_STALE_SECONDS env override (guard-594 calibration knob).
        # Before 2026-07-07 this env var was documented but only reached the
        # sync-ownership filter (owncloud_sync._owned_agents) — the actual
        # lock-break (reclaim_if_stale) always used the constructor default,
        # so the two consumers could disagree on staleness. Parse it here so
        # ONE value governs both.
        _stale_env = os.environ.get("OWNERSHIP_STALE_SECONDS", "").strip()
        try:
            runner_stale = (int(_stale_env) if _stale_env
                            else DEFAULT_RUNNER_STALE_SECONDS)
        except ValueError:
            runner_stale = DEFAULT_RUNNER_STALE_SECONDS
        return cls(
            env_id=os.environ.get("ENVIRONMENT_ID", "ayoai-mind"),
            bucket=os.environ["STORAGE_S3_BUCKET"],
            lock_table=os.environ["STORAGE_DDB_LOCK_TABLE"],
            sessions_table=os.environ["STORAGE_DDB_SESSIONS_TABLE"],
            root_map=cls._resolve_root_map(),
            cache_ttl=int(os.environ.get("OWNCLOUD_CACHE_TTL", "30")),
            machine_id=machine_id,
            region=os.environ.get("AWS_DEFAULT_REGION", "us-east-2"),
            runner_stale_seconds=runner_stale,
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

    def _assert_not_tempdir_put(self, path: PathLike) -> None:
        """Refuse (fail loud) an own-cloud S3 PUT whose path resolves under a
        tempfile/pytest temp dir -- the UNIVERSAL test-isolation net for
        g-115-1875. _s3_key ignores the local filesystem path (it is
        customer_prefix + env_id + _rel(path)), so a tmp-world PUT collides on
        the PRODUCTION S3 key and truncates the real store (rb-2983/guard-955:
        world/aspirations.jsonl truncated 22 asp -> 1 fixture record on
        2026-07-09, when a subprocess seeded a tmp world but inherited
        STORAGE_BACKEND=own-cloud). Fires INSIDE the backend, below every
        runner, so it catches what conftest's STORAGE_BACKEND=local pin cannot:
        main()-style test files run directly (`python3 test_x.py` -- conftest
        never loads) and the bash aggregator (run-asp-257-suite.sh) that ran the
        truncating test.

        Escape hatch: the pytest conftest (core/scripts/tests/conftest.py) sets
        MIND_ALLOW_TMP_OWNCLOUD_PUT=1 session-wide, so the tripwire is DORMANT
        under pytest -- where every backend is hermetic (LocalBackend, or a
        moto-mocked OwnCloudBackend that never touches real S3). It ARMS for
        NON-pytest runners (main()-style `python3 test_x.py`, the bash
        aggregator) where the conftest never loads and a real own-cloud PUT to a
        tmp world would collide on the production key. The env var's presence IS
        the "hermetic pytest session" signal. Fail-open on an unresolvable path
        -- a resolution error must never block a real write."""
        if os.environ.get("MIND_ALLOW_TMP_OWNCLOUD_PUT") == "1":
            return
        try:
            resolved = Path(path).resolve()
        except Exception:
            return  # unresolvable -> fail-open (never block a legitimate write)
        under_tmp = False
        try:
            resolved.relative_to(Path(tempfile.gettempdir()).resolve())
            under_tmp = True
        except ValueError:
            # pytest tmp factories usually nest under gettempdir, but some CI
            # relocate them (TMPDIR / --basetemp); a 'pytest-' path segment is
            # the backstop marker.
            under_tmp = any(seg.startswith("pytest-") for seg in resolved.parts)
        if under_tmp:
            raise RuntimeError(
                "own-cloud PUT REFUSED (g-115-1875 test-isolation tripwire): "
                f"path {resolved} resolves under a tempfile/pytest temp dir. "
                "_s3_key ignores the local path, so this tmp PUT would collide "
                "on the PRODUCTION S3 key and truncate the real store "
                "(rb-2983/guard-955 -- world/aspirations.jsonl was truncated "
                "22->1 asp on 2026-07-09 this way). A test's world-write code "
                "leaked into own-cloud mode: pin STORAGE_BACKEND=local for the "
                "test/runner (see core/scripts/tests/conftest.py), or set "
                "MIND_ALLOW_TMP_OWNCLOUD_PUT=1 if this is an intentional "
                "own-cloud test against a mocked S3.")

    # --- reads -------------------------------------------------------------
    def _range_tail_pull(self, path: PathLike, key: str, head: dict,
                         local: Path, etag: str) -> Optional[bytes]:
        """g-358-17: try to refresh an APPEND-MOSTLY plaintext object by GETting
        only the bytes past the local mirror's end. Returns the complete new
        body on success, or None to fall back to the full GET.

        WHY THIS IS SAFE, and it is the whole design: the returned bytes are
        accepted ONLY when md5(local_prefix + tail) equals the object's ETag.
        For a single-part plaintext object the ETag IS the content md5, so that
        equality is an EXACT PROOF that the concatenation is byte-identical to
        what a full GET would have returned. Every guard below is therefore a
        COST filter (don't spend a range request that is unlikely to pay), not
        a correctness gate — a wrong guess costs one small range GET and then
        falls back. Do not "strengthen" a guard on correctness grounds; the
        proof does not live in them.

        In particular this does NOT lean on the caller's "download" verdict to
        mean local == baseline. It does not: `_overwrite_decision` also returns
        "download" for a no-baseline first pull and for a multipart ETag (see
        its docstring). The baseline equality is re-established here explicitly.

        RMW SAFETY (guard-2227): the local prefix is read ONCE into memory and
        the SAME bytes are both hashed and written back. Nothing appends to the
        file in place, so a concurrent writer cannot slip between the hash and
        the write — the caller writes the exact buffer that was proven.
        """
        # (1) PLAIN only. An encoded object's ETag digests the COMPRESSED bytes
        # and its tail is not a suffix of the plaintext mirror, so neither the
        # concatenation nor the md5 test is meaningful.
        if _codec_head_plain_md5(head) is not None or head.get("ContentEncoding"):
            return None
        # (2) Single-part only: a multipart ETag ('<hex>-N') is not a content
        # md5, so the proof above is unavailable.
        try:
            from owncloud_sync import (_etag_is_multipart, _load_manifest,
                                       _manifest_entry)
        except Exception:
            return None  # no helpers -> no proof -> full GET (never fail open)
        if _etag_is_multipart(etag):
            return None
        # (3) The object must have GROWN. Equal or shrunk is an in-place edit
        # (or a truncation) and there is no tail to fetch.
        try:
            remote_len = int(head.get("ContentLength", 0))
            local_size = local.stat().st_size
        except (OSError, TypeError, ValueError):
            return None
        if local_size <= 0 or remote_len <= local_size:
            return None
        # (4) Allowlisted append-mostly store (see _RANGE_TAIL_STORES).
        try:
            rel = self._rel(path)
        except Exception:
            return None
        if not any(rel == s or rel.startswith(s) for s in _RANGE_TAIL_STORES):
            return None
        # (5) The mirror must be exactly the last-pulled prefix: local == the
        # persistent manifest baseline. A local that diverged from baseline
        # would already have been caught as "no_clobber" upstream, but the
        # no-baseline first-pull path reaches "download" too, and there the
        # mirror is not proven to be a prefix of anything.
        try:
            local_bytes = local.read_bytes()
        except OSError:
            return None
        try:
            _mtime, baseline_md5 = _manifest_entry(
                _load_manifest().get(rel))
        except Exception:
            return None
        if not baseline_md5:
            return None
        if hashlib.md5(local_bytes).hexdigest() != baseline_md5:
            return None
        # Fetch ONLY the appended bytes.
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key,
                                     Range=f"bytes={local_size}-")
            tail = obj["Body"].read()
        except ClientError:
            return None  # range unsupported/raced -> full GET
        if not tail:
            return None
        candidate = local_bytes + tail
        # THE PROOF. Reuses the production equality helper rather than
        # re-implementing ETag quote-stripping and the multipart rule
        # (guard-4323: validate through the production predicate).
        if _codec_content_matches(etag, None,
                                  hashlib.md5(candidate).hexdigest()):
            _LOG.debug("owncloud range-tail hit: %s +%d bytes (of %d)",
                       rel, len(tail), remote_len)
            return candidate
        return None

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
        # A path under NO configured world/meta/agents root is git-shipped
        # (e.g. core/config/*.yaml) -- never on S3, always present locally on
        # any clone. _s3_key -> _rel raises ValueError for it; there is nothing
        # to fetch and nothing to fence, so ensure_local/refresh is a no-op.
        # This lets a dual-use reader (one code path that reads BOTH a synced
        # world/meta file AND a git-shipped core/config file) call ensure_local
        # unconditionally without guarding the config case -- the keystone that
        # makes the own-cloud read-path helper fixes trivial and un-reintroducible
        # (own-cloud read-path class fix, 2026-07-02). The WRITE path (_put /
        # _lock_key) still raises on out-of-root, by design (lock-key aliasing).
        try:
            key = self._s3_key(path)
        except ValueError:
            return self._local(path)
        # Reset the both-diverged flag; only the no_clobber verdict below re-adds
        # it. Outcomes that re-checked S3 (identical / download / unchanged /
        # absent) genuinely prove the key is not both-diverged right now. The
        # warm-cache early-return below proves NOTHING (no S3 contact), so this
        # discard CAN drop a still-true flag there — benign by redundancy: the
        # flag is a proactive-merge optimization only, and _put's 412 path
        # dispatches _merge_reconcile_put "regardless of _diverged_keys state"
        # (g-115-1741), while unregistered stores keep freeze-on-conflict either
        # way. Verified during the g-115-2385 W2 read-lane enumeration.
        self._diverged_keys.discard(key)
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
        # g-358-11: the plaintext md5 an ENCODED writer recorded in metadata
        # (None for a plain object). For an encoded object the ETag digests the
        # compressed bytes, so byte-identity against the DECODED local mirror
        # must go through this value — see _owncloud_codec.content_matches.
        remote_plain_md5 = _codec_head_plain_md5(head)
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
            decision = self._overwrite_decision(path, local, etag,
                                                remote_plain_md5=remote_plain_md5)
            if decision == "identical":
                # local already byte-identical to S3; the empty post-restart
                # cache only made it look stale. Adopt the ETag as the fence
                # token and skip the needless re-download. Byte-identity proves
                # any local delta vs the old baseline is already ON S3 --
                # nothing unpushed to mask, so re-stamp the baseline too.
                self._etags[key] = etag
                self._stamp_manifest_baseline(path, local.read_bytes(), etag=etag)
                return local
            if decision == "no_clobber":
                # local is authoritative: it holds unpushed writes (local != the
                # persistent manifest baseline) AND S3 moved (a peer wrote) --
                # the both-diverged state. Do NOT overwrite local here. Flag the
                # key so a following _put can reconcile: for a REGISTERED
                # coordination store (reasoning-bank.jsonl, team-state.yaml)
                # _merge_reconcile_put MERGES local+remote (gap #5); for any
                # OTHER file _put leaves self._etags untouched so a stale
                # IfMatch 412s and the RMW conflict-retries -- the
                # freeze-on-genuine-conflict that protects the unpushed write
                # from a silent clobber. (Multipart ETags reach "no_clobber"
                # ONLY via this same unpushed-writes gate now; multipart-with-
                # local==baseline returns "download" so its fence refreshes --
                # see _overwrite_decision, 2026-07-02 freeze fix.)
                self._diverged_keys.add(key)
                return local
            # decision == "download" -> fall through to the pull below.
            # g-358-17: for an append-mostly PLAINTEXT store, try fetching only
            # the bytes past the mirror's end first. Deliberately placed INSIDE
            # the local.exists() arm and AFTER the no-clobber verdict: the tail
            # path needs a local prefix to extend, and it must never pre-empt
            # the data-protection decision above. Returns None whenever the
            # md5 proof is unavailable, costing at most one small range GET
            # before the full GET below runs exactly as it always has.
            tail_body = self._range_tail_pull(path, key, head, local, etag)
            if tail_body is not None:
                _atomic_write_local(local, tail_body)
                self._etags[key] = etag
                self._stamp_manifest_baseline(path, tail_body, etag=etag)
                return local
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        # g-358-11: the mirror holds DECODED bytes — every consumer above the
        # backend keeps reading plaintext, whatever the object's encoding.
        body = _codec_decode_response(obj, key=key)
        _atomic_write_local(local, body)
        self._etags[key] = etag
        # Reaching this line requires verdict=="download" or local-absent --
        # both mean S3 is authoritative and the mirror now byte-equals S3
        # (as plaintext), which is precisely the state the baseline records.
        self._stamp_manifest_baseline(path, body, etag=etag)
        return local

    def _stamp_manifest_baseline(self, path: PathLike, body: bytes,
                                 etag: Optional[str] = None) -> None:
        """Stamp the persistent sync-manifest baseline for a just-pushed key
        (g-115-1946 — root fix for the cross-box lost-update lanes).

        `etag` (g-358-11) records the S3 ETag the baseline was reconciled
        against. `md5` stays the PLAINTEXT md5 (it is compared against local
        bytes to detect unpushed writes); for a transport-encoded object the
        ETag no longer equals that md5, so the sync layer's LIST pre-filter
        needs the last-seen ETag to tell "S3 unchanged" without a HEAD.

        _put/_merge success previously advanced only the IN-PROCESS fence
        (self._etags); the PERSISTENT baseline was stamped only by the periodic
        owncloud_sync sweep (default 120s), so every post-write window falsely
        presented as "unpushed local writes" to _overwrite_decision's no_clobber
        gate and to the sweep's authority ladder — the state that let concurrent
        boxes read stale local inside the write lock, mint duplicate goal ids,
        and clobber each other. Stamping {mtime, md5} here makes
        local == baseline == S3 immediately after every backend write.

        Fail-open by contract: a stamp failure WARNs and never fails the PUT
        (the stale-baseline window then simply persists until the next sweep —
        exactly the pre-fix behavior). Concurrency: _save_manifest is atomic
        (tmp + os.replace) and the manifest is a machine-local skip-cache, never
        the SSOT; a concurrent sweep save can drop this stamp (whole-file
        last-writer-wins), which only re-opens the window until that sweep's own
        stamp — never worse than pre-fix. Two in-process backend threads
        stamping DIFFERENT paths are serialized by _MANIFEST_STAMP_LOCK
        (g-001-41) around the load+mutate+save, so a same-process interleave can
        no longer drop a peer thread's baseline; only the cross-PROCESS sweep
        collision remains accepted. Lazy import mirrors _overwrite_decision
        (keeps owncloud_sync off the backend's import path).

        RESTORED 2026-07-14 (g-115-2179). Deleted as collateral by the c5814933
        origin-checkout — see stranded-checkout-check.sh."""
        try:
            from owncloud_sync import _load_manifest, _save_manifest
            local = self._local(path)
            # g-001-41: serialize load+mutate+save so a concurrent backend
            # thread stamping a DIFFERENT path cannot last-writer-wins-drop
            # this entry (per-path daemon locks do not cover the shared
            # manifest). In-process lock; see _MANIFEST_STAMP_LOCK above.
            with _MANIFEST_STAMP_LOCK:
                m = _load_manifest()
                entry = {
                    "mtime": local.stat().st_mtime_ns,
                    "md5": hashlib.md5(body).hexdigest(),
                }
                if etag:
                    entry["etag"] = str(etag).strip('"')
                m[self._rel(path)] = entry
                _save_manifest(m)
        except Exception as e:  # noqa: BLE001 — fail-open by contract
            _LOG.warning(
                "manifest baseline stamp failed for %s: %s (stale-baseline "
                "window persists until next sweep)", path, e)

    def _overwrite_decision(self, path: PathLike, local: Path, etag: str,
                            remote_plain_md5: Optional[str] = None) -> str:
        """Classify whether _refresh may overwrite an EXISTING local file with
        the S3 object at ``etag``, mirroring owncloud_sync._pull_one (L781-805)
        so the read path shares the sweep path's no-clobber semantics
        (g-115-1574 / rb-2096). ``remote_plain_md5`` (g-358-11) is the
        writer-recorded plaintext md5 of a transport-ENCODED object (None for a
        plain one): the "identical" test compares the decoded local mirror
        against it, because an encoded object's ETag digests the compressed
        bytes and can never equal a plaintext md5. Returns one of:

          "identical"  local is byte-identical to S3 -> skip the download; the
                       caller adopts the ETag as the fence token.
          "no_clobber" local MAY hold unpushed writes -> keep local, do NOT
                       download. Two cases: (a) local diverged from the PERSISTENT
                       sync-manifest baseline (local is authoritative), or (b)
                       g-115-2178 FAIL-CLOSED -- the baseline could not be READ
                       (owncloud_sync import raised, or the manifest read raised),
                       so local == baseline cannot be verified and a download risks
                       PERMANENT loss of an unpushed-not-yet-committed record. Both
                       surfaced loudly via _LOG.warning; the caller (L637) flags the
                       key diverged so a following _put reconciles.
          "download"   safe (or freeze-avoiding) to pull S3 over local: local ==
                       baseline and S3 moved (a peer/other machine wrote), there is
                       no manifest baseline (ABSENT, not a read failure -- the
                       DELIBERATE S3-authoritative first-pull policy, matching
                       _pull_one's no-baseline branch and preserving force_fresh
                       cache-coherence; the backend's own write path does NOT
                       populate owncloud_sync's persistent manifest, so absence is
                       the common pre-sweep state), OR the S3 ETag is multipart
                       (uncomparable by md5 -- for the large S3-authoritative shared
                       stores a fleet-wide freeze is worse than a rare stale-keep,
                       so a multipart object pulls to refresh the fence even without
                       a baseline; 2026-07-02 fix, without which IfMatch froze).

        FAIL-CLOSED (g-115-2178, rb-3422): a data-protection guard whose failure
        mode is OVERWRITE is not a guard. When the baseline cannot be READ (import
        or manifest-read EXCEPTION), this returns "no_clobber" + logs loudly rather
        than silently downloading over a possibly-unpushed local (the write ->
        first-commit exposure window: a record clobbered before its first git
        commit has NO trace in git, S3, or .history). SCOPE: only a baseline-read
        FAILURE (exception) fails closed; a baseline that is merely ABSENT (no
        manifest entry) is NOT a failure -- it is the tested S3-authoritative
        first-pull path above (over-failing THAT closed broke peer-update
        visibility -- test_refresh_no_baseline_pulls_s3_authoritative). The other
        deliberate fail-OPENs: an UNREADABLE local (OSError) -> "download" (a local
        we cannot read cannot be preserved; S3 is the only recovery source), and
        multipart (freeze-avoidance) -> "download"."""
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
                                       _etag_is_multipart)
        except Exception:
            # (g-115-2178) FAIL CLOSED: without the manifest/ETag helpers we
            # cannot verify local == baseline, so we cannot prove an S3 pull is
            # safe. Overwriting an unverifiable local can PERMANENTLY lose an
            # unpushed-not-yet-committed record (rb-3422). Keep local + surface
            # loudly; the caller (L637) flags the key diverged so _put reconciles.
            _LOG.warning(
                "owncloud _overwrite_decision FAIL-CLOSED: owncloud_sync import "
                "failed for %s -- keeping local, refusing S3 overwrite "
                "(g-115-2178/rb-3422)", self._rel(path))
            return "no_clobber"
        # g-358-11: plaintext-md5 metadata wins when present (encoded object);
        # otherwise the classic single-part ETag == md5 rule (plain object).
        if _codec_content_matches(etag, remote_plain_md5, local_md5):
            return "identical"
        # baseline_read_failed distinguishes a baseline-read EXCEPTION (the
        # manifest read raised -> NO trustworthy baseline -> FAIL CLOSED below)
        # from a baseline that is simply ABSENT (no manifest entry -> the
        # DELIBERATE S3-authoritative first-pull policy). Only the former is a
        # "read failure" (g-115-2178); absence is the common pre-sweep state
        # (the backend's own write path does not populate owncloud_sync's
        # persistent manifest) and MUST stay "download" -- failing it closed
        # broke force_fresh peer-update visibility
        # (test_refresh_no_baseline_pulls_s3_authoritative).
        baseline_read_failed = False
        try:
            _mtime, baseline_md5 = _manifest_entry(
                _load_manifest().get(self._rel(path)))
        except Exception:
            baseline_md5 = None
            baseline_read_failed = True  # route 2: the manifest read RAISED
        if baseline_md5 is not None and local_md5 != baseline_md5:
            return "no_clobber"  # unpushed local writes -> local is authoritative
        if _etag_is_multipart(etag):
            # (2026-07-02 fleet-wide-freeze fix) A multipart S3 ETag is
            # uncomparable to a local md5, but reaching HERE guarantees local ==
            # baseline: the gate above already returned "no_clobber" for
            # local != baseline (unpushed local writes -> rb-2096 protection).
            # So S3 is authoritative and safe to pull -- identical to the
            # no-baseline "download" policy on the next line. The prior
            # "no_clobber" here was over-conservative and CAUSED A FLEET-WIDE
            # WRITE FREEZE: it never refreshed the in-process fence (self._etags),
            # so _put kept sending IfMatch(stale) and every write to a
            # multipart-stored file (e.g. the ~8MB world/aspirations.jsonl)
            # 412'd DETERMINISTICALLY forever. "download" pulls S3 and adopts the
            # current ETag as the fence, curing the freeze; the baseline gate
            # above still protects unpushed local writes (rb-2096 intact).
            return "download"
        if baseline_read_failed:
            # (g-115-2178) FAIL CLOSED on a baseline-read FAILURE -- the manifest
            # read RAISED (or, above, owncloud_sync failed to import). We have NO
            # trustworthy baseline, so we cannot prove local == baseline and a
            # download could silently overwrite a possibly-unpushed local. A
            # record clobbered before its first git commit is PERMANENTLY lost
            # (no git/S3/.history trace -- the write->first-commit exposure
            # window). A data-protection guard's failure mode must be PRESERVE,
            # not overwrite (rb-3422): keep local; the caller (L637) flags the key
            # diverged so a following _put reconciles. Surface loudly so a genuine
            # manifest fault is seen, not silently disarmed.
            # SCOPE (g-115-2178): a baseline that is merely ABSENT (no manifest
            # entry) is NOT a read failure -- it falls through to the
            # S3-authoritative "download" below, the DELIBERATE policy that
            # test_refresh_no_baseline_pulls_s3_authoritative pins and that
            # force_fresh cache-coherence relies on. Only a genuine read EXCEPTION
            # fails closed here.
            _LOG.warning(
                "owncloud _overwrite_decision FAIL-CLOSED: manifest-read failure "
                "for %s (local_md5=%s) -- keeping local, refusing S3 overwrite to "
                "protect possibly-unpushed writes (g-115-2178/rb-3422)",
                self._rel(path), local_md5)
            return "no_clobber"
        return "download"  # local == baseline and S3 moved (peer wrote), OR no
                           # baseline (ABSENT: S3-authoritative first pull, matching
                           # _pull_one) -> safe to adopt S3

    def read_bytes(self, path: PathLike, *, force_fresh: bool = False) -> bytes:
        local = self._refresh(path, force_fresh)
        return local.read_bytes()  # FileNotFoundError if truly absent (matches local)

    def read_text(self, path: PathLike, encoding: str = "utf-8",
                  *, force_fresh: bool = False) -> str:
        local = self._refresh(path, force_fresh)
        return local.read_text(encoding=encoding)

    def read_authoritative_bytes(self, path: PathLike) -> bytes:
        """Pure read of the S3 object, straight to memory (g-115-1987).

        Unlike read_bytes/read_text(force_fresh=True) -> _refresh, this NEVER
        touches the local mirror: no download-into-cache (the rb-3128
        read-side clobber), no _etags/_cache_check mutation, and no
        no_clobber fallback -- in the both-diverged state _refresh returns
        the LOCAL path, so a "fresh" read_text serves the non-authoritative
        local content exactly when a diagnostic most needs S3 truth.
        Machine-local and out-of-root (git-shipped) paths are never on S3 --
        plain local read, mirroring _refresh's no-op branches. Raises
        FileNotFoundError when absent (matches LocalBackend semantics).

        RESTORED 2026-07-14 (g-115-2179). Deleted as collateral by the
        c5814933 origin-checkout (see stranded-checkout-check.sh). It is the
        ONLY StorageBackend protocol method OwnCloudBackend was missing --
        LocalBackend and the test FakeBackend both implement it, so its
        absence was invisible to the suite and live ONLY on own-cloud (the
        production backend). _merge_reconcile_sweep cannot be correct without
        it: in the diverged state that is the merge's only trigger,
        read_bytes(force_fresh=True) returns LOCAL bytes, so a merge built on
        it would merge local-against-local and emit garbage."""
        if self._machine_local(path):
            return self._local(path).read_bytes()
        try:
            key = self._s3_key(path)
        except ValueError:
            return self._local(path).read_bytes()
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                raise FileNotFoundError(
                    f"absent in S3 store: s3://{self.bucket}/{key}") from e
            _reraise_access_denied(e, "read_authoritative_bytes GetObject")
            raise
        return _codec_decode_response(obj, key=key)  # g-358-11: plaintext out

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
        # must tolerate 0 (FileStat contract). version is the ETag. plain_md5
        # (g-358-11) is the writer-recorded plaintext md5 of an ENCODED object,
        # None for a plain one — the sync layer's in-sync checks prefer it.
        return FileStat(version=h["ETag"], size=int(h["ContentLength"]),
                        mtime_ns=0, plain_md5=_codec_head_plain_md5(h))

    def head_last_modified(self, path: PathLike) -> Optional[float]:
        """S3 LastModified as epoch seconds, or None when the key is absent.
        Companion to delete_object's caller-side newer-than guards; kept
        separate from stat() because FileStat's mtime_ns=0 contract is
        load-bearing for existing callers."""
        try:
            h = self.s3.head_object(Bucket=self.bucket, Key=self._s3_key(path))
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                return None
            raise
        lm = h.get("LastModified")
        return lm.timestamp() if lm is not None else None

    def delete_object(self, path: PathLike) -> bool:
        """Delete ONE S3 object. No local-mirror side effects — the caller owns
        any local twin. Deliberately the sync layer's only delete primitive
        (g-115-2122 part 2 move-propagation); every caller must satisfy
        archive-before-delete (e.g. sweep deletes a temp/ root key only when
        its drained/ twin exists — the drained copy IS the archive). The
        bucket is versioned, so this writes a delete marker; noncurrent
        versions remain until lifecycle expiry. Returns True when the delete
        was accepted, False when the key was already absent. Requires
        s3:DeleteObject (lodestar-own-cloud policy, granted 2026-07-17)."""
        key = self._s3_key(path)
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                return False
            raise
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            _reraise_access_denied(e, "delete_object DeleteObject")
            raise
        return True

    def iter_paths_under(self, path: PathLike):
        """Yield the LOCAL-shaped absolute Path for every S3 object whose key
        sits under `path`'s prefix, recursively. Read-only companion to
        delete_object: callers get handles they can feed straight back to
        delete_object / head without touching keys or the client (g-115-6196
        Lane-3 dir propagation; g-115-6229 backlog enumeration). Cost scales
        with what is actually IN the store — a huge local-only dir whose
        contents never synced (worker-box H4a skip) yields nothing, where a
        local walk would enumerate every file."""
        base = Path(path)
        prefix = self._s3_key(base)
        if prefix.endswith("/."):
            prefix = prefix[:-1]
        if not prefix.endswith("/"):
            prefix += "/"
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                rest = o["Key"][len(prefix):]
                if rest:
                    yield base / rest

    def list_dir(self, path: PathLike) -> List[str]:
        prefix = self._s3_key(path)
        # When `path` IS a governed root (path == root), _rel maps it to
        # '<logical_prefix>/.' (Path('.').as_posix() == '.'), producing an
        # S3 key like 'env-id/world/.' — no S3 key matches that trailing
        # dot.  Strip it so the delimiter-list uses the correct prefix
        # (e.g. 'env-id/world/').  Only list_dir hits this: file-level
        # callers (_refresh, _put, stat, exists) never pass a bare root.
        if prefix.endswith("/."):
            prefix = prefix[:-1]
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
            try:
                resp = self.s3.list_objects_v2(**kw)
            except ClientError as e:
                # g-328-20: S3 enumeration twin of list_runner_claims' Scan — a
                # missing s3:ListBucket grant would otherwise degrade to an empty
                # dir listing. Surface it as a diagnosable permission error.
                _reraise_access_denied(e, "list_dir ListObjectsV2")
                raise
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

    def list_objects(self, path: PathLike) -> List[tuple]:
        """Flat recursive paginated list under `path`: [(rel_posix, etag, size)].

        The cheap enumeration the pull sweep (owncloud_sync.pull_sweep,
        g-115-2268 Gap A) needs: one ListObjectsV2 page per 1000 objects with
        NO Delimiter, returning every descendant object's key + ETag in ~3-7
        requests per governed root — vs one HEAD per file. rel paths are
        POSIX-relative to `path`. Same customer/env scope assert as list_dir."""
        prefix = self._s3_key(path)
        if prefix.endswith("/."):
            prefix = prefix[:-1]
        if not prefix.endswith("/"):
            prefix += "/"
        expected = self._customer_prefix() + self.env_id + "/"
        assert prefix.startswith(expected), (
            f"list_objects prefix {prefix!r} escapes customer/env scope "
            f"{expected!r} — IAM ListBucket is prefix-conditioned on it")
        out = []
        token = None
        while True:
            kw = dict(Bucket=self.bucket, Prefix=prefix)
            if token:
                kw["ContinuationToken"] = token
            try:
                resp = self.s3.list_objects_v2(**kw)
            except ClientError as e:
                _reraise_access_denied(e, "list_objects ListObjectsV2")
                raise
            for c in resp.get("Contents", []):
                rel = c["Key"][len(prefix):]
                if rel:
                    out.append((rel, c["ETag"], int(c["Size"])))
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break
        return out

    def ensure_local(self, path: PathLike) -> Path:
        return self._refresh(path, force_fresh=False)

    def refresh(self, path: PathLike) -> None:
        # Pull the latest remote object into the local cache (fix #2): bypasses
        # the cache TTL (force_fresh) and records the current ETag as the
        # If-Match fence token. Materializes a remote-only file locally. Used by
        # _fileops before an in-lock raw read so a read-modify-write starts from
        # the latest remote state, not a stale local cache.
        self._refresh(path, force_fresh=True)

    def prefetch(self, path: PathLike) -> dict:
        """Warm `_cache_check`/`_etags` for every object under `path` from ONE
        bulk listing, so the reads that follow skip their per-file HEAD.

        Why this exists (g-115-6660, operator-approved 2026-08-10): walking the
        knowledge tree measured **1368 HEAD + 2 GET, 1.36 MB, 78.3s** — 78
        seconds of round-trips moving almost no bytes. `_refresh` already has a
        per-file TTL cache that skips the HEAD, but a walk touches each file
        ONCE, so every file misses it and pays a HEAD. The listing returns the
        same ETag token that freshness check compares, at ~1 request per 1000
        keys. Measured in the same mail: 3 list calls for 2,717 keys in 0.8s.

        THE ONLY INVARIANT THAT MATTERS: this may reduce requests, never change
        what a read returns. So an entry is warmed ONLY where the listing PROVES
        the local copy is already current — its md5 equals the object's ETag.
        Every uncertain case falls through to the normal per-file path:
          - no local copy      -> the read must GET it anyway
          - multipart ETag     -> not the object md5; cannot compare (rb-2096)
          - gzip-encoded       -> ETag digests COMPRESSED bytes, so a decoded
                                  local mirror mismatches by construction. A
                                  LIST cannot return the plaintext md5 (that
                                  lives in object METADATA, HEAD-only), so
                                  encoded objects simply keep their HEAD
                                  (g-358-11 / `_codec_head_plain_md5`).
        Every skip is COUNTED, not silent: a caller comparing `warmed` against
        `listed` can see how much of the tree actually benefited, which is the
        difference between a measurement and a hopeful assertion.

        Batch validity deliberately inherits `cache_ttl` (default 30s) rather
        than inventing a second expiry concept — the mail asked for "a decision
        about how long a batch stays valid" and the existing TTL already IS that
        decision, applied by the one code path that consumes it. A walk longer
        than the TTL degrades to per-file HEADs for its tail; safe, not wrong.

        Fail-open: a failed listing returns stats with `errors` set and warms
        nothing, so the caller's reads behave exactly as they do today."""
        stats = {"backend": "own-cloud", "listed": 0, "warmed": 0,
                 "skipped_no_local": 0, "skipped_multipart": 0,
                 "skipped_mismatch": 0, "skipped_machine_local": 0,
                 "errors": 0, "ttl_seconds": self.cache_ttl}
        root = Path(path)
        try:
            objs = self.list_objects(root)
        except Exception as e:  # noqa: BLE001 — optimization must never raise
            stats["errors"] += 1
            stats["error"] = str(e)
            return stats
        stats["listed"] = len(objs)
        now = time.monotonic()
        for rel, etag, _size in objs:
            try:
                local = root / rel
                if self._machine_local(local):
                    stats["skipped_machine_local"] += 1
                    continue
                if not local.exists():
                    stats["skipped_no_local"] += 1
                    continue
                tag = (etag or "").strip('"')
                if "-" in tag:
                    stats["skipped_multipart"] += 1
                    continue
                h = hashlib.md5()
                with open(local, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                if h.hexdigest() != tag:
                    stats["skipped_mismatch"] += 1
                    continue
                key = self._s3_key(local)
            except Exception:  # noqa: BLE001 — one bad path must not stop the sweep
                stats["errors"] += 1
                continue
            self._etags[key] = etag
            self._cache_check[str(local)] = now
            stats["warmed"] += 1
        return stats

    # --- writes (with the If-Match fence) ----------------------------------
    def _body_kwargs(self, path: PathLike, body: bytes) -> dict:
        """put_object Body kwargs for ``body`` — the PLAINTEXT store bytes.

        g-358-11 unit 3 (transport gzip). Returns the ENCODED form (gzip Body +
        ``ContentEncoding`` + the plaintext-md5 / codec metadata, see
        ``_owncloud_codec.put_kwargs``) when the writer flag
        ``OWNCLOUD_GZIP_STORES`` NAMES this backend's ``env_id`` (the
        deployment whose store this object belongs to — a peer-board-post
        backend carries the PEER's env_id and stays plain until that
        deployment is listed) AND the path's env-scoped logical path
        (``_rel``, e.g. ``world/aspirations.jsonl``) is on the hot-store
        allowlist; otherwise ``{"Body": body}`` — byte-for-byte the pre-codec
        PUT. Both PUT sites (``_put`` and ``_merge_reconcile_put``) call this,
        and every write path funnels through those two, so the flag governs
        all writes at one seam.

        Everything around the PUT keeps working on PLAINTEXT: the local mirror
        write, the manifest baseline stamp (md5 of ``body``), and the merge
        handlers (``_get_remote_raw`` decodes). Only the wire bytes change; the
        returned ETag (the fence token) is whatever S3 computed for the stored
        bytes, opaque to every If-Match / IfNoneMatch use. Default OFF —
        reader-first rollout (g-328-39): a peer whose reader predates the codec
        would pull the gzip bytes RAW into its local mirror, so the flag flips
        only after the fleet-wide + downstream reader attestation."""
        if _codec_should_encode(self._rel(path), self.env_id):
            return _codec_put_kwargs(body)
        return {"Body": body}

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
            _atomic_write_local(local, body)
            return WriteResult(version=str(local.stat().st_mtime_ns),
                               fallback_used=False)
        # g-115-1875: UNIVERSAL test-isolation tripwire (fires below every
        # runner). Refuse a PUT whose path resolves under a tempfile/pytest temp
        # dir -- _s3_key ignores the local path, so a tmp-world PUT collides on
        # the PRODUCTION S3 key and truncates the real store (rb-2983/guard-955).
        # This is the net that covers what conftest's STORAGE_BACKEND=local pin
        # cannot: main()-style test files run directly (`python3 test_x.py`,
        # conftest never loads) and the bash aggregator that ran the 2026-07-09
        # truncating test. See _assert_not_tempdir_put for the full rationale.
        self._assert_not_tempdir_put(path)
        # g-115-8028: ownership consult BEFORE the first remote round-trip, so
        # "no wasted round-trip" holds by construction rather than by review.
        # ORDER IS LOAD-BEARING and is asserted by test_no_claim_error.py: this
        # sits AFTER the guard-955 tempdir tripwire (a tmp-world PUT is the
        # worse error and must not be masked by an ownership verdict) and after
        # the _machine_local early return above (those writes never reach S3, so
        # ownership is not a question -- refusing there would be a false
        # refusal and would break the rb-2396 per-agent-telemetry path).
        # Fires ONLY on provenance "live-claims". "local-backend",
        # "transient-error" and "unknown-machine" all fall through to the
        # ordinary fenced PUT, because on those this box may in fact own the dir
        # and merely failed to prove it -- asserting a structural impossibility
        # there would be the same confident-and-wrong error this fix removes.
        # Function-local import: the house pattern for this module pair (five
        # existing owncloud_sync imports in this file, L684/784/1000/1078).
        try:
            from _paths import agents_root
            from owncloud_sync import _owned_agents_with_provenance
            _root = Path(agents_root()).resolve()
            try:
                _agent = Path(path).resolve().relative_to(_root).parts[0]
            except (ValueError, IndexError):
                _agent = None          # not under an agent dir -- not our case
            if _agent is not None:
                _owned, _prov = _owned_agents_with_provenance()
                if _prov == "live-claims" and _agent not in (_owned or set()):
                    raise NoClaimError(
                        "no_claim: this box does not hold the live runner claim "
                        "for agent dir '%s'. The write did NOT land, and NO "
                        "retry or refresh can EVER succeed from here -- this box "
                        "is permanently behind the claim-holder's advancing "
                        "version. STRUCTURAL, not a race: do not retry, do not "
                        "refresh. Relay instead -- post the full payload plus "
                        "registration instructions to the coordination board for "
                        "the claim-holding instance to execute (worked example: "
                        "msg-20260827-110602-bravo-6570, g-364-104)." % _agent)
        except NoClaimError:
            raise
        except Exception as _consult_exc:
            # NEVER a bare `pass`. A fail-open wrapper around a NEW code path
            # turns every authoring error in it into silence: an earlier draft
            # of this block called a helper that did not exist, and the
            # resulting NameError was swallowed here -- the guard never fired,
            # compiled clean, and passed every existing test. Naming the
            # exception type keeps "the consult is broken" distinguishable from
            # "the consult ran and found nothing" (guard-1715 class).
            _LOG.warning("[no-claim-consult] skipped: %s: %s",
                         type(_consult_exc).__name__, _consult_exc)
        key = self._s3_key(path)
        local = self._local(path)
        local.parent.mkdir(parents=True, exist_ok=True)
        # gap #5 (g-328-15): the last _refresh saw the both-diverged state for
        # this key (local unpushed writes + S3 moved). For a REGISTERED
        # coordination store, MERGE local+remote instead of freezing (stale
        # fence -> perpetual 412) or clobbering the peer (empty post-restart
        # fence -> unconditional PUT). Unregistered files fall through to the
        # normal fenced PUT, preserving their safe-freeze-on-conflict behavior.
        if key in self._diverged_keys:
            handler = _coordination_merge_handler(path)
            if handler is not None:
                return self._merge_reconcile_put(path, key, local, body, handler)
        kw = dict(Bucket=self.bucket, Key=key)
        kw.update(self._body_kwargs(path, body))  # g-358-11 transport encode
        fence = self._etags.get(key)
        if fence is None:
            # W1 fix (g-115-2370, from the g-115-2360 RCA of the 2026-07-16
            # aspirations.jsonl clobber): the in-process fence cache is EMPTY
            # after every daemon restart, and stays unpopulated for any write
            # whose base read never touched S3 (warm-cache early-return,
            # head_object-404 return-local). The previous behavior — an
            # UNCONDITIONAL PutObject — could replace an S3 head this process
            # never read (composed with a stale-local read = silent multi-goal
            # data loss). Resolve against S3 NOW, before the PUT:
            #   - key absent  -> conditional CREATE (IfNoneMatch="*"): a peer
            #     creating concurrently 412s us into the conflict lane below
            #     instead of last-writer-wins.
            #   - key exists + registered merge handler -> merge-reconcile.
            #     PUT-time head-fencing is NOT sufficient for these: a body
            #     derived from a stale local read would pass a fence fetched at
            #     write time and still clobber the head (the W1∘W2 composition),
            #     so union with the current remote on a fresh fence instead —
            #     the same safe degradation as the stale-fence 412 path below
            #     (g-115-1741).
            #   - key exists + unregistered -> adopt the CURRENT etag as the
            #     fence. Single-writer per-agent files are the population here;
            #     plain-PUT-over-own-history is their intended semantic, and the
            #     fence closes the concurrent-writer race window without
            #     introducing a post-restart freeze class (rb-3636 fence-wedge).
            try:
                head = self.s3.head_object(Bucket=self.bucket, Key=key)
            except ClientError as e:
                if e.response["Error"]["Code"] not in _NOT_FOUND:
                    raise
                head = None
            if head is None:
                kw["IfNoneMatch"] = "*"
            else:
                handler = _coordination_merge_handler(path)
                if handler is not None:
                    return self._merge_reconcile_put(path, key, local, body,
                                                     handler)
                fence = head["ETag"]
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
        if fence is not None or "IfNoneMatch" in kw:
            # g-328-21: conditional (IfMatch or IfNoneMatch) write — both can
            # 412, so both count in the conflict-rate denominator.
            self._cas_writes += 1
        try:
            r = self.s3.put_object(**kw)
        except ParamValidationError as e:
            # botocore < 1.35 rejects PutObject(IfMatch=...) CLIENT-SIDE, before
            # any network call — the exact failure the init preflight guards, but
            # re-checked here so a version skew mid-process (or a backend built
            # bypassing __init__) can never silently drop the write. Only remap
            # when IfMatch was actually in play; an unrelated param error surfaces
            # as-is. Do NOT retry without IfMatch — that would drop compare-and-swap.
            if "IfMatch" in kw or "IfNoneMatch" in kw:
                raise RuntimeError(
                    _IFMATCH_UPGRADE_MSG + f"\n(original client-side error: {e})")
            raise
        except ClientError as e:
            if e.response["Error"]["Code"] in _PRECONDITION:
                self._cas_conflicts += 1  # g-328-21: 412 event (conflict-rate numerator)
                # g-115-1741: a HOT coordination store (team-state.yaml, written
                # every iteration by every agent) 412s HERE with an EMPTY
                # _diverged_keys, because _refresh's warm-cache early-return
                # (L456) returns BEFORE the no_clobber divergence detection that
                # would have populated _diverged_keys -- the cache is ALWAYS warm
                # for a per-iteration store, so that detection NEVER runs. The
                # L663 merge PRE-check therefore misses and we reach here with a
                # stale self._etags fence. Raising into the _fileops locked-RMW
                # retry just re-hits the SAME warm-cache-stale-fence 412
                # deterministically -- the >22min single-writer deadlock zeta
                # observed on cc-02 (rb-2639: per-object stale-IfMatch deadlock).
                # If the store has a commutative merge handler, reconcile NOW:
                # _merge_reconcile_put re-GETs the CURRENT remote ETag, merges
                # local+remote, and PUTs fenced on the FRESH ETag -- curing the
                # freeze regardless of _diverged_keys state, and preserving
                # unpushed local writes via the commutative merge (rb-2096 intact,
                # NOT a clobber). Non-coordination stores keep the safe
                # freeze-on-conflict -> RMW retry below. This is the write-path
                # twin of bdab36a's read-path multipart fence-refresh fix.
                handler = _coordination_merge_handler(path)
                if handler is not None:
                    return self._merge_reconcile_put(
                        path, key, local, body, handler, entered_from_conflict=True)
                # fix A2: surface, never silently drop. Caller re-runs the RMW
                # (G1 conflict-retry in _fileops' locked RMW helpers).
                raise ConflictError(
                    f"If-Match failed for {key}: remote changed since the in-lock "
                    "read; re-run the read-modify-write")
            raise
        # PUT succeeded — NOW make the local cache match what S3 holds.
        _atomic_write_local(local, body)
        self._etags[key] = r["ETag"]
        self._cache_check[str(local)] = time.monotonic()
        self._diverged_keys.discard(key)  # this write resolved any divergence
        self._stamp_manifest_baseline(path, body, etag=r["ETag"])
        return WriteResult(version=r["ETag"], fallback_used=False)

    def _get_remote_raw(self, key: str):
        """RAW S3 GET of the current object + ETag, BYPASSING the no-clobber
        guard in _refresh — the "read-remote-authoritative" primitive the
        both-diverged merge needs (refresh() refuses to surface remote when
        local holds unpushed writes, which is exactly the state we must merge
        out of). Returns (body_bytes, etag), or (b"", None) if the object is
        absent. Does NOT touch the local cache or the fence."""
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                return b"", None
            raise
        # g-358-11: merge handlers see PLAINTEXT; the ETag stays the CAS token.
        return _codec_decode_response(obj, key=key), obj["ETag"]

    def _merge_reconcile_put(self, path: PathLike, key: str, local: Path,
                             body: bytes, handler,
                             entered_from_conflict: bool = False) -> WriteResult:
        """Reconcile a both-diverged write to a registered coordination store:
        GET the remote-authoritative bytes, MERGE them with the outgoing local
        bytes via the store's commutative handler, and PUT the merged result
        fenced on the remote ETag. If S3 moved again during the merge (a third
        writer), the fenced PUT 412s and we re-GET / re-merge — a bounded CAS
        loop that terminates because the handler is commutative (both machines
        compute the same merged bytes). See core/scripts/coordination_merge.py.

        entered_from_conflict (g-328-21): True when the caller reached here from a
        412 it already counted (the _put PreconditionFailed path), so a successful
        merge here counts as a RESOLVED conflict even if this loop sees no further
        412. False for the proactive both-diverged pre-check dispatch (no 412 yet)."""
        saw_conflict = entered_from_conflict
        for attempt in range(_MERGE_RECONCILE_CAP):
            remote_bytes, remote_etag = self._get_remote_raw(key)
            try:
                merged = handler(body, remote_bytes)
            except Exception as e:
                # A malformed store blob must not wedge writes forever. Surface
                # as a ConflictError so the caller's RMW retry / operator sees
                # it, rather than silently clobbering with un-merged local.
                raise ConflictError(
                    f"coordination merge failed for {key}: {e}")
            if merged is None:
                # A handler REFUSES by returning None -- it is not an error, it
                # is the store's deliberate safe-freeze for a divergence only a
                # reader can resolve (merge_tree_node_md on same-heading
                # divergence or an undecodable side, g-115-7071). Honor it
                # as a freeze and leave BOTH sides untouched.
                #
                # Exactly ONE of coordination_merge's 31 handlers carries an
                # explicit None-refusal today -- merge_tree_node_md, measured by
                # AST 2026-08-23, NOT by grepping `return None` (that reports 13
                # and is wrong: the other 12 are merge_handler_for's own returns
                # and helpers). The check is written against the CONTRACT, not
                # that population, so a handler that adopts a refusal later is
                # covered without touching this file.
                #
                # Without this check the refusal fell through to
                # put_object(Body=None), which boto3 rejects client-side as
                # ParamValidationError -- so a merge conflict presented as a
                # transport fault ("union-merge push failed ... Invalid type for
                # parameter Body") and was nearly filed as an S3 outage on two
                # separate boxes. Detection-corrupting, not just noisy: the
                # sweep retried it every pass and the node never synced.
                # (g-115-7211; the handler-EXCEPTION channel above was already
                # wrapped -- this is the handler-RETURNS-NONE channel.)
                raise ConflictError(
                    f"coordination merge REFUSED for {key}: the store's merge "
                    f"handler declined to reconcile diverged content (same-heading "
                    f"divergence or an undecodable side). Frozen for reader "
                    f"reconciliation -- no write attempted.")
            kw = dict(Bucket=self.bucket, Key=key)
            kw.update(self._body_kwargs(path, merged))  # g-358-11 transport encode
            if remote_etag is not None:
                kw["IfMatch"] = remote_etag  # CAS on the version we merged against
            self._cas_writes += 1  # g-328-21: each merge attempt is a fenced write
            try:
                r = self.s3.put_object(**kw)
            except ClientError as e:
                if e.response["Error"]["Code"] in _PRECONDITION:
                    self._cas_conflicts += 1  # g-328-21: 412 during merge
                    saw_conflict = True
                    time.sleep(_conflict_backoff(attempt))
                    continue  # S3 moved during merge; re-GET and re-merge
                raise
            _atomic_write_local(local, merged)
            self._etags[key] = r["ETag"]
            self._cache_check[str(local)] = time.monotonic()
            self._diverged_keys.discard(key)
            self._stamp_manifest_baseline(path, merged, etag=r["ETag"])
            if saw_conflict:
                self._cas_conflicts_resolved += 1  # g-328-21: retry recovered
                _LOG.info(
                    "owncloud CAS conflict resolved for %s via merge-reconcile "
                    "(attempt %d); running 409-rate %.3f (%d conflicts / %d writes)",
                    key, attempt, self._cas_conflict_rate(),
                    self._cas_conflicts, self._cas_writes)
            return WriteResult(version=r["ETag"], fallback_used=False)
        _LOG.warning(
            "owncloud CAS merge-reconcile EXHAUSTED %d retries for %s; running "
            "409-rate %.3f (%d conflicts / %d writes)",
            _MERGE_RECONCILE_CAP, key, self._cas_conflict_rate(),
            self._cas_conflicts, self._cas_writes)
        raise ConflictError(
            f"merge-reconcile exhausted {_MERGE_RECONCILE_CAP} retries for "
            f"{key}: S3 kept moving mid-merge")

    def _cas_conflict_rate(self) -> float:
        """Running 409/412 conflict rate = conflicts / fenced-writes (g-328-21).
        0.0 when no fenced writes have occurred yet."""
        return self._cas_conflicts / self._cas_writes if self._cas_writes else 0.0

    def cas_metrics(self) -> dict:
        """Per-process CAS (If-Match compare-and-swap) conflict telemetry
        (g-328-21). Makes the 409/412 rate MEASURABLE: writes = fenced put_object
        attempts, conflicts = 412 events, resolved = merge-reconciles that
        recovered after >=1 conflict, conflict_rate = conflicts/writes. Counters
        are per-process (reset on daemon restart); the always-on cross-restart
        surface is the _LOG line emitted on each merge-reconcile resolve/exhaust."""
        return {
            "writes": self._cas_writes,
            "conflicts": self._cas_conflicts,
            "resolved": self._cas_conflicts_resolved,
            "conflict_rate": self._cas_conflict_rate(),
        }

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

    def merge_put(self, path: PathLike, content: bytes) -> Optional[WriteResult]:
        """Union-merge push for a merge-REGISTERED store (g-115-2297): GET the
        current remote bytes, MERGE with ``content`` via the store's commutative
        handler (coordination_merge), and PUT fenced on the remote ETag — the
        bounded CAS loop in ``_merge_reconcile_put``. Returns ``None`` when the
        store has no registered handler or the path is machine-local; the
        caller then falls back to its default action (e.g. ``mirror_put``).
        On success the merged bytes land on BOTH S3 and the local cache, so
        both sides converge to the union.

        Sibling of ``mirror_put`` for the sync sweep. A whole-object PUT of an
        append-only log CLOBBERS whenever S3 holds records local lacks — the
        If-Match fence cannot catch it because it fences on the just-observed
        CURRENT etag, so a stale-TAIL local (appends degraded to LocalBackend
        while peers appended to S3) replaces the newer head and the fence
        passes (observed 2026-07-16T03:09:14 on meta/gate-firings.jsonl).
        Registered stores therefore never take the blind PUT from the sync
        path; the union is a superset of the push, so local-only records still
        land. Not on the StorageBackend Protocol: own-cloud-only, like
        ``mirror_put``."""
        if self._machine_local(path):
            return None
        handler = _coordination_merge_handler(path)
        if handler is None:
            return None
        self._assert_not_tempdir_put(path)  # guard-955 parity with _put
        key = self._s3_key(path)
        local = self._local(path)
        local.parent.mkdir(parents=True, exist_ok=True)
        return self._merge_reconcile_put(path, key, local, content, handler)

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
            try:
                resp = self.ddb.scan(**kw)
            except ClientError as e:
                # g-328-20: a missing dynamodb:Scan grant here was the 2026-07-04
                # fleet-wedge root cause — surface it as a diagnosable permission
                # error, never let a caller degrade it to an empty owned-set.
                _reraise_access_denied(e, "list_runner_claims Scan")
                raise
            for item in resp.get("Items", []):
                skey = item.get("session_key", {}).get("S", "")
                if not skey.startswith(prefix):
                    continue  # defense-in-depth: never leak a peer env's claim
                hb_raw = item.get("heartbeat_at", {}).get("N")
                # The Scan carries no ProjectionExpression, so `item` already
                # holds the raw runner_token. It is digested HERE and the raw
                # value is dropped on the floor — this line is the boundary the
                # token must not cross (see runner_token_fingerprint).
                claims.append(RunnerClaim(
                    agent=skey[len(prefix):],
                    machine_id=item.get("machine_id", {}).get("S"),
                    agent_state=item.get("agent_state", {}).get("S", "IDLE"),
                    heartbeat_at=int(hb_raw) if hb_raw is not None else 0,
                    runner_token_fp=runner_token_fingerprint(
                        item.get("runner_token", {}).get("S"))))
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                break
        return claims

    def health_check(self) -> dict:
        """Proactive IAM/permission probe for the governed ops (g-328-20). Runs a
        BOUNDED governed DDB Scan (``Limit=1`` on the sessions table) and a BOUNDED
        governed S3 list (``MaxKeys=1`` on the env prefix) — the two enumeration
        surfaces whose silent-AccessDenied degrade caused the 2026-07-04
        fleet-wedge (g-328-19). On an IAM/permission gap this raises the
        diagnosable :class:`OwnCloudPermissionError` (via
        :func:`_reraise_access_denied`); any other ``ClientError`` propagates
        unchanged. Returns ``{"ok": True, "checked": [...]}`` when both governed
        surfaces are reachable. The infra-health own-cloud check calls this and
        surfaces the raise as an ALERT, so a permission gap is detected at
        health-check time — not days later, silently, inside a sweep."""
        checked: List[str] = []
        # Governed DDB surface: a Limit=1 Scan exercises dynamodb:Scan on the
        # sessions table (the exact grant missing in g-328-19) without reading it.
        try:
            self.ddb.scan(TableName=self.sessions_table, Limit=1)
            checked.append("ddb:Scan")
        except ClientError as e:
            _reraise_access_denied(e, "health_check ddb.Scan")
            raise
        # Governed S3 surface: a MaxKeys=1 list exercises s3:ListBucket on the
        # env-scoped prefix.
        try:
            self.s3.list_objects_v2(
                Bucket=self.bucket,
                Prefix=self._customer_prefix() + self.env_id + "/",
                MaxKeys=1)
            checked.append("s3:ListBucket")
        except ClientError as e:
            _reraise_access_denied(e, "health_check s3.ListObjectsV2")
            raise
        return {"ok": True, "checked": checked}
