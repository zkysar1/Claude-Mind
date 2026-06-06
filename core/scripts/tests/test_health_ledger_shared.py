"""test_health_ledger_shared.py — unit tests for _health_ledger.py, the shared
ledger-read SSOT (Phase 2). Spec: core/config/conventions/health-ledger.md.

HERMETIC: pure functions + tmp-dir ledger reads. No daemon, no live config.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
import sys
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _health_ledger as hl  # noqa: E402


class TestLsqSlope(unittest.TestCase):
    def test_short_series(self):
        self.assertEqual(hl.lsq_slope([]), 0.0)
        self.assertEqual(hl.lsq_slope([0.5]), 0.0)

    def test_flat(self):
        self.assertAlmostEqual(hl.lsq_slope([0.6, 0.6, 0.6]), 0.0)

    def test_decline(self):
        self.assertAlmostEqual(hl.lsq_slope([0.9, 0.8, 0.7, 0.6]), -0.1)


class TestRecentRecords(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, recs):
        (self.tmp / name).write_text(
            "".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")

    def test_absent_dir_empty(self):
        self.assertEqual(hl.recent_records(self.tmp / "nope", 10), [])

    def test_newest_first_across_day_files(self):
        # older day-file, then newer day-file; within a file, appended oldest-first
        self._write("2026-06-01.jsonl", [{"iteration": 1}, {"iteration": 2}])
        self._write("2026-06-02.jsonl", [{"iteration": 3}, {"iteration": 4}])
        recs = hl.recent_records(self.tmp, 10)
        # newest date first, reversed within file → 4, 3, 2, 1
        self.assertEqual([r["iteration"] for r in recs], [4, 3, 2, 1])

    def test_limit_honored(self):
        self._write("2026-06-02.jsonl", [{"i": 1}, {"i": 2}, {"i": 3}])
        recs = hl.recent_records(self.tmp, 2)
        self.assertEqual(len(recs), 2)

    def test_corrupt_line_skipped(self):
        (self.tmp / "2026-06-02.jsonl").write_text(
            '{"ok":1}\nNOT JSON\n{"ok":2}\n', encoding="utf-8")
        recs = hl.recent_records(self.tmp, 10)
        self.assertEqual(len(recs), 2)


class TestLatestNonwarmup(unittest.TestCase):
    def test_skips_warmup_and_null(self):
        recs = [
            {"warmup": True, "composite": 0.9},
            {"warmup": False, "composite": None},
            {"warmup": False, "composite": 0.7, "iteration": 42},
        ]
        self.assertEqual(hl.latest_nonwarmup(recs)["iteration"], 42)

    def test_all_warmup_none(self):
        self.assertIsNone(hl.latest_nonwarmup([{"warmup": True, "composite": 0.5}]))


class TestConsecutiveBelowBaseline(unittest.TestCase):
    def test_three_consecutive(self):
        recs = [
            {"warmup": False, "composite": 0.3, "below_baseline": True},
            {"warmup": False, "composite": 0.3, "below_baseline": True},
            {"warmup": False, "composite": 0.3, "below_baseline": True},
            {"warmup": False, "composite": 0.6, "below_baseline": False},
        ]
        self.assertEqual(hl.consecutive_below_baseline(recs), 3)

    def test_warmup_skipped_not_breaking(self):
        recs = [
            {"warmup": False, "composite": 0.3, "below_baseline": True},
            {"warmup": True, "composite": 0.9},        # skipped, not a break
            {"warmup": False, "composite": 0.3, "below_baseline": True},
        ]
        self.assertEqual(hl.consecutive_below_baseline(recs), 2)

    def test_newest_not_below_is_zero(self):
        recs = [
            {"warmup": False, "composite": 0.6, "below_baseline": False},
            {"warmup": False, "composite": 0.3, "below_baseline": True},
        ]
        self.assertEqual(hl.consecutive_below_baseline(recs), 0)

    def test_null_composite_skipped(self):
        recs = [
            {"warmup": False, "composite": 0.3, "below_baseline": True},
            {"warmup": False, "composite": None},      # skipped
            {"warmup": False, "composite": 0.3, "below_baseline": True},
        ]
        self.assertEqual(hl.consecutive_below_baseline(recs), 2)


class TestCalibrationProgress(unittest.TestCase):
    def test_distinct_dates_and_count(self):
        recs = [
            {"ts": "2026-06-01T10:00:00"},
            {"ts": "2026-06-01T11:00:00"},
            {"ts": "2026-06-02T09:00:00"},
        ]
        days, count = hl.calibration_progress(recs)
        self.assertEqual((days, count), (2, 3))

    def test_missing_ts_counts_record_not_day(self):
        recs = [{"ts": "2026-06-01T10:00:00"}, {"foo": 1}]
        days, count = hl.calibration_progress(recs)
        self.assertEqual((days, count), (1, 2))


class TestFullCalibrationProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, recs):
        (self.tmp / name).write_text(
            "".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")

    def test_absent_dir_zero(self):
        self.assertEqual(hl.full_calibration_progress(self.tmp / "nope"), (0, 0))

    def test_distinct_days_across_day_files(self):
        self._write("2026-06-01.jsonl", [{"ts": "2026-06-01T10:00:00"},
                                         {"ts": "2026-06-01T11:00:00"}])
        self._write("2026-06-02.jsonl", [{"ts": "2026-06-02T09:00:00"}])
        days, records = hl.full_calibration_progress(self.tmp)
        self.assertEqual((days, records), (2, 3))

    def test_many_records_one_day_counts_one_day(self):
        # The bug this helper fixes: many iterations on a single calendar day
        # must count as ONE day, not inflate the day count.
        self._write("2026-06-01.jsonl",
                    [{"ts": f"2026-06-01T{h:02d}:00:00"} for h in range(20)])
        days, records = hl.full_calibration_progress(self.tmp)
        self.assertEqual((days, records), (1, 20))

    def test_corrupt_and_blank_lines_counted_as_records_no_day(self):
        # A record line counts toward num_records even if unparseable; only a
        # valid ts contributes a day. Blank lines are ignored entirely.
        (self.tmp / "2026-06-01.jsonl").write_text(
            '{"ts":"2026-06-01T10:00:00"}\nNOT JSON\n\n{"foo":1}\n', encoding="utf-8")
        days, records = hl.full_calibration_progress(self.tmp)
        self.assertEqual((days, records), (1, 3))   # 3 non-blank lines, 1 valid date

    def test_non_jsonl_files_ignored(self):
        # The .calibrated / .last-check markers (no .jsonl suffix) must not be read.
        self._write("2026-06-01.jsonl", [{"ts": "2026-06-01T10:00:00"}])
        (self.tmp / ".calibrated").write_text("2026-06-01T10:00:00 days=30\n", encoding="utf-8")
        (self.tmp / ".last-check").write_text("42\n", encoding="utf-8")
        days, records = hl.full_calibration_progress(self.tmp)
        self.assertEqual((days, records), (1, 1))


if __name__ == "__main__":
    unittest.main()
