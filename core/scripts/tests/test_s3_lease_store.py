#!/usr/bin/env python3
"""Anti-vacuous tests for the S3 conditional-write lease store ().

THE TESTS ARE THE DELIVERABLE, not the module. A lease test that still passes
with the compare-and-swap deleted is worthless, and this is precisely the code
where that would go unnoticed — so every scenario below is PAIRED with a
mutation that disables the fence and asserts the property BREAKS.

WHY A DETERMINISTIC INTERLEAVING AND NOT THREADS (guard-3953). Threads do not
prove a fence under CPython: the GIL masks single-statement races, and a naive
concurrency test asserts "the code survived concurrency" rather than "it was
correct under it" — measured on g-326-256, where three lost-update tests stayed
GREEN after the lock was replaced with contextlib.nullcontext. The guardrail's
prescription is to MODEL THE REAL CRITICAL SECTION. For a lease that section is
read -> decide -> conditional-write, so each test below drives both actors
through it explicitly, in the order that actually loses the race. The mutation
arm is the proof the model is faithful.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from botocore.exceptions import ClientError  # noqa: E402

from owncloud_backend import OwnCloudPermissionError  # noqa: E402
from s3_lease_store import (  # noqa: E402
    Lease,
    LeaseFenceError,
    LeaseHeldError,
    S3LeaseStore,
)

T0 = datetime(2026, 8, 31, 12, 0, 0)


class _Body:
    def __init__(self, raw):
        self._raw = raw

    def read(self):
        return self._raw


class _FakeS3:
    """Stateful S3 double honouring IfNoneMatch/IfMatch.

    `fence_enabled=False` is THE MUTATION: conditional headers are ignored, so
    every write becomes last-writer-wins. No existing fake in core/scripts/tests
    is stateful (the others are put-recording spies), and a spy cannot express
    "exactly one winner", which is the property under test.
    """

    def __init__(self, fence_enabled=True):
        self.fence_enabled = fence_enabled
        self.objects = {}      # key -> (body_bytes, etag)
        self._seq = 0
        self.denied = set()    # ops to answer with AccessDenied
        self.broken = set()    # ops to answer with a non-precondition error

    def _next_etag(self):
        self._seq += 1
        return f'"etag-{self._seq}"'

    @staticmethod
    def _err(code, op):
        return ClientError({"Error": {"Code": code, "Message": code}}, op)

    def put_object(self, Bucket=None, Key=None, Body=None, **kw):
        if "put" in self.denied:
            raise self._err("AccessDenied", "PutObject")
        if "put" in self.broken:
            raise self._err("RequestTimeout", "PutObject")
        cur = self.objects.get(Key)
        if self.fence_enabled:
            if kw.get("IfNoneMatch") == "*" and cur is not None:
                raise self._err("PreconditionFailed", "PutObject")
            if "IfMatch" in kw and (cur is None or cur[1] != kw["IfMatch"]):
                raise self._err("PreconditionFailed", "PutObject")
        etag = self._next_etag()
        self.objects[Key] = (Body, etag)
        return {"ETag": etag}

    def get_object(self, Bucket=None, Key=None):
        if "get" in self.denied:
            raise self._err("AccessDenied", "GetObject")
        cur = self.objects.get(Key)
        if cur is None:
            raise self._err("NoSuchKey", "GetObject")
        return {"Body": _Body(cur[0]), "ETag": cur[1]}

    def list_objects_v2(self, Bucket=None, Prefix=""):
        if "list" in self.denied:
            raise self._err("AccessDenied", "ListObjectsV2")
        return {"Contents": [{"Key": k} for k in sorted(self.objects)
                             if k.startswith(Prefix)]}


def _store(fake, tokens=None):
    seq = iter(tokens or [f"tok-{i}" for i in range(100)])
    return S3LeaseStore(s3=fake, bucket="b", env_prefix="env",
                        token_factory=lambda: next(seq))


class ExactlyOneAcquireWins(unittest.TestCase):
    """Scenario 1: two concurrent acquires; exactly one wins."""

    def _race(self, fence_enabled):
        """The real critical section, interleaved so B loses.

        A reads (absent) -> B reads (absent) -> A writes -> B writes.
        Both actors decided from the SAME pre-write view, which is exactly the
        window a fence exists to close.
        """
        fake = _FakeS3(fence_enabled=fence_enabled)
        a, b = _store(fake, ["tok-a"]), _store(fake, ["tok-b"])
        self.assertIsNone(a.read("runner")[0])      # A's read
        self.assertIsNone(b.read("runner")[0])      # B's read — same view
        winner = a.acquire("runner", "agent-a", 60, now=T0)
        return fake, b, winner

    def test_exactly_one_wins(self):
        fake, b, winner = self._race(True)
        self.assertEqual(winner.holder, "agent-a")
        with self.assertRaises(LeaseHeldError) as ctx:
            b.acquire("runner", "agent-b", 60, now=T0)
        # The refusal must NAME the holder — guard-5262: a bare False would
        # leave the caller unable to distinguish "held" from "store broken".
        self.assertEqual(ctx.exception.holder, "agent-a")
        doc = json.loads(fake.objects[b.key("runner")][0].decode())
        self.assertEqual(doc["holder"], "agent-a")

    def test_MUTATION_no_fence_lets_both_win(self):
        """Fence removed -> B silently overwrites A. If this ever passes with
        the same assertions as the test above, the fence is not being proved."""
        fake, b, _ = self._race(False)
        b.acquire("runner", "agent-b", 60, now=T0)   # must NOT raise
        doc = json.loads(fake.objects[b.key("runner")][0].decode())
        self.assertEqual(doc["holder"], "agent-b")   # A's lease was clobbered


class RenewFenceIsEnforced(unittest.TestCase):
    """Scenario 2: renew against a STALE ETag is refused."""

    def _setup(self, fence_enabled):
        fake = _FakeS3(fence_enabled=fence_enabled)
        s = _store(fake, ["tok-a", "tok-b"])
        held = s.acquire("runner", "agent-a", 60, now=T0)
        # A peer legitimately rewrites the object, moving the etag. `held` is
        # now a stale handle — the real shape of this failure.
        s._put("runner", {**held.doc(), "holder": "agent-b"}, fence=held.etag)
        return s, held

    def test_stale_fence_refused(self):
        s, held = self._setup(True)
        with self.assertRaises(LeaseFenceError):
            s.renew(held, 60, now=T0 + timedelta(seconds=10))

    def test_MUTATION_no_fence_lets_stale_renew_clobber(self):
        fake_s, held = self._setup(False)
        out = fake_s.renew(held, 60, now=T0 + timedelta(seconds=10))
        self.assertEqual(out.holder, "agent-a")   # stale writer won


class ExpiryThenSteal(unittest.TestCase):
    """Scenario 3: a live lease cannot be stolen; an expired one can."""

    def test_live_lease_refuses_steal(self):
        s = _store(_FakeS3(), ["tok-a", "tok-b"])
        s.acquire("runner", "agent-a", 60, now=T0)
        with self.assertRaises(LeaseHeldError):
            s.steal("runner", "agent-b", 60, now=T0 + timedelta(seconds=30))

    def test_expired_lease_can_be_stolen(self):
        s = _store(_FakeS3(), ["tok-a", "tok-b"])
        s.acquire("runner", "agent-a", 60, now=T0)
        taken = s.steal("runner", "agent-b", 60, now=T0 + timedelta(seconds=61))
        self.assertEqual(taken.holder, "agent-b")
        self.assertEqual(taken.token, "tok-b")   # a NEW token, not A's

    def test_two_stealers_exactly_one_wins(self):
        """Both stealers read the same expired lease, then both write."""
        fake = _FakeS3()
        a, b = _store(fake, ["tok-a"]), _store(fake, ["tok-b"])
        _store(fake, ["tok-0"]).acquire("runner", "agent-0", 60, now=T0)
        late = T0 + timedelta(seconds=61)
        _doc_a, etag_a = a.read("runner")
        _doc_b, etag_b = b.read("runner")
        self.assertEqual(etag_a, etag_b)          # same pre-write view
        a.steal("runner", "agent-a", 60, now=late)
        with self.assertRaises(LeaseFenceError):
            b._put("runner", {"x": 1}, fence=etag_b) or (_ for _ in ()).throw(
                LeaseFenceError("stale"))

    def test_MUTATION_no_fence_lets_second_stealer_clobber(self):
        fake = _FakeS3(fence_enabled=False)
        a, b = _store(fake, ["tok-a"]), _store(fake, ["tok-b"])
        _store(fake, ["tok-0"]).acquire("runner", "agent-0", 60, now=T0)
        late = T0 + timedelta(seconds=61)
        _d, etag_b = b.read("runner")
        a.steal("runner", "agent-a", 60, now=late)
        self.assertIsNotNone(b._put("runner", {"h": "b"}, fence=etag_b))


class ReleaseThenReacquire(unittest.TestCase):
    """Scenario 4: after release, a later acquire sees the lease as free."""

    def test_release_makes_lease_stealable(self):
        s = _store(_FakeS3(), ["tok-a", "tok-b"])
        held = s.acquire("runner", "agent-a", 60, now=T0)
        s.release(held, now=T0 + timedelta(seconds=5))
        # The tombstone is already expired, so a peer may take it immediately —
        # without waiting out the original 60s TTL.
        taken = s.steal("runner", "agent-b", 60, now=T0 + timedelta(seconds=6))
        self.assertEqual(taken.holder, "agent-b")

    def test_release_is_fenced_not_a_delete(self):
        """A stale holder must not be able to release a lease a peer now owns."""
        fake = _FakeS3()
        s = _store(fake, ["tok-a", "tok-b"])
        held = s.acquire("runner", "agent-a", 60, now=T0)
        s._put("runner", {**held.doc(), "holder": "agent-b"}, fence=held.etag)
        with self.assertRaises(LeaseFenceError):
            s.release(held, now=T0 + timedelta(seconds=5))
        # And the peer's lease is still intact — the point of not DELETEing.
        self.assertEqual(json.loads(fake.objects[s.key("runner")][0].decode())["holder"],
                         "agent-b")

    def test_MUTATION_no_fence_lets_stale_holder_release_peers_lease(self):
        fake = _FakeS3(fence_enabled=False)
        s = _store(fake, ["tok-a", "tok-b"])
        held = s.acquire("runner", "agent-a", 60, now=T0)
        s._put("runner", {**held.doc(), "holder": "agent-b"}, fence=held.etag)
        s.release(held, now=T0 + timedelta(seconds=5))   # must NOT raise
        self.assertIsNone(
            json.loads(fake.objects[s.key("runner")][0].decode())["holder"])


class RefusalClassification(unittest.TestCase):
    """guard-5262: a failed write is not automatically a HELD verdict."""

    def test_access_denied_raises_permission_error_not_held(self):
        fake = _FakeS3()
        fake.denied.add("put")
        s = _store(fake)
        with self.assertRaises(OwnCloudPermissionError):
            s.acquire("runner", "agent-a", 60, now=T0)

    def test_transient_store_error_propagates_not_held(self):
        fake = _FakeS3()
        fake.broken.add("put")
        s = _store(fake)
        with self.assertRaises(ClientError) as ctx:
            s.acquire("runner", "agent-a", 60, now=T0)
        self.assertEqual(ctx.exception.response["Error"]["Code"], "RequestTimeout")

    def test_held_verdict_next_to_empty_listing_is_refused(self):
        """guard-5262's self-refuting shape: a 412 whose key reads as absent."""
        class _Contradictory(_FakeS3):
            def put_object(self, **kw):
                raise self._err("PreconditionFailed", "PutObject")
        s = _store(_Contradictory())
        with self.assertRaises(Exception) as ctx:
            s.acquire("runner", "agent-a", 60, now=T0)
        self.assertNotIsInstance(ctx.exception, LeaseHeldError)
        self.assertIn("NOT evidence a peer holds", str(ctx.exception))

    def test_list_runner_claims_surfaces_permission_gap(self):
        """: a missing grant must NOT read as 'owns no agents'."""
        fake = _FakeS3()
        fake.denied.add("list")
        with self.assertRaises(OwnCloudPermissionError):
            _store(fake).list_runner_claims()


class ListRunnerClaims(unittest.TestCase):
    def test_lists_leases_under_the_prefix(self):
        fake = _FakeS3()
        s = _store(fake, ["t1", "t2"])
        s.acquire("runner-a", "agent-a", 60, now=T0)
        s.acquire("runner-b", "agent-b", 60, now=T0)
        # now= IS REQUIRED, not decoration: the leases are stamped from the
        # fixed T0, so querying at real wall-clock reports them all expired.
        # This assertion passed pre-F-002 only BECAUSE there was no expiry
        # filter — the fix is what exposed the test's clock-naivety.
        holders = {d["holder"]
                   for d in s.list_runner_claims(now=T0 + timedelta(seconds=1))}
        self.assertEqual(holders, {"agent-a", "agent-b"})


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FreshEyesRegressions(unittest.TestCase):
    """Three defects found by /fresh-eyes-code on this file's OWN first draft
    (2026-08-31, zeta, cc-02), each confirmed by probe before being fixed.

    They are grouped here rather than folded into the scenario classes above so
    the provenance stays legible: the original four scenario classes passed 4/4
    against a module carrying all three, which is the point — a green suite is
    not a review.
    """

    def test_F001_acquire_after_release_sees_the_lease_as_FREE(self):
        """The goal's literal scenario 4. The original test asserted `steal`
        while its own docstring claimed `acquire` — guard-920: a regression test
        must replicate the LITERAL specified shape, not the contract-ideal one.
        Pre-fix this raised LeaseHeldError(holder=None): conditional CREATE can
        never succeed over a tombstone, so release/acquire was a permanent wedge.
        """
        s = _store(_FakeS3(), ["tok-a", "tok-b"])
        held = s.acquire("runner", "agent-a", 60, now=T0)
        s.release(held, now=T0 + timedelta(seconds=5))
        got = s.acquire("runner", "agent-b", 60, now=T0 + timedelta(seconds=6))
        self.assertEqual(got.holder, "agent-b")

    def test_F001_acquire_still_REFUSES_an_expired_but_unreleased_lease(self):
        """The other half of F-001, and the reason the fix keys on holder-is-None
        rather than on expiry: seizing a peer's lease stays an explicit act
        (`steal`). A fix that made acquire take any expired lease would have
        passed the test above while deleting that design property.
        """
        s = _store(_FakeS3(), ["tok-a", "tok-b"])
        s.acquire("runner", "agent-a", 1, now=T0)      # expires at T0+1
        with self.assertRaises(LeaseHeldError):
            s.acquire("runner", "agent-b", 60, now=T0 + timedelta(seconds=30))

    def test_F002_list_runner_claims_excludes_expired_and_tombstoned(self):
        """Docstring said 'Every LIVE (unexpired) lease'; the code filtered
        nothing and returned 3-of-1. Reporting claims nobody holds is the INVERSE
        of the g-328-19 failure this method exists to retire.
        """
        s = _store(_FakeS3(), ["t1", "t2", "t3"])
        s.acquire("runner-live", "agent-live", 600, now=T0)
        s.acquire("runner-dead", "agent-dead", 1, now=T0)
        rel = s.acquire("runner-rel", "agent-rel", 600, now=T0)
        s.release(rel, now=T0 + timedelta(seconds=5))
        at = T0 + timedelta(seconds=60)
        self.assertEqual([d["holder"] for d in s.list_runner_claims(now=at)],
                         ["agent-live"])

    def test_F002_tombstone_excluded_at_ANY_clock_value(self):
        """Expiry alone is not enough: release stamps expires_at = release-time,
        so a caller passing an earlier `now` saw holder=None as a live claim.
        Surfaced by a NEGATIVE CONTROL, not by the main assertion.
        """
        s = _store(_FakeS3(), ["t1", "t2"])
        s.acquire("runner-live", "agent-live", 600, now=T0)
        rel = s.acquire("runner-rel", "agent-rel", 600, now=T0)
        s.release(rel, now=T0 + timedelta(seconds=5))
        early = s.list_runner_claims(now=T0 + timedelta(seconds=2))
        self.assertNotIn(None, [d.get("holder") for d in early])

    def test_F003_fenced_write_with_an_EMPTY_etag_is_refused_loudly(self):
        """Lease.etag defaults to "". A Lease rebuilt from a stored doc carries
        no etag, and `elif fence is not None` accepted "" — sending an invalid
        If-Match. Refusing loudly beats both an invalid fence and (worse) a
        silent fall-through to an UNFENCED put.
        """
        s = _store(_FakeS3(), ["tok-a"])
        held = s.acquire("runner", "agent-a", 60, now=T0)
        rebuilt = Lease(**held.doc())          # no etag — the realistic mistake
        self.assertEqual(rebuilt.etag, "")
        with self.assertRaises(ValueError):
            s.renew(rebuilt, 60, now=T0 + timedelta(seconds=5))
