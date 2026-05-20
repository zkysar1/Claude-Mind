"""test_tree_maintenance_aggregate_sentinels.py —  regression test.

Pins the _aggregate fix in tree-maintenance-read.py that handles non-numeric
string sentinels in `skipped_by_reason` without crashing.

Bug (g-115-872 zeta close, 2026-05-17): `tree-maintenance-read.sh --since Nd
--aggregate` crashed with `ValueError: invalid literal for int() with base
10: 'all'` because `llm_reported.<phase>.skipped_by_reason` can carry string
sentinels like "all" (e.g. `not_inspected_this_run: all`, `none_eligible:
all`). The prior `int(n or 0)` form raised before the value could be summed.

Fix (g-115-882 this Apply): extract _tally_skip_reason() helper that type-
guards `isinstance(n, (int, float))` and routes non-numeric sentinels to a
separate `_sentinels` sub-key, preserving visibility instead of crashing.

Contract:
  (a) numeric-only aggregate baseline — existing behavior preserved
  (b) mixed numeric + 'all' sentinel — no crash, totals + sentinels split
  (c) sentinels co-occurring across multiple records — counted correctly
  (d) pre_filter path also defended (defensive, parallel symmetry)

Cross-references:
  - g-115-872 (zeta investigation) — identified the crash
  - g-115-882 (this Apply) — alpha patched _aggregate via _tally_skip_reason
  - core/scripts/tree-maintenance-read.py:_tally_skip_reason + _aggregate
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

MODULE_PATH = CORE_SCRIPTS / "tree-maintenance-read.py"
spec = importlib.util.spec_from_file_location("tree_maintenance_read", MODULE_PATH)
tmr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tmr)


def _record(run_id, llm_phase_skipped_by_reason=None, pre_phase_skipped_by_reason=None):
    """Build a minimal record with llm_reported.distill.skipped_by_reason
    set to the given dict (numeric or mixed).
    """
    return {
        "run_id": run_id,
        "agent": "alpha",
        "mode": "test",
        "started_at": "2026-05-17T13:00:00",
        "ended_at": "2026-05-17T13:01:00",
        "candidates_pre_filter": {
            "distill": {
                "candidates_in": 0,
                "skipped_by_reason": pre_phase_skipped_by_reason or {},
            }
        },
        "llm_reported": {
            "distill": {
                "actioned": 0,
                "skipped_by_reason": llm_phase_skipped_by_reason or {},
            }
        },
        "post_run_debt": {"total": 0, "cleared": 0},
    }


class TestAggregateSentinels(unittest.TestCase):
    """Four-case contract pinning the _aggregate sentinel-handling fix."""

    def test_a_numeric_only_baseline(self):
        """Numeric-only skip reasons must still aggregate via integer sum.
        Regression: pre-fix behavior must be preserved when no sentinels appear.
        """
        records = [
            _record("r-1", llm_phase_skipped_by_reason={
                "insufficient_retrievals": 2,
                "child_count_at_limit": 1,
            }),
            _record("r-2", llm_phase_skipped_by_reason={
                "insufficient_retrievals": 3,
            }),
        ]
        agg = tmr._aggregate(records)
        distill = agg["llm_reported"]["distill"]
        self.assertEqual(distill["skipped_by_reason_total"]["insufficient_retrievals"], 5)
        self.assertEqual(distill["skipped_by_reason_total"]["child_count_at_limit"], 1)
        # No sentinels expected — sub-key should be absent.
        self.assertNotIn("_sentinels", distill)

    def test_b_mixed_numeric_and_all_sentinel(self):
        """Mixed numeric + 'all' sentinel must NOT crash. Numeric reasons
        sum into skipped_by_reason_total; sentinels land in _sentinels.
        """
        records = [
            _record("r-1", llm_phase_skipped_by_reason={
                "insufficient_retrievals": 4,
                "not_inspected_this_run": "all",  # the canonical bug
            }),
        ]
        # Pre-fix this raised ValueError.
        agg = tmr._aggregate(records)
        distill = agg["llm_reported"]["distill"]
        self.assertEqual(distill["skipped_by_reason_total"]["insufficient_retrievals"], 4)
        # The 'all' sentinel must NOT pollute the numeric totals.
        self.assertNotIn("not_inspected_this_run", distill["skipped_by_reason_total"])
        # It must land in _sentinels with the sentinel value preserved.
        self.assertIn("_sentinels", distill)
        self.assertEqual(distill["_sentinels"]["not_inspected_this_run"]["all"], 1)

    def test_c_sentinel_repeats_across_records(self):
        """Sentinel co-occurrences across records must increment the
        _sentinels[reason][value] count, not collapse to one.
        """
        records = [
            _record("r-1", llm_phase_skipped_by_reason={"none_eligible": "all"}),
            _record("r-2", llm_phase_skipped_by_reason={"none_eligible": "all"}),
            _record("r-3", llm_phase_skipped_by_reason={"none_eligible": "all"}),
        ]
        agg = tmr._aggregate(records)
        distill = agg["llm_reported"]["distill"]
        self.assertEqual(distill["_sentinels"]["none_eligible"]["all"], 3)
        # And numeric totals stay clean.
        self.assertEqual(distill["skipped_by_reason_total"], {})

    def test_d_pre_filter_defends_against_sentinel_too(self):
        """The pre_filter path uses the same _tally_skip_reason helper, so
        a hypothetical sentinel in candidates_pre_filter.<phase>.
        skipped_by_reason is handled identically. This pins the symmetric
        defensive shape — future pre-filter protocol drift that introduces
        a sentinel will not reintroduce the crash.
        """
        records = [
            _record(
                "r-1",
                pre_phase_skipped_by_reason={
                    "precision_below_floor": 7,
                    "unknown_sentinel": "all",
                },
            ),
        ]
        agg = tmr._aggregate(records)
        pre = agg["candidates_pre_filter"]["distill"]
        self.assertEqual(pre["skipped_by_reason_total"]["precision_below_floor"], 7)
        self.assertNotIn("unknown_sentinel", pre["skipped_by_reason_total"])
        self.assertIn("_sentinels", pre)
        self.assertEqual(pre["_sentinels"]["unknown_sentinel"]["all"], 1)


class TestTallyHelper(unittest.TestCase):
    """Direct unit tests of _tally_skip_reason — the helper's public contract."""

    def _fresh_agg_phase(self):
        return {"skipped_by_reason_total": {}}

    def test_int_value_increments_total(self):
        ap = self._fresh_agg_phase()
        tmr._tally_skip_reason(ap, "r1", 5)
        tmr._tally_skip_reason(ap, "r1", 3)
        self.assertEqual(ap["skipped_by_reason_total"]["r1"], 8)
        self.assertNotIn("_sentinels", ap)

    def test_string_routes_to_sentinels(self):
        ap = self._fresh_agg_phase()
        tmr._tally_skip_reason(ap, "r1", "all")
        self.assertEqual(ap["_sentinels"]["r1"]["all"], 1)
        # Numeric total absent for r1.
        self.assertNotIn("r1", ap["skipped_by_reason_total"])

    def test_float_value_truncated_to_int_in_total(self):
        ap = self._fresh_agg_phase()
        tmr._tally_skip_reason(ap, "r1", 2.7)
        # int(2.7) == 2 — same as the original int(n) form would have done.
        self.assertEqual(ap["skipped_by_reason_total"]["r1"], 2)

    def test_bool_excluded_from_numeric_path(self):
        """bool is technically int subclass; we explicitly exclude it so
        True/False land in _sentinels (more meaningful) instead of summing
        as 1/0 against a numeric reason.
        """
        ap = self._fresh_agg_phase()
        tmr._tally_skip_reason(ap, "r1", True)
        self.assertNotIn("r1", ap["skipped_by_reason_total"])
        self.assertEqual(ap["_sentinels"]["r1"]["True"], 1)


if __name__ == "__main__":
    unittest.main()
