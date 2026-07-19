"""test_tree_distill_oversized_append_grown.py - 0 regression test.

Covers crit3 in tree.py get_distill_candidates: the PROACTIVE distill trigger
for append-grown recurring-sweep nodes that fires BEFORE a node trips the Read
~25k-token cap (the rb-2085 reactive-distill shape, made proactive so the node
does not re-trip read-before-edit on every sweep).

Two layers:
  - _analyze_node_body (pure helper): ~4-chars/token estimate + dated
    "Refresh"/"Verified Values" heading count (the append-grown signature).
  - get_distill_candidates crit3: oversized AND append-grown -> candidate with
    trigger="oversized_append_grown", INDEPENDENT of utility_ratio/feedback
    (so it catches the HIGH-utility sweep nodes crit1/crit2 structurally miss),
    sorted ahead of low-utility / large-mediocre candidates.

Cross-references:
  - g-115-1570 (this Idea) - proactive distill trigger
  - rb-2085 - the distill EXECUTION procedure crit3 routes work into
  - core/scripts/tree.py get_distill_candidates (crit3) + _analyze_node_body
  - core/config/tree.yaml pruning.distill_token_cap / _token_ratio /
    _refresh_min_sections
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

MODULE_PATH = CORE_SCRIPTS / "tree.py"
spec = importlib.util.spec_from_file_location("tree_engine_g1570", MODULE_PATH)
tree_engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tree_engine)


class TestAnalyzeNodeBody(unittest.TestCase):
    """Pure-helper layer: token estimate + dated-refresh-heading detection."""

    def test_small_node_no_refresh(self):
        lc, tok, refs = tree_engine._analyze_node_body("# Title\n\nshort body line\n")
        self.assertEqual(refs, 0)
        self.assertEqual(lc, 3)
        self.assertGreater(tok, 0)

    def test_token_estimate_is_chars_over_4(self):
        lc, tok, refs = tree_engine._analyze_node_body("x" * 4000)
        self.assertEqual(tok, 1000)  # 4000 // 4

    def test_dated_refresh_headings_counted(self):
        text = (
            "---\nkey: v\n---\n"
            "## Refresh 2026-06-21\nsome sweep output\n"
            "## Refresh 2026-06-14\nmore output\n"
            "### Verified Values 2026-06-01\ntable\n"
        )
        _, _, refs = tree_engine._analyze_node_body(text)
        self.assertEqual(refs, 3)  # 2 Refresh + 1 Verified Values headings

    def test_case_insensitive_and_non_heading_excluded(self):
        text = (
            "## REFRESH log\n"                  # heading -> counts
            "we refresh the cache here\n"        # body line -> must NOT count
            "Verified Values appear below\n"     # body line (no #) -> must NOT count
            "#### verified values 2026-01-01\n"  # heading -> counts
        )
        _, _, refs = tree_engine._analyze_node_body(text)
        self.assertEqual(refs, 2)

    def test_indented_heading_still_counted(self):
        # lstrip means an indented heading still registers.
        _, _, refs = tree_engine._analyze_node_body("   ## Refresh 2026-06-21\n")
        self.assertEqual(refs, 1)

    def test_empty_body(self):
        self.assertEqual(tree_engine._analyze_node_body(""), (0, 0, 0))


class TestCrit3GetDistillCandidates(unittest.TestCase):
    """Integration against the real core/config/tree.yaml thresholds
    (token_cap 25000 * ratio 0.8 = 20000 trigger; refresh_min 3). Fixtures use
    generous size margins so modest config retuning does not flake the test."""

    def _write_tmp(self, text):
        fd, path = tempfile.mkstemp(suffix=".md", prefix="distill-g1570-")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _node(self, **over):
        n = {"file": "", "retrieval_count": 0, "utility_ratio": 0.0,
             "times_helpful": 0, "times_noise": 0, "children": []}
        n.update(over)
        return n

    def test_oversized_append_grown_fires_despite_high_utility(self):
        # ~27k tokens + 4 dated refresh headings, HIGH utility WITH feedback ->
        # crit1 (ur<0.3) and crit2 (ur<0.5) both MISS; crit3 must catch it.
        body = "## Refresh 2026-06-21\n" * 4 + ("payload line of sweep text\n" * 4000)
        path = self._write_tmp(body)
        tree = {"nodes": {"sweep-node": self._node(
            file=path, retrieval_count=50, utility_ratio=0.95,
            times_helpful=10, times_noise=0)}}
        cands = {c["key"]: c for c in tree_engine.get_distill_candidates(tree)}
        self.assertIn("sweep-node", cands)
        self.assertEqual(cands["sweep-node"]["trigger"], "oversized_append_grown")
        self.assertGreaterEqual(cands["sweep-node"]["refresh_sections"], 4)
        self.assertGreaterEqual(cands["sweep-node"]["est_tokens"], 20000)

    def test_oversized_append_grown_fires_even_when_distill_exempt(self):
        # 0: maintain_exempt:["distill"] suppresses the UTILITY triggers
        # (crit1/crit2) but NOT the STRUCTURAL crit3 -- a coherent node too big to
        # Read must still get the non-destructive rollup. Same oversized fixture as
        # the test above, now distill-exempt: it MUST still surface as a candidate.
        body = "## Refresh 2026-06-21\n" * 4 + ("payload line of sweep text\n" * 4000)
        path = self._write_tmp(body)
        tree = {"nodes": {"sweep-node": self._node(
            file=path, retrieval_count=50, utility_ratio=0.95,
            times_helpful=10, times_noise=0, maintain_exempt=["distill"])}}
        cands = {c["key"]: c for c in tree_engine.get_distill_candidates(tree)}
        self.assertIn("sweep-node", cands)  # crit3 fires despite the exemption
        self.assertEqual(cands["sweep-node"]["trigger"], "oversized_append_grown")

    def test_distill_exempt_suppresses_low_utility_crit1(self):
        # The complement: a low-utility node (crit1) WITH the distill exemption is
        # NOT a candidate -- the over-flag fix. No oversized body, so only crit1
        # could fire, and the exemption clears it.
        # 7: votes + fresh last_retrieved keep crit1 otherwise-live so
        # the exemption is provably what clears it.
        tree = {"nodes": {"niche-node": self._node(
            file="", retrieval_count=10, utility_ratio=0.1,
            times_helpful=3, times_noise=0,
            last_retrieved=date.today().isoformat(),
            maintain_exempt=["distill"])}}
        cands = {c["key"]: c for c in tree_engine.get_distill_candidates(tree)}
        self.assertNotIn("niche-node", cands)

    def test_oversized_without_refresh_not_crit3(self):
        # Huge but ZERO refresh headings + high utility / no feedback -> no crit.
        body = "## Section heading 2026-06-21\n" * 4 + ("plain payload text line\n" * 4000)
        path = self._write_tmp(body)
        tree = {"nodes": {"big-non-sweep": self._node(
            file=path, retrieval_count=50, utility_ratio=0.95)}}
        cands = {c["key"]: c for c in tree_engine.get_distill_candidates(tree)}
        self.assertNotIn("big-non-sweep", cands)

    def test_refresh_without_size_not_crit3(self):
        # 4 refresh headings but tiny body + high utility / no feedback -> no crit.
        body = "## Refresh 2026-06-21\n" * 4 + "tiny body\n"
        path = self._write_tmp(body)
        tree = {"nodes": {"small-sweep": self._node(
            file=path, retrieval_count=50, utility_ratio=0.95)}}
        cands = {c["key"]: c for c in tree_engine.get_distill_candidates(tree)}
        self.assertNotIn("small-sweep", cands)

    def test_crit3_sorts_ahead_of_low_utility(self):
        # crit3 node (oversized, HIGH utility) must outrank a crit1 node (LOW
        # utility, with feedback) so it wins the max_distill_per_invocation budget.
        body = "## Refresh 2026-06-21\n" * 4 + ("payload line of sweep text\n" * 4000)
        big = self._write_tmp(body)
        tree = {"nodes": {
            "oversized": self._node(file=big, retrieval_count=50,
                                    utility_ratio=0.95, times_helpful=10),
            # crit1, file="" (size N/A). 7: crit1 now needs
            # >= distill_min_feedback_votes (3) + a fresh last_retrieved.
            "low-util": self._node(retrieval_count=10, utility_ratio=0.1,
                                   times_helpful=3,
                                   last_retrieved=date.today().isoformat()),
        }}
        ranked = tree_engine.get_distill_candidates(tree)
        self.assertEqual(ranked[0]["key"], "oversized")
        self.assertEqual(ranked[0]["trigger"], "oversized_append_grown")
        # Both are candidates; the low-utility one is present but ranked lower.
        self.assertIn("low-util", {c["key"] for c in ranked})

    def test_interior_node_skipped(self):
        # Nodes with children are never distill candidates regardless of size.
        body = "## Refresh 2026-06-21\n" * 4 + ("payload line of sweep text\n" * 4000)
        path = self._write_tmp(body)
        tree = {"nodes": {"parent": self._node(
            file=path, retrieval_count=50, utility_ratio=0.95,
            times_helpful=10, children=["child-a"])}}
        cands = {c["key"]: c for c in tree_engine.get_distill_candidates(tree)}
        self.assertNotIn("parent", cands)


if __name__ == "__main__":
    unittest.main()
