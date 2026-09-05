#!/usr/bin/env python3
""" — a dependent must not be released on its blocker's STATUS alone.

The two tests below are the goal's two verification checks, verbatim:

  1. close a blocker whose commit exists only on a worker carrier ref and assert
     the dependent stays blocked;
  2. assert the dependent IS released once that commit is reachable from
     origin/main.

WHY THE PROBER IS INJECTED RATHER THAN MOCKED AWAY. `_clear_stale_blockers`
loads `commit-reachability.py` lazily and that prober shells out to git, so a
test driving it for real would assert against THIS checkout's ref layout — which
differs per box and per fetch, making the test's verdict a function of where it
runs (the pinned-state rule). The seam is the prober module itself: these tests
pass a stub exposing the same `triage(repo, sha, target_ref=...)` contract, so
the code under test is the REAL release path with only the git boundary
replaced. The stub returns the prober's own verdict strings, so a rename there
fails these tests rather than silently passing.

THE FAIL-OPEN CASES ARE TESTED TOO, and they matter more than the happy path: a
missing sha, an unloadable prober and an INCONCLUSIVE verdict must all RELEASE.
A regression that turned any of them into a hold would freeze every blocked goal
on the box the first time git hiccuped, and no test asserting only the two
checks above would notice.
"""

import os
import sys
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import _delivery_gate as dg  # noqa: E402


class _StubProber:
    """Same `triage` contract as commit-reachability.py, no git."""

    def __init__(self, verdict, reason="stub"):
        self.verdict = verdict
        self.reason = reason
        self.calls = []

    def triage(self, repo, sha, target_ref="origin/main"):
        self.calls.append((repo, sha, target_ref))
        return {"verdict": self.verdict, "reason": self.reason,
                "landed": self.verdict == "LANDED"}


def _load_aspirations():
    import importlib.util
    path = os.path.join(SCRIPTS, "aspirations.py")
    spec = importlib.util.spec_from_file_location("_asp_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _items(blocker_extra=None):
    """One aspiration: blocker g-blk (terminal) and dependent g-dep blocked on it."""
    blocker = {"id": "g-blk", "status": "completed"}
    blocker.update(blocker_extra or {})
    return [{
        "id": "asp-t",
        "goals": [
            blocker,
            {"id": "g-dep", "status": "blocked",
             "blocked_by": ["g-blk"], "blocked_since": "2026-09-04T00:00:00"},
        ],
    }]


def _dep(items):
    return [g for g in items[0]["goals"] if g["id"] == "g-dep"][0]


class TestDeliveryGatePredicate(unittest.TestCase):
    def test_no_recorded_sha_is_unknown_and_releases(self):
        state, detail = dg.blocker_delivery_state({"id": "g-blk"})
        self.assertEqual(state, dg.UNKNOWN)
        self.assertIn("commit_sha", detail)

    def test_worker_ref_only_is_pending(self):
        stub = _StubProber("STRANDED_WORKER_REF", "only on refs/workers/**")
        state, _ = dg.blocker_delivery_state(
            {"id": "g-blk", "commit_sha": "c0cb93130"}, prober=stub)
        self.assertEqual(state, dg.PENDING)

    def test_landed_is_delivered(self):
        stub = _StubProber("LANDED")
        state, _ = dg.blocker_delivery_state(
            {"id": "g-blk", "commit_sha": "c0cb93130"}, prober=stub)
        self.assertEqual(state, dg.DELIVERED)

    def test_inconclusive_releases_rather_than_freezing(self):
        stub = _StubProber("INCONCLUSIVE", "probe could not run")
        state, _ = dg.blocker_delivery_state(
            {"id": "g-blk", "commit_sha": "c0cb93130"}, prober=stub)
        self.assertEqual(state, dg.UNKNOWN)

    def test_unrecognised_verdict_releases(self):
        """A future/renamed verdict must fall through to UNKNOWN, never to PENDING."""
        stub = _StubProber("SOME_NEW_VERDICT")
        state, _ = dg.blocker_delivery_state(
            {"id": "g-blk", "commit_sha": "c0cb93130"}, prober=stub)
        self.assertEqual(state, dg.UNKNOWN)

    def test_prober_exception_releases(self):
        class Boom:
            def triage(self, *a, **k):
                raise RuntimeError("git exploded")
        state, detail = dg.blocker_delivery_state(
            {"id": "g-blk", "commit_sha": "c0cb93130"}, prober=Boom())
        self.assertEqual(state, dg.UNKNOWN)
        self.assertIn("raised", detail)

    def test_no_verdict_is_reused_across_calls(self):
        """Outcome 2: the probe re-runs; it must not inherit an earlier verdict."""
        goal = {"id": "g-blk", "commit_sha": "c0cb93130"}
        stranded = _StubProber("STRANDED_WORKER_REF")
        self.assertEqual(dg.blocker_delivery_state(goal, prober=stranded)[0],
                         dg.PENDING)
        landed = _StubProber("LANDED")
        self.assertEqual(dg.blocker_delivery_state(goal, prober=landed)[0],
                         dg.DELIVERED)
        # and the record carries no stamped verdict of its own
        self.assertEqual(set(goal), {"id", "commit_sha"})


class TestClearStaleBlockersDeliveryGate(unittest.TestCase):
    """The goal's two checks, against the REAL release path."""

    @classmethod
    def setUpClass(cls):
        cls.asp = _load_aspirations()

    def _run(self, items, prober):
        """Drive _clear_stale_blockers with the prober swapped at the seam."""
        real_load = dg._load_prober
        dg._load_prober = lambda script_dir=None: prober
        try:
            self.asp._clear_stale_blockers(items, {"g-blk"})
        finally:
            dg._load_prober = real_load

    def test_check1_worker_ref_blocker_does_not_release_dependent(self):
        items = _items({"commit_sha": "c0cb93130"})
        self._run(items, _StubProber("STRANDED_WORKER_REF",
                                     "only on refs/workers/alpha/<sid>"))
        dep = _dep(items)
        self.assertEqual(dep["blocked_by"], ["g-blk"],
                         "dependent was released while the blocker's deliverable "
                         "existed only on a worker carrier ref")
        self.assertIsNotNone(dep["blocked_since"])

    def test_check2_dependent_released_once_commit_is_reachable(self):
        items = _items({"commit_sha": "c0cb93130"})
        self._run(items, _StubProber("LANDED"))
        dep = _dep(items)
        self.assertEqual(dep["blocked_by"], [])
        self.assertIsNone(dep["blocked_since"])

    def test_blocker_without_sha_still_releases(self):
        """Backward compatibility: the pre-gate behaviour for every existing goal.

        commit_sha is populated on ~no goal today, so if this released nothing the
        gate would freeze the entire live blocked population on the first close.
        """
        items = _items()
        self._run(items, _StubProber("STRANDED_WORKER_REF"))
        self.assertEqual(_dep(items)["blocked_by"], [])

    def test_unrelated_blocker_entry_is_preserved(self):
        """Only the resolved id is considered; other dependencies stay untouched."""
        items = _items({"commit_sha": "c0cb93130"})
        _dep(items)["blocked_by"] = ["g-blk", "g-other"]
        self._run(items, _StubProber("LANDED"))
        self.assertEqual(_dep(items)["blocked_by"], ["g-other"])

    def test_gate_can_be_disabled(self):
        items = _items({"commit_sha": "c0cb93130"})
        self.asp._clear_stale_blockers(items, {"g-blk"}, delivery_gate=False)
        self.assertEqual(_dep(items)["blocked_by"], [])


if __name__ == "__main__":
    unittest.main()
