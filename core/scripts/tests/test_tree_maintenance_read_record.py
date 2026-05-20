"""test_tree_maintenance_read_record.py -  regression test.

Verifies the _fmt_human_record patch in tree-maintenance-read.py shows
BOTH pre-filter and llm-reported skip reasons with `pre:` / `llm:`
prefixes (mirroring _fmt_human_aggregate's dual-source display).

Per g-115-872 (zeta close, 2026-05-17): the single-record human view of
tree-maintenance-log.jsonl previously dropped `candidates_pre_filter.
<phase>.skipped_by_reason` entirely, only rendering llm-reported
skip reasons. That hid the upstream filter rejections (precision_below_floor,
no_capacity_for_split, etc.) that explain WHY a phase had no candidates
to action on.

Contract: cover (a) record with pre-only reasons (decompose blocked at
pre-filter), (b) record with both pre + llm reasons (distill with pre-filter
rejections AND llm-reported skips), (c) idempotency / no-skip baseline.

Cross-references:
  - g-115-872 (zeta investigation) - identified the gap
  - g-115-884 (this Apply) - alpha patched _fmt_human_record
  - core/scripts/tree-maintenance-read.py:166 (_fmt_human_record)
  - core/scripts/tree-maintenance-read.py:199 (_fmt_human_aggregate — mirror pattern)
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


def _minimal_record(**overrides):
    """Build a minimal-but-valid maintenance record."""
    rec = {
        "run_id": "test-run-001",
        "agent": "alpha",
        "mode": "test",
        "started_at": "2026-05-17T13:00:00",
        "ended_at": "2026-05-17T13:01:00",
        "candidates_pre_filter": {},
        "llm_reported": {},
        "post_run_debt": {"total": 0, "cleared": 0, "threshold": 20},
    }
    rec.update(overrides)
    return rec


class TestFmtHumanRecordDualSource(unittest.TestCase):
    """Four-case contract for the _fmt_human_record dual-source patch."""

    def test_pre_only_reasons_decompose(self):
        """A record with pre-filter rejections but no llm-reported skips
        must show the pre: prefixed reasons (previously dropped)."""
        rec = _minimal_record(
            candidates_pre_filter={
                "decompose": {
                    "candidates_in": 5,
                    "skipped_by_reason": {
                        "precision_below_floor": 3,
                        "no_capacity_for_split": 2,
                    },
                },
            },
            llm_reported={"decompose": {"actioned": 0, "skipped_by_reason": {}}},
        )
        out = tmr._fmt_human_record(rec)
        self.assertIn("pre:precision_below_floor=3", out)
        self.assertIn("pre:no_capacity_for_split=2", out)
        # No llm reasons should appear since llm.skipped_by_reason is empty.
        self.assertNotIn("llm:precision_below_floor", out)

    def test_both_pre_and_llm_reasons_distill(self):
        """A record with BOTH pre-filter and llm-reported skip reasons
        must show both, distinguished by `pre:` / `llm:` prefixes."""
        rec = _minimal_record(
            candidates_pre_filter={
                "distill": {
                    "candidates_in": 8,
                    "skipped_by_reason": {"precision_below_floor": 4},
                },
            },
            llm_reported={
                "distill": {
                    "actioned": 2,
                    "skipped_by_reason": {
                        "ambiguous_grouping": 1,
                        "domain_mismatch": 1,
                    },
                },
            },
        )
        out = tmr._fmt_human_record(rec)
        # Pre-side reason present
        self.assertIn("pre:precision_below_floor=4", out)
        # LLM-side reasons present with explicit prefix
        self.assertIn("llm:ambiguous_grouping=1", out)
        self.assertIn("llm:domain_mismatch=1", out)
        # Pre-reason ordering: pre: entries come before llm: entries on the
        # distill row (mirrors _fmt_human_aggregate where pre is iterated
        # first; readers rely on this ordering to scan upstream rejections
        # before downstream skips).
        distill_lines = [ln for ln in out.split("\n") if ln.lstrip().startswith("distill")]
        self.assertEqual(len(distill_lines), 1)
        pre_idx = distill_lines[0].find("pre:precision_below_floor")
        llm_idx = distill_lines[0].find("llm:ambiguous_grouping")
        self.assertGreater(pre_idx, 0)
        self.assertGreater(llm_idx, pre_idx)

    def test_llm_only_reasons_unprefiltered_phase(self):
        """For LLM-only phases (split / sprout / merge / prune / retire —
        outside _PRE_FILTER_PHASES), pre_reasons is structurally empty.
        Only llm:-prefixed reasons should appear; the row should still
        render cleanly (no crashes on missing pre block)."""
        rec = _minimal_record(
            llm_reported={
                "split": {
                    "actioned": 1,
                    "skipped_by_reason": {"capacity_full": 2},
                },
            },
        )
        out = tmr._fmt_human_record(rec)
        self.assertIn("llm:capacity_full=2", out)
        self.assertNotIn("pre:capacity_full", out)
        # split row appears with actioned=1
        self.assertRegex(out, r"split\s+—\s+1\s+llm:capacity_full=2")

    def test_no_skip_reasons_renders_em_dash(self):
        """A record with zero skip reasons on either side must render the
        em-dash placeholder, not an empty string or a bare comma."""
        rec = _minimal_record(
            candidates_pre_filter={
                "decompose": {"candidates_in": 3, "skipped_by_reason": {}},
            },
            llm_reported={"decompose": {"actioned": 3, "skipped_by_reason": {}}},
        )
        out = tmr._fmt_human_record(rec)
        # The decompose row should end with the em-dash placeholder.
        # Header text changed in  from "skipped_by_reason" to
        # "skip_reasons (pre|llm)"; verify the new header is present.
        self.assertIn("skip_reasons (pre|llm)", out)
        # Confirm the em-dash appears in the decompose row body.
        decompose_lines = [ln for ln in out.split("\n") if ln.lstrip().startswith("decompose")]
        self.assertEqual(len(decompose_lines), 1)
        self.assertTrue(decompose_lines[0].endswith("—"))


class TestFmtHumanRecordLegacySchema(unittest.TestCase):
    """Regression check that 's int-block guard still holds."""

    def test_legacy_int_block_in_llm_reported(self):
        """Older records sometimes store actioned-count directly under the
        phase key as an int. Type-guard must convert without crashing."""
        rec = _minimal_record(
            # Older schema: llm["decompose"] = 5 (int, not dict)
            llm_reported={"decompose": 5},
        )
        out = tmr._fmt_human_record(rec)
        # Row renders without raising; actioned shows 5, skipped is em-dash.
        decompose_lines = [ln for ln in out.split("\n") if ln.lstrip().startswith("decompose")]
        self.assertEqual(len(decompose_lines), 1)
        self.assertIn("5", decompose_lines[0])
        self.assertTrue(decompose_lines[0].endswith("—"))


if __name__ == "__main__":
    unittest.main()
