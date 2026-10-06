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

from _long_path import long_path
from storage_backend import (
    FileStat, WriteResult, replace_with_retry,
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
    CodecError as _CodecError,
    decode as _codec_decode,
    decode_response as _codec_decode_response,
    head_plain_md5 as _codec_head_plain_md5,
    content_matches as _codec_content_matches,
    should_encode as _codec_should_encode,
    put_kwargs as _codec_put_kwargs,
    META_PLAIN_MD5 as _CODEC_META_PLAIN_MD5,
)
# g-358-202 (U2c): the composite head+segment layout for the goal-queue store. The READ side is
# always on for the allowlisted store (a head is joined with its segments, a plain object is
# untouched), like the codec's decode; the writer is behind its own default-OFF flag (U2d: _store_put).
import _owncloud_composite as _composite

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
    # g-358-49: its date segments. A PREFIX entry (matched by the
    # `rel.startswith(s)` arm below) rather than an exact name, because segment
    # basenames are dates and cannot be enumerated. Same append-mostly class as
    # the legacy file above — records are only ever appended, never rewritten —
    # so a range-tail pull is byte-safe.
    "world/productivity-snapshots-",
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

    Windows (g-115-7257): os.replace raises PermissionError (WinError 5/32)
    while another process holds the target open, as any plain open() does, so
    the publish retries through the one shared backoff
    (storage_backend.replace_with_retry, guard-472) and raises only when that
    runs out. There is deliberately no in-place fallback after it: that is the
    truncate window this function exists to close. `local` is re-spelled with
    long_path (g-115-11323) so the mkdir and the mkstemp sibling, 13 chars
    longer than the target, work past MAX_PATH on a box without long paths.
    """
    local = long_path(local)
    local.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=local.name + ".", suffix=".tmp",
                                    dir=str(local.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        outcome = replace_with_retry(tmp_name, local, retry_on=(PermissionError,))
        if not outcome.ok:
            raise outcome.last_err
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


# g-115-11275. The no_claim refusal text, named so its wording is testable. Two
# readers bound it: server.py hands the refused writer only str(e)[:600], and
# other files quote the diagnosis sentences, which therefore stay verbatim. The
# remedy is the delivery-first order (/encode-session NO_CLAIM BRANCH,
# g-115-10122); it replaced "relay to the coordination board", because five
# board relays sat undelivered 5-21h while a routed goal landed in ~3.5h.
# action_type:land-write is the relay verb insight-trigger-sweep.py floors at
# MEDIUM (RELAY_ACTION_TYPES); test_no_claim_error.py pins the pair together.
# Format it with a dict, as _put does: a template that fails to format raises
# inside _put's fail-open consult and would let the refused write through.
NO_CLAIM_MESSAGE = (
    "no_claim: this box does not hold the live runner claim "
    "for agent dir '%(agent)s'. The write did NOT land, and NO "
    "retry or refresh can EVER succeed from here -- this box "
    "is permanently behind the claim-holder's advancing "
    "version. STRUCTURAL, not a race. Deliver it instead: (1) run the same "
    "write on the holder's box (runner-claim.sh status --agent %(agent)s names "
    "it); (2) else file a world goal with the payload inline and "
    "intended_agent + handoff_to set to that agent. A board post alone is not "
    "delivery; if you post one, tag it requires_action_by, "
    "action_type:land-write, severity.")


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


def _last_modified_epoch(resp) -> Optional[float]:
    """The last_modified of a HeadObject response as epoch seconds, or None when it carries none."""
    stamp = resp.get("LastModified")
    return stamp.timestamp() if hasattr(stamp, "timestamp") else None


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
    so every existing positional construction stays valid.

    ``holder_since`` is epoch-seconds marking when the CURRENT ``machine_id``
    became the holder — NOT when the claim was last acquired (g-306-379). It is
    deliberately NOT bumped when the SAME machine re-acquires, because the
    question it answers is "has one box held this claim continuously?", and a
    plain acquisition timestamp cannot distinguish continuous tenure from
    re-acquisition after a peer held it (both produce the same value). 0 means
    UNKNOWN — a legacy row that has not changed hands since this field shipped —
    and every consumer must fail OPEN on 0, exactly as ``heartbeat_at``'s 0
    means "never heartbeated" rather than "infinitely stale". Defaulted for the
    same positional-construction reason as ``runner_token_fp``."""
    agent: str
    machine_id: Optional[str]
    agent_state: str
    heartbeat_at: int
    runner_token_fp: Optional[str] = None
    holder_since: int = 0


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

# Reader-cache TTL: seconds a cached read trusts the local mirror before
# re-checking S3 freshness (HEAD + ETag compare). Raised 30 -> 120 on
# 2026-08-30 (S3 cost plan): egress attribution measured the fleet's read
# amplification as the dominant S3 cost, and a 30s TTL re-HEADs (and, for a
# store that changes every ~33s like world/aspirations.jsonl, re-GETs) on
# nearly every clustered read. 120s matches the push-sweep cadence the fleet
# already tolerates for cross-box visibility, and write correctness never
# rode on this value: RMW writes refresh force_fresh and the fenced PUT +
# merge handlers arbitrate conflicts. Per-box override: OWNCLOUD_CACHE_TTL.
DEFAULT_CACHE_TTL_SECONDS = 120


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
                 cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS, machine_id: str = "unknown",
                 region: str = "us-east-2",
                 runner_stale_seconds: int = DEFAULT_RUNNER_STALE_SECONDS,
                 aws_access_key_id: str = None, aws_secret_access_key: str = None,
                 s3=None, ddb=None, s3_endpoint_url=None):
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
        # Optional object-store endpoint override (g-372-01, Phase 1 of the
        # storage exit): point the S3 client at a self-hosted S3-compatible
        # store by URL. Unset/blank = today's client construction byte-for-byte
        # (no endpoint_url kwarg at all); reversible by unsetting the var. S3
        # ONLY — the DynamoDB client keeps its regional endpoint on purpose
        # (Phase 1a leaves the lock/session tables where they are). botocore
        # addresses a custom endpoint path-style by default (measured on
        # botocore 1.43.39), so no addressing_style config is needed.
        # `s3_endpoint_url` is an EXPLICIT override of that env read (g-372-13):
        # None = read the env exactly as before; "" = the regional AWS endpoint
        # even when the env override is set; a URL = that store. The env var
        # repoints EVERY caller that resolves through this factory at once, and
        # one caller — the cold-snapshot DR archive — exists to be somewhere
        # else (guard-6373), so it needs a way to say so per instance.
        if s3_endpoint_url is None:
            s3_endpoint_url = os.environ.get("STORAGE_S3_ENDPOINT_URL", "")
        _s3_endpoint = str(s3_endpoint_url).strip()
        _s3_kw = {"endpoint_url": _s3_endpoint} if _s3_endpoint else {}
        self.s3_endpoint_url = _s3_endpoint or None
        if (s3 is None or ddb is None) and aws_access_key_id and aws_secret_access_key:
            _cred_sess = boto3.Session(aws_access_key_id=aws_access_key_id,
                                       aws_secret_access_key=aws_secret_access_key,
                                       region_name=region)
            _mk = lambda svc: _cred_sess.client(
                svc, region_name=region, config=_cfg,
                **(_s3_kw if svc == "s3" else {}))
        else:
            _mk = lambda svc: boto3.client(
                svc, region_name=region, config=_cfg,
                **(_s3_kw if svc == "s3" else {}))
        self.s3 = s3 if s3 is not None else _mk("s3")
        self.ddb = ddb if ddb is not None else _mk("dynamodb")
        # ETag observed at the most recent read of each key — the If-Match fence
        # token (fix #3). Per-process; the DDB lock serializes RMW on a key, so
        # read-then-write within a held lock is sequential and this is race-free.
        self._etags: dict = {}
        # g-358-202 U2d: S3 key -> (ETag, bytes) of the composite HEAD this process last read or
        # wrote. _store_put plans a write against it only while that ETag is still the fence, so a
        # write PUTs just the segments the head does not already name. _composite_warned holds the
        # (key, reason) pairs it has already logged for a store the layout refused.
        self._composite_heads: dict = {}
        self._composite_warned: set = set()
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
        self._merge_noop_identical = 0
        # Preflight: fail loud NOW if botocore is too old for PutObject IfMatch,
        # rather than letting every _put crash cryptically at runtime (reads
        # would still work, masking the break). See _assert_ifmatch_supported.
        _assert_ifmatch_supported()

    # --- env wiring --------------------------------------------------------
    @classmethod
    def from_env(cls, env=None) -> "OwnCloudBackend":
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
        # `env` (g-372-13): the mapping to read from, default the process env.
        # A caller that must talk to a DIFFERENT store than the box's live one
        # (the cold-snapshot DR archive) passes an overlaid copy here instead of
        # mutating os.environ, which would also repoint get_backend()'s cache.
        env = os.environ if env is None else env
        missing = [v for v in ("STORAGE_S3_BUCKET", "STORAGE_DDB_LOCK_TABLE",
                               "STORAGE_DDB_SESSIONS_TABLE")
                   if not env.get(v)]
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
        akid = env.get("MIND_AWS_ACCESS_KEY_ID")
        asec = env.get("MIND_AWS_SECRET_ACCESS_KEY")
        allow_default_chain = env.get(
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
        machine_id = env.get("MACHINE_ID", "").strip()
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
        _stale_env = env.get("OWNERSHIP_STALE_SECONDS", "").strip()
        try:
            runner_stale = (int(_stale_env) if _stale_env
                            else DEFAULT_RUNNER_STALE_SECONDS)
        except ValueError:
            runner_stale = DEFAULT_RUNNER_STALE_SECONDS
        return cls(
            env_id=env.get("ENVIRONMENT_ID", "ayoai-mind"),
            bucket=env["STORAGE_S3_BUCKET"],
            lock_table=env["STORAGE_DDB_LOCK_TABLE"],
            sessions_table=env["STORAGE_DDB_SESSIONS_TABLE"],
            root_map=cls._resolve_root_map(),
            cache_ttl=int(env.get("OWNCLOUD_CACHE_TTL",
                                         str(DEFAULT_CACHE_TTL_SECONDS))),
            machine_id=machine_id,
            region=env.get("AWS_DEFAULT_REGION", "us-east-2"),
            runner_stale_seconds=runner_stale,
            # Scoped least-privilege creds (Zak_first_test), separate from the
            # root AWS_* keys. Both-None is only reached when the operator set
            # MIND_AWS_ALLOW_DEFAULT_CHAIN=1 above -> __init__ default chain.
            aws_access_key_id=akid,
            aws_secret_access_key=asec,
            s3_endpoint_url=env.get("STORAGE_S3_ENDPOINT_URL", ""),
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

    def _rel_of_key(self, key: str) -> str:
        """The env-scoped logical path (`_rel`) an S3 key was built from: the inverse of `_s3_key`,
        or "" for a key outside this backend's `<customer>/<env_id>/` namespace."""
        prefix = f"{self._customer_prefix()}{self.env_id}/"
        return key[len(prefix):] if key.startswith(prefix) else ""

    def _lock_key(self, lock_path: PathLike) -> str:
        # The DDB lock key is the customer+env-scoped logical path of the lock
        # file. Acquire and release derive it identically, so the .lock suffix is
        # harmless. The customer prefix isolates locks across tenants.
        return f"{self._customer_prefix()}{self.env_id}/{self._rel(lock_path)}"

    def _holder(self) -> str:
        return f"{self.machine_id}:{os.getpid()}:{threading.get_ident()}"

    def _local(self, path: PathLike) -> Path:
        # The I/O spelling of a governed path: identity below ~240 chars, the
        # \\?\ form above it on Windows (g-115-11323). Every _cache_check key is
        # built from this spelling too. Never feed the result to _rel/_s3_key.
        return long_path(path)

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

    def _cache_fetch(self, key: str, etag: str) -> Optional[bytes]:
        """g-358-40: ask the LAN object cache for this exact (key, ETag).

        Returns the decoded plaintext on a hit, or None — and None ALWAYS means
        "caller proceeds exactly as it did before this feature existed".

        OFF BY DEFAULT. Enabled only when OWNCLOUD_OBJECT_CACHE names a base
        URL (e.g. http://10.63.163.202:34915). The URL *is* the flag, so there
        is no separate on/off switch that could disagree with the address it
        points at, and the kill switch is one `unset`.

        WHY THIS IS SAFE TO PUT IN THE HOT READ PATH:
        the caller has already HEADed S3 and holds the authoritative `etag`, so
        this asks a content-addressed store for one specific content hash. The
        cache can only answer with bytes stored under that exact ETag. A hit is
        therefore byte-equivalent to `get_object`; it is not a freshness
        judgement and no staleness window applies (distinct from `cache_ttl`
        above it, which IS time-based — guard-1578/guard-5617).

        FAIL-OPEN, TOTAL: every failure mode — flag unset, DNS, refused
        connection, timeout, 404 miss, 401, malformed reply, short read — takes
        the same path, `return None`, and the caller does its normal S3 GET.
        Nothing here can turn a working read into a failed one; the worst case
        is one wasted round-trip on the LAN.
        """
        base = os.environ.get("OWNCLOUD_OBJECT_CACHE", "").strip()
        if not base:
            return None  # feature off: byte-identical to the pre-feature path
        try:
            import urllib.parse
            import urllib.request

            url = (base.rstrip("/") + "/v1/cache/object?"
                   + urllib.parse.urlencode({"key": key, "etag": etag}))
            headers = {"X-Runtime-Client": "owncloud-backend"}
            # g-358-40 item 5, DECIDED (option B — decouple). The cache client
            # carries its OWN credential key, falling back to MIND_API_TOKEN so
            # every box that has not set the new one behaves byte-identically.
            # WHY a second key instead of reusing MIND_API_TOKEN on clients:
            # putting MIND_API_TOKEN in a CLIENT box's .env.local does two
            # unrelated things — it supplies this Bearer (wanted), AND it flips
            # THAT box's own daemon to FR-4 auth-required at its next start
            # (server.py:298, which reads the same key server-side). The second
            # effect is a fleet-wide security-posture change no goal decided,
            # and under daemon-only architecture its blast radius is a total
            # agent wedge rather than degradation. With this key a client gets
            # the credential and its own daemon's posture is untouched, so the
            # per-box worst case drops from "daemon wedge" to "cache fails
            # open" — which is already this function's contract.
            token = (os.environ.get("OWNCLOUD_OBJECT_CACHE_TOKEN", "").strip()
                     or os.environ.get("MIND_API_TOKEN", "").strip())
            if token:
                headers["Authorization"] = "Bearer " + token
            timeout = float(os.environ.get("OWNCLOUD_OBJECT_CACHE_TIMEOUT", "2"))
            req = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                body = resp.read()
            # A truncated body would be silently wrong, and this is the one
            # error the transport can hand us that still looks like success.
            declared = resp.headers.get("Content-Length")
            if declared is not None and len(body) != int(declared):
                return None
            self._cache_hits = getattr(self, "_cache_hits", 0) + 1
            return body
        except Exception:
            # Deliberately bare: this is the fail-open boundary. Any exception
            # at all means "cache unavailable", never "read failed".
            self._cache_errors = getattr(self, "_cache_errors", 0) + 1
            return None

    def _composite_whole(self, key: str, body: bytes, etag, local: Optional[Path] = None):
        """g-358-202 (U2c): the READ half of the composite layout, one adapter for the three read
        paths below (and, through `join_composite`, for the raw-S3 readers). If `body` (the decoded
        object at `key`) is a composite HEAD of an allowlisted store, return (the whole legacy file
        joined from its segment objects, the ETag of the head it was joined from). Anything else
        comes back as (body, etag) untouched, so every other object is byte-identical to the
        pre-composite backend.

        The join, the bounded retry and the fence rule live in `_owncloud_composite.read_whole`,
        shared with the raw-S3 readers; this supplies only its two I/O steps. `local` (passed by
        `_refresh` alone) lets a segment the mirror already carries be reused after an md5 check
        against the head's manifest, instead of fetched."""
        if not _composite.is_head(body) or not _composite.reads_composite(self._rel_of_key(key)):
            return body, etag
        try:
            local_raw = local.read_bytes() if local is not None else None
        except OSError:
            local_raw = None  # an unreadable mirror just means fetch every segment

        def fetch_segment(name: str) -> bytes:
            seg_key = _composite.segment_s3_key(key, name)
            try:
                obj = self.s3.get_object(Bucket=self.bucket, Key=seg_key)
            except ClientError as e:
                if e.response["Error"]["Code"] in _NOT_FOUND:
                    raise _composite.SegmentMissing(seg_key) from e
                _reraise_access_denied(e, "composite segment GetObject")
                raise
            try:
                return _codec_decode_response(obj, key=seg_key)
            except _CodecError as e:
                raise _composite.IntegrityError(str(e)) from e

        seen = [(body, etag)]  # the newest head read and its ETag: what a re-read replaces

        def reread_head():
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            seen[0] = (_codec_decode_response(obj, key=key), obj["ETag"])
            return seen[0]

        raw, joined_etag = _composite.read_whole(body, etag, fetch_segment, reread_head, local_raw=local_raw)
        head, head_etag = seen[0]
        if joined_etag == head_etag and _composite.is_head(head):
            self._composite_heads[key] = (head_etag, head)  # the old head _store_put plans a write against
        return raw, joined_etag

    def join_composite(self, key: str, body: bytes, etag) -> bytes:
        """g-358-202 (U3): the whole legacy bytes for a raw-S3 reader that already holds the DECODED
        `body` of the object at `key` and its `etag` (`_owncloud_composite.decode_whole` calls this for
        a composite head only). Such a reader bypasses the mirror, so no local file is offered for
        segment reuse. Raises what `_composite_whole` raises (IntegrityError once the bounded re-reads
        are spent); those readers treat any exception as 'the authoritative read is unavailable'."""
        return self._composite_whole(key, body, etag)[0]

    def composite_gc_enumerate(self, path: PathLike, ledger, now: float, *,
                               grace_s: float = _composite.GC_GRACE_S,
                               max_delete: int = _composite.GC_MAX_DELETE) -> _composite.GcEnumeration:
        """g-358-202 (U2e): the READ-ONLY half of orphan collection for the composite store at `path`: what
        a later delete pass may remove, and the evidence for it. It GETs the head, lists the segment
        directory to the END of its pagination, then HEADs the head again, and hands the three to
        `_owncloud_composite.plan_gc`. It never PUTs, deletes or copies, never touches the local mirror, and
        does not consult the writer flag (a box whose flag is unset can still enumerate what a flagged box
        wrote).

        An orphan is only defined against the head the listing was taken under, so a head that moved (or
        vanished) between the first read and the last abandons the pass: refused
        `head-moved-during-enumeration`, nothing planned. The other refusals are `store-not-allowlisted` (no
        S3 call is made), `head-missing`, and the planner's own (a head that is not a composite head is
        refused without listing anything). An S3 error propagates: a pass that failed has no plan.

        Returns a GcEnumeration: the plan, the head ETag it was computed against, and for each name the plan
        would delete its size, last_modified (epoch), ETag and first_seen, which is the enumeration
        archive-before-delete step 1 asks for (names and sizes, not a bare count, guard-6063)."""
        key = self._s3_key(path)

        def refused(why: str) -> _composite.GcEnumeration:
            return _composite.GcEnumeration(_composite.GcPlan([], dict(ledger), [why], [], {}), None, {})

        if not _composite.reads_composite(self._rel_of_key(key)):
            return refused("store-not-allowlisted")
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                return refused("head-missing")
            _reraise_access_denied(e, "composite GC head GetObject")
            raise
        head, etag = _codec_decode_response(obj, key=key), obj["ETag"]
        if not _composite.is_head(head):
            plan = _composite.plan_gc([], head, ledger, now, grace_s=grace_s, max_delete=max_delete)
            return _composite.GcEnumeration(plan, etag, {})
        found = self._composite_segment_listing(key)
        try:
            after = self.s3.head_object(Bucket=self.bucket, Key=key)["ETag"]
        except ClientError as e:
            if e.response["Error"]["Code"] not in _NOT_FOUND:
                _reraise_access_denied(e, "composite GC head HeadObject")
                raise
            after = None
        if after != etag:
            return refused("head-moved-during-enumeration")
        plan = _composite.plan_gc([(n, m["last_modified"], m["size"]) for n, m in found.items()], head, ledger,
                                  now, grace_s=grace_s, max_delete=max_delete)
        items = {n: {**found[n], "first_seen": plan.ledger[n]} for n in plan.delete}
        return _composite.GcEnumeration(plan, etag, items)

    def _composite_segment_listing(self, key: str) -> dict:
        """name -> {size, last_modified (epoch seconds), etag} for every object under the segment directory
        of the store whose head is at `key`, names relative to that directory, read to the END of the
        listing. A listing that says it is truncated and gives no token to continue is an error, never
        a short answer or a loop."""
        prefix = _composite.segment_s3_key(key, "")
        expected = self._customer_prefix() + self.env_id + "/"
        assert prefix.startswith(expected), (
            f"composite GC prefix {prefix!r} escapes customer/env scope {expected!r} "
            "— IAM ListBucket is prefix-conditioned on it")
        found: dict = {}
        token = None
        while True:
            kw = dict(Bucket=self.bucket, Prefix=prefix)
            if token:
                kw["ContinuationToken"] = token
            try:
                resp = self.s3.list_objects_v2(**kw)
            except ClientError as e:
                _reraise_access_denied(e, "composite GC ListObjectsV2")
                raise
            for c in resp.get("Contents", []):
                name = c["Key"][len(prefix):]
                if name:
                    found[name] = {"size": int(c["Size"]), "last_modified": c["LastModified"].timestamp(),
                                   "etag": c["ETag"]}
            if not resp.get("IsTruncated"):
                return found
            token = resp.get("NextContinuationToken")
            if not token:
                raise _composite.CompositeError("segment listing truncated without a continuation token")

    def _composite_object_versions(self, obj_key: str) -> list:
        """Every version and delete marker of exactly `obj_key`, newest first, read to the END of the listing:
        dicts of `version_id`, `is_latest`, `marker`, `etag` (None on a marker), `size` and `last_modified`
        (epoch seconds). The delete pass lists the chain of each orphan it archives, so it can remove exactly
        those versions (`_owncloud_composite.plan_version_delete`). Needs ListBucketVersions, which a principal
        that can list objects may still lack: that is an OwnCloudPermissionError, never a short answer. A
        listing that says it is truncated and gives no key marker to continue is an error, never a loop. An
        unversioned store lists each object once with the version id 'null'."""
        expected = self._customer_prefix() + self.env_id + "/"
        assert obj_key.startswith(expected), (
            f"composite GC version prefix {obj_key!r} escapes customer/env scope {expected!r} "
            "— IAM ListBucketVersions is prefix-conditioned on it")
        chain: list = []
        kw = dict(Bucket=self.bucket, Prefix=obj_key)
        while True:
            try:
                resp = self.s3.list_object_versions(**kw)
            except ClientError as e:
                _reraise_access_denied(e, "composite GC ListObjectVersions")
                raise
            for entry, marker in ([(v, False) for v in resp.get("Versions", [])]
                                  + [(m, True) for m in resp.get("DeleteMarkers", [])]):
                if entry["Key"] == obj_key:  # the prefix also matches longer keys
                    chain.append({"version_id": entry["VersionId"], "is_latest": bool(entry.get("IsLatest")),
                                  "marker": marker, "etag": None if marker else entry.get("ETag"),
                                  "size": 0 if marker else int(entry.get("Size", 0)),
                                  "last_modified": entry["LastModified"].timestamp()})
            if not resp.get("IsTruncated"):
                return sorted(chain, key=lambda v: -v["last_modified"])
            if not resp.get("NextKeyMarker"):
                raise _composite.CompositeError("version listing truncated without a key marker")
            kw["KeyMarker"] = resp["NextKeyMarker"]
            if resp.get("NextVersionIdMarker"):
                kw["VersionIdMarker"] = resp["NextVersionIdMarker"]
            else:
                kw.pop("VersionIdMarker", None)

    def _gc_read_head(self, key: str) -> Optional[bytes]:
        """The decoded head object at `key`, or None when it is absent (composite GC)."""
        try:
            return _codec_decode_response(self.s3.get_object(Bucket=self.bucket, Key=key), key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                return None
            _reraise_access_denied(e, "composite GC head GetObject")
            raise

    def _gc_head(self, key: str) -> Optional[dict]:
        """The HeadObject response of the current object at `key`, or None when there is none. A delete marker
        reads as absent (composite GC)."""
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                return None
            _reraise_access_denied(e, "composite GC HeadObject")
            raise

    def _gc_present(self, key: str) -> bool:
        """Is there a current object at `key`? A delete marker reads as absent (composite GC)."""
        return self._gc_head(key) is not None

    def _gc_delete(self, key: str, version_id: Optional[str] = None) -> None:
        """One DeleteObject for an orphan (composite GC): the key itself, or exactly the version `version_id`.
        A version that is already gone is the state asked for, not an error (S3 answers 204; a store that says
        NoSuchVersion is read the same way): the read-back after the deletes is what judges the outcome."""
        kw = dict(Bucket=self.bucket, Key=key)
        if version_id is not None:
            kw["VersionId"] = version_id
        try:
            self.s3.delete_object(**kw)
        except ClientError as e:
            if version_id is not None and e.response["Error"]["Code"] in (_NOT_FOUND | {"NoSuchVersion"}):
                return
            _reraise_access_denied(e, "composite GC DeleteObject")
            raise

    def _gc_put_receipt(self, receipt_key: str, receipt: dict) -> None:
        try:
            self.s3.put_object(Bucket=self.bucket, Key=receipt_key,
                               Body=json.dumps(receipt, indent=1, sort_keys=True).encode("utf-8"))
        except ClientError as e:
            _reraise_access_denied(e, "composite GC receipt PutObject")
            raise

    def _composite_archive_names(self) -> set:
        """g-358-202 (U30): every directory name directly under `<env root>_composite-gc-archive/`: run ids, `_state`, `_pruned`
        and any stray name alike, read to the END of the listing. `composite_gc_runs` keeps the run ids; the prune pass hands the
        whole set to its planner, which reports what is neither. A listing that says it is truncated and gives no continuation
        token is an error, never a loop."""
        prefix = self._customer_prefix() + self.env_id + "/" + _composite.GC_ARCHIVE_DIR + "/"
        names = set()
        token = None
        while True:
            kw = dict(Bucket=self.bucket, Prefix=prefix, Delimiter="/")
            if token:
                kw["ContinuationToken"] = token
            try:
                resp = self.s3.list_objects_v2(**kw)
            except ClientError as e:
                _reraise_access_denied(e, "composite GC archive ListObjectsV2")
                raise
            for cp in resp.get("CommonPrefixes", []):
                names.add(cp["Prefix"][len(prefix):].rstrip("/"))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
            if not token:
                raise _composite.CompositeError("archive listing truncated without a continuation token")
        return names

    def composite_gc_runs(self) -> List[str]:
        """g-358-202 (U14): the run ids of this environment's composite GC archives, oldest first: the directories directly under
        `<env root>_composite-gc-archive/` whose name is a run id (the `_state` directory and any stray name are not). Listing the
        archive is how a late restore finds the runs it owes a look, so a pass that died before it could record its own run
        still gets one. A listing that says it is truncated and gives no continuation token is an error, never a loop."""
        return sorted(n for n in self._composite_archive_names() if _composite.gc_run_time(n) is not None)

    def composite_gc_state_get(self, path: PathLike, doc: str) -> Optional[dict]:
        """g-358-202 (U14): one of the scheduled pass's two documents (`_composite.GC_STATE_DOCS`) for the composite store
        at `path`, parsed, or None when it has never been written. ONLY an object that is not there is None: every other
        failure raises (an access denial, a transport error, a body that is not JSON), because an unreadable ledger is
        not an empty one and a caller that took it for empty would overwrite the real one with a fresh start."""
        key = self._s3_key(path)
        rel = self._rel_of_key(key)
        if not _composite.reads_composite(rel):
            raise _composite.CompositeError("%r is not a composite store" % rel)
        skey = _composite.gc_state_key(self._customer_prefix() + self.env_id + "/", rel, doc)
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=skey)["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                return None
            _reraise_access_denied(e, "composite GC state GetObject")
            raise
        return json.loads(body.decode("utf-8"))

    def composite_gc_state_put(self, path: PathLike, doc: str, obj: dict) -> None:
        """g-358-202 (U14): write one scheduled-pass document (see `composite_gc_state_get`): one PutObject of compact JSON
        beside the archive, outside the governed roots, so the sync layer never mirrors it."""
        key = self._s3_key(path)
        rel = self._rel_of_key(key)
        if not _composite.reads_composite(rel):
            raise _composite.CompositeError("%r is not a composite store" % rel)
        skey = _composite.gc_state_key(self._customer_prefix() + self.env_id + "/", rel, doc)
        try:
            self.s3.put_object(Bucket=self.bucket, Key=skey,
                               Body=json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        except ClientError as e:
            _reraise_access_denied(e, "composite GC state PutObject")
            raise

    def composite_gc_apply(self, path: PathLike, ledger, now: float, *,
                           grace_s: float = _composite.GC_GRACE_S,
                           max_delete: int = _composite.GC_MAX_DELETE,
                           batch: int = _composite.GC_DELETE_BATCH) -> _composite.GcApplied:
        """g-358-202 (U2e): the DESTRUCTIVE half of orphan collection for the composite store at `path`.

        Inert unless OWNCLOUD_COMPOSITE_GC names this environment (`_composite.should_gc`): with the flag unset it
        returns `stopped='gc-not-enabled'` having made no S3 call, and a grace window under `GC_GRACE_S` is
        refused the same way (`grace-below-floor`). The writer's path never calls it.

        The pass: enumerate (`composite_gc_enumerate`, read-only: any refusal there stops the pass with nothing
        done); ARCHIVE every object the plan names to `_composite.gc_archive_key` and read the copy back, keeping
        an object whose bytes do not hash to the md5 its name carries or whose copy reads back different; write
        the run's RECEIPT.json; only then delete, by single `delete_object` calls in batches of `batch`, reading
        the head before each batch and keeping any name it now lists, HEADing each object just before its delete
        and keeping one re-written since the listing (a writer's re-PUT of an old segment it found present,
        `moved_since_listing`), and reading each delete back; finally
        `composite_gc_restore`, which puts back from the archive any deleted name the head names after all (the
        writer treats a 412 on a segment PUT as 'already there' and re-PUTs only an old one, so a head can still
        commit over an object this pass deletes in the gap between that HEAD and the delete). What is deleted is
        the archive's manifest, never the plan: an object the archive does not
        verifiably hold is never deleted.

        On a VERSIONED store (the archive's GET returns a VersionId) the pass lists each archived object's chain
        of versions and delete markers (`_composite_object_versions`, recorded in the receipt before any delete)
        and deletes exactly those versions BY ID, the latest last, instead of the key
        (`_owncloud_composite.plan_version_delete`). The re-check before the delete is by VersionId: the HEAD's
        must be the listed latest, else the name is kept as rewritten-since-listing, so a writer's re-PUT of an
        old segment (a version the listing never saw) survives even when it lands between that HEAD and the
        delete. A listed version whose ETag differs from the archived bytes keeps the whole name
        (version-bytes-not-archived: the archive holds the current version only). No delete marker and no
        noncurrent version is left, so the archive is the only copy of a collected segment. An unversioned store
        (no VersionId in the responses) keeps the key delete and the last_modified re-check.

        A permission failure raises OwnCloudPermissionError and any other S3 error propagates. Before the first
        delete nothing is lost (an archive object is harmless); after it the receipt written first still names
        every object and where its copy is, and the restore step still runs. The caller persists the returned
        ledger; `restored` is never expected to be non-empty, and when it is the pass has met the 412 window."""
        key = self._s3_key(path)
        rel = self._rel_of_key(key)

        def result(stopped, plan=None, led=None, run_id=None, deleted=(), restored=(), skipped=None):
            return _composite.GcApplied(stopped, plan, dict(ledger if led is None else led), run_id,
                                        list(deleted), list(restored), dict(skipped or {}))

        if not _composite.should_gc(rel, self.env_id):
            return result("gc-not-enabled")
        if not _composite.gc_grace_ok(grace_s):
            return result("grace-below-floor")
        enum = self.composite_gc_enumerate(path, ledger, now, grace_s=grace_s, max_delete=max_delete)
        plan = enum.plan
        if plan.refused:
            return result(plan.refused[0], plan, plan.ledger)
        env_root = self._customer_prefix() + self.env_id + "/"
        run_id = _composite.gc_run_id(now, plan.delete)
        archived: dict = {}
        skipped: dict = {}
        for name in plan.delete:
            src = _composite.segment_s3_key(key, name)
            dst = _composite.gc_archive_key(env_root, run_id, rel, name)
            try:
                obj = self.s3.get_object(Bucket=self.bucket, Key=src)
            except ClientError as e:
                if e.response["Error"]["Code"] in _NOT_FOUND:
                    skipped[name] = "gone-since-listing"
                    continue
                _reraise_access_denied(e, "composite GC segment GetObject")
                raise
            raw = obj["Body"].read()
            enc, meta = obj.get("ContentEncoding"), dict(obj.get("Metadata") or {})
            try:
                plain = _codec_decode(raw, content_encoding=enc, metadata=meta, key=src)
            except _CodecError:
                skipped[name] = "undecodable"
                continue
            plain_md5 = name.rsplit(".", 2)[1]  # the md5 in '<asp>/<token>.<md5>.jsonl'
            if hashlib.md5(plain).hexdigest() != plain_md5:
                skipped[name] = "content-does-not-match-its-name"
                continue
            kw = dict(Bucket=self.bucket, Key=dst, Body=raw)
            if enc:
                kw["ContentEncoding"] = enc
            if meta:
                kw["Metadata"] = meta
            try:
                self.s3.put_object(**kw)
            except ClientError as e:
                _reraise_access_denied(e, "composite GC archive PutObject")
                raise
            if self.s3.get_object(Bucket=self.bucket, Key=dst)["Body"].read() != raw:
                skipped[name] = "archive-readback-mismatch"
                continue
            version_id = obj.get("VersionId")  # absent on an unversioned store; 'null' on a suspended one
            versions = self._composite_object_versions(src) if version_id not in (None, "null") else []
            archived[name] = {"source_key": src, "archive_key": dst, "size": len(raw),
                              "md5": hashlib.md5(raw).hexdigest(), "plain_md5": plain_md5,
                              "content_encoding": enc, "metadata": meta, "etag": obj.get("ETag"),
                              "version_id": version_id, "versions": versions,
                              "last_modified": enum.items[name]["last_modified"],
                              "first_seen": enum.items[name]["first_seen"]}
        if not archived:
            return result(None, plan, plan.ledger, skipped=skipped)
        receipt_key = _composite.gc_receipt_key(env_root, run_id)
        receipt = {"kind": "composite-gc-archive", "format": 1, "run_id": run_id, "store": rel, "head_key": key,
                   "head_etag": enum.head_etag, "grace_s": grace_s,
                   "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)), "status": "archived",
                   "objects": archived, "deleted": [], "restored": [], "skipped": skipped,
                   "restore": ("An object's copy is at its archive_key. To put one back: GET it, check its md5 equals "
                               "`md5`, and PUT it at `source_key` on this same endpoint with the recorded "
                               "content_encoding and metadata. OwnCloudBackend.composite_gc_restore does that for "
                               "every object here that the current head names and the store lacks. Never restore "
                               "into a local mirror: the sync layer would push it as a file. On a versioned store "
                               "the pass deletes by version id: `versions` names what was removed, and no "
                               "noncurrent copy remains, so the archive object is the only copy.")}
        self._gc_put_receipt(receipt_key, receipt)
        deleted: list = []
        restored: list = []
        stopped = None
        status = "done"
        step = max(1, int(batch))
        names = [n for n in plan.delete if n in archived]
        try:
            for i in range(0, len(names), step):
                head = self._gc_read_head(key)
                if head is None or not _composite.is_head(head):
                    stopped = "head-changed-layout"
                    status = "stopped: " + stopped
                    break
                named = _composite.head_object_names(head)
                for name in names[i:i + step]:
                    if name in named:
                        skipped[name] = "re-referenced"
                        continue
                    rec = archived[name]
                    current = self._gc_head(rec["source_key"])
                    if current is None:
                        skipped[name] = "gone-since-archive"
                        continue
                    if rec["version_id"] not in (None, "null"):  # a versioned store: remove exactly the versions listed
                        verdict = _composite.plan_version_delete(rec["versions"], rec["last_modified"],
                                                                 current.get("VersionId"), rec["etag"])
                        if verdict.keep:
                            skipped[name] = verdict.keep
                            continue
                        for vid in verdict.order:
                            self._gc_delete(rec["source_key"], vid)
                        after = self._gc_head(rec["source_key"])
                        if after is None:
                            deleted.append(name)
                        elif after.get("VersionId") in {v["version_id"] for v in rec["versions"]}:
                            skipped[name] = "delete-not-effective"
                        else:  # present, but as a version the listing never saw: a writer's re-PUT, which survives
                            skipped[name] = "rewritten-since-listing"
                        continue
                    if _composite.moved_since_listing(rec["last_modified"], _last_modified_epoch(current)):
                        skipped[name] = "rewritten-since-listing"
                        continue
                    self._gc_delete(rec["source_key"])
                    if self._gc_present(rec["source_key"]):
                        skipped[name] = "delete-not-effective"
                    else:
                        deleted.append(name)
        except Exception as exc:
            status = "stopped: " + type(exc).__name__
            raise
        finally:
            try:
                restored = self.composite_gc_restore(path, run_id)
            finally:
                receipt.update(status=status, deleted=deleted, restored=restored, skipped=skipped)
                self._gc_put_receipt(receipt_key, receipt)
        return result(stopped, plan, {n: t for n, t in plan.ledger.items() if n not in deleted}, run_id,
                      deleted, restored, skipped)

    def composite_gc_restore(self, path: PathLike, run_id: str) -> List[str]:
        """g-358-202 (U2e): put back from the archive of delete pass `run_id` every object that the CURRENT head
        names and the store lacks; returns their names. It reads the live head and the live store, never the
        receipt's own list of what was deleted, so it is right after a pass that died half way and right for a
        late sweep; a name the head does not list, or that is present, is left alone, which also makes it
        idempotent. The copy is checked against the md5 in the receipt before it is PUT (a mismatch raises
        CompositeError and puts nothing back). It needs no GC flag: recovery must not depend on the switch that
        enables deletion."""
        key = self._s3_key(path)
        rel = self._rel_of_key(key)
        if not _composite.reads_composite(rel):
            raise _composite.CompositeError("%r is not a composite store" % rel)
        env_root = self._customer_prefix() + self.env_id + "/"
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=_composite.gc_receipt_key(env_root, run_id))["Body"].read()
        except ClientError as e:
            _reraise_access_denied(e, "composite GC receipt GetObject")
            raise
        objects = json.loads(body.decode("utf-8"))["objects"]
        head = self._gc_read_head(key)
        if head is None or not _composite.is_head(head):
            return []
        named = _composite.head_object_names(head)
        restored: List[str] = []
        for name in sorted(objects):
            rec = objects[name]
            if rec["source_key"] != _composite.segment_s3_key(key, name):
                raise _composite.CompositeError("receipt %s describes another store" % run_id)
            if name not in named or self._gc_present(rec["source_key"]):
                continue
            raw = self.s3.get_object(Bucket=self.bucket, Key=rec["archive_key"])["Body"].read()
            if hashlib.md5(raw).hexdigest() != rec["md5"]:
                raise _composite.CompositeError("archive object %s no longer matches its receipt" % rec["archive_key"])
            kw = dict(Bucket=self.bucket, Key=rec["source_key"], Body=raw)
            if rec.get("content_encoding"):
                kw["ContentEncoding"] = rec["content_encoding"]
            if rec.get("metadata"):
                kw["Metadata"] = rec["metadata"]
            self.s3.put_object(**kw)
            restored.append(name)
        return restored

    def _composite_run_listing(self, run_id: str) -> dict:
        """g-358-202 (U30): key -> size for every object under one archive run's prefix, read to the END of the listing. The run id
        is checked by `gc_run_prefix`, so the listing cannot leave the run's directory. A listing that says it is truncated and
        gives no continuation token is an error, never a short answer."""
        prefix = _composite.gc_run_prefix(self._customer_prefix() + self.env_id + "/", run_id)
        found: dict = {}
        token = None
        while True:
            kw = dict(Bucket=self.bucket, Prefix=prefix)
            if token:
                kw["ContinuationToken"] = token
            try:
                resp = self.s3.list_objects_v2(**kw)
            except ClientError as e:
                _reraise_access_denied(e, "composite GC archive run ListObjectsV2")
                raise
            for c in resp.get("Contents", []):
                found[c["Key"]] = int(c["Size"])
            if not resp.get("IsTruncated"):
                return found
            token = resp.get("NextContinuationToken")
            if not token:
                raise _composite.CompositeError("archive run listing truncated without a continuation token")

    def _composite_json_object(self, key: str, what: str) -> Optional[dict]:
        """g-358-202 (U30): the JSON object stored at `key` (an archive run's receipt or its tombstone, named by `what`), or None when
        there is none or the body is not a JSON object (the planner keeps a run with no receipt as `no-receipt`). ONLY an object that
        is not there, or a body that is not a JSON object, reads as None: every other failure raises, because a record that could
        not be read is not a record that is not there."""
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                return None
            _reraise_access_denied(e, "composite GC archive %s GetObject" % what)
            raise
        try:
            obj = json.loads(body.decode("utf-8"))
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    def _composite_run_receipt(self, run_id: str) -> Optional[dict]:
        """g-358-202 (U30): the parsed RECEIPT.json of an archive run, or None (`_composite_json_object`)."""
        return self._composite_json_object(_composite.gc_receipt_key(self._customer_prefix() + self.env_id + "/", run_id), "receipt")

    def _gc_needed_names(self, key: str):
        """g-358-202 (U30): `(needed, why)` for the store whose head is at `key`: the segment names the head names and the store
        lacks, which are the only archive objects `composite_gc_restore` could still take, or `(None, reason)` when that cannot be
        said ('head-missing', 'head-not-composite', 'head-moved-during-read'). The head is read, the segment directory listed to
        its end, and the head HEADed again: a head that moved in between is not an answer."""
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                return None, "head-missing"
            _reraise_access_denied(e, "composite GC head GetObject")
            raise
        head, etag = _codec_decode_response(obj, key=key), obj["ETag"]
        if not _composite.is_head(head):
            return None, "head-not-composite"
        listed = self._composite_segment_listing(key)
        after = self._gc_head(key)
        if after is None or after["ETag"] != etag:
            return None, "head-moved-during-read"
        return frozenset(_composite.head_object_names(head)) - frozenset(listed), None

    def composite_gc_prune_enumerate(self, path: PathLike, now: float, *,
                                     prune_after_s: float = _composite.GC_PRUNE_AFTER_S,
                                     max_runs: int = _composite.GC_PRUNE_MAX_RUNS) -> _composite.PruneEnumeration:
        """g-358-202 (U30): the READ-ONLY half of archive pruning for the composite store at `path`: which archive runs a prune pass
        may remove (`plan_archive_prune`) and, for each, the keys it may remove or why that run stays (`plan_run_removal`). It lists
        the archive's top-level names, GETs the receipt of each run at least `prune_after_s` old (a younger run needs none),
        reads the head and the segment directory for `needed`, and lists each planned run's prefix. It never PUTs, deletes or
        copies and does not consult any flag: a box that has not been armed can still say what a pass would do. `store-not-allowlisted`
        is answered before any S3 call. An S3 error propagates: a pass that failed has no plan."""
        key = self._s3_key(path)
        rel = self._rel_of_key(key)
        if not _composite.reads_composite(rel):
            return _composite.PruneEnumeration(_composite.PrunePlan([], {}, ["store-not-allowlisted"], [], {}, {}), {}, None)
        env_root = self._customer_prefix() + self.env_id + "/"
        names = sorted(self._composite_archive_names())
        receipts: dict = {}
        for name in names:
            when = _composite.gc_run_time(name)
            try:
                old_enough = when is not None and now - when >= prune_after_s
            except TypeError:  # a clock or window that is not a number: the planner refuses on it below, and no receipt is read for it
                old_enough = False
            if old_enough:
                receipts[name] = self._composite_run_receipt(name)
        needed, why = self._gc_needed_names(key)
        plan = _composite.plan_archive_prune(names, receipts, needed, now, rel, prune_after_s, max_runs)
        runs = {run_id: _composite.plan_run_removal(run_id, receipts[run_id], self._composite_run_listing(run_id), env_root)
                for run_id in plan.prune}
        return _composite.PruneEnumeration(plan, runs, why)

    def composite_gc_prune_control(self, now: float, *, token: Optional[str] = None) -> _composite.PruneControl:
        """g-358-202 (U30): the control a prune pass runs before its first removal, and the one to run BEFORE the prune flag is
        set (guard-1301). It needs no flag: it touches one throwaway sentinel under `_state/_prune-control/` that it made itself,
        and removes every version of it before it returns.

        It does to the sentinel what the pass does to a run's object. PUT it (the response must carry a VersionId that is not
        'null': the store is versioned), delete it by the same plain single call, and read its versions back: exactly one delete
        marker, newest, and exactly one noncurrent version, the PUT's, at the sentinel's size. Restore from that noncurrent
        version, compare its md5 with the original's, and put the restored bytes back as the current object. Any miss stops the
        pass (`failed` names it) and the pass removes nothing. A sentinel key that already has ANY entry is refused
        ('control-key-not-fresh') before the PUT, so the cleanup only ever removes versions this call made. The control proves the
        delete is undoable on this store NOW; that the undo window is long enough is a reading of the bucket's lifecycle
        configuration, taken by the storage host at arming (design record, U30). It needs ListBucketVersions, like the delete pass."""
        env_root = self._customer_prefix() + self.env_id + "/"
        token = os.urandom(16).hex() if token is None else token
        key = _composite.gc_prune_control_key(env_root, token)
        body = ("composite gc prune control %s %s\n" % (token, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))).encode("utf-8")
        md5 = hashlib.md5(body).hexdigest()
        evidence: dict = {"key": key, "size": len(body), "md5": md5}
        if self._composite_object_versions(key):
            return _composite.PruneControl(False, "control-key-not-fresh", evidence)
        failed = None
        try:
            vid = self.s3.put_object(Bucket=self.bucket, Key=key, Body=body).get("VersionId")
            if vid in (None, "null"):
                failed = "control-store-not-versioned"
            elif self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read() != body:
                failed = "control-readback-mismatch"
            else:
                evidence["version_id"] = vid
                self._gc_delete(key)
                if self._gc_present(key):
                    failed = "control-delete-not-effective"
                else:
                    chain = self._composite_object_versions(key)
                    markers = [v for v in chain if v["marker"]]
                    noncurrent = [v for v in chain if not v["marker"]]
                    if not (len(markers) == 1 and markers[0]["is_latest"] and len(noncurrent) == 1
                            and noncurrent[0]["version_id"] == vid and noncurrent[0]["size"] == len(body)):
                        failed = "control-delete-left-no-recoverable-version"
                    else:
                        evidence["marker_version_id"] = markers[0]["version_id"]
                        back = self.s3.get_object(Bucket=self.bucket, Key=key, VersionId=vid)["Body"].read()
                        if hashlib.md5(back).hexdigest() != md5:
                            failed = "control-restore-mismatch"
                        else:
                            self.s3.put_object(Bucket=self.bucket, Key=key, Body=back)
                            if self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read() != body:
                                failed = "control-restored-copy-mismatch"
        finally:
            for entry in self._composite_object_versions(key):
                self._gc_delete(key, entry["version_id"])
            clean = not self._composite_object_versions(key)
        evidence["cleanup"] = "clean" if clean else "left"
        if failed is None and not clean:
            failed = "control-cleanup-failed"
        return _composite.PruneControl(failed is None, failed, evidence)

    def _composite_prune_run(self, run_id: str, receipt: dict, removal, control, now: float, prune_after_s: float,
                             rel: str, env_root: str) -> Optional[str]:
        """g-358-202 (U30): remove one planned run. Returns why it stopped, or None when the run is gone.

        The ORDER is the protocol. Each object the run's receipt names and its listing holds goes by a plain single
        `delete_object` (never by version id, so a versioned store keeps it as a noncurrent version for the bucket's undo window)
        after a HEAD shows it still has the receipt's size, and is read back absent. Only then is the tombstone written to
        `_pruned/<run id>/RECEIPT.json` and read back equal, and only after that is the run's own RECEIPT.json deleted and read back
        absent. The receipt therefore stays until the tombstone is stored, so the record of what was removed never has a gap, and
        a pass that dies anywhere leaves a run whose receipt still reads `done`: the next pass finishes it (guard-4747). A tombstone
        rewritten by that next pass keeps the version ids an earlier tombstone of the run recorded, so the way back is never lost."""
        sizes = {r["archive_key"]: r["size"] for r in receipt["objects"].values()}
        tkey = _composite.gc_pruned_key(env_root, run_id)
        earlier = (self._composite_json_object(tkey, "tombstone") or {}).get("removed")  # read first: a read that fails stops the run before it deletes
        removed: dict = {}
        for akey in removal.delete:
            head = self._gc_head(akey)
            if head is None:
                continue  # gone since the listing, which is the state asked for
            if int(head["ContentLength"]) != sizes[akey]:
                return "changed-since-listing"
            self._gc_delete(akey)
            if self._gc_present(akey):
                return "delete-not-effective"
            removed[akey] = head.get("VersionId")
        if isinstance(earlier, dict):  # a pass that died after writing this tombstone and before the receipt went: the rewrite keeps its version ids
            removed = {**earlier, **removed}
        gone = [k for k in removal.gone if k not in removed]
        tombstone = {"kind": "composite-gc-archive-pruned", "format": 1, "run_id": run_id, "store": rel, "status": "pruned",
                     "pruned_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)), "window_s": prune_after_s,
                     "objects": {n: {k: r.get(k) for k in ("archive_key", "source_key", "size", "md5", "plain_md5", "etag")}
                                 for n, r in receipt["objects"].items()},
                     "removed": removed, "gone": gone, "control": control.evidence,
                     "restore": ("Each removed object went by a plain delete, so on a versioned store it is a noncurrent version "
                                 "(`removed` maps its archive_key to that version id) until the bucket's noncurrent-version window "
                                 "ends: GET it with that VersionId and PUT the bytes back at its archive_key. After the window the "
                                 "run is gone, which is what pruning is for. Never restore into a local mirror.")}
        self._gc_put_receipt(tkey, tombstone)
        if json.loads(self.s3.get_object(Bucket=self.bucket, Key=tkey)["Body"].read().decode("utf-8")) != tombstone:
            return "tombstone-readback-mismatch"
        rkey = _composite.gc_receipt_key(env_root, run_id)
        self._gc_delete(rkey)
        if self._gc_present(rkey):
            return "receipt-delete-not-effective"
        return None

    def composite_gc_prune_apply(self, path: PathLike, now: float, *,
                                 prune_after_s: float = _composite.GC_PRUNE_AFTER_S,
                                 max_runs: int = _composite.GC_PRUNE_MAX_RUNS) -> _composite.PruneApplied:
        """g-358-202 (U30): the DESTRUCTIVE half of archive pruning for the composite store at `path`. Nothing calls it yet: it lands
        dark (guard-1301) and is armed by its own flag.

        Inert unless OWNCLOUD_COMPOSITE_GC_PRUNE names this environment (`_composite.should_prune_archive`): with the flag unset it
        returns `stopped='prune-not-enabled'` having made no S3 call. The pass: enumerate (`composite_gc_prune_enumerate`: any
        refusal stops it with nothing done; a plan with no removable run returns without a write); run the control
        (`composite_gc_prune_control`: a miss removes nothing); then, run by run, oldest first, re-read what the plan was computed
        from INSIDE the removal (guard-5952): the head's needed set, the run's receipt and its listing, and re-plan that run
        (`plan_archive_prune` for the one run, `plan_run_removal`), so a head that began naming an archived object after the
        enumeration keeps its run. A run that stays for a reason is skipped and the pass goes on; a run that cannot be finished
        (an object changed, a delete not effective, a tombstone that does not read back) stops the pass. The removal itself is
        `_composite_prune_run`. An S3 error propagates, and a permission failure raises OwnCloudPermissionError; after a death at
        any point the receipt of every unfinished run still reads `done` and the next pass finishes it."""
        key = self._s3_key(path)
        rel = self._rel_of_key(key)

        def result(stopped, plan=None, control=None, pruned=(), skipped=None):
            return _composite.PruneApplied(stopped, plan, control, list(pruned), dict(skipped or {}))

        if not _composite.should_prune_archive(rel, self.env_id):
            return result("prune-not-enabled")
        enum = self.composite_gc_prune_enumerate(path, now, prune_after_s=prune_after_s, max_runs=max_runs)
        plan = enum.plan
        if plan.refused:
            why = plan.refused[0]
            return result("%s: %s" % (why, enum.needed_why) if why == "needed-unknown" and enum.needed_why else why, plan)
        skipped = {r: enum.runs[r].keep for r in plan.prune if enum.runs[r].keep is not None}
        todo = [r for r in plan.prune if enum.runs[r].keep is None]
        if not todo:
            return result(None, plan, None, [], skipped)
        control = self.composite_gc_prune_control(now)
        if not control.ok:
            return result("control-failed: %s" % control.failed, plan, control, [], skipped)
        env_root = self._customer_prefix() + self.env_id + "/"
        pruned: list = []
        for run_id in todo:
            needed, why = self._gc_needed_names(key)
            if needed is None:
                return result("needed-unknown: %s" % why, plan, control, pruned, skipped)
            receipt = self._composite_run_receipt(run_id)
            fresh = _composite.plan_archive_prune([run_id], {run_id: receipt}, needed, now, rel, prune_after_s, 1)
            if fresh.prune != [run_id]:
                skipped[run_id] = fresh.kept.get(run_id) or "refused: %s" % ",".join(fresh.refused)
                continue
            removal = _composite.plan_run_removal(run_id, receipt, self._composite_run_listing(run_id), env_root)
            if removal.keep:
                skipped[run_id] = removal.keep
                continue
            stopped = self._composite_prune_run(run_id, receipt, removal, control, now, prune_after_s, rel, env_root)
            if stopped:
                skipped[run_id] = stopped
                return result("run-stopped: %s" % stopped, plan, control, pruned, skipped)
            pruned.append(run_id)
        return result(None, plan, control, pruned, skipped)

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
        # g-358-40: try the LAN object cache before spending S3 egress. `etag`
        # here is the value HEADed from S3 a few lines above, so this asks for
        # one specific, already-verified content hash. None -> unchanged path.
        #
        # ORDERING IS DELIBERATE — do NOT hoist this above _range_tail_pull.
        # The tail pull moves only the appended DELTA over the internet; a cache
        # hit moves the WHOLE object over the LAN. For an append-mostly store
        # (1 KB appended to a 100 MB file) the range GET is far cheaper in real
        # terms even though the LAN looks "free", and it also leaves the object
        # out of the cache's working set. Cheapest-transfer-first is the rule:
        # range GET -> LAN cache -> full S3 GET.
        body = self._cache_fetch(key, etag)
        if body is None:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            # g-358-11: the mirror holds DECODED bytes — every consumer above
            # the backend keeps reading plaintext, whatever the encoding.
            body = _codec_decode_response(obj, key=key)
        # g-358-202: a composite HEAD is not the file. Join it with its segments so the mirror, the
        # baseline stamp and the fence below all describe the whole legacy file; `etag` follows the
        # head the bytes were joined from. Anything but an allowlisted store's head passes through.
        body, etag = self._composite_whole(key, body, etag, local)
        _atomic_write_local(local, body)
        self._etags[key] = etag
        # Reaching this line requires verdict=="download" or local-absent --
        # both mean S3 is authoritative and the mirror now byte-equals S3
        # (as plaintext), which is precisely the state the baseline records.
        self._stamp_manifest_baseline(path, body, etag=etag)
        return local

    def _stamp_manifest_baseline(self, path: PathLike, body: bytes,
                                 etag: Optional[str] = None,
                                 record_mtime: bool = True) -> None:
        """Stamp the persistent sync-manifest baseline for a just-pushed key
        (g-115-1946 — root fix for the cross-box lost-update lanes).

        `record_mtime=False` (g-115-7257) stamps {md5, etag} only, for a PUT
        that did not write the local file. The file's mtime then proves nothing
        about `body`: another writer may have changed the file since `body` was
        read, and pairing the new mtime with the old md5 would make the sweep's
        mtime shortcut skip that change. With no mtime the next sweep compares
        the md5 once, which is always right.

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
                entry = {"md5": hashlib.md5(body).hexdigest()}
                if record_mtime:
                    entry = {"mtime": local.stat().st_mtime_ns, **entry}
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
            # _rel raises for BOTH a genuinely out-of-root (git-shipped) path
            # and a merely RELATIVE one -- they are indistinguishable there, but
            # they need OPPOSITE handling. Out-of-root is never on S3, so the
            # local read below is correct and deliberate (see docstring); do not
            # delete it. A RELATIVE path is a CALLER BUG, and swallowing it here
            # hands back local-mirror bytes from the one API whose contract is
            # "NEVER touches the local mirror" -- a false all-clear produced by
            # the very API built to prevent false all-clears (guard-980 class,
            # inside the guard-980 remedy).
            #
            # Measured on cc-02 2026-07-31 (g-115-4256): a fleet probe built on
            # Path('agents')/name/... reported local==authoritative for all 5
            # agents; the same probe with .resolve() showed 4 of 5 DIVERGED,
            # confirmed by s3.head_object. Re-measured 2026-09-05, unchanged.
            #
            # Fail loud instead. Resolving here would be worse than raising: it
            # would silently mint an S3 key from the process CWD, so the same
            # relative path would read different objects from different working
            # directories.
            if not Path(path).is_absolute():
                raise ValueError(
                    f"read_authoritative_bytes requires an ABSOLUTE path; got the "
                    f"relative {path!r}. A relative path cannot be mapped to an "
                    f"S3 key and would silently return LOCAL mirror bytes, which "
                    f"this API promises never to do. Resolve it first "
                    f"(Path(...).resolve(), or join it to the governed root)."
                ) from None
            return self._local(path).read_bytes()
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND:
                raise FileNotFoundError(
                    f"absent in S3 store: s3://{self.bucket}/{key}") from e
            _reraise_access_denied(e, "read_authoritative_bytes GetObject")
            raise
        body = _codec_decode_response(obj, key=key)  # g-358-11: plaintext out
        # g-358-202: the WHOLE file, joined from a composite head's segments, still without
        # touching the mirror.
        return self._composite_whole(key, body, obj["ETag"])[0]

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

    def delete(self, path: PathLike) -> bool:
        """Remove `path` from BOTH lanes and verify both. The
        ``StorageBackend.delete`` contract; ``delete_object`` above is the
        store-only HALF of it.

        The two operations are independent and neither implies the other
        (guard-1493): ``delete_object`` drops the S3 object and leaves the
        local mirror, while a bare ``unlink`` drops the mirror and is silently
        RE-MATERIALIZED by read-through on the next read. Until this method
        existed every caller had to chain them by hand — and the failure is
        self-concealing, because verifying only the lane you happened to call
        returns a truthful green while the file survives in the other. So the
        chain and BOTH read-backs live here, once, and no consumer repeats
        them.

        Two asymmetries a caller should know. The store side is a TOMBSTONE,
        not an erasure: the bucket is versioned, so ``delete_object`` writes a
        delete marker and noncurrent versions survive until lifecycle expiry —
        that is the recovery layer, and per archive-before-delete it counts as
        one only for as long as the retention config says it does. The local
        side has no such layer and is final.

        Returns True when either lane held something, False when the path was
        already absent from both (idempotent). Raises OSError when either lane
        still reports present afterwards — never a True over a half-delete."""
        local = self._local(path)
        removed_remote = self.delete_object(path)
        removed_local = local.exists()
        if removed_local:
            local.unlink(missing_ok=True)
        # Drop the freshness stamp with the file — HYGIENE, not a correctness
        # guard, and the distinction is worth stating because the obvious
        # reading is wrong. _refresh's freshness-window short-circuit is gated
        # on `local.exists()` FIRST (see _refresh), and the unlink above has
        # just made that False, so the stamp could not have caused a later read
        # to skip its HEAD whether or not it was popped. The verify below does
        # not depend on it either: exists() issues a raw head_object and never
        # consults this dict. What the pop actually buys is that the dict does
        # not accumulate entries for paths that no longer exist across
        # delete/recreate cycles.
        self._cache_check.pop(str(local), None)
        still_remote = self.exists(path)
        still_local = local.exists()
        if still_remote or still_local:
            raise OSError(
                f"delete incomplete for {path}: store_present={still_remote} "
                f"local_present={still_local}")
        return removed_remote or removed_local

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

        Batch validity deliberately inherits `cache_ttl` (DEFAULT_CACHE_TTL_SECONDS) rather
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
                # _refresh keys _cache_check by the _local spelling, so warm
                # that same spelling (it differs only past ~240 chars).
                io_path = self._local(local)
                if not io_path.exists():
                    stats["skipped_no_local"] += 1
                    continue
                tag = (etag or "").strip('"')
                if "-" in tag:
                    stats["skipped_multipart"] += 1
                    continue
                h = hashlib.md5()
                with open(io_path, "rb") as f:
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
            self._cache_check[str(io_path)] = now
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
        PUT. Its one caller is ``_store_put`` (g-358-202 U12: a composite write
        never calls it), the seam both PUT sites (``_put`` and
        ``_merge_reconcile_put``) go through, and every write path funnels
        through those two, so the flag governs all whole-object writes at one seam.

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

    def _store_put(self, path: PathLike, key: str, body: bytes, kw: dict):
        """The ONE PUT seam of the two write sites (_put, _merge_reconcile_put). `kw` is the
        PUT they began (Bucket, Key, IfMatch or IfNoneMatch: no Body, the codec has not run) and
        `body` the PLAINTEXT store bytes; returns the response of the PUT that commits the write.

        g-358-202 U2d. Unless the composite writer is on for this store (_composite.should_composite:
        the OWNCLOUD_COMPOSITE_STORES flag names this env and the path is allowlisted) this is the
        pre-U2d PUT, untouched. When it is on, the write is: each segment object the old head does not
        already name, created if absent (immutable and content-addressed, so a 412 means the same bytes
        are already there, and `_freshen_segment` makes sure an old one stays), THEN the head under the
        caller's fence. The head PUT is the only commit
        point: a failure or a 412 before it leaves orphan segments and the old head exactly as it was.

        The head is stored PLAIN and padded to HEAD_MIN_BYTES (a gzipped or small head would be inlined
        into the key's metadata file with every retained version: outcome 6 condition C2, rb-12204),
        and its plain-md5 metadata is the md5 of the JOINED bytes, which content_matches compares with
        the local file. A segment is gzipped exactly when the store itself would be. A store under
        MIN_RAW_BYTES, or one `split` refuses, goes whole; that PUT also reverts the layout, which every
        reader tolerates (read_whole returns a whole object unchanged).

        g-358-202 U12. The body is encoded HERE, by `_body_kwargs`, and only for a whole-object PUT (the flag off, a
        store under MIN_RAW_BYTES, a store `split` refuses). `_put` used to encode first, so a composite write gzipped
        the whole 32.6 MB body (0.95 s of a 1.51 s write, U11) and dropped it: the composite plan sends only its
        segments, each through the codec on its own."""
        rel = self._rel(path)
        if not _composite.should_composite(rel, self.env_id):
            return self.s3.put_object(**{**kw, **self._body_kwargs(path, body)})
        plan = None
        if len(body) >= _composite.MIN_RAW_BYTES:
            held = self._composite_heads.get(key)
            old_head = held[1] if held is not None and held[0] == kw.get("IfMatch") else None
            try:
                plan = _composite.plan_write(old_head, body)
            except _composite.NotSplittable as exc:
                if (key, str(exc)) not in self._composite_warned:
                    self._composite_warned.add((key, str(exc)))
                    _LOG.warning("owncloud composite: %s goes whole, the layout refused it: %s", key, exc)
        if plan is None:
            return self.s3.put_object(**{**kw, **self._body_kwargs(path, body)})
        encode = _codec_should_encode(rel, self.env_id)
        for name, seg in plan.segments.items():
            skw = dict(Bucket=self.bucket, Key=_composite.segment_s3_key(key, name), IfNoneMatch="*")
            skw.update(_codec_put_kwargs(seg) if encode else {"Body": seg})
            try:
                self.s3.put_object(**skw)
            except ClientError as e:
                if e.response["Error"]["Code"] not in _PRECONDITION:
                    _reraise_access_denied(e, "composite segment PutObject")
                    raise
                self._freshen_segment(skw)
        head = _composite.pad_head(plan.head)
        hkw = {k: v for k, v in kw.items() if k not in ("Body", "ContentEncoding", "Metadata")}
        hkw["Body"] = head
        hkw["Metadata"] = {_CODEC_META_PLAIN_MD5: hashlib.md5(body).hexdigest()}
        r = self.s3.put_object(**hkw)
        self._composite_heads[key] = (r["ETag"], head)
        return r

    def _freshen_segment(self, skw: dict) -> None:
        """g-358-202 (e): a segment PUT answered 412, so the object is already there, and the head this write is
        about to commit will name it. Make sure it is still there when the head lands: HEAD it, and when it is
        missing (a sweep took it since) or within `FRESHEN_MARGIN_S` of collectable (`_composite.needs_freshen`),
        PUT the same bytes again, unconditionally. The name carries the md5 of the content, so the overwrite
        changes nothing but the object's last_modified, which restarts the delete pass's grace clock (git's
        'freshen' of a loose object) and which that pass re-checks before each delete. A young object is left
        alone, so a segment is re-PUT at most once per GC_GRACE_S - FRESHEN_MARGIN_S however many writes find it.
        Not closed here: a delete that lands between the pass's last check and its delete call, which
        `composite_gc_restore` repairs."""
        try:
            present = self.s3.head_object(Bucket=skw["Bucket"], Key=skw["Key"])
        except ClientError as e:
            if e.response["Error"]["Code"] not in _NOT_FOUND:
                _reraise_access_denied(e, "composite segment HeadObject")
                raise
            present = None
        if present is not None and not _composite.needs_freshen(_last_modified_epoch(present), time.time()):
            return
        try:
            self.s3.put_object(**{k: v for k, v in skw.items() if k != "IfNoneMatch"})
        except ClientError as e:
            _reraise_access_denied(e, "composite segment PutObject")
            raise

    def _put(self, path: PathLike, body: bytes, *,
             local_is_source: bool = False) -> WriteResult:
        # local_is_source=True (mirror_put, g-115-7257): `body` was READ FROM the
        # local file, so the file already holds it and is never rewritten here.
        # See the post-PUT comment below for why the rewrite was not harmless.
        #
        # g-115-1654: machine-local paths (_EXCLUDE_DIRS / _is_machine_local)
        # must NOT be pushed to S3 -- write the local file only, mirroring
        # LocalBackend, so a per-op write (e.g. jsonl_hygiene presence
        # truncation under own-cloud, reached via write_jsonl/append/mirror_put
        # -> _put) shares the LocalBackend writer's backend and S3 never lags
        # local for disposable per-agent telemetry (rb-2396). All writes funnel
        # through _put, so this single guard covers every write path.
        if self._machine_local(path):
            local = self._local(path)
            if not local_is_source:
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
            from owncloud_sync import (
                _own_sid_carrier_path,
                _owned_agents_with_provenance,
            )
            _root = Path(agents_root()).resolve()
            try:
                _agent = Path(path).resolve().relative_to(_root).parts[0]
            except (ValueError, IndexError):
                _agent = None          # not under an agent dir -- not our case
            if _agent is not None:
                _owned, _prov = _owned_agents_with_provenance()
                if _prov == "live-claims" and _agent not in (_owned or set()):
                    # g-306-430: carry the g-306-235 carve-out DOWN to this
                    # layer. That goal exempted THIS session's own
                    # body-heartbeat carrier from the ownership gate in
                    # owncloud_sync -- correctly, and in both of that module's
                    # publication paths. This consult (g-115-8028) is a THIRD,
                    # independent ownership refusal added later at the write
                    # itself, and it did not carry the exemption. Net effect
                    # measured on cc-09 2026-09-04: sync_file's H4a gate admits
                    # the carrier (`would_push: 1`, exempt path resolves), the
                    # PUT one call below refuses it `no_claim`, the file never
                    # reaches S3 from any worker box, and every peer-side reader
                    # of it -- stranded-claim-sweep's foreign-SID grace,
                    # worker_stall's S3 prefix listing, reducer_promotion --
                    # sees `absent` for a demonstrably live Body. The sweep
                    # thread re-attempted the same PUT every cycle: that is the
                    # standing `errors 1 (scanned 6907)` in spawn.log.
                    # A worker Body NEVER holds the claim, so the one file whose
                    # entire purpose is to let it vouch for itself cross-box was
                    # the one file structurally guaranteed to be refused.
                    #
                    # SHAPE per guard-2860: the admitted set has exactly ONE
                    # member BY CONSTRUCTION, not by test -- the path is
                    # COMPUTED from MIND_AGENT + MIND_SID by the same SSOT
                    # helper owncloud_sync's two carve-outs call, never matched
                    # by glob or prefix. A peer's carrier sitting here as a
                    # pulled read-through cache carries a foreign sid and can
                    # never match, so the peer-clobber hole the gate exists to
                    # close stays closed. Reusing the helper rather than
                    # recomputing is deliberate: three copies of this predicate
                    # would be three things to keep in sync (guard-130).
                    #
                    # The helper additionally requires the local file to EXIST,
                    # which narrows the exemption further -- the fail-CLOSED
                    # direction, and correct for the real flow (heartbeat-tick
                    # writes locally, then the push follows).
                    _own_carrier = _own_sid_carrier_path(self)
                    _is_own_carrier = (
                        _own_carrier is not None
                        and Path(path).resolve() == _own_carrier[0].resolve())
                    if not _is_own_carrier:
                        raise NoClaimError(NO_CLAIM_MESSAGE % {"agent": _agent})
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
        # no body kwargs yet: _store_put encodes the body only when a whole-object PUT goes out (g-358-202 U12)
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
            r = self._store_put(path, key, body, kw)
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
        # PUT succeeded — NOW make the local cache match what S3 holds. Unless
        # the bytes CAME from the local file (mirror_put, g-115-7257): rewriting
        # a file with its own bytes is not a no-op. On Windows the replace
        # raises while any other process holds the file open (a scheduled job
        # appending to its log); the raise skipped the baseline stamp below, so
        # S3 had moved while the baseline had not, and the file stayed
        # both-diverged for good. On POSIX the replace swaps the inode under an
        # appender, whose later writes land in the orphaned file.
        if not local_is_source:
            _atomic_write_local(local, body)
        self._etags[key] = r["ETag"]
        self._cache_check[str(local)] = time.monotonic()
        self._diverged_keys.discard(key)  # this write resolved any divergence
        self._stamp_manifest_baseline(path, body, etag=r["ETag"],
                                      record_mtime=not local_is_source)
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
        # g-358-202: and the WHOLE file, joined from a composite head's segments, so a handler is
        # never run over a segment; the token is that of the head the bytes were joined from.
        return self._composite_whole(key, _codec_decode_response(obj, key=key), obj["ETag"])

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
            if remote_etag is not None and merged == remote_bytes:
                # g-358-41: the union added NOTHING beyond what S3 already
                # holds (local is a subset of remote), so a PUT here would
                # create a new version of byte-identical content — and that
                # ETag movement is what every OTHER box's next refresh pulls
                # and re-merges, often producing another identical PUT. The
                # echo measured ~23x on the hot stores (1,186 versions /
                # 8.15 GB PUT in 12h on the goal queue vs ~95 logical
                # writes/day, 2026-09-01 census). Skip the PUT: converge the
                # LOCAL side to the union and adopt the observed remote ETag
                # as the fence. Fail-safe under a concurrent third writer —
                # if S3 moved after our GET, skipping merely leaves this box
                # one cycle behind (the same terminal state as never having
                # been called); the next refresh/sweep pulls and re-merges.
                # The local write is itself conditional: rewriting identical
                # bytes would bump mtime and re-arm the next sweep cycle,
                # turning one no-op into a perpetual-motion no-op loop.
                self._merge_noop_identical += 1
                try:
                    local_now = local.read_bytes()
                except OSError:
                    local_now = None
                if local_now != merged:
                    # Converge the local cache to the union — but only when
                    # it actually differs: rewriting identical bytes bumps
                    # mtime, which re-arms the next sweep cycle. Compare the
                    # FILE, not `body`: in the _put entry paths `body` is the
                    # new outgoing content and the local file still holds the
                    # pre-write bytes (local is written only after S3 success).
                    _atomic_write_local(local, merged)
                self._etags[key] = remote_etag
                self._cache_check[str(local)] = time.monotonic()
                self._diverged_keys.discard(key)
                self._stamp_manifest_baseline(path, merged, etag=remote_etag)
                if saw_conflict:
                    self._cas_conflicts_resolved += 1
                    _LOG.info(
                        "owncloud CAS conflict resolved for %s by IDENTITY — "
                        "merge added nothing, PUT skipped (merge_noop_identical=%d)",
                        key, self._merge_noop_identical)
                elif self._merge_noop_identical % 50 == 1:
                    _LOG.info(
                        "owncloud merge-noop: PUT skipped for %s — merged bytes "
                        "identical to remote (merge_noop_identical=%d this process)",
                        key, self._merge_noop_identical)
                return WriteResult(version=remote_etag, fallback_used=False)
            kw = dict(Bucket=self.bucket, Key=key)
            # no body kwargs yet: _store_put encodes only a whole-object PUT (g-358-202 U12)
            if remote_etag is not None:
                kw["IfMatch"] = remote_etag  # CAS on the version we merged against
            self._cas_writes += 1  # g-328-21: each merge attempt is a fenced write
            try:
                r = self._store_put(path, key, merged, kw)
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
            "merge_noop_identical": self._merge_noop_identical,
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
                   *, expected_version: Optional[str] = None,
                   local_is_source: bool = False) -> WriteResult:
        """Push LOCAL-authoritative bytes to S3 with an optional If-Match fence,
        WITHOUT downloading first — so a locally-newer file is never clobbered by
        the older remote copy. (``read_bytes(force_fresh=True)`` would download
        and overwrite local; that is exactly the wrong move for a local->S3 mirror
        of a file a raw write path persisted locally but never pushed — B15.)

        ``expected_version`` is the ETag from a prior ``stat()`` of the SAME key:
        the PUT is fenced on it (If-Match), so a concurrent backend write that
        moved the object underneath raises ``ConflictError`` and the caller skips
        (the next sweep reconciles). ``None`` => unconditional PUT (the object is
        absent on S3 / brand new).

        ``local_is_source=True`` says ``content`` was just READ from the local
        file, so the local file is left alone and only S3 is written (g-115-7257).
        The sweep passes it. Rewriting a file with its own bytes is not a no-op:
        on Windows the rewrite fails while another process holds the file open,
        and that failure wedged an appended-to log. The default (False) also
        writes ``content`` to the local file, which the hand-made-union repair
        (guard-4778, rb-9443) relies on: its bytes differ from the local file.

        Used by ``core/scripts/owncloud-sync.py`` (the governed-dir mirror sweep)
        and its PostToolUse single-file push. Not on the StorageBackend Protocol:
        it is an own-cloud-only reconciliation primitive; the sweep refuses to run
        under any other backend (no S3 to mirror to)."""
        key = self._s3_key(path)
        if expected_version is not None:
            self._etags[key] = expected_version  # fence on the version we observed
        else:
            self._etags.pop(key, None)            # new object — unconditional PUT
        return self._put(path, content, local_is_source=local_is_source)

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

    def append_jsonl_records(self, path: PathLike,
                             records: List[dict]) -> WriteResult:
        """Append N records in ONE read-modify-write, i.e. ONE whole-object PUT.

        THIS IS THE POINT OF THE METHOD. There is no native append in S3, so the
        singular append_jsonl_record above re-PUTs the ENTIRE object per record:
        N rows cost N full PUTs of a growing file (guard-6134/guard-6904 — one
        appended row re-PUTs the whole object). Measured on
        capture-evictions-archive.jsonl: 4,829 versions / 231.3 GiB in one UTC
        day, mean PUT 51,425,047 B against a 49,857,387 B object. Batching
        divides the PUT COUNT by N; it does NOT bound the PUT SIZE, which keeps
        growing with the file — a discount, not a bound (guard-6904).
        force_fresh so the If-Match fence token is the CURRENT remote ETag.
        Caller holds the lock."""
        if not records:
            return self.write_jsonl(path, self._read_jsonl_fresh(path))
        items = self._read_jsonl_fresh(path)
        items.extend(records)
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
            # Stale-break instrumentation (g-115-8536), VICTIM side: this
            # cond-fail is positive proof a peer stole the lock while we were
            # inside the critical section — our just-completed write may have
            # been overwritten (lost update). Silent until 2026-09-01. One
            # stderr line; never raises past the except.
            try:
                import sys
                sys.stderr.write(
                    f"[lock-stale-break] victim=self lock_key={lock_key} "
                    f"holder={self._holder()} — peer stole the lock mid-work; "
                    f"our RMW may be a lost update (g-115-8536)\n")
            except Exception:
                pass

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
        # TWO ATTEMPTS, and the split is the whole point of holder_since
        # (g-306-379). Attempt 1 is the SAME-MACHINE re-acquire: it asserts
        # machine_id is already us, so it must NOT touch holder_since — this box's
        # tenure never lapsed to a peer, and bumping it here would forget that.
        # Attempt 2 is a tenure CHANGE (a different machine held it, or the row
        # was never claimed), so holder_since is stamped to now.
        #
        # Atomicity is unchanged: BOTH attempts carry `agent_state = :idle`, which
        # is the condition that makes acquire mutually exclusive. Attempt 1 adding
        # a second conjunct can only make it refuse MORE often, and its sole
        # failure follow-up is attempt 2, whose condition is the original one — so
        # the acquire semantics this method already guaranteed are preserved
        # exactly, and a concurrent peer still loses at attempt 2.
        #
        # Attempt 1 deliberately does NOT set machine_id: its own condition has
        # already proven the stored value equals ours.
        common = {":run": {"S": "RUNNING"},
                  ":idle": {"S": "IDLE"},
                  ":tok": {"S": token},
                  ":hb": {"N": str(now)},
                  ":mid": {"S": self.machine_id}}
        try:
            self.ddb.update_item(
                TableName=self.sessions_table,
                Key={"session_key": {"S": skey}},
                UpdateExpression=("SET agent_state = :run, runner_token = :tok, "
                                  "heartbeat_at = :hb"),
                ConditionExpression="agent_state = :idle AND machine_id = :mid",
                ExpressionAttributeValues=common)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] != _COND_FAILED:
                raise
            # Not a same-machine re-acquire (different machine_id, or the
            # attribute is absent on a never-claimed row), OR not IDLE. Attempt 2
            # decides which — it re-tests IDLE alone.
        try:
            self.ddb.update_item(
                TableName=self.sessions_table,
                Key={"session_key": {"S": skey}},
                UpdateExpression=("SET agent_state = :run, runner_token = :tok, "
                                  "heartbeat_at = :hb, machine_id = :mid, "
                                  "holder_since = :hs"),
                ConditionExpression="agent_state = :idle",
                ExpressionAttributeValues=dict(common, **{":hs": {"N": str(now)}}))
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
                # g-306-379: absent on every legacy row -> 0 -> consumers fail
                # OPEN. The Scan carries no ProjectionExpression (see the
                # runner_token note below), so this attribute needs no projection
                # change to become visible here.
                hs_raw = item.get("holder_since", {}).get("N")
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
                        item.get("runner_token", {}).get("S")),
                    holder_since=int(hs_raw) if hs_raw is not None else 0))
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
