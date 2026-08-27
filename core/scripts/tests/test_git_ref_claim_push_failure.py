"""A failed push that is NOT a lease rejection is a store failure, never a held claim.

Measured 2026-08-27 on coach-mind (zc-03): the repo's origin is the staging repo over
anonymous HTTPS — readable, never writable — so every ``git push`` died with
``could not read Username``. ``_write_ref`` returned False for ANY non-zero push,
``acquire_runner`` read False as "lost the CAS race", the daemon answered
``{"held": true}`` and ``runner-claim.sh`` told the operator "another machine owns a
live claim for this agent" while the claim namespace was empty. The operator's agent
spent 103 minutes and destroyed the repo's object store chasing that phantom holder.

These tests pin: an unwritable remote raises ``GitRefClaimError`` (with the remote's
own words), a genuine lease rejection still returns False, the classifier's three
answers, ``default_remote``'s precedence, and a ``claims`` remote arbitrating when
origin is read-only.
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from git_ref_claim import (  # noqa: E402
    GitRefClaimError,
    GitRefClaimStore,
    _is_lease_rejection,
    claim_ref,
)
from owncloud_backend import RunnerHeld  # noqa: E402

ENV = "test-env"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=False,
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


def _store(repo, machine, remote="origin"):
    return GitRefClaimStore(
        repo_root=repo, env_id=ENV, machine_id=machine,
        runner_stale_seconds=3900, remote=remote,
    )


class PushFailureTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.origin, self.a, self.b = _mk_fleet(self.tmp)
        self.sa = _store(self.a, "boxA")
        self.sb = _store(self.b, "boxB")
        self._env = os.environ.get("RUNNER_CLAIM_REMOTE")
        os.environ.pop("RUNNER_CLAIM_REMOTE", None)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("RUNNER_CLAIM_REMOTE", None)
        else:
            os.environ["RUNNER_CLAIM_REMOTE"] = self._env
        self._tmp.cleanup()

    def test_unwritable_remote_raises_a_store_error_not_held(self):
        # The remote exists as a URL but is not a repository — the same shape as
        # "no credential": git refuses the push and it is nobody's claim.
        _git(self.a, "remote", "set-url", "origin", os.path.join(self.tmp, "missing.git"))
        with self.assertRaises(GitRefClaimError) as cm:
            self.sa.acquire_runner("coach", "tok-a")
        self.assertNotIsInstance(cm.exception, RunnerHeld)
        self.assertIn("unwritable", str(cm.exception))
        self.assertIn("origin", str(cm.exception))
        # The failed fetch is remembered for the listing endpoint to surface.
        self.assertIsNotNone(self.sa.last_fetch_error)
        self.assertIn("origin", self.sa.last_fetch_error)

    def test_a_genuine_lease_rejection_is_still_a_lost_race(self):
        self.assertTrue(self.sb.acquire_runner("coach", "tok-b"))
        # boxA pushes with an expect-absent lease WITHOUT fetching first: git
        # rejects the CAS ("stale info") — the ordinary lost-race outcome.
        ref = claim_ref("coach", ENV)
        payload = self.sa._payload("coach", "tok-a", "RUNNING")
        self.assertFalse(self.sa._write_ref(ref, payload, None))
        # …and through the public API a live holder is reported as HELD, with no
        # fetch error recorded (the store was perfectly readable).
        with self.assertRaises(RunnerHeld):
            self.sa.acquire_runner("coach", "tok-a")
        self.assertIsNone(self.sa.last_fetch_error)

    def test_lease_rejection_classifier(self):
        self.assertTrue(_is_lease_rejection(
            " ! [rejected]  abc -> refs/mind/claim/x/y (stale info)\n"))
        self.assertTrue(_is_lease_rejection("! [rejected] (fetch first)"))
        self.assertFalse(_is_lease_rejection(
            " ! [remote rejected] abc -> refs/x (pre-receive hook declined)"))
        self.assertFalse(_is_lease_rejection(
            "fatal: could not read Username for 'https://github.com': "
            "No such device or address"))
        self.assertFalse(_is_lease_rejection("fatal: Could not resolve host: github.com"))
        self.assertFalse(_is_lease_rejection(""))

    def test_default_remote_precedence(self):
        self.assertEqual(GitRefClaimStore.default_remote(self.a), "origin")
        claims = os.path.join(self.tmp, "claims.git")
        subprocess.run(["git", "init", "--bare", "-q", claims], check=True)
        _git(self.a, "remote", "add", "claims", claims)
        self.assertEqual(GitRefClaimStore.default_remote(self.a), "claims")
        os.environ["RUNNER_CLAIM_REMOTE"] = "elsewhere"
        self.assertEqual(GitRefClaimStore.default_remote(self.a), "elsewhere")

    def test_claims_remote_arbitrates_when_origin_is_read_only(self):
        claims = os.path.join(self.tmp, "claims.git")
        subprocess.run(["git", "init", "--bare", "-q", claims], check=True)
        _git(self.a, "remote", "add", "claims", claims)
        _git(self.a, "remote", "set-url", "origin", os.path.join(self.tmp, "missing.git"))
        store = _store(self.a, "boxA", remote=GitRefClaimStore.default_remote(self.a))
        self.assertEqual(store.remote, "claims")
        self.assertTrue(store.acquire_runner("coach", "tok-a"))
        self.assertIsNone(store.last_fetch_error)
        # The claim landed on the claims arbiter, not on origin.
        listing = subprocess.run(
            ["git", "ls-remote", claims, "refs/mind/claim/*"],
            capture_output=True, text=True, check=False,
        ).stdout
        self.assertIn(claim_ref("coach", ENV), listing)


if __name__ == "__main__":
    unittest.main()
