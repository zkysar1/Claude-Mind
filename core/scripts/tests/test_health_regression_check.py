"""test_health_regression_check.py — unit tests for the Phase 2 detection sweep.
Spec: core/config/conventions/health-ledger.md §8, §9, §10.

HERMETIC: pure gate/degraded-signal/window functions + tmp-dir evolution-log.
The full main() path (config + attribution + JSON) is exercised by the CLI smoke
in the build, not here (it needs _paths + git). Safe with a live daemon present.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "health_regression_check", CORE_SCRIPTS / "health-regression-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MOD = _load()
_DET = {"slope_threshold": -0.02, "floor": 0.40, "consecutive_below_baseline": 3, "interval": 10}


class TestEvaluateGate(unittest.TestCase):
    def _rec(self, trend, comp, below):
        return {"composite_trend": trend, "composite": comp, "below_baseline": below}

    def test_all_three_trip(self):
        ok, _ = _MOD.evaluate_gate(self._rec(-0.03, 0.35, True), _DET)
        self.assertTrue(ok)

    def test_slope_not_steep_enough(self):
        ok, reasons = _MOD.evaluate_gate(self._rec(-0.01, 0.35, True), _DET)
        self.assertFalse(ok)
        self.assertTrue(any("slope" in r for r in reasons))

    def test_above_floor(self):
        ok, reasons = _MOD.evaluate_gate(self._rec(-0.05, 0.55, True), _DET)
        self.assertFalse(ok)
        self.assertTrue(any("floor" in r for r in reasons))

    def test_not_below_baseline(self):
        ok, reasons = _MOD.evaluate_gate(self._rec(-0.05, 0.35, False), _DET)
        self.assertFalse(ok)
        self.assertTrue(any("baseline" in r for r in reasons))

    def test_none_record(self):
        ok, _ = _MOD.evaluate_gate(None, _DET)
        self.assertFalse(ok)


class TestDegradedSignal(unittest.TestCase):
    def _recs(self, series):
        # series: list of dicts {signal: value}; returned newest-first
        out = [{"warmup": False, "signals": s} for s in series]
        return list(reversed(out))  # caller passes oldest->newest; we store newest-first

    def test_picks_steepest_decliner(self):
        # deep_ratio declines sharply, others flat
        series = [
            {"composite_productivity": 0.8, "encoding_ratio": 0.8, "deep_ratio": 0.9, "tree_writes": 4},
            {"composite_productivity": 0.8, "encoding_ratio": 0.8, "deep_ratio": 0.6, "tree_writes": 4},
            {"composite_productivity": 0.8, "encoding_ratio": 0.8, "deep_ratio": 0.3, "tree_writes": 4},
        ]
        recs = self._recs(series)
        self.assertEqual(_MOD.degraded_signal(recs, cap=8), "deep_ratio")

    def test_fallback_insufficient_records(self):
        recs = [{"warmup": False, "signals": {"deep_ratio": 0.5}}]
        self.assertEqual(_MOD.degraded_signal(recs, cap=8), "composite_productivity")

    def test_fallback_when_no_decline(self):
        # all flat → no negative slope → fallback
        series = [{"composite_productivity": 0.8, "deep_ratio": 0.8}] * 3
        recs = self._recs(series)
        self.assertEqual(_MOD.degraded_signal(recs, cap=8), "composite_productivity")

    def test_warmup_excluded(self):
        # warmup records must not enter the slope computation
        recs = [
            {"warmup": False, "signals": {"deep_ratio": 0.3}},
            {"warmup": False, "signals": {"deep_ratio": 0.6}},
            {"warmup": True, "signals": {"deep_ratio": 0.1}},  # would skew if counted
        ]
        # newest-first: deep_ratio 0.3 (new), 0.6 (older) → declining → deep_ratio
        self.assertEqual(_MOD.degraded_signal(recs, cap=8), "deep_ratio")


class TestWindowBounds(unittest.TestCase):
    def test_bounds_over_window(self):
        recs = [
            {"warmup": False, "ts": "2026-06-03T12:00:00"},
            {"warmup": False, "ts": "2026-06-03T11:00:00"},
            {"warmup": False, "ts": "2026-06-03T10:00:00"},
        ]
        after, before = _MOD._window_bounds(recs)
        from datetime import datetime
        self.assertEqual(before, datetime(2026, 6, 3, 12).timestamp())
        self.assertEqual(after, datetime(2026, 6, 3, 10).timestamp())

    def test_insufficient_records(self):
        self.assertEqual(_MOD._window_bounds([{"warmup": False, "ts": "2026-06-03T12:00:00"}]),
                         (None, None))


class TestEvolutionChangeInWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ts(self, iso):
        from datetime import datetime
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S").timestamp()

    def test_change_in_window_true(self):
        (self.tmp / "evolution-log.jsonl").write_text(
            '{"timestamp": "2026-06-03T11:00:00", "change": "x"}\n', encoding="utf-8")
        self.assertTrue(_MOD._evolution_change_in_window(
            self.tmp, self._ts("2026-06-03T10:00:00"), self._ts("2026-06-03T12:00:00")))

    def test_change_outside_window_false(self):
        (self.tmp / "evolution-log.jsonl").write_text(
            '{"timestamp": "2026-06-03T09:00:00", "change": "x"}\n', encoding="utf-8")
        self.assertFalse(_MOD._evolution_change_in_window(
            self.tmp, self._ts("2026-06-03T10:00:00"), self._ts("2026-06-03T12:00:00")))

    def test_absent_log_false(self):
        self.assertFalse(_MOD._evolution_change_in_window(
            self.tmp, self._ts("2026-06-03T10:00:00"), self._ts("2026-06-03T12:00:00")))


class TestRunEndToEnd(unittest.TestCase):
    """Exercise run() — the testable core of main() — through the full trip and
    no-trip paths with a tmp config + ledger + injected attribute_fn."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config_dir = self.tmp / "config"
        self.meta_dir = self.tmp / "meta"
        self.agent_dir = self.tmp / "agent"
        self.health_dir = self.agent_dir / "health"
        for d in (self.config_dir, self.meta_dir, self.health_dir):
            d.mkdir(parents=True)
        self.now = 1_000_000_000

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _config(self, mode):
        (self.config_dir / "aspirations.yaml").write_text(
            "health_regression:\n"
            f"  mode: {mode}\n"
            "  tree_writes_cap: 8\n"
            "  detection:\n"
            "    interval: 10\n"
            "    slope_threshold: -0.02\n"
            "    floor: 0.40\n"
            "    consecutive_below_baseline: 3\n"
            "  calibration:\n"
            "    min_days: 30\n"
            "    min_records: 50\n"
            "  revert:\n"
            "    confidence_gate: 0.70\n", encoding="utf-8")

    def _ledger(self, recs_newest_first, name="2026-06-03.jsonl"):
        # The real ledger is appended OLDEST-first (one record per iteration);
        # hl.recent_records reverses each day-file to yield newest-first. Author
        # fixtures newest-first (intuitive) but write the file oldest-first so the
        # round-trip through recent_records matches production ordering.
        import json as _j
        recs = list(reversed(recs_newest_first))
        (self.health_dir / name).write_text(
            "".join(_j.dumps(r) + "\n" for r in recs), encoding="utf-8")

    def _tripping_records(self):
        # newest-first: 3 consecutive below-baseline, declining deep_ratio,
        # composite below floor, negative trend.
        base = {"warmup": False, "baseline": 0.71, "below_baseline": True}
        return [
            {**base, "ts": "2026-06-03T12:00:00", "iteration": 30, "composite": 0.30,
             "composite_trend": -0.05, "signals": {"deep_ratio": 0.20, "composite_productivity": 0.4}},
            {**base, "ts": "2026-06-03T11:00:00", "iteration": 29, "composite": 0.34,
             "composite_trend": -0.04, "signals": {"deep_ratio": 0.40, "composite_productivity": 0.5}},
            {**base, "ts": "2026-06-03T10:00:00", "iteration": 28, "composite": 0.38,
             "composite_trend": -0.03, "signals": {"deep_ratio": 0.60, "composite_productivity": 0.6}},
        ]

    def _run(self, force=True, attribute_fn=None):
        return _MOD.run(config_dir=self.config_dir, meta_dir=self.meta_dir,
                        agent_dir=self.agent_dir, repo_root=self.tmp,
                        now_ts=self.now, force=force, attribute_fn=attribute_fn)

    def test_collect_only_disabled(self):
        self._config("collect-only")
        self._ledger(self._tripping_records())
        v = self._run()
        self.assertFalse(v["tripped"])
        self.assertIn("collect-only", v["reason"])

    def test_trip_path_full_verdict(self):
        self._config("detect-and-report")
        self._ledger(self._tripping_records())
        seen = {}

        def fake_attr(after_ts, before_ts, signal, **kw):
            seen["signal"] = signal
            seen["window"] = (after_ts, before_ts)
            return [{"path": "meta/goal-selection-strategy.yaml", "ring": "3",
                     "authority": "auto", "score": 0.81, "commit": "abc", "ts": 1}]

        v = self._run(attribute_fn=fake_attr)
        self.assertTrue(v["tripped"])
        self.assertEqual(v["signal"], "deep_ratio")          # steepest decliner
        self.assertEqual(seen["signal"], "deep_ratio")        # passed to attribution
        # iteration MUST be in the verdict — health-revert.append_pending reads it
        # for revert_iteration; without it the verify window collapses.
        self.assertEqual(v["iteration"], 30)                  # latest non-warmup iteration
        self.assertEqual(v["consecutive"], 3)
        self.assertEqual(len(v["candidates"]), 1)
        self.assertFalse(v["calibrated"])                     # 1 day, 3 records
        self.assertFalse(v["revert_eligible"])                # not full + not calibrated
        self.assertTrue(v["dedup_key"].startswith("health-regression:deep_ratio:"))

    def test_no_trip_when_consecutive_short(self):
        self._config("detect-and-report")
        recs = self._tripping_records()
        recs[2]["below_baseline"] = False  # break the streak at the 3rd → only 2
        self._ledger(recs)
        v = self._run()
        self.assertFalse(v["tripped"])
        self.assertIn("consecutive", v["reason"])

    def test_full_mode_calibrated_revert_eligible(self):
        self._config("full")
        self._ledger(self._calibrated_records())
        v = self._run(attribute_fn=lambda *a, **k: [])
        self.assertTrue(v["tripped"])
        self.assertTrue(v["calibrated"])
        self.assertTrue(v["revert_eligible"])   # mode=full AND calibrated

    # --- calibration-complete trigger (spec §10) -----------------------------
    def _calibrated_records(self):
        # 60 records across 30 distinct dates, all tripping → satisfies the
        # 30-day/50-record AND-gate. Newest-first.
        recs = []
        for d in range(1, 31):                # 30 distinct days
            for it in range(2):               # 60 records total
                recs.append({"warmup": False, "ts": f"2026-04-{d:02d}T{it:02d}:00:00",
                             "iteration": d * 10 + it, "composite": 0.30,
                             "composite_trend": -0.05, "baseline": 0.71,
                             "below_baseline": True,
                             "signals": {"deep_ratio": 0.2}})
        recs.sort(key=lambda r: r["ts"], reverse=True)  # newest-first
        return recs

    def test_calibration_evaluated_in_collect_only(self):
        # Calibration must be reported even in collect-only (the rollout-launch
        # default) — it is the signal the agent uses to advance the mode.
        self._config("collect-only")
        self._ledger(self._calibrated_records())
        v = self._run()
        self.assertFalse(v["tripped"])                      # collect-only: detection off
        self.assertIn("collect-only", v["reason"])
        self.assertTrue(v["calibrated"])                    # but calibration IS evaluated
        self.assertTrue(v["calibration_just_completed"])    # first crossing fires
        self.assertEqual(v["calibration_dedup_key"], "health-calibration-complete")

    def test_calibration_just_completed_fires_once(self):
        self._config("collect-only")
        self._ledger(self._calibrated_records())
        first = self._run()
        self.assertTrue(first["calibration_just_completed"])
        # Marker now written — the edge must NOT re-fire on the next sweep.
        second = self._run()
        self.assertFalse(second["calibration_just_completed"])
        self.assertTrue(second["calibrated"])               # still calibrated
        self.assertEqual(second["calibration"], {"complete": True})  # marker short-circuit
        self.assertTrue((self.health_dir / ".calibrated").exists())

    def test_calibration_not_complete_short_history(self):
        # 3 records, 1 distinct date → gate not satisfied → no edge.
        self._config("collect-only")
        self._ledger(self._tripping_records())
        v = self._run()
        self.assertFalse(v["calibrated"])
        self.assertFalse(v["calibration_just_completed"])
        self.assertEqual(v["calibration"], {"days": 1, "records": 3})

    def test_calibration_days_counted_across_full_ledger(self):
        # Regression guard for the sliding-window bug: an agent recording MANY
        # iterations per day must still calibrate on distinct-day count. 8
        # records/day × 30 days = 240 records (well past a 120-record window that
        # would span < 30 days). full_calibration_progress reads the whole ledger.
        self._config("detect-and-report")
        recs = []
        for d in range(1, 31):
            for it in range(8):
                recs.append({"warmup": False, "ts": f"2026-04-{d:02d}T{it:02d}:00:00",
                             "iteration": d * 100 + it, "composite": 0.9,
                             "composite_trend": 0.01, "baseline": 0.71,
                             "below_baseline": False, "signals": {"deep_ratio": 0.9}})
        recs.sort(key=lambda r: r["ts"], reverse=True)
        self._ledger(recs)
        v = self._run()
        self.assertTrue(v["calibrated"])                    # 30 days despite 240 records
        self.assertTrue(v["calibration_just_completed"])


if __name__ == "__main__":
    unittest.main()
