""": sparse-feedback + stale-signal filters on distill candidacy.

The utility triggers (crit1 low_utility, crit2 large_mediocre) previously fired
on `has_feedback = (th+tn) >= 1` — ONE noise vote made utility_ratio=0 look
meaningful, flagging 525 of 817 leaves as a STANDING false-positive pool that
polluted the tree-debt metric (692 vs threshold 40). The fix gates crit1/crit2
on `utility_signal_ok = (votes >= distill_min_feedback_votes) AND
(last_retrieved within distill_recency_days)`. crit3 (oversized_append_grown)
is STRUCTURAL and deliberately unaffected — a node too big to Read needs the
rollup regardless of how sparse its utility signal is.

Tests run against the REAL core/config/tree.yaml thresholds (min_votes=3,
recency=45d, ur<0.3, min_ret=5) with generous margins, mirroring
test_tree_distill_oversized_append_grown.py's fixture pattern.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "tree.py"
# tree.py imports sibling helpers (`from _stdio import reconfigure_stdio`,
# added post-daemon-migration), so core/scripts must be importable BEFORE the
# spec loader executes the module ( — the test predated that import).
if str(MODULE_PATH.parent) not in sys.path:
    sys.path.insert(0, str(MODULE_PATH.parent))
spec = importlib.util.spec_from_file_location("tree_engine_g2317", MODULE_PATH)
tree_engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tree_engine)

FRESH = date.today().isoformat()                          # well inside 45d
STALE = (date.today() - timedelta(days=60)).isoformat()   # past 45d


class DistillCandidateFilterTest(unittest.TestCase):

    def _node(self, **over):
        node = {"file": "", "retrieval_count": 0, "utility_ratio": 0.0,
                "times_helpful": 0, "times_noise": 0, "children": []}
        node.update(over)
        return node

    def _write_tmp(self, text):
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write(text)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _skips(self, tree):
        res = tree_engine.get_distill_candidates(tree, include_skipped=True)
        return ({c["key"]: c for c in res["candidates"]},
                {s["node_key"]: s["skip_reason"] for s in res["skipped"]})

    # ── crit1 (low_utility) vote/recency gating ──────────────────────────

    def test_one_noise_vote_is_not_evidence(self):
        # The  over-flag shape: ur=0.0 backed by a SINGLE noise vote.
        # Fresh signal, plenty of retrievals — still skipped: 1 vote is noise.
        cands, skips = self._skips({"nodes": {"n": self._node(
            retrieval_count=50, utility_ratio=0.0, times_noise=1,
            last_retrieved=FRESH)}})
        self.assertNotIn("n", cands)
        self.assertEqual(skips["n"], "insufficient_feedback_votes")

    def test_zero_votes_still_attributed_no_feedback(self):
        # votes==0 keeps its own skip reason (distinct from 0<votes<min).
        cands, skips = self._skips({"nodes": {"n": self._node(
            retrieval_count=50, utility_ratio=0.0, last_retrieved=FRESH)}})
        self.assertNotIn("n", cands)
        self.assertEqual(skips["n"], "no_feedback")

    def test_min_votes_boundary_fresh_is_candidate(self):
        # Exactly distill_min_feedback_votes (3) + fresh signal → crit1 fires.
        cands, _ = self._skips({"nodes": {"n": self._node(
            retrieval_count=50, utility_ratio=0.1, times_helpful=1,
            times_noise=2, last_retrieved=FRESH)}})
        self.assertIn("n", cands)
        self.assertEqual(cands["n"]["trigger"], "low_utility")

    def test_enough_votes_but_stale_signal_skipped(self):
        # 5 votes but last_retrieved 60d ago → signal too stale to act on.
        cands, skips = self._skips({"nodes": {"n": self._node(
            retrieval_count=50, utility_ratio=0.1, times_helpful=5,
            last_retrieved=STALE)}})
        self.assertNotIn("n", cands)
        self.assertEqual(skips["n"], "stale_retrieval_signal")

    def test_missing_last_retrieved_counts_as_stale(self):
        cands, skips = self._skips({"nodes": {"n": self._node(
            retrieval_count=50, utility_ratio=0.1, times_helpful=5)}})
        self.assertNotIn("n", cands)
        self.assertEqual(skips["n"], "stale_retrieval_signal")

    # ── crit2 (large_mediocre) gated identically ─────────────────────────

    def test_crit2_sparse_votes_skipped(self):
        # 60 lines (> distill_line_threshold 50), ur=0.4 isolates crit2
        # (above crit1's 0.3, below crit2's 0.5). One vote → skipped.
        path = self._write_tmp("padding line of body text\n" * 60)
        cands, skips = self._skips({"nodes": {"n": self._node(
            file=path, retrieval_count=50, utility_ratio=0.4,
            times_helpful=1, last_retrieved=FRESH)}})
        self.assertNotIn("n", cands)
        self.assertEqual(skips["n"], "insufficient_feedback_votes")

    def test_crit2_enough_votes_fresh_is_candidate(self):
        path = self._write_tmp("padding line of body text\n" * 60)
        cands, _ = self._skips({"nodes": {"n": self._node(
            file=path, retrieval_count=50, utility_ratio=0.4,
            times_helpful=4, last_retrieved=FRESH)}})
        self.assertIn("n", cands)
        self.assertEqual(cands["n"]["trigger"], "large_mediocre")

    # ── crit3 (oversized_append_grown) is vote/recency-IMMUNE ────────────

    def test_crit3_fires_without_votes_or_recency(self):
        # Structural trigger: oversized + append-grown, HIGH utility, zero
        # votes, NO last_retrieved — must still be a candidate (the gate is
        # crit1/crit2-only by design).
        body = "## Refresh 2026-06-21\n" * 4 + ("payload line of sweep text\n" * 4000)
        path = self._write_tmp(body)
        cands, _ = self._skips({"nodes": {"n": self._node(
            file=path, retrieval_count=50, utility_ratio=0.95)}})
        self.assertIn("n", cands)
        self.assertEqual(cands["n"]["trigger"], "oversized_append_grown")

    # ── skip-attribution precedence ──────────────────────────────────────

    def test_distill_exempt_precedes_vote_attribution(self):
        # An exempt node that ALSO qualifies on votes+recency skips as
        # distill_exempt (checked first), not as a vote/recency reason.
        cands, skips = self._skips({"nodes": {"n": self._node(
            retrieval_count=50, utility_ratio=0.1, times_helpful=5,
            last_retrieved=FRESH, maintain_exempt=["distill"])}})
        self.assertNotIn("n", cands)
        self.assertEqual(skips["n"], "distill_exempt")


if __name__ == "__main__":
    unittest.main()
