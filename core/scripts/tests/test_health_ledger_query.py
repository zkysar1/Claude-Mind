"""test_health_ledger_query.py — unit tests for the read-only ledger query
utility (Phase 2). Spec: core/config/conventions/health-ledger.md.

HERMETIC: exercises build_summary (the pure core) over synthetic record sets.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "health_ledger_query", CORE_SCRIPTS / "health-ledger-query.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MOD = _load()
_CFG = {"mode": "collect-only", "calibration": {"min_days": 30, "min_records": 50}}


class TestBuildSummary(unittest.TestCase):
    def test_empty_ledger(self):
        s = _MOD.build_summary([], _CFG)
        self.assertEqual(s["record_count"], 0)
        self.assertIsNone(s["latest"])
        self.assertFalse(s["calibration"]["complete"])
        self.assertEqual(s["calibration"]["days_remaining"], 30)
        self.assertEqual(s["calibration"]["records_remaining"], 50)

    def test_latest_is_newest_nonwarmup(self):
        recs = [  # newest-first
            {"warmup": True, "composite": 0.9, "ts": "2026-06-03T13:00:00"},
            {"warmup": False, "composite": 0.7, "iteration": 40, "ts": "2026-06-03T12:00:00",
             "composite_trend": -0.01, "baseline": 0.71, "below_baseline": False},
        ]
        s = _MOD.build_summary(recs, _CFG)
        self.assertEqual(s["latest"]["iteration"], 40)
        self.assertEqual(s["record_count"], 2)  # warmup still counts toward calibration

    def test_calibration_incomplete_partial(self):
        recs = [{"ts": f"2026-06-{d:02d}T10:00:00", "warmup": False, "composite": 0.7}
                for d in range(1, 6)]  # 5 distinct days, 5 records
        s = _MOD.build_summary(recs, _CFG)
        self.assertEqual(s["calibration"]["days"], 5)
        self.assertEqual(s["calibration"]["records"], 5)
        self.assertFalse(s["calibration"]["complete"])
        self.assertEqual(s["calibration"]["days_remaining"], 25)
        self.assertEqual(s["calibration"]["records_remaining"], 45)

    def test_calibration_complete(self):
        recs = []
        for d in range(1, 31):            # 30 distinct days
            for _ in range(2):            # 60 records
                recs.append({"ts": f"2026-04-{d:02d}T10:00:00", "warmup": False, "composite": 0.7})
        s = _MOD.build_summary(recs, _CFG)
        self.assertTrue(s["calibration"]["complete"])
        self.assertEqual(s["calibration"]["days_remaining"], 0)
        self.assertEqual(s["calibration"]["records_remaining"], 0)

    def test_consecutive_surfaced(self):
        recs = [
            {"warmup": False, "composite": 0.3, "below_baseline": True, "ts": "2026-06-03T12:00:00"},
            {"warmup": False, "composite": 0.3, "below_baseline": True, "ts": "2026-06-03T11:00:00"},
        ]
        s = _MOD.build_summary(recs, _CFG)
        self.assertEqual(s["consecutive_below_baseline"], 2)

    def test_calibration_uses_full_history_when_health_dir_given(self):
        # Windowed `records` spans only 1 day (8 iterations same date), but the
        # full ledger on disk spans 30 days → calibration must read the disk, not
        # the window. Guards the sliding-window under-count.
        import json, tempfile, shutil
        from pathlib import Path as _P
        tmp = _P(tempfile.mkdtemp())
        try:
            health = tmp / "health"
            health.mkdir()
            for d in range(1, 31):
                (health / f"2026-04-{d:02d}.jsonl").write_text(
                    "".join(json.dumps({"ts": f"2026-04-{d:02d}T{h:02d}:00:00",
                                        "warmup": False, "composite": 0.7}) + "\n"
                            for h in range(2)), encoding="utf-8")
            window = [{"ts": "2026-04-30T07:00:00", "warmup": False, "composite": 0.7}
                      for _ in range(8)]  # 1 distinct day in the window
            s = _MOD.build_summary(window, _CFG, health_dir=health)
            self.assertEqual(s["calibration"]["days"], 30)     # full history, not 1
            self.assertEqual(s["calibration"]["records"], 60)
            self.assertTrue(s["calibration"]["complete"])
            self.assertEqual(s["record_count"], 8)             # window count unchanged
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_render_does_not_crash(self):
        recs = [{"warmup": False, "composite": 0.7, "ts": "2026-06-03T12:00:00",
                 "composite_trend": -0.01, "baseline": 0.71, "below_baseline": False,
                 "iteration": 5}]
        s = _MOD.build_summary(recs, _CFG)
        out = _MOD._render(s, recs, 5)
        self.assertIn("HEALTH LEDGER", out)
        self.assertIn("Calibration", out)


if __name__ == "__main__":
    unittest.main()
