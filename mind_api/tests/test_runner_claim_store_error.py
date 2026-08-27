"""An UNWRITABLE claim store is a store error at the daemon boundary — never HELD.

Sibling of test_runner_claim_local_arm.py. Pins the 2026-08-27 coach-mind fix at
the endpoint layer: when the git-ref store's remote refuses the push for a reason
that is not a lease rejection (no credential, not a repository, unreachable), the
``runner/acquire`` endpoint answers ``ok:false`` + ``store_error:true`` with the
remote's own words, so ``runner-claim.sh`` prints FAILED (rc=2) instead of
"another machine owns a live claim" (rc=4) over an empty claim namespace. The
``runner/claims`` listing carries the fetch failure alongside its (empty) rows.
"""

import asyncio
import inspect
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MIND_API = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_MIND_API)
for p in (_MIND_API, os.path.join(_ROOT, "core", "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.endpoints import admin  # noqa: E402


def _payload(resp):
    """Extract the JSON body from a Response across attribute spellings."""
    for attr in ("body", "data", "payload", "_body"):
        raw = getattr(resp, attr, None)
        if raw is None:
            continue
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, dict):
            return raw
    raise AssertionError(f"could not read body off {type(resp).__name__}")


def _status(resp):
    for attr in ("status", "status_code", "code"):
        val = getattr(resp, attr, None)
        if isinstance(val, int):
            return val
    return None


def _ctx(project_root, **query):
    from pathlib import Path
    return types.SimpleNamespace(
        query=dict(query),
        paths=types.SimpleNamespace(project_root=Path(project_root)),
    )


def _call(endpoint, ctx):
    result = endpoint(ctx)
    if inspect.iscoroutine(result):
        result = asyncio.run(result)
    return result


def _mk_repo(tmp, name, origin_url):
    repo = os.path.join(tmp, name)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email",
                    "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", origin_url],
                   check=True)
    return repo


class StoreErrorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self._env = {k: os.environ.get(k)
                     for k in ("STORAGE_BACKEND", "ENVIRONMENT_ID",
                               "RUNNER_CLAIM_REMOTE")}
        os.environ["STORAGE_BACKEND"] = "local"
        os.environ["ENVIRONMENT_ID"] = "test-env"
        os.environ.pop("RUNNER_CLAIM_REMOTE", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _unwritable_repo(self, name="ro"):
        # A remote URL that is not a repository: git refuses the push exactly
        # the way a credential-less HTTPS origin does — loudly, and for a reason
        # that is nobody's claim.
        return _mk_repo(self.tmp, name, os.path.join(self.tmp, "missing.git"))

    def test_acquire_on_unwritable_store_is_a_store_error_not_held(self):
        repo = self._unwritable_repo()
        resp = _call(admin.runner_acquire, _ctx(repo, agent="coach", token="tok"))
        body = _payload(resp)
        self.assertFalse(body.get("ok"), body)
        self.assertTrue(body.get("store_error"), body)
        self.assertFalse(body.get("held"), body)
        self.assertIn("unwritable", body.get("error", ""), body)
        self.assertIn("origin", body.get("error", ""), body)
        self.assertEqual(_status(resp), 500)

    def test_claims_listing_carries_the_fetch_failure(self):
        repo = self._unwritable_repo("ro2")
        body = _payload(_call(admin.runner_claims, _ctx(repo)))
        self.assertEqual(body.get("claims"), [], body)
        self.assertIsNotNone(body.get("store_error"), body)
        self.assertIn("origin", body["store_error"])

    def test_writable_store_acquires_and_reports_no_store_error(self):
        bare = os.path.join(self.tmp, "arbiter.git")
        subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        repo = _mk_repo(self.tmp, "rw", bare)
        body = _payload(_call(admin.runner_acquire,
                              _ctx(repo, agent="coach", token="tok")))
        self.assertTrue(body.get("ok"), body)
        self.assertFalse(body.get("held"), body)
        self.assertFalse(body.get("store_error"), body)
        listing = _payload(_call(admin.runner_claims, _ctx(repo)))
        self.assertIsNone(listing.get("store_error"), listing)
        self.assertEqual(len(listing.get("claims") or []), 1, listing)


if __name__ == "__main__":
    unittest.main()
