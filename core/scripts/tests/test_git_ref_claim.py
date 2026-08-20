"""Tests for the git-ref runner-claim store (the local-backend arm, ).

TWO CLONES, NOT ONE. Every test that asserts cross-machine behaviour runs
against a real bare origin plus TWO working clones, because the defect this
module is most likely to regress into is invisible on a single box: git's
default fetch refspec is ``+refs/heads/*:refs/remotes/origin/*``, so a claim at
``refs/mind/claim/...`` is NOT fetched by a plain ``git fetch``. A one-box
acquire/refuse test passes with the explicit refspec removed — box A reads back
its own local ref and everything looks correct — while box B silently sees no
holder and both boxes run as reducer. ``test_fetch_trap_*`` exists purely to
fail if ``_fetch_claims`` stops using the explicit refspec.

Real git is used rather than a mock on purpose: the compare-and-swap being
relied on is git's own non-fast-forward rejection, so mocking the push would
mock away the entire mechanism under test.
"""

import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from git_ref_claim import (  # noqa: E402
    GitRefClaimStore,
    GitRefClaimError,
    claim_ref,
    ZERO_OID,
)

ENV = "test-env"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=False,
    )


def _mk_fleet(tmp):
    """Bare origin + two independent clones. Returns (origin, boxA, boxB)."""
    origin = os.path.join(tmp, "origin.git")
    subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
    a = os.path.join(tmp, "boxA")
    b = os.path.join(tmp, "boxB")
    for path in (a, b):
        subprocess.run(["git", "clone", "-q", origin, path], check=True)
        _git(path, "config", "user.email", "test@example.invalid")
        _git(path, "config", "user.name", "test")
    return origin, a, b


def _store(repo, machine, stale=3900):
    return GitRefClaimStore(
        repo_root=repo, env_id=ENV, machine_id=machine,
        runner_stale_seconds=stale,
    )


class GitRefClaimTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.origin, self.a, self.b = _mk_fleet(self.tmp)
        self.sa = _store(self.a, "boxA")
        self.sb = _store(self.b, "boxB")

    def tearDown(self):
        self._tmp.cleanup()

    # ---------------------------------------------------------- basic acquire

    def test_acquire_publishes_claim_to_origin(self):
        self.assertTrue(self.sa.acquire_runner("zeta", "tok-a"))
        ls = _git(self.origin, "for-each-ref", "--format=%(refname)")
        self.assertIn(claim_ref("zeta", ENV), ls.stdout)

    def test_acquire_is_env_scoped(self):
        self.sa.acquire_runner("zeta", "tok-a")
        other = GitRefClaimStore(repo_root=self.a, env_id="other-env",
                                 machine_id="boxA")
        # A different env-id is a different ref, so it must be free — this is
        # what stops two deployments sharing a repo from fighting over one name.
        self.assertIsNone(other.get_runner_state("zeta"))

    # ------------------------------------------------- THE FETCH TRAP ()

    def test_fetch_trap_plain_fetch_does_not_carry_the_ref(self):
        """Positive control: proves the trap is real in THIS git, so the next
        test is meaningful rather than vacuously passing."""
        self.sa.acquire_runner("zeta", "tok-a")
        _git(self.b, "fetch", "origin")          # the DEFAULT refspec only
        local = _git(self.b, "rev-parse", "--verify", "-q",
                     claim_ref("zeta", ENV))
        self.assertNotEqual(local.returncode, 0,
                            "plain fetch carried a refs/mind/claim ref — the "
                            "trap this module defends against is not reproducing, "
                            "so the next assertion proves nothing")

    def test_fetch_trap_store_still_sees_the_claim_from_box_b(self):
        """The regression guard. Deleting the explicit refspec in
        `_fetch_claims` makes THIS fail while every single-box test still
        passes."""
        self.sa.acquire_runner("zeta", "tok-a")
        _git(self.b, "fetch", "origin")
        state = self.sb.get_runner_state("zeta")
        self.assertIsNotNone(state, "box B could not see box A's live claim")
        self.assertEqual(state["machine_id"], "boxA")

    # -------------------------------------------------------- mutual exclusion

    def test_second_box_refused_while_claim_is_live(self):
        self.sa.acquire_runner("zeta", "tok-a")
        with self.assertRaises(Exception) as cm:
            self.sb.acquire_runner("zeta", "tok-b")
        self.assertIn("boxA", str(cm.exception))

    def test_create_race_has_exactly_one_winner(self):
        """Both boxes race from expect-absent. git's CAS must pick one."""
        wins = []
        for store, tok in ((self.sa, "tok-a"), (self.sb, "tok-b")):
            try:
                # Neither box fetches first — this is the genuine simultaneous
                # case, where both believe the ref is absent.
                if store.acquire_runner("zeta", tok):
                    wins.append(store.machine_id)
            except Exception:
                pass
        self.assertEqual(len(wins), 1, f"expected exactly one winner, got {wins}")

    def test_different_agents_do_not_collide(self):
        self.assertTrue(self.sa.acquire_runner("zeta", "tok-a"))
        self.assertTrue(self.sb.acquire_runner("alpha", "tok-b"))

    # -------------------------------------------------------------- lease/stale

    def _backdate(self, store, agent, token, seconds):
        """Rewrite the live claim with an older heartbeat, in place."""
        ref = claim_ref(agent, ENV)
        store._fetch_claims()
        oid, payload = store._read_ref(ref)
        payload["heartbeat_at"] = int(time.time()) - seconds
        self.assertTrue(store._write_ref(ref, payload, oid))

    def test_fresh_claim_cannot_be_broken(self):
        self.sa.acquire_runner("zeta", "tok-a")
        self.assertFalse(self.sb.reclaim_if_stale("zeta"))

    def test_stale_claim_can_be_broken_and_reacquired(self):
        self.sa.acquire_runner("zeta", "tok-a")
        self._backdate(self.sa, "zeta", "tok-a", 4000)   # > 3900 threshold
        self.assertTrue(self.sb.reclaim_if_stale("zeta"))
        self.assertTrue(self.sb.acquire_runner("zeta", "tok-b"))
        self.assertEqual(self.sb.get_runner_state("zeta")["machine_id"], "boxB")

    def test_stepdown_threshold_is_below_takeover_threshold(self):
        """The lease invariant, read from the SSOTs rather than re-stated.

        `reducer_self_fence` steps the HOLDER down at
        `runner_heartbeat.stepdown_seconds`; this store lets a PEER seize at
        `DEFAULT_RUNNER_STALE_SECONDS`. If those ever cross, a holder still
        believes it is leader at the moment a peer may legally take over — two
        live reducers, the precise failure the claim exists to prevent.

        Both numbers are read from where they actually live. An earlier version
        of this test hardcoded `stepdown = 1950`, which was itself the second
        copy the filer's fresh-eyes note warned against: it would have kept
        passing after someone raised the config value past the takeover
        threshold, certifying an invariant that had already been broken.
        """
        import pathlib
        from reducer_self_fence import load_stepdown_seconds
        from git_ref_claim import DEFAULT_RUNNER_STALE_SECONDS
        cfg = (pathlib.Path(__file__).resolve().parents[2]
               / "config" / "aspirations.yaml")
        self.assertTrue(cfg.is_file(), f"config not found at {cfg}")
        stepdown = load_stepdown_seconds(cfg)
        self.assertIsNotNone(stepdown, "stepdown_seconds unreadable")
        self.assertLess(stepdown, DEFAULT_RUNNER_STALE_SECONDS)

    def test_takeover_default_matches_owncloud(self):
        """The local arm must not FORK T_takeover.

        `test_reducer_self_fence.py::test_config_invariant_stepdown_precedes_takeover`
        proves the lease safe against `owncloud_backend`'s constant. If this
        module carried its own drifting copy, the local arm would sit outside
        that proof while appearing covered by it.
        """
        try:
            from owncloud_backend import DEFAULT_RUNNER_STALE_SECONDS as oc
        except Exception:
            self.skipTest("owncloud_backend unavailable (no cloud SDK)")
        from git_ref_claim import DEFAULT_RUNNER_STALE_SECONDS as local
        self.assertEqual(local, oc)

    # ---------------------------------------------------------- ownership/token

    def test_heartbeat_requires_matching_token(self):
        self.sa.acquire_runner("zeta", "tok-a")
        self.assertFalse(self.sb.heartbeat("zeta", "wrong-token"))
        self.assertTrue(self.sa.heartbeat("zeta", "tok-a"))

    def test_release_transitions_exactly_once(self):
        self.sa.acquire_runner("zeta", "tok-a")
        self.assertTrue(self.sa.release_runner("zeta", "tok-a"))
        # Second release performed no transition — the contract runner-claim.sh
        # rc=5 "UNCONFIRMED" depends on.
        self.assertFalse(self.sa.release_runner("zeta", "tok-a"))

    def test_released_claim_is_reacquirable_immediately(self):
        self.sa.acquire_runner("zeta", "tok-a")
        self.sa.release_runner("zeta", "tok-a")
        self.assertTrue(self.sb.acquire_runner("zeta", "tok-b"))

    def test_raw_token_never_reaches_the_ref(self):
        """The security invariant. A git ref is readable by anyone with repo
        access, and the token is the bearer credential authorising heartbeat and
        release — so only its fingerprint may be published."""
        secret = "super-secret-runner-token-9f3a"
        self.sa.acquire_runner("zeta", secret)
        oid = _git(self.origin, "rev-parse", claim_ref("zeta", ENV)).stdout.strip()
        blob = _git(self.origin, "cat-file", "-p", oid).stdout
        self.assertNotIn(secret, blob)
        payload = json.loads(blob)
        self.assertTrue(payload["runner_token_fp"])
        self.assertNotIn(secret, json.dumps(payload))

    def test_fingerprint_matches_owncloud_helper(self):
        """The two arms must agree on the digest, or a claim written by one and
        read by the other would never match."""
        try:
            from owncloud_backend import runner_token_fingerprint
        except Exception:
            self.skipTest("owncloud_backend unavailable")
        from git_ref_claim import _fingerprint
        self.assertEqual(_fingerprint("abc"), runner_token_fingerprint("abc"))

    # ------------------------------------------------------------------ listing

    def test_list_runner_claims_returns_rows_without_raw_tokens(self):
        self.sa.acquire_runner("zeta", "tok-a")
        self.sb.acquire_runner("alpha", "tok-b")
        rows = self.sa.list_runner_claims()
        by_agent = {r.agent: r for r in rows}
        self.assertEqual(set(by_agent), {"zeta", "alpha"})
        self.assertEqual(by_agent["zeta"].machine_id, "boxA")
        self.assertEqual(by_agent["zeta"].agent_state, "RUNNING")
        self.assertIsInstance(by_agent["zeta"].heartbeat_at, int)
        for r in rows:
            self.assertNotIn("tok-", str(r.runner_token_fp or ""))

    def test_list_is_empty_before_any_claim(self):
        self.assertEqual(self.sa.list_runner_claims(), [])

    # --------------------------------------------------------------- robustness

    def test_unreadable_payload_refuses_rather_than_clobbering(self):
        """A corrupted blob must not read as 'absent' — that would silently
        authorise a second runner, the exact outcome the lease prevents."""
        ref = claim_ref("zeta", ENV)
        oid = subprocess.run(
            ["git", "-C", self.a, "hash-object", "-w", "--stdin"],
            input="not json at all", capture_output=True, text=True, check=True,
        ).stdout.strip()
        _git(self.a, "push", "-q", "origin", f"{oid}:{ref}")
        with self.assertRaises(GitRefClaimError):
            self.sb.acquire_runner("zeta", "tok-b")

    def test_available_false_without_remote(self):
        solo = os.path.join(self.tmp, "solo")
        subprocess.run(["git", "init", "-q", solo], check=True)
        self.assertFalse(GitRefClaimStore.available(solo))

    def test_available_true_with_remote(self):
        self.assertTrue(GitRefClaimStore.available(self.a))

    def test_zero_oid_is_fortyzeros(self):
        self.assertEqual(ZERO_OID, "0" * 40)
        self.assertEqual(len(ZERO_OID), 40)


class StaleSecondsTestCase(unittest.TestCase):
    """OWNERSHIP_STALE_SECONDS parsing — a zero or negative threshold would make
    every claim instantly stale and turn the lease into a free-for-all, so bad
    values must fall back rather than coerce."""

    def setUp(self):
        self._orig = os.environ.get("OWNERSHIP_STALE_SECONDS")

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("OWNERSHIP_STALE_SECONDS", None)
        else:
            os.environ["OWNERSHIP_STALE_SECONDS"] = self._orig

    def _read(self):
        from git_ref_claim import _stale_seconds
        return _stale_seconds()

    def test_valid_override_is_honoured(self):
        os.environ["OWNERSHIP_STALE_SECONDS"] = "1200"
        self.assertEqual(self._read(), 1200)

    def test_zero_negative_and_garbage_fall_back_to_default(self):
        from git_ref_claim import DEFAULT_RUNNER_STALE_SECONDS
        for bad in ("0", "-5", "abc", ""):
            os.environ["OWNERSHIP_STALE_SECONDS"] = bad
            self.assertEqual(self._read(), DEFAULT_RUNNER_STALE_SECONDS,
                             f"{bad!r} should not have been accepted")


if __name__ == "__main__":
    unittest.main()
