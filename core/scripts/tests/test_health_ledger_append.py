"""test_health_ledger_append.py — unit tests for the health-ledger subsystem
pure functions (Phase 1 / collect-only).

Spec: core/config/conventions/health-ledger.md. Design: workflow
wf_b82a26e9-958 (2026-06-03).

These tests are HERMETIC: they exercise only the three pure functions
(`_lsq_slope`, `normalize_signals`, `compute_composite`) — no file I/O, no
path resolution, no productivity-snapshots read, no daemon. Safe to run with a
live daemon present (guard-672). The fire-and-forget main() append path is
covered by the runtime smoke test (agents/<agent>/health/<date>.jsonl), not
here.

Anchoring case: the zeta smoke-test composite, computed by hand:
    0.40·0.9179 + 0.25·1.0 + 0.20·0.8974 + 0.15·1.0 = 0.94664  → round(.,4) = 0.9466
verifies the weight-renormalization arithmetic against a known-good record.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent


def _load_module():
    """Load health-ledger-append.py as a module despite the hyphenated name."""
    spec_path = CORE_SCRIPTS / "health-ledger-append.py"
    spec = importlib.util.spec_from_file_location("health_ledger_append", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Weights mirror core/config/aspirations.yaml health_regression.weights and the
# script's _DEFAULTS. Kept local so the test pins the arithmetic regardless of
# config drift — a config change SHOULD break this and force a deliberate update.
_WEIGHTS = {
    "composite_productivity": 0.40,
    "encoding_ratio": 0.25,
    "deep_ratio": 0.20,
    "tree_writes": 0.15,
}
_CAP = 8


class TestLsqSlope(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_fewer_than_two_points_is_zero(self):
        self.assertEqual(self.mod._lsq_slope([]), 0.0)
        self.assertEqual(self.mod._lsq_slope([0.5]), 0.0)

    def test_flat_series_zero_slope(self):
        self.assertAlmostEqual(self.mod._lsq_slope([0.7, 0.7, 0.7, 0.7]), 0.0)

    def test_perfect_upward_slope(self):
        # y = 0.1 * x  → slope 0.1
        self.assertAlmostEqual(self.mod._lsq_slope([0.0, 0.1, 0.2, 0.3]), 0.1)

    def test_perfect_downward_slope(self):
        # declining health: slope is negative (the detection signal)
        self.assertAlmostEqual(self.mod._lsq_slope([0.9, 0.8, 0.7, 0.6]), -0.1)

    def test_two_points(self):
        self.assertAlmostEqual(self.mod._lsq_slope([0.5, 0.7]), 0.2)

    def test_noisy_decline_negative(self):
        # not monotone but trending down — slope must still be < 0
        slope = self.mod._lsq_slope([0.80, 0.85, 0.70, 0.65, 0.60])
        self.assertLess(slope, 0.0)


class TestNormalizeSignals(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_rate_signals_passthrough(self):
        norm = self.mod.normalize_signals(
            {"composite_productivity": 0.9179, "encoding_ratio": 1.0,
             "deep_ratio": 0.8974}, _CAP)
        self.assertAlmostEqual(norm["composite_productivity"], 0.9179)
        self.assertAlmostEqual(norm["encoding_ratio"], 1.0)
        self.assertAlmostEqual(norm["deep_ratio"], 0.8974)

    def test_tree_writes_cap_normalization(self):
        # 4 writes against cap 8 → 0.5
        norm = self.mod.normalize_signals({"tree_writes": 4}, _CAP)
        self.assertAlmostEqual(norm["tree_writes"], 0.5)

    def test_tree_writes_clamped_at_cap(self):
        # 20 writes against cap 8 → clamped to 1.0
        norm = self.mod.normalize_signals({"tree_writes": 20}, _CAP)
        self.assertAlmostEqual(norm["tree_writes"], 1.0)

    def test_tree_writes_zero_cap_is_one(self):
        # defensive: cap 0 must not divide-by-zero; yields 1.0
        norm = self.mod.normalize_signals({"tree_writes": 5}, 0)
        self.assertAlmostEqual(norm["tree_writes"], 1.0)

    def test_rate_signal_clamped_to_unit_interval(self):
        norm = self.mod.normalize_signals(
            {"encoding_ratio": 1.5, "deep_ratio": -0.3}, _CAP)
        self.assertAlmostEqual(norm["encoding_ratio"], 1.0)
        self.assertAlmostEqual(norm["deep_ratio"], 0.0)

    def test_none_dropped(self):
        norm = self.mod.normalize_signals(
            {"composite_productivity": 0.8, "encoding_ratio": None}, _CAP)
        self.assertIn("composite_productivity", norm)
        self.assertNotIn("encoding_ratio", norm)

    def test_non_numeric_dropped(self):
        norm = self.mod.normalize_signals(
            {"composite_productivity": "n/a", "deep_ratio": 0.5}, _CAP)
        self.assertNotIn("composite_productivity", norm)
        self.assertIn("deep_ratio", norm)

    def test_numeric_string_accepted(self):
        # float()-coercible strings are accepted (defensive against JSON quirks)
        norm = self.mod.normalize_signals({"deep_ratio": "0.5"}, _CAP)
        self.assertAlmostEqual(norm["deep_ratio"], 0.5)


class TestComputeComposite(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_zeta_smoke_test_record(self):
        # The canonical anchoring case (see module docstring).
        norm = {"composite_productivity": 0.9179, "encoding_ratio": 1.0,
                "deep_ratio": 0.8974, "tree_writes": 1.0}
        composite = self.mod.compute_composite(norm, _WEIGHTS, min_signals=2)
        self.assertEqual(composite, 0.9466)

    def test_all_full_signals_is_one(self):
        norm = {k: 1.0 for k in _WEIGHTS}
        self.assertEqual(self.mod.compute_composite(norm, _WEIGHTS, 2), 1.0)

    def test_weight_renormalization_on_dropout(self):
        # Only two signals present. The composite must renormalize over the
        # available weights (0.40 + 0.20 = 0.60), NOT divide by the full 1.0.
        norm = {"composite_productivity": 0.8, "deep_ratio": 0.5}
        expected = round((0.40 * 0.8 + 0.20 * 0.5) / (0.40 + 0.20), 4)
        self.assertEqual(self.mod.compute_composite(norm, _WEIGHTS, 2), expected)
        # Sanity: renormalized value (0.7) must differ from the non-normalized
        # weighted sum (0.42), proving renormalization actually fires.
        self.assertEqual(expected, 0.7)
        self.assertNotEqual(expected, round(0.40 * 0.8 + 0.20 * 0.5, 4))

    def test_below_min_signals_returns_none(self):
        # Only one signal in the weight set → None (excluded from trend/detection)
        norm = {"composite_productivity": 0.9}
        self.assertIsNone(self.mod.compute_composite(norm, _WEIGHTS, 2))

    def test_signals_outside_weights_ignored(self):
        # An unknown signal does not count toward min_signals nor the composite.
        norm = {"composite_productivity": 0.8, "unknown_signal": 1.0}
        self.assertIsNone(self.mod.compute_composite(norm, _WEIGHTS, 2))

    def test_empty_returns_none(self):
        self.assertIsNone(self.mod.compute_composite({}, _WEIGHTS, 2))

    def test_zero_weight_sum_returns_none(self):
        # Defensive: if every available signal maps to a zero weight, the
        # renormalization denominator is 0 → None rather than ZeroDivisionError.
        norm = {"a": 0.5, "b": 0.5}
        zero_weights = {"a": 0.0, "b": 0.0}
        self.assertIsNone(self.mod.compute_composite(norm, zero_weights, 2))

    def test_min_signals_one_accepts_single(self):
        norm = {"composite_productivity": 0.6}
        # With min_signals=1 a single signal yields its own renormalized value.
        self.assertEqual(self.mod.compute_composite(norm, _WEIGHTS, 1), 0.6)


if __name__ == "__main__":
    unittest.main()
