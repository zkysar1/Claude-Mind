#!/usr/bin/env python3
"""S3 conditional-write lease store — STANDALONE AND UNWIRED ().

FIRST SLICE of g-328-53 (retire the key-value coordination store from the
own-cloud coordination plane). Nothing in the live coordination path imports
this module. The cutover is a SEPARATE goal, deliberately, because
acquire_lock/release_lock is the highest-blast-radius edit in this framework —
every agent's claim and every runner lease depend on it. Building the
replacement in isolation makes the risky step later, small, and reviewable on
its own.

TRUST MODEL — stated plainly because it looks like a regression and is not.
Expiry is evaluated CLIENT-SIDE: `steal` reads the lease, compares
`expires_at` against a caller-supplied `now`, and only then writes under an
If-Match fence. That is the SAME trust model the existing key-value
coordination path already has — its conditional expression compares against a
client-supplied `:now` too — VERIFIED at owncloud_backend.py:2018-2020,
`ConditionExpression="attribute_not_exists(lock_key) OR #t < :now"` with
`":now": {"N": str(now)}`, i.e. computed by the caller. So
client-side expiry is not a new weakness introduced here; it is the existing
one, made visible. What the fence DOES guarantee is that two racing stealers
cannot both win: the loser's If-Match is stale.

WHAT THE FENCE DOES NOT GUARANTEE (guard-5322). A compare-and-swap proves NO
LOST UPDATE, never that a derived value is UNIQUE. Every value this module
writes is either caller-supplied (`holder`) or freshly minted inside the write
path (`token`); nothing is derived from a read taken outside the critical
section, so the guard-5322 collision shape does not arise here. Preserve that
property: if a future field is ever computed as max+1 over a read, it must be
computed from an in-cycle fresh read, not from the value this module returns.

REFUSAL CLASSIFICATION IS THE CONTRACT (guard-5262). "NEVER report a
claim/lease as HELD from a failed write alone: classify the remote's refusal
first." A failed conditional write means one of three DIFFERENT things and this
module never collapses them into a bool:
  * 412 PreconditionFailed  -> a peer holds it / our fence is stale
                               -> LeaseHeldError or LeaseFenceError (both
                                  subclass the backend's ConflictError)
  * AccessDenied            -> OwnCloudPermissionError, fail LOUD (g-328-20).
                               A permission gap silently read as "not held"
                               is the 2026-07-04 fleet-wedge class.
  * anything else           -> re-raised. The store is UNWRITABLE; that is a
                               store error, not a lease verdict.

WHY RELEASE IS A TOMBSTONE PUT AND NOT A DELETE. S3 has no conditional DELETE,
so an unconditional delete would let a stale holder erase a lease a peer had
already legitimately stolen. Releasing writes an already-expired document under
If-Match instead, which is fenced and idempotent-safe: a later `acquire` reads
it as free.

BONUS over the previous path: `list_runner_claims` is a ListObjectsV2 over one
prefix, which retires the g-328-19 failure class outright — a missing scan
grant on the old key-value store was silently read as "owns no agents" for
days.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from botocore.exceptions import ClientError  # noqa: E402

from owncloud_backend import (  # noqa: E402
    ConflictError,
    OwnCloudPermissionError,
    _NOT_FOUND,
    _PRECONDITION,
    _reraise_access_denied,
)

__all__ = [
    "S3LeaseStore",
    "Lease",
    "LeaseHeldError",
    "LeaseFenceError",
]

COORDINATION_INFIX = "coordination"


class LeaseHeldError(ConflictError):
    """A live lease is held by someone else. Carries the current holder and
    expiry so the caller can report WHO holds it rather than a bare failure."""

    def __init__(self, name, holder, expires_at):
        self.name, self.holder, self.expires_at = name, holder, expires_at
        super().__init__(
            f"lease {name!r} is held by {holder!r} until {expires_at} "
            f"(not expired) — refusing to take it"
        )


class LeaseFenceError(ConflictError):
    """Our If-Match fence was stale: the lease moved under us between the read
    and the write. Distinct from LeaseHeldError because the remedy differs —
    re-read and retry, rather than back off and wait for an expiry."""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")


@dataclass(frozen=True)
class Lease:
    """A held lease plus the ETag that fences its next write.

    `etag` is what makes renew/release safe; a Lease whose etag is stale is
    exactly the LeaseFenceError case.
    """

    name: str
    holder: str
    token: str
    acquired_at: str
    expires_at: str
    etag: str = ""

    def doc(self) -> dict:
        return {
            "name": self.name,
            "holder": self.holder,
            "token": self.token,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
        }

    def is_expired(self, now: datetime) -> bool:
        return _parse(self.expires_at) <= now


@dataclass
class S3LeaseStore:
    """Lease objects under ``<env_prefix>/coordination/<name>.json``.

    The s3 client is INJECTED (never built here) so this module stays testable
    without network or credentials — the same shape owncloud_backend uses for
    its own client (``s3 if s3 is not None else _mk("s3")``).
    """

    s3: object
    bucket: str
    env_prefix: str
    token_factory: object = field(default=lambda: uuid.uuid4().hex)

    def key(self, name: str) -> str:
        return f"{self.env_prefix}/{COORDINATION_INFIX}/{name}.json"

    # ---- reads -----------------------------------------------------------

    def read(self, name: str):
        """(lease_doc, etag) or (None, None) when absent.

        An absent lease is a legitimate state (nobody holds it), so _NOT_FOUND
        returns cleanly. Every OTHER ClientError propagates — an unreadable
        store must never render as "free" (the g-328-19 shape).
        """
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=self.key(name))
        except ClientError as e:
            _reraise_access_denied(e, f"lease-read:{name}")
            if e.response["Error"]["Code"] in _NOT_FOUND:
                return None, None
            raise
        body = resp["Body"].read()
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return json.loads(body), resp["ETag"]

    def list_runner_claims(self, *, now: datetime = None) -> list:
        """Every LIVE (unexpired) lease under the coordination prefix.

        ListObjectsV2 rather than a scan of the old store — see the docstring's
        g-328-19 note.
        """
        prefix = f"{self.env_prefix}/{COORDINATION_INFIX}/"
        try:
            resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        except ClientError as e:
            _reraise_access_denied(e, "lease-list")
            raise
        now = now or datetime.now()
        out = []
        for obj in resp.get("Contents", []) or []:
            name = obj["Key"][len(prefix):]
            if not name.endswith(".json"):
                continue
            doc, _etag = self.read(name[: -len(".json")])
            if doc is None:
                continue
            # EXPIRY IS FILTERED HERE, INSIDE THE LOOP — not left to the caller.
            # An expired lease and a release tombstone (holder None, expires_at
            # already past) are both DEAD, and returning them as claims is the
            # INVERSE of the  failure this method exists to retire:
            # reporting claims nobody holds (F-002).
            # A TOMBSTONE IS NEVER A CLAIM, at any clock value. Expiry alone is
            # not enough: release stamps expires_at = release-time, so a caller
            # passing an earlier `now` would see holder=None as a live claim.
            # Checking the holder makes the predicate clock-independent.
            if doc.get("holder") is None:
                continue
            if _parse(doc["expires_at"]) <= now:
                continue
            out.append(doc)
        return out

    # ---- writes ----------------------------------------------------------

    def _put(self, name: str, doc: dict, *, fence: str = None,
             create_only: bool = False):
        kw = dict(Bucket=self.bucket, Key=self.key(name),
                  Body=json.dumps(doc, indent=2).encode("utf-8"))
        if create_only:
            kw["IfNoneMatch"] = "*"
        elif fence is not None:
            if not fence:
                # An empty etag cannot fence anything, and falling through to an
                # unconditional PUT would SILENTLY drop the fence — precisely what
                # the mutation tests prove must never happen. Refuse loudly (F-003).
                raise ValueError(
                    f"lease {name!r}: refusing a fenced write with an EMPTY etag — "
                    f"a Lease reconstructed without its etag cannot be fenced; "
                    f"re-read the lease to obtain one"
                )
            kw["IfMatch"] = fence
        try:
            resp = self.s3.put_object(**kw)
        except ClientError as e:
            # guard-5262 ORDER IS LOAD-BEARING: permission first, THEN
            # precondition, THEN re-raise. An AccessDenied classified as
            # "precondition" would read as "a peer holds it" and hide an IAM gap.
            _reraise_access_denied(e, f"lease-write:{name}")
            if e.response["Error"]["Code"] in _PRECONDITION:
                return None          # caller decides HELD vs STALE-FENCE
            raise                    # store is unwritable — NOT a lease verdict
        return resp.get("ETag", "")

    def acquire(self, name: str, holder: str, ttl_seconds: int, *,
                now: datetime = None) -> Lease:
        """Conditional CREATE. Raises LeaseHeldError if anyone holds it.

        Deliberately does NOT auto-steal an expired lease: taking someone's
        lease is an explicit act with its own method, so a caller can never
        silently seize one by calling the innocuous-sounding verb.
        """
        now = now or datetime.now()
        lease = Lease(
            name=name, holder=holder, token=self.token_factory(),
            acquired_at=_iso(now), expires_at=_iso(now + timedelta(seconds=ttl_seconds)),
        )
        etag = self._put(name, lease.doc(), create_only=True)
        if etag is None:
            doc, existing_etag = self.read(name)
            if doc is None:
                # 412 on create, yet absent on read: the store contradicted
                # itself. guard-5262's self-refuting shape — surface it.
                raise ConflictError(
                    f"lease {name!r}: conditional create was refused but the "
                    f"key reads as absent — the write path is suspect, this is "
                    f"NOT evidence a peer holds the lease"
                )
            # A RELEASE TOMBSTONE (holder None) is explicitly free: the object
            # exists only because S3 has no conditional DELETE, so a conditional
            # CREATE can NEVER succeed over it and acquire would report the lease
            # as "held by None" forever (F-001 — the module docstring's claim that
            # a later acquire reads it as free was false until this branch).
            # NOT auto-stealing: an expired lease that was never RELEASED still
            # raises below, because seizing a peer's lease stays an explicit act.
            if doc.get("holder") is None:
                etag = self._put(name, lease.doc(), fence=existing_etag)
                if etag is None:
                    raise LeaseFenceError(
                        f"acquire of {name!r} over a release-tombstone lost the "
                        f"race: a peer took it first — re-read before retrying"
                    )
                return Lease(**{**lease.doc(), "etag": etag})
            raise LeaseHeldError(name, doc.get("holder"), doc.get("expires_at"))
        return Lease(**{**lease.doc(), "etag": etag})

    def renew(self, lease: Lease, ttl_seconds: int, *,
              now: datetime = None) -> Lease:
        """Extend OUR lease under an If-Match fence. A stale fence means the
        lease moved under us — LeaseFenceError, never a silent overwrite."""
        now = now or datetime.now()
        renewed = Lease(
            name=lease.name, holder=lease.holder, token=lease.token,
            acquired_at=lease.acquired_at,
            expires_at=_iso(now + timedelta(seconds=ttl_seconds)),
        )
        etag = self._put(lease.name, renewed.doc(), fence=lease.etag)
        if etag is None:
            raise LeaseFenceError(
                f"renew of {lease.name!r} refused: our If-Match etag is stale, "
                f"so the lease changed since we read it — re-read before retrying"
            )
        return Lease(**{**renewed.doc(), "etag": etag})

    def steal(self, name: str, holder: str, ttl_seconds: int, *,
              now: datetime = None) -> Lease:
        """Take an EXPIRED lease. Refuses a live one (LeaseHeldError)."""
        now = now or datetime.now()
        doc, etag = self.read(name)
        if doc is None:
            return self.acquire(name, holder, ttl_seconds, now=now)
        if _parse(doc["expires_at"]) > now:
            raise LeaseHeldError(name, doc.get("holder"), doc.get("expires_at"))
        taken = Lease(
            name=name, holder=holder, token=self.token_factory(),
            acquired_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=ttl_seconds)),
        )
        new_etag = self._put(name, taken.doc(), fence=etag)
        if new_etag is None:
            raise LeaseFenceError(
                f"steal of {name!r} refused: another stealer won the race "
                f"(our If-Match etag was stale)"
            )
        return Lease(**{**taken.doc(), "etag": new_etag})

    def release(self, lease: Lease, *, now: datetime = None) -> None:
        """Fenced expiry-tombstone PUT — see the module docstring for why this
        is not a DELETE."""
        now = now or datetime.now()
        tomb = {
            "name": lease.name, "holder": None, "token": None,
            "acquired_at": lease.acquired_at,
            "expires_at": _iso(now),          # already expired == free
            "released_by": lease.holder,
        }
        if self._put(lease.name, tomb, fence=lease.etag) is None:
            raise LeaseFenceError(
                f"release of {lease.name!r} refused: our If-Match etag is "
                f"stale, so we no longer hold this lease — do NOT retry "
                f"unfenced, that would erase the current holder's lease"
            )
