"""Regression tests for the explicit-supersedes-infer path ().

WHY THIS EXISTS
`iteration-close.sh do_state_update` runs `utilization-feedback.sh --infer`
immediately BEFORE `phase-4-26-gate.sh` — it is the PRODUCER for the flag that
gate consumes (g-115-3123, whose ordering fix is guarded by
`test_utilization_repair_ordering.py`). But `--infer` CLOSES the session
(`utilization_pending=false`), so when the gate then refused with

    "method=infer with helpful=0 — no positive signal
     ... run utilization-feedback.sh manually with explicit --helpful items"

that instructed recovery returned `{"status": "already_processed"}` and did
nothing. The recovery was unreachable BY CONSTRUCTION, leaving
`--no-retrieval-applicable` as the only exit — an assertion that is FALSE
whenever retrieval genuinely helped. So the gate's own escape hatch wrote a
false record into the very learning-signal store the gate exists to protect.
Observed 4x in ~2 days: g-335-43, g-335-44, g-335-236, g-335-254.

THE FIX: one sanctioned supersede. An EXPLICIT verdict may supersede an
INFERRED one, because they touch DIFFERENT counters (`--infer` bumps
times_inferred_helpful; explicit bumps times_helpful) — so it is a correction,
not a double-count. Explicit-over-explicit stays refused; that WOULD double-count.

HERMETIC: every case runs with `--dry-run`, which returns before any counter is
applied, and SESSION_PATH is redirected to a tmp file. No real store is touched.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "utilization_feedback_supersede", CORE_SCRIPTS / "utilization-feedback.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MOD = _load()


class _Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.session = self.tmp / "retrieval-session.json"
        self._saved_path = _MOD.SESSION_PATH
        _MOD.SESSION_PATH = self.session

    def tearDown(self):
        import shutil
        _MOD.SESSION_PATH = self._saved_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_session(self, *, method, pending=False):
        """A closed session carrying one genuinely-helpful supplementary entry
        plus one that is real noise — the g-335-254 shape."""
        self.session.write_text(json.dumps({
            "schema_version": 3,
            "goal_id": "g-test-01",
            "tree_nodes_loaded": ["node-a", "node-b"],
            "supplementary_items": [
                {"id": "rb-3452", "type": "reasoning_bank"},
                {"id": "rb-9999", "type": "reasoning_bank"},
            ],
            "utilization_pending": pending,
            "utilization_completed_at": None if pending else "2026-07-26T04:24:00",
            "utilization_method": method,
            "inference_stats": {"helpful": 0, "noise": 20},
        }), encoding="utf-8")

    def _run(self, argv):
        """Invoke main() with argv; return (exit_code, parsed_stdout)."""
        import io
        import contextlib
        saved = sys.argv
        sys.argv = ["utilization-feedback.py"] + argv
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                try:
                    _MOD.main()
                    code = 0
                except SystemExit as e:
                    code = e.code or 0
        finally:
            sys.argv = saved
        out = buf.getvalue().strip()
        try:
            return code, json.loads(out)
        except json.JSONDecodeError:
            return code, {"_raw": out}


class TestExplicitSupersedesInfer(_Base):
    def test_explicit_supersedes_an_infer_verdict(self):
        # THE regression: before the fix this returned already_processed and the
        # goal's only remaining exit was to assert no-retrieval-applicable.
        self._write_session(method="infer")
        code, out = self._run(["--goal", "g-test-01", "--helpful", "rb-3452", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(out.get("status"), "dry_run",
                         f"explicit --helpful must supersede method=infer, got {out}")
        self.assertTrue(out.get("superseding_infer"))

    def test_supersede_does_not_reapply_noise(self):
        # The --helpful path marks everything NOT named as noise, and the infer
        # pass already applied its own noise verdict to the same items — so a
        # naive supersede would double-count times_noise on every one.
        self._write_session(method="infer")
        _, out = self._run(["--goal", "g-test-01", "--helpful", "rb-3452", "--dry-run"])
        self.assertEqual(out.get("supp_noise"), [],
                         "supersede must not re-apply the infer pass's noise verdict")
        self.assertEqual(out.get("tree_noise"), [])
        # ...while still contributing the corrective helpful signal.
        self.assertIn("rb-3452", out.get("supp_helpful") or [])

    def test_all_helpful_also_supersedes(self):
        self._write_session(method="infer")
        _, out = self._run(["--goal", "g-test-01", "--all-helpful", "--dry-run"])
        self.assertEqual(out.get("status"), "dry_run")
        self.assertTrue(out.get("superseding_infer"))


class TestAllNonPositiveMethodsAreSupersedable(_Base):
    """: the infer-only fix was too narrow.

    One iteration after g-115-3173 landed, the identical unreachable recovery
    reappeared with `method=all_noise`: phase-4-26-gate refused state-update
    saying "run utilization-feedback.sh manually with explicit --helpful items",
    and that call returned already_processed. The gate's instruction does not
    vary by method, so neither may the supersede — otherwise the only remaining
    exit is `--no-retrieval-applicable`, false whenever retrieval actually ran.

    All three superseded methods leave `times_helpful` untouched (infer bumps
    times_inferred_helpful, all_noise bumps times_noise, all_unknown bumps
    nothing), so an explicit-positive pass over any of them corrects on a
    counter the prior pass never wrote.
    """

    def test_explicit_supersedes_all_noise(self):
        self._write_session(method="all_noise")
        code, out = self._run(["--goal", "g-test-01", "--helpful", "rb-3452", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(out.get("status"), "dry_run",
                         f"explicit --helpful must supersede method=all_noise, got {out}")
        self.assertTrue(out.get("superseding_infer"))

    def test_explicit_supersedes_all_unknown(self):
        self._write_session(method="all_unknown")
        _, out = self._run(["--goal", "g-test-01", "--all-helpful", "--dry-run"])
        self.assertEqual(out.get("status"), "dry_run")
        self.assertTrue(out.get("superseding_infer"))

    def test_all_noise_supersede_does_not_reapply_noise(self):
        # all_noise already marked EVERY item noise; re-applying would double it.
        self._write_session(method="all_noise")
        _, out = self._run(["--goal", "g-test-01", "--helpful", "rb-3452", "--dry-run"])
        self.assertEqual(out.get("supp_noise"), [])
        self.assertEqual(out.get("tree_noise"), [])
        self.assertIn("rb-3452", out.get("supp_helpful") or [])


class TestSupersedeIsNarrow(_Base):
    def test_explicit_does_not_supersede_explicit(self):
        # A second explicit verdict WOULD double-count times_helpful.
        self._write_session(method="manual")
        _, out = self._run(["--goal", "g-test-01", "--helpful", "rb-3452", "--dry-run"])
        self.assertEqual(out.get("status"), "already_processed")
        self.assertIn("hint", out, "the refusal must name the one reachable correction")

    def test_infer_does_not_supersede_infer(self):
        self._write_session(method="infer")
        _, out = self._run(["--goal", "g-test-01", "--infer", "--dry-run"])
        self.assertEqual(out.get("status"), "already_processed")

    def test_all_noise_is_not_a_supersede(self):
        # The SUPERSEDING call must be explicit-POSITIVE. --all-noise carries no
        # positive signal, so it may not supersede anything (in either direction).
        self._write_session(method="infer")
        _, out = self._run(["--goal", "g-test-01", "--all-noise", "--dry-run"])
        self.assertEqual(out.get("status"), "already_processed")

    def test_all_helpful_does_not_supersede_all_helpful(self):
        self._write_session(method="all_helpful")
        _, out = self._run(["--goal", "g-test-01", "--all-helpful", "--dry-run"])
        self.assertEqual(out.get("status"), "already_processed",
                         "explicit-positive over explicit-positive double-counts times_helpful")


class TestNormalPathsUnaffected(_Base):
    def test_open_session_still_processes_normally(self):
        self._write_session(method=None, pending=True)
        _, out = self._run(["--goal", "g-test-01", "--helpful", "rb-3452", "--dry-run"])
        self.assertEqual(out.get("status"), "dry_run")
        self.assertFalse(out.get("superseding_infer"),
                         "an open session is a normal write, not a supersede")
        # Noise IS applied on the normal path — only supersede suppresses it.
        self.assertEqual([s for s in (out.get("supp_noise") or [])], ["rb-9999"])

    def test_goal_mismatch_still_refused(self):
        self._write_session(method="infer")
        _, out = self._run(["--goal", "g-other-99", "--helpful", "rb-3452", "--dry-run"])
        self.assertEqual(out.get("status"), "goal_mismatch")


if __name__ == "__main__":
    unittest.main()
