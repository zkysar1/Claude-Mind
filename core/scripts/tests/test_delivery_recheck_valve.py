#!/usr/bin/env python3
""" — the OTHER release path, and the valve that keeps its hold finite.

`test_delivery_gated_unblock.py` drives `aspirations.py _clear_stale_blockers`.
That is one of TWO release paths and it is **not** the one `/aspirations-verify`
Phase 5 calls, nor the one that performs the literal `blocked` -> `pending`
transition the goal's outcome 1 names — `dependent-unblock.py` Step 1b does
both. A gate is only as broad as its entry points (guard-3448), so the goal's
two checks are re-asserted here against that path rather than assumed to
transfer from the sibling file.

The valve tests exist because the hold and its release are SEPARABLE defects.
Both gate sites fire only on a predecessor's terminal TRANSITION, and a held
dependent's predecessor has already made that transition — so nothing would ever
look at it again. A gate with no read-time re-probe is not a safety mechanism,
it is a permanent freeze, and no test of the gate alone can tell the two apart.

FIXTURES WRITE A REAL STORE FILE INTO A TemporaryDirectory and repoint the
module's WORLD_DIR at it, following the tmp-world convention these test trees
already use. Stubbing the scan instead would assert against the stub rather than
against the reader the defect lives in.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _delivery_gate as dg  # noqa: E402
from test_delivery_gated_unblock import _StubProber  # noqa: E402


def _load_hyphenated(filename, modname):
    """Load a hyphenated sibling script as a module (its name is not importable)."""
    path = os.path.join(SCRIPTS, filename)
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StoreFixture:
    """A tmp store plus a recording `_update`, wired into a loaded module."""

    def __init__(self, goals):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "aspirations.jsonl").write_text(
            json.dumps({"id": "asp-t", "status": "active", "goals": goals}) + "\n",
            encoding="utf-8")
        self.writes = []

    def bind(self, mod):
        mod.WORLD_DIR = self.root
        mod.AGENT_DIR = None
        return self

    def update(self, source, goal_id, field, value, dry_run):
        self.writes.append((goal_id, field, value))
        return True, ""

    def close(self):
        self.tmp.cleanup()


def _blocked_pair(sha="c0cb93130"):
    """Blocker g-blk terminal with a recorded sha; g-dep blocked on it."""
    return [
        {"id": "g-blk", "status": "completed", "commit_sha": sha},
        {"id": "g-dep", "status": "blocked", "blocked_by": ["g-blk"],
         "blocked_since": "2026-09-04T00:00:00"},
    ]


class TestDependentUnblockDeliveryGate(unittest.TestCase):
    """The goal's two checks, against the path that flips the status."""

    def setUp(self):
        self.mod = _load_hyphenated("dependent-unblock.py", "_dep_unblock_t")
        self.fx = _StoreFixture(_blocked_pair()).bind(self.mod)
        self.mod._update = self.fx.update
        self.addCleanup(self.fx.close)

    def _run(self, verdict, extra=()):
        stub = _StubProber(verdict)
        real = dg._load_prober
        dg._load_prober = lambda script_dir=None: stub
        try:
            return self.mod.main(["--goal", "g-blk", "--summary", "done",
                                  *extra])
        finally:
            dg._load_prober = real

    def test_check1_stranded_predecessor_does_not_transition_dependent(self):
        self.assertEqual(self._run("STRANDED_WORKER_REF"), 0)
        fields = [w[1] for w in self.fx.writes]
        self.assertNotIn("status", fields,
                         "dependent was flipped blocked->pending while the "
                         "predecessor's commit was only on a worker carrier ref")
        self.assertNotIn("blocked_by", fields)

    def test_check2_landed_predecessor_releases_dependent(self):
        self.assertEqual(self._run("LANDED"), 0)
        writes = {(w[0], w[1]): w[2] for w in self.fx.writes}
        self.assertEqual(writes.get(("g-dep", "blocked_by")), "[]")
        self.assertEqual(writes.get(("g-dep", "status")), "pending")

    def test_ignore_delivery_flag_restores_pre_gate_behaviour(self):
        """The documented escape hatch for deliverables that are not commits."""
        self._run("STRANDED_WORKER_REF", extra=("--ignore-delivery",))
        self.assertIn("status", [w[1] for w in self.fx.writes])


class TestDeliveryRecheckValve(unittest.TestCase):
    """The read-time re-probe: without it every hold above is permanent."""

    def _sweep(self, goals, verdict, apply=True):
        mod = _load_hyphenated("delivery-recheck.py", "_delivery_recheck_t")
        fx = _StoreFixture(goals).bind(mod)
        self.addCleanup(fx.close)
        stub = _StubProber(verdict)
        real = dg._load_prober
        dg._load_prober = lambda script_dir=None: stub
        try:
            return mod.sweep(apply=apply, update=fx.update), fx
        finally:
            dg._load_prober = real

    def test_held_dependent_is_released_once_the_commit_lands(self):
        out, fx = self._sweep(_blocked_pair(), "LANDED")
        self.assertEqual(out["terminal_entries_checked"], 1)
        self.assertEqual(out["candidate_count"], 1)
        writes = {(w[0], w[1]): w[2] for w in fx.writes}
        self.assertEqual(writes.get(("g-dep", "blocked_by")), "[]")
        self.assertEqual(writes.get(("g-dep", "status")), "pending")

    def test_still_stranded_dependent_stays_held(self):
        out, fx = self._sweep(_blocked_pair(), "STRANDED_WORKER_REF")
        self.assertEqual(out["candidate_count"], 0)
        self.assertEqual(len(out["still_held"]), 1)
        self.assertEqual(fx.writes, [])

    def test_dry_run_reports_without_writing(self):
        out, fx = self._sweep(_blocked_pair(), "LANDED", apply=False)
        self.assertEqual(out["mode"], "dry-run")
        self.assertEqual(out["candidate_count"], 1)
        self.assertEqual(fx.writes, [])

    def test_live_predecessor_is_not_this_sweeps_business(self):
        """An unfinished blocker is an ordinary dependency wait, not a hold."""
        goals = _blocked_pair()
        goals[0]["status"] = "in-progress"
        out, fx = self._sweep(goals, "LANDED")
        self.assertEqual(out["terminal_entries_checked"], 0)
        self.assertEqual(fx.writes, [])

    def test_status_is_not_restored_when_a_blocker_ref_remains(self):
        """Mirrors dependent-unblock Step 1b: a structured blocker is not ours."""
        goals = _blocked_pair()
        goals[1]["blocker_ref"] = "pq-something"
        _out, fx = self._sweep(goals, "LANDED")
        fields = [w[1] for w in fx.writes]
        self.assertIn("blocked_by", fields)
        self.assertNotIn("status", fields)

    def test_walk_is_positive_controlled(self):
        """The live sweep reports 0 — prove that 0 comes from a reader that works.

        A store walk that silently returned nothing would produce the identical
        clean number every run, forever, and nothing would ever say otherwise
        (guard-2298). Two numbers that differ are the evidence.
        """
        hit, _ = self._sweep(_blocked_pair(), "LANDED", apply=False)
        miss, _ = self._sweep([{"id": "g-solo", "status": "pending"}],
                              "LANDED", apply=False)
        self.assertEqual((hit["terminal_entries_checked"],
                          miss["terminal_entries_checked"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
