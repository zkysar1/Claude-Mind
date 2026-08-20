"""The LOCAL-backend arm of the runner-claim endpoints ().

`test_runtime_runner_claim.py` covers the own-cloud endpoint contract and the
historical non-own-cloud NO-OP. This file covers what replaced part of that
no-op: on a local backend with a usable git remote, the four runner endpoints
now route to a git-ref claim store instead of returning `{ok, noop}`.

WHY A SEPARATE FILE AND NOT AN EXTENSION OF THAT ONE: the no-op tests there
still pass, and it would be easy to read that as "nothing changed". It is
passing for a specific reason — the daemon fixture's project_root has no git
remote, so `available()` is False and the historical no-op is preserved exactly.
That is the intended fail-safe, but a suite that only ever exercises the
unavailable path proves the arm is INERT, not that it works. Everything below
supplies the other half.

File basename starts with `test_` so domain-leak-check.sh skips it (agent names
here are fixtures, not a domain leak).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
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


def _ctx(project_root, **query):
    from pathlib import Path
    return types.SimpleNamespace(
        query=dict(query),
        paths=types.SimpleNamespace(project_root=Path(project_root)),
    )


def _mk_repo(tmp, name, with_remote=True):
    repo = os.path.join(tmp, name)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email",
                    "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "test"], check=True)
    if with_remote:
        origin = os.path.join(tmp, f"{name}-origin.git")
        subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", origin],
                       check=True)
    return repo


class LocalArmTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self._env = {k: os.environ.get(k)
                     for k in ("STORAGE_BACKEND", "ENVIRONMENT_ID")}
        os.environ["STORAGE_BACKEND"] = "local"
        os.environ["ENVIRONMENT_ID"] = "test-env"

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    # ------------------------------------------------------- store resolution

    def test_store_resolves_when_remote_present(self):
        repo = _mk_repo(self.tmp, "withremote")
        store = admin._local_claim_store(_ctx(repo))
        self.assertIsNotNone(store, "local arm did not engage despite a remote")
        self.assertEqual(store.env_id, "test-env")

    def test_store_is_none_without_remote(self):
        repo = _mk_repo(self.tmp, "noremote", with_remote=False)
        self.assertIsNone(admin._local_claim_store(_ctx(repo)))

    def test_store_is_none_outside_a_repo(self):
        plain = os.path.join(self.tmp, "notarepo")
        os.makedirs(plain)
        self.assertIsNone(admin._local_claim_store(_ctx(plain)))

    # ------------------------------------------------------------- preamble

    def test_preamble_routes_to_store_when_available(self):
        repo = _mk_repo(self.tmp, "p1")
        backend, get_backend, early = admin._runner_preamble(
            _ctx(repo, agent="zeta", token="tok"))
        self.assertIsNone(early, "preamble short-circuited despite a usable store")
        self.assertEqual(backend, "local")
        store = get_backend()
        # Must expose the SAME method names OwnCloudBackend does, or the four
        # endpoints AttributeError at call time rather than at wiring time.
        for m in ("acquire_runner", "heartbeat", "release_runner",
                  "get_runner_state", "reclaim_if_stale", "list_runner_claims"):
            self.assertTrue(callable(getattr(store, m, None)),
                            f"store is missing {m}()")

    def test_preamble_noops_without_remote(self):
        repo = _mk_repo(self.tmp, "p2", with_remote=False)
        backend, get_backend, early = admin._runner_preamble(
            _ctx(repo, agent="zeta", token="tok"))
        self.assertIsNotNone(early, "expected the historical no-op")
        body = _payload(early)
        self.assertTrue(body["ok"])
        self.assertTrue(body["noop"])
        self.assertIs(body["claim_store"], False)

    def test_preamble_still_validates_params_before_backend(self):
        repo = _mk_repo(self.tmp, "p3")
        _b, _g, early = admin._runner_preamble(_ctx(repo, agent="", token=""))
        self.assertIsNotNone(early)
        self.assertEqual(early.status, 400)

    def test_preamble_own_cloud_path_untouched(self):
        """Criterion 4: own-cloud must not route through the local arm."""
        os.environ["STORAGE_BACKEND"] = "own-cloud"
        repo = _mk_repo(self.tmp, "p4")
        backend, get_backend, early = admin._runner_preamble(
            _ctx(repo, agent="zeta", token="tok"))
        self.assertEqual(backend, "own-cloud")
        self.assertIsNone(early)
        # It resolved storage_backend.get_backend, NOT a GitRefClaimStore. The
        # callable is never invoked here, so no cloud call is made.
        self.assertEqual(getattr(get_backend, "__name__", ""), "get_backend")

    # -------------------------------------------------------- claims endpoint

    def test_claims_reports_claim_store_false_without_remote(self):
        repo = _mk_repo(self.tmp, "c1", with_remote=False)
        body = _payload(admin.runner_claims(_ctx(repo)))
        self.assertTrue(body["ok"])
        self.assertEqual(body["claims"], [])
        self.assertIs(body["claim_store"], False)

    def test_claims_reports_a_real_verdict_with_remote(self):
        """Criterion 1's data path: a live claim must surface as a real row
        with a usable freshness threshold — not an rc=4 REFUSE.

        `runner-claim.sh status` refuses unless BOTH `claim_store` is truthy and
        `runner_stale_seconds` is a positive int, so both are asserted here.
        """
        repo = _mk_repo(self.tmp, "c2")
        store = admin._local_claim_store(_ctx(repo))
        self.assertTrue(store.acquire_runner("zeta", "tok-a"))

        body = _payload(admin.runner_claims(_ctx(repo)))
        self.assertTrue(body["ok"])
        self.assertIs(body["claim_store"], True)
        self.assertEqual(body["environment_id"], "test-env")
        self.assertIsInstance(body["runner_stale_seconds"], int)
        self.assertGreater(body["runner_stale_seconds"], 0)

        rows = [c for c in body["claims"] if c["agent"] == "zeta"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_state"], "RUNNING")
        self.assertTrue(rows[0]["machine_id"])
        self.assertIsInstance(rows[0]["heartbeat_at"], int)

    def test_claims_never_exposes_the_raw_token(self):
        repo = _mk_repo(self.tmp, "c3")
        store = admin._local_claim_store(_ctx(repo))
        secret = "raw-bearer-token-do-not-publish"
        store.acquire_runner("zeta", secret)
        body = _payload(admin.runner_claims(_ctx(repo)))
        self.assertNotIn(secret, json.dumps(body))
        self.assertTrue(body["claims"][0]["runner_token_fp"])


if __name__ == "__main__":
    unittest.main()
